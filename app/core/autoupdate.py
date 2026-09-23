"""自动更新引擎：主题队列驱动，支持定时、启动时与手动触发。

行为
----
- 后台单线程串行执行（避免并发抓取把对方站点打爆，也便于进度可读）；
- 每次运行都会写入 update_logs，UI 可查看历史与失败原因；
- 定时策略：启动后延迟一小段（不拖慢开窗），此后每 interval_hours 检查一次；
- 主题来源：用户配置的主题列表 + 主题队列（聊天中发现的资料缺口会自动入队）。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Sequence

from . import secrets
from .fetch import Fetcher, build_fetcher
from .ingest import update_topic
from .llm import build_llm
from .search import build_search
from .store import KnowledgeBase

MAX_PROGRESS_LINES = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class UpdateManager:
    """后台自动更新调度器。"""

    def __init__(self, config: Any, kb: KnowledgeBase) -> None:
        self.config = config
        self.kb = kb
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._scheduler: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # 取消标志与 _stop 分开：_stop 只让**调度线程**退出，
        # 而正在跑的更新由 _run() 在主题之间检查 _cancel 才会真的停下来。
        self._cancel = threading.Event()
        self._cancelled = False
        self._running = False
        self._current_topic = ""
        self._progress: Deque[Dict[str, str]] = deque(maxlen=MAX_PROGRESS_LINES)
        self._summary: Dict[str, Any] = {}
        self._last_finished = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._scheduler and self._scheduler.is_alive():
                return
            self._stop.clear()
            self._cancel.clear()
            self._cancelled = False
            self._scheduler = threading.Thread(target=self._scheduler_loop, name="nte-rag-scheduler", daemon=True)
            self._scheduler.start()

    def stop(self, wait: float = 5.0) -> None:
        """停掉调度线程，并请求正在跑的那轮更新在主题之间收尾。

        以前这里只 `self._stop.set()`：那只让 scheduler 线程退出，工作线程
        （name="nte-rag-update"）接着跑完所有主题，界面却已经显示「已停止」。
        现在额外置 `_cancel`，并给工作线程一点时间把当前主题做完。
        """
        self._stop.set()
        self._cancel.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(wait)))

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._running

    # ------------------------------------------------------------------
    # 触发
    # ------------------------------------------------------------------

    def trigger(
        self,
        topics: Optional[Sequence[str]] = None,
        trigger: str = "manual",
        max_pages: Optional[int] = None,
        max_topics: Optional[int] = None,
    ) -> Dict[str, Any]:
        """异步触发一次更新；已在运行则返回当前状态。"""
        with self._lock:
            if self._running:
                return {"started": False, "reason": "已有更新任务在执行", "status": self.status()}
            self._running = True
            self._cancel.clear()
            self._cancelled = False
            self._progress.clear()
            self._log("任务已启动")
            self._thread = threading.Thread(
                target=self._run,
                args=(list(topics) if topics else None, trigger, max_pages, max_topics),
                name="nte-rag-update",
                daemon=True,
            )
            self._thread.start()
        return {"started": True}

    def _collect_topics(self, topics: Optional[Sequence[str]], max_topics: Optional[int]) -> List[str]:
        if topics:
            ordered = [t.strip() for t in topics if t and t.strip()]
        else:
            section = self.config.section("auto_update")
            configured = [str(t).strip() for t in (section.get("topics") or []) if str(t).strip()]
            queued = [row["topic"] for row in self.kb.dequeue_topics(limit=20)]
            ordered = []
            for topic in configured + queued:
                if topic not in ordered:
                    ordered.append(topic)
        limit = int(max_topics or self.config.get("auto_update", "max_topics_per_run", 6))
        return ordered[: max(1, limit)]

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def _run(
        self,
        topics: Optional[List[str]],
        trigger: str,
        max_pages: Optional[int],
        max_topics: Optional[int],
    ) -> None:
        started_at = time.time()
        log_id = self.kb.start_update_log(trigger, "")
        totals = {
            "fetched": 0,     # 取到正文的页数
            "pages": 0,       # 其中真正写入或更新的页数
            "skipped": 0,     # 其中内容没变、没有写入的页数
            "skipped_by_policy": 0,  # 按 robots/黑名单/冷却主动跳过的页数
            "failed": 0,      # 真失败：网络错误、403/超时、正文过短
            "chunks": 0,
            "facts_added": 0,
            "facts_updated": 0,
            "table_facts_added": 0,
            "api_facts_added": 0,
            "confirmed": 0,
            "duplicates": 0,
            "conflicts": 0,
            "filtered": 0,
            "errors": [],
        }
        try:
            topic_list = self._collect_topics(topics, max_topics)
            if not topic_list:
                self._log("没有待更新的主题，可在「自动更新」页添加关键词")
                self.kb.finish_update_log(log_id, "ok", "没有待更新的主题")
                return

            self._log(f"本轮主题（{len(topic_list)}）：{'、'.join(topic_list)}")
            search = build_search(self.config)
            fetcher = build_fetcher(self.config)
            llm = build_llm(self.config) if _llm_configured(self.config) else None
            if llm is None:
                self._log("未配置模型：本轮只入库原文证据，不抽取结构化条目")

            planned_pages = int(max_pages or self.config.get("auto_update", "max_pages_per_run", 25))
            per_topic = max(2, planned_pages // max(1, len(topic_list)))

            for topic in topic_list:
                if self._cancel.is_set():
                    self._cancelled = True
                    self._log("已收到停止请求：本轮在主题之间收尾")
                    break
                with self._lock:
                    self._current_topic = topic
                self._log(f"开始主题：{topic}")
                try:
                    stats = update_topic(
                        self.kb,
                        self.config,
                        topic,
                        search,
                        fetcher,
                        llm=llm,
                        max_pages=per_topic,
                        max_facts_per_page=4,
                        on_progress=self._log,
                    )
                except Exception as error:  # noqa: BLE001
                    self._log(f"主题失败：{secrets.scrub(str(error))[:160]}")
                    totals["errors"].append(secrets.scrub(str(error))[:200])
                    continue
                totals["fetched"] += stats.get("fetched", 0)
                totals["pages"] += stats.get("pages", 0)
                totals["skipped"] += stats.get("skipped", 0)
                totals["skipped_by_policy"] += stats.get("skipped_by_policy", 0)
                totals["failed"] += stats.get("failed", 0)
                totals["chunks"] += stats.get("chunks", 0)
                totals["facts_added"] += stats.get("facts_added", 0)
                totals["facts_updated"] += stats.get("facts_updated", 0)
                totals["table_facts_added"] += stats.get("table_facts_added", 0)
                totals["api_facts_added"] += stats.get("api_facts_added", 0)
                totals["confirmed"] += stats.get("confirmed", 0)
                totals["duplicates"] += stats.get("duplicates", 0)
                totals["conflicts"] += stats.get("conflicts", 0)
                totals["filtered"] += stats.get("filtered", 0)
                # 收集时不过滤：错误总数要如实反映，展示时才截前几条
                totals["errors"].extend(stats.get("errors", []))
                self.kb.mark_topic_run(topic)
                self._log(
                    f"主题完成：{topic}｜新页面 {stats.get('pages', 0)}｜"
                    f"内容未变 {stats.get('skipped', 0)}｜新增条目 {stats.get('facts_added', 0)}"
                    f"｜过滤 {stats.get('filtered', 0)}"
                    f"｜按规则跳过 {stats.get('skipped_by_policy', 0)}"
                    f"｜冲突 {stats.get('conflicts', 0)}"
                )

            message = f"完成：取到正文 {totals['fetched']} 页（新页面 {totals['pages']} 页"
            if totals["skipped"]:
                message += f"，内容未变 {totals['skipped']} 页"
            message += f"），新增条目 {totals['facts_added']} 条"
            if totals["table_facts_added"]:
                message += f"（其中表格数值条目 {totals['table_facts_added']} 条）"
            if totals["api_facts_added"]:
                message += f"（结构化字段条目 {totals['api_facts_added']} 条）"
            if totals["confirmed"]:
                message += f"，{totals['confirmed']} 条被第二来源确认（可信度上调）"
            if totals["duplicates"]:
                message += f"，{totals['duplicates']} 条与已有条目重复（已合并，不新增）"
            if totals["conflicts"]:
                message += f"，{totals['conflicts']} 条与已有条目数值冲突（已同时保留，未覆盖）"
            if totals["skipped_by_policy"]:
                message += f"，按 robots/黑名单规则跳过 {totals['skipped_by_policy']} 页"
            if totals["filtered"]:
                message += f"，过滤低质量或与主题无关的页面 {totals['filtered']} 页"
            if totals["failed"]:
                message += f"，抓取失败 {totals['failed']} 页"
            if totals["errors"]:
                message += f"，{len(totals['errors'])} 条错误"
            if self._cancelled:
                message += "（已按停止请求提前收尾，未跑完的主题留到下次）"
            # 「部分完成」只留给真正出问题的情形：按规则主动跳过、页面被过滤都不算
            status = "partial" if (totals["failed"] or totals["errors"] or self._cancelled) else "ok"
            self.kb.finish_update_log(
                log_id,
                status,
                message + ("｜" + "；".join(totals["errors"][:3]) if totals["errors"] else ""),
                pages_fetched=totals["fetched"],
                pages_failed=totals["failed"],
                facts_added=totals["facts_added"],
                facts_updated=totals["facts_updated"],
                chunks_added=totals["chunks"],
            )
            self._log(message)
        except Exception as error:  # noqa: BLE001
            self.kb.finish_update_log(log_id, "failed", secrets.scrub(str(error))[:1000])
            self._log(f"任务异常：{secrets.scrub(str(error))[:160]}")
        finally:
            with self._lock:
                self._running = False
                self._current_topic = ""
                self._last_finished = _now_iso()
                self._summary = {
                    "finished_at": self._last_finished,
                    "duration_s": round(time.time() - started_at, 1),
                    "trigger": trigger,
                    **{k: v for k, v in totals.items() if k != "errors"},
                    "error_count": len(totals["errors"]),
                    "errors": totals["errors"][:5],
                }
            self.kb.set_meta("last_update_finished", self._last_finished)

    # ------------------------------------------------------------------
    # 定时
    # ------------------------------------------------------------------

    def _scheduler_loop(self) -> None:
        # 启动后先等 20 秒，避免和开窗、种子导入抢资源
        if self._stop.wait(20):
            return
        section = self.config.section("auto_update")
        # enabled 是总开关：以前启动分支只判 on_startup，于是用户关掉自动更新后
        # 每次启动照样全网抓取写库，而界面文字显示「已关闭」。
        if (
            section.get("enabled", True)
            and section.get("on_startup", True)
            and self._should_run()
        ):
            self._log("启动时更新：开始")
            self.trigger(trigger="startup")
        while not self._stop.wait(300):
            try:
                section = self.config.section("auto_update")
                if not section.get("enabled", True):
                    continue
                if self._should_run():
                    self._log("定时更新：开始")
                    self.trigger(trigger="timer")
            except Exception:
                continue

    def _should_run(self) -> bool:
        interval_hours = float(self.config.get("auto_update", "interval_hours", 24) or 24)
        last = self.kb.get_meta("last_update_finished", "")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        if self._running:
            return False
        return datetime.now(timezone.utc) - last_dt >= timedelta(hours=interval_hours)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def _log(self, message: str, **extra: Any) -> None:
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "message": secrets.scrub(str(message))}
        if extra:
            entry["extra"] = extra  # type: ignore[assignment]
        with self._lock:
            self._progress.append(entry)  # type: ignore[arg-type]

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "current_topic": self._current_topic,
                "progress": list(self._progress),
                "summary": self._summary,
                "last_finished": self._last_finished,
                "next_check_hint": self._next_hint(),
            }

    def _next_hint(self) -> str:
        section = self.config.section("auto_update")
        if not section.get("enabled", True):
            return "自动更新已关闭"
        interval = float(section.get("interval_hours", 24) or 24)
        last = self.kb.get_meta("last_update_finished", "")
        if not last:
            return "尚未执行过，将尽快进行首次更新"
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return "计划中"
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        next_dt = last_dt + timedelta(hours=interval)
        return next_dt.astimezone().strftime("%Y-%m-%d %H:%M 之后")


def _llm_configured(config: Any) -> bool:
    return bool(config.get_secret("key_enc", "llm")) and bool(config.get("llm", "model", ""))
