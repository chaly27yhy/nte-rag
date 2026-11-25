"""自动更新引擎：主题队列驱动，支持定时、启动时与手动触发。

行为
----
- 后台单线程串行执行（避免并发抓取把对方站点打爆，也便于进度可读）；
- 每次运行都会写入 update_logs，UI 可查看历史与失败原因；
- 定时策略：启动后延迟一小段，定期检查计划更新和到期重试；
- 配置主题轮转，与到期的队列项交替；所有主题共享一轮抓取预算。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Sequence

from . import secrets
from .fetch import build_fetcher
from .ingest import update_topic
from .llm import build_llm
from .search import build_search, retryable_search_error
from .store import KnowledgeBase

MAX_PROGRESS_LINES = 200


@dataclass(frozen=True)
class _ScheduledTopic:
    name: str
    lane: str
    queue_id: Optional[int] = None


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
        *,
        retry_only: bool = False,
    ) -> Dict[str, Any]:
        """异步触发一次更新；已在运行则返回当前状态。"""
        with self._lock:
            if self._stop.is_set():
                return {"started": False, "reason": "更新调度器正在停止"}
            if self._running:
                return {"started": False, "reason": "已有更新任务在执行", "status": self.status()}
            selected = list(topics) if topics else None
            self._running = True
            self._cancel.clear()
            self._cancelled = False
            self._progress.clear()
            self._log("任务已启动")
            self._thread = threading.Thread(
                target=self._run,
                args=(selected, trigger, max_pages, max_topics),
                kwargs={"retry_only": retry_only},
                name="nte-rag-update",
                daemon=True,
            )
            self._thread.start()
        return {"started": True}

    def _include_chat_topics(self) -> bool:
        return bool(self.config.get("answer", "auto_web", True))

    def _configured_topics(self) -> List[str]:
        topics = self.config.get("auto_update", "topics", []) or []
        return list(dict.fromkeys(str(topic).strip() for topic in topics if str(topic).strip()))

    def _collect_topics(
        self, topics: Optional[Sequence[str]], max_topics: Optional[int], *, retry_only: bool = False,
    ) -> List[_ScheduledTopic]:
        limit = max(1, int(max_topics or self.config.get("auto_update", "max_topics_per_run", 6)))
        if topics:
            names = [topic.strip() for topic in topics if topic and topic.strip()]
            return [_ScheduledTopic(name, "manual") for name in names[:limit]]
        now = time.time()
        include_chat = self._include_chat_topics()
        eligible = self.kb.dequeue_topics(limit=limit, include_chat=include_chat, retry_only=retry_only, now=now)
        queued = deque(_ScheduledTopic(row["topic"], "queued", row["id"]) for row in eligible)
        if retry_only:
            return list(queued)

        # 已交给队列的同名主题由队列处理，配置名单不能绕过它的退避。
        queued_names = {
            row["topic"] for row in self.kb.list_topics()
            if not row["last_run"] and (include_chat or row["origin"] != "chat")
        }
        configured = self._configured_topics()
        cursor = self.kb.get_meta("update_last_configured_topic", "")
        if cursor in configured:
            successor = configured.index(cursor) + 1
            configured = configured[successor:] + configured[:successor]
        regular = deque(_ScheduledTopic(name, "configured") for name in configured if name not in queued_names)
        ordered = []
        last_lane = self.kb.get_meta("update_last_topic_lane", "")
        while (regular or queued) and len(ordered) < limit:
            use_queue = bool(queued) and (not regular or last_lane == "configured")
            entry = (queued if use_queue else regular).popleft()
            ordered.append(entry)
            last_lane = entry.lane
        return ordered

    def _defer(self, entry: _ScheduledTopic, *, minimum_delay: float = 0) -> Optional[float]:
        if entry.lane == "configured" and entry.name not in self._configured_topics():
            return None
        next_retry = self.kb.defer_topic(
            entry.name, queue_id=entry.queue_id, now=time.time(), minimum_delay=minimum_delay,
        )
        if next_retry is not None:
            display = datetime.fromtimestamp(next_retry).strftime("%Y-%m-%d %H:%M")
            self._log(f"主题「{entry.name}」已保留，{display} 后可重试")
        return next_retry

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def _run(
        self,
        topics: Optional[List[str]],
        trigger: str,
        max_pages: Optional[int],
        max_topics: Optional[int],
        *,
        retry_only: bool = False,
    ) -> None:
        started_at = time.time()
        log_id = None
        totals = {
            "attempted": 0,   # 候选抓取次数；失败或按规则跳过也占预算
            "page_budget": 0,
            "retries_scheduled": 0,
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

        def count_attempt() -> None:
            totals["attempted"] += 1

        try:
            log_id = self.kb.start_update_log(trigger, "")
            topic_list = self._collect_topics(topics, max_topics, retry_only=retry_only)
            if not topic_list:
                message = "当前没有可运行的主题，可检查「自动更新」页的主题与下一次检查时间"
                self._log(message)
                self.kb.finish_update_log(log_id, "ok", message)
                return

            page_budget = max(1, int(max_pages or self.config.get("auto_update", "max_pages_per_run", 25)))
            totals["page_budget"] = page_budget
            self._log(f"本轮候选主题（{len(topic_list)}）：{'、'.join(entry.name for entry in topic_list)}"
                      f"｜整轮最多 {page_budget} 页")
            search = build_search(self.config)
            fetcher = build_fetcher(self.config)
            llm = build_llm(self.config) if _llm_configured(self.config) else None
            if llm is None:
                self._log("未配置模型：本轮只入库原文证据，不抽取结构化条目")

            for i, entry in enumerate(topic_list):
                if self._cancel.is_set():
                    self._cancelled = True
                    self._log("已收到停止请求：本轮在主题之间收尾")
                    break
                remaining = page_budget - totals["attempted"]
                if remaining <= 0:
                    self._log("已用完本轮抓取预算，其余主题留到下一轮")
                    break
                if entry.queue_id is not None:
                    queued = self.kb.queued_topic(entry.queue_id)
                    if queued is None or queued["last_run"] or queued["next_retry_at"] > time.time():
                        continue
                    if queued["origin"] == "chat" and not self._include_chat_topics():
                        continue
                elif entry.lane == "configured" and entry.name not in self._configured_topics():
                    continue
                topic = entry.name
                if not retry_only and entry.lane != "manual":
                    self.kb.set_meta("update_last_topic_lane", entry.lane)
                    if entry.lane == "configured":
                        self.kb.set_meta("update_last_configured_topic", topic)
                # 按剩余主题平分预算，少于分配上限的候选不会占用后续主题的预算。
                per_topic = max(1, remaining // (len(topic_list) - i))
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
                        on_attempt=count_attempt,
                    )
                except Exception as error:  # noqa: BLE001
                    self._log(f"主题失败：{secrets.scrub(str(error))[:160]}")
                    totals["errors"].append(secrets.scrub(str(error))[:200])
                    if retryable_search_error(error) and self._defer(entry) is not None:
                        totals["retries_scheduled"] += 1
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
                if stats.get("retry_required"):
                    if self._defer(entry, minimum_delay=stats.get("retry_after", 0)) is not None:
                        totals["retries_scheduled"] += 1
                else:
                    self.kb.mark_topic_run(topic, queue_id=entry.queue_id)
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
            message += f"，抓取预算 {totals['attempted']}/{page_budget} 页"
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
            if totals["retries_scheduled"]:
                message += f"，{totals['retries_scheduled']} 个主题等待重试"
            if self._cancelled:
                message += "（已按停止请求提前收尾，未跑完的主题留到下次）"
            # 「部分完成」只留给真正出问题的情形：按规则主动跳过、页面被过滤都不算
            incomplete = (
                totals["failed"] or totals["errors"] or totals["retries_scheduled"] or self._cancelled
            )
            status = "partial" if incomplete else "ok"
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
            detail = secrets.scrub(str(error))[:1000]
            totals["errors"].append(detail)
            if log_id is not None:
                self.kb.finish_update_log(log_id, "failed", detail)
            self._log(f"任务异常：{detail[:160]}")
        finally:
            finished = _now_iso()
            try:
                if not retry_only:
                    self.kb.set_meta("last_update_finished", finished)
            finally:
                with self._lock:
                    self._running = False
                    self._current_topic = ""
                    self._last_finished = finished
                    self._summary = {
                        "finished_at": finished,
                        "duration_s": round(time.time() - started_at, 1),
                        "trigger": trigger,
                        **{k: v for k, v in totals.items() if k != "errors"},
                        "error_count": len(totals["errors"]),
                        "errors": totals["errors"][:5],
                    }

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
        if section.get("enabled", True) and section.get("on_startup", True) and not self.busy:
            if self._should_run():
                self._log("启动时更新：开始")
                self.trigger(trigger="startup")
            elif self._retry_due():
                self.trigger(trigger="retry", retry_only=True)
        while not self._stop.wait(300):
            try:
                section = self.config.section("auto_update")
                if not section.get("enabled", True) or self.busy:
                    continue
                if self._should_run():
                    self._log("定时更新：开始")
                    self.trigger(trigger="timer")
                elif self._retry_due():
                    self.trigger(trigger="retry", retry_only=True)
            except Exception:
                continue

    def _should_run(self) -> bool:
        if self.busy:
            return False
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
        return datetime.now(timezone.utc) - last_dt >= timedelta(hours=interval_hours)

    def _retry_due(self) -> bool:
        next_retry = self.kb.next_topic_retry(include_chat=self._include_chat_topics())
        return next_retry is not None and next_retry <= time.time()

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
        next_retry = self.kb.next_topic_retry(include_chat=self._include_chat_topics())
        if next_retry is not None and next_retry < next_dt.timestamp():
            return datetime.fromtimestamp(next_retry).strftime("待补主题将在 %Y-%m-%d %H:%M 后重试")
        return next_dt.astimezone().strftime("%Y-%m-%d %H:%M 之后")


def _llm_configured(config: Any) -> bool:
    return bool(config.get_secret("key_enc", "llm")) and bool(config.get("llm", "model", ""))
