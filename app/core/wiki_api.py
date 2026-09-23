"""BWIKI 结构化采集：直接读 MediaWiki API，而不是抓渲染后的 HTML。

背景与取舍
----------
1. 渲染后的图鉴页只有「表格里看得见的列」，而**条目页的模板字段**才是原始数据。
   实测弧盘「倾世之雨」这一页的 wikitext 是：

       {{弧盘|弧盘名=「倾世之雨」|稀有度=S|效果=详见描述
         |描述=…造成伤害提升30.00%；…提升36点，持续15秒…
         |获取途径=商城18元礼包|适用对象=动态}}

   其中「30.00% / 36点 / 15秒 / 商城18元礼包」在 HTML 表格里并不完整，
   模型摘写时也最容易丢——而模板字段是**字段名→值**的确定对应，不需要启发式。
2. `action=ask`（SMW）实测可用但不是万能的：`[[分类:弧盘]]|?最高攻击|?最高生命`
   返回 45 条却**全是空值**（这两个属性在该 wiki 根本不存在），
   真正有值的是 `?弧盘名|?稀有度|?效果|?获得方式`。因此 SMW 只当索引与补充，
   主路径是「分类成员 → 逐页 wikitext → 模板字段」。
3. BWIKI 的 WAF 会在连续抓取后返回 HTTP 567（实测约 190 次请求后触发，
   冷却 10 分钟）。所以这里**必须**做本地落盘缓存：一次成功就写盘，
   之后重跑直接读缓存，既省请求，被中断后下次也能续上。

依赖：只用标准库 + 项目自己的 `Fetcher`（限速、冷却、UA、重试都在那里）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

LOGGER = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 7 * 24 * 3600  # 结构化数据变化慢，7 天内直接用缓存
# 索引类查询（分类成员 / 全站页面列表）是**发现新条目的唯一入口**，恰恰变得最快：
# 新角色、新弧盘上线的头几天全靠它，7 天不可见等于「每日自动更新」失效。
INDEX_TTL_SECONDS = 6 * 3600
CACHE_MAX_BYTES = 512 * 1024 * 1024  # 缓存目录总容量上限，超了按最旧优先删

# 清扫与写盘互斥：同进程里 autoupdate 与聊天可能同时用同一个 cache 目录，
# glob + unlink 与 write_text 并发会互相删到对方刚写的文件。
_SWEEP_LOCK = threading.RLock()

# MediaWiki 的正常响应必带 `query` 或 `parse`（SMW 的 action=ask 也回 `query`）。
# 空对象、只带 `batchcomplete` 的风控桩数据一旦被缓存，就是 7 天的静默失效。
_ENVELOPE_KEYS = ("query", "parse")

# 模板字段里的噪声键（图片、排序键、页面名等），对回答玩家问题没有价值
_SKIP_FIELDS = {
    "图片", "头像", "图标", "立绘", "文件", "排序", "排序值", "页面名", "页面", "id",
    "编号", "是否显示", "显示名", "隐藏", "class", "style", "宽度", "高度", "大小",
    "类型图标", "攻击类型图标", "属性图标", "命座图标", "icon",
}

# 版式容器字段：实测有「标签2内容=装备推荐」「标签4内容=600px\n600px（角色贺图图片尺寸）」
# 「五维名称1=3（图标编号）」这类，是页面排版用的槽位，不是实体信息。
_SKIP_FIELD_RE = re.compile(r"^(?:标签\d(?:名称|内容)|五维名称\d?)$")

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_REF_RE = re.compile(r"\[\s*(?:注|\d+|[a-z])\s*\]")
_BOLD_RE = re.compile(r"'{2,5}")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_LINK_RE = re.compile(r"\[\[([^\[\]|]+)\|([^\[\]]+)\]\]")
_PLAIN_LINK_RE = re.compile(r"\[\[([^\[\]|]+)\]\]")
_EXT_LINK_RE = re.compile(r"\[(https?://\S+)\s+([^\]]+)\]")
_TAG_RE = re.compile(r"</?(?:span|div|br|p|center|small|big|font|b|i|u|sup|sub|ruby|rt|ref)[^>]*>", re.I)
_HEADING_RE = re.compile(r"^=+\s*(.*?)\s*=+$", re.M)
_TEMPLATE_MAX_VALUE = 400

# 非法 JSON 转义：`\u` 后面不是四位十六进制。实测 BWIKI 的批量接口（一次 20 页）会
# 返回这种内容 —— 页面原文里带了 `\u` 字样（正则/编码表一类），站点没有把它转义成 `\\u`，
# 于是整个响应变成非法 JSON，`json.loads` 直接抛「Invalid \uXXXX escape」，
# 20 页会被整批判成「请求失败」（此前因此丢过 20 个弧盘页）。
# 只能修 `\u`：其余 `\x` 之类本身也非法，但不改也能让 loads 走到同一处报错，
# 而把反斜杠一律改写有把 `\\"` 这类正常转义改坏的风险。
_BAD_UNICODE_ESCAPE_RE = re.compile(r"\\u(?![0-9a-fA-F]{4})")


def repair_json_text(text: str) -> str:
    """把站点返回里非法的 `\\uXXXX` 转义修成字面反斜杠，让 JSON 能解析。

    这是**只做最小修补**的容错：合法的转义原样保留，非法的那一个反斜杠被转义成 `\\\\`。
    修完仍然解析不了的，说明响应根本不是 JSON（WAF 页等），照旧按失败处理。
    """
    return _BAD_UNICODE_ESCAPE_RE.sub(lambda _m: "\\\\u", text)  # → 字面 \\u（两个反斜杠 + u）



class WikiApi:
    """MediaWiki action API 的最小客户端（含落盘缓存）。"""

    def __init__(
        self,
        fetcher: Any,
        api_url: str,
        cache_dir: Optional[Path] = None,
        ttl_seconds: int = CACHE_TTL_SECONDS,
    ) -> None:
        self.fetcher = fetcher
        self.api_url = api_url
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ttl_seconds = int(ttl_seconds)
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0}
        # 最后一次失败的原因（给上层的「抓取失败已跳过」日志用，
        # 否则调用方只知道「wikitext 是空的」，排查不了是被限流还是页面不存在）
        self.last_error = ""
        # 批量取原文时**整批请求失败**的页面 → 失败原因。
        # 必须和「页面确实不存在/为空」区分开：前者重跑能补上（失败不写缓存），
        # 后者永远取不到。混在一起会让报告把限流说成「页面可能不存在」。
        self.batch_failures: Dict[str, str] = {}
        if self.cache_dir:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa: BLE001
                self.cache_dir = None
        # 缓存清扫：每个 URL 一个 JSON、逻辑 TTL 只影响「要不要重取」，
        # 文件本身从不删除，长期跑下去就是无界增长（一个来源几百页 × 多轮更新）。
        self._sweep_cache()

    def _sweep_cache(self) -> None:
        """加锁跑一次清扫；失败不影响抓取。"""
        try:
            with _SWEEP_LOCK:
                self._sweep_cache_locked()
        except Exception:  # noqa: BLE001
            LOGGER.info("wiki 缓存清扫失败（不影响本轮抓取）", exc_info=True)

    def _sweep_cache_locked(self) -> None:
        """删掉远超 TTL 的旧缓存；仍然超容量上限时按最旧优先删。

        glob 从 `*.json` 改成全部文件 —— `_bad_response_*.txt` 每个最大
        200 KB 且永不被重写，只清扫 JSON 会让「持续返回风控页的站点」把缓存目录
        撑到无界，而 CACHE_MAX_BYTES 正是为防这件事建的。
        """
        if not self.cache_dir:
            return
        now = time.time()
        stale_after = self.ttl_seconds * 2
        kept: list = []
        total = 0
        try:
            entries = [item for item in self.cache_dir.glob("*") if item.is_file()]
        except OSError:
            return
        for path in entries:
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime > stale_after:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            kept.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
        if total <= CACHE_MAX_BYTES:
            return
        for _mtime, size, path in sorted(kept):
            if total <= CACHE_MAX_BYTES:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                continue

    # ------------------------------------------------------------------
    # 取数
    # ------------------------------------------------------------------

    def _cache_file(self, url: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / f"{digest}.json"

    def _discard_cache_file(self, path: Path) -> None:
        """删掉形状不对的缓存文件。

        留着它每次读取都会抛异常，而 `ingest_sources` 只在整源级捕获异常——
        一个坏文件能让整个源永久失败且永不自修。
        """
        try:
            path.unlink()
        except OSError:
            pass

    def _ttl_for(self, url: str) -> float:
        """索引类查询用更短的 TTL（理由见 INDEX_TTL_SECONDS）。"""
        if "list=categorymembers" in url or "list=allpages" in url:
            return float(min(self.ttl_seconds, INDEX_TTL_SECONDS))
        return float(self.ttl_seconds)

    def _read_cache(self, url: str) -> Optional[Dict[str, Any]]:
        path = self._cache_file(url)
        if not path or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        # 形状假设也必须受保护：语法合法但结构不对的缓存（写成 list、
        # fetched_at 不是数字）以前会抛 AttributeError / ValueError 穿到
        # category_members，整源失败且文件永不自修。
        if not isinstance(payload, dict):
            self._discard_cache_file(path)
            return None
        try:
            fetched_at = float(payload.get("fetched_at") or 0)
        except (TypeError, ValueError):
            self._discard_cache_file(path)
            return None
        if time.time() - fetched_at > self._ttl_for(url):
            return None
        data = payload.get("data")
        if data is not None and not isinstance(data, dict):
            self._discard_cache_file(path)
            return None
        self.stats["cache_hits"] += 1
        return data

    def _write_cache(self, url: str, data: Dict[str, Any]) -> None:
        if not data:
            return  # 空对象绝不落盘（防静默失效的第一道防线）
        path = self._cache_file(url)
        if not path:
            return
        payload = json.dumps({"url": url, "fetched_at": time.time(), "data": data}, ensure_ascii=False)
        # 先写临时文件再 os.replace：就地 write_text 一旦被并发读到半个文件，
        # 代价是对着「缓存本要规避的那个 WAF」再多打一轮请求。
        tmp = path.with_name(path.name + ".tmp")
        try:
            with _SWEEP_LOCK:
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            try:
                tmp.unlink()
            except OSError:
                pass

    def _dump_bad_response(self, label: str, text: str) -> None:
        """把解析不了的响应落盘一份，下次再遇到能直接看内容（不留则完全无从查起）。"""
        if not self.cache_dir or not text:
            return
        try:
            safe = re.sub(r"[^0-9A-Za-z._-]+", "_", label)[:60]
            path = self.cache_dir / f"_bad_response_{safe}.txt"
            path.write_text(text[:200_000], encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    def _json(self, url: str, label: str) -> Optional[Dict[str, Any]]:
        cached = self._read_cache(url)
        if cached is not None:
            return cached
        self.stats["requests"] += 1
        result = self.fetcher.fetch(url)
        if not result.ok:
            self.stats["errors"] += 1
            self.last_error = f"{label}：{result.error or ('HTTP ' + str(result.status))}"
            LOGGER.info("wiki api %s 失败：%s", label, result.error or result.status)
            return None
        try:
            # strict=False：模板原文里含裸控制字符（实测 BWIKI 的 wikitext 就有），
            # 严格模式会直接抛 JSONDecodeError。
            data = json.loads(result.text, strict=False)
        except Exception as error:  # noqa: BLE001
            repaired = repair_json_text(result.text or "")
            data = None
            if repaired != (result.text or ""):
                try:
                    data = json.loads(repaired, strict=False)
                except Exception:  # noqa: BLE001
                    data = None
            if data is None:
                self._dump_bad_response(label, result.text)
                self.stats["errors"] += 1
                self.last_error = f"{label}：返回内容不是 JSON（{error}）"
                LOGGER.info("wiki api %s 返回的不是 JSON：%s", label, error)
                return None
            self.stats["repaired"] = self.stats.get("repaired", 0) + 1
            LOGGER.info("wiki api %s：响应含非法转义，已修补后解析（%s）", label, error)
        if not isinstance(data, dict) or "error" in data:
            self.stats["errors"] += 1
            self.last_error = f"{label}：{str(data)[:150]}"
            LOGGER.info("wiki api %s 返回错误：%s", label, str(data)[:200])
            return None
        if not any(key in data for key in _ENVELOPE_KEYS):
            # 既不是查询结果也不是解析结果，多半是 WAF 返回的 JSON 桩数据。
            # 以前会被原样缓存 7 天 → category_members() 恒返回 []，上层还把归因
            # 写成「分类名可能已改」，整个源静默失效一周。
            self.stats["errors"] += 1
            self.last_error = f"{label}：响应缺少 query/parse 字段（疑似风控桩数据）：{str(data)[:120]}"
            LOGGER.info("wiki api %s 响应形状不对，不写缓存：%s", label, str(data)[:200])
            return None
        self._write_cache(url, data)
        return data

    def category_members(self, category: str, limit: int = 500) -> List[str]:
        """列出分类成员页面的标题。"""
        url = (
            f"{self.api_url}?action=query&format=json&list=categorymembers"
            f"&cmtitle={quote('分类:' + category)}&cmlimit={int(limit)}"
        )
        data = self._json(url, f"category:{category}")
        if not data:
            return []
        members = (data.get("query") or {}).get("categorymembers") or []
        return [str(item.get("title") or "") for item in members if item.get("title")]

    def _page_url(self, page: str) -> str:
        return (
            f"{self.api_url}?action=parse&format=json&prop=wikitext"
            f"&page={quote(page)}"
        )

    def wikitext(self, page: str) -> str:
        """取某个条目的模板原文（不是渲染后的 HTML）。"""
        url = self._page_url(page)
        data = self._json(url, f"wikitext:{page}")
        if not data:
            return ""
        return str(((data.get("parse") or {}).get("wikitext") or {}).get("*") or "")

    # 一次请求能带多少页：MediaWiki 的 titles 上限是 50，
    # 但响应体越大越容易被站点拦，20 是实测稳的值。
    BATCH_TITLES = 20

    def wikitext_many(self, pages: Sequence[str]) -> Dict[str, str]:
        """**一次请求取多页原文**（`action=query&prop=revisions&rvprop=content`）。

        批量取回的原因：BWIKI 的 WAF 大约每个冷却窗口只放行几次请求，
        逐页 `action=parse` 把 46 页弧盘变成 46 次请求（实测第 8 次就 567），
        而批量接口能把同样 46 页压到 3 次请求。

        缓存策略：单页缓存（与 `wikitext()` 共用同一个 key）优先，
        只对没缓存的页面发批量请求，拿到后再回写单页缓存 ——
        这样先前逐页抓到的内容不会被浪费，两种路径互通。
        """
        out: Dict[str, str] = {}
        pending: List[str] = []
        self.batch_failures = {}
        for page in pages or []:
            title = str(page or "").strip()
            if not title:
                continue
            cached = self._read_cache(self._page_url(title))
            if cached is not None:
                text = str(((cached.get("parse") or {}).get("wikitext") or {}).get("*") or "")
                if text:
                    out[title] = text
                    continue
            pending.append(title)

        for start in range(0, len(pending), self.BATCH_TITLES):
            batch = pending[start:start + self.BATCH_TITLES]
            url = (
                f"{self.api_url}?action=query&format=json&prop=revisions"
                f"&rvprop=content&rvslots=main&titles={quote('|'.join(batch))}"
            )
            data = self._json(url, f"wikitext×{len(batch)}")
            if not data:
                reason = self.last_error or "请求失败（未返回可用数据）"
                for title in batch:
                    self.batch_failures[title] = reason
                continue
            page_map = (data.get("query") or {}).get("pages") or {}
            if isinstance(page_map, list):  # 老版本 API 会返回数组
                page_map = {str(i): item for i, item in enumerate(page_map)}
            for item in page_map.values():
                if not isinstance(item, dict) or "missing" in item:
                    continue
                title = str(item.get("title") or "")
                revisions = item.get("revisions") or []
                if not title or not revisions:
                    continue
                slots = revisions[0].get("slots") or {}
                content = (slots.get("main") or {}).get("*") or revisions[0].get("*") or ""
                if not content:
                    continue
                out[title] = str(content)
                # 回写单页缓存：下次走 wikitext() 也不会再发请求
                self._write_cache(
                    self._page_url(title),
                    {"parse": {"title": title, "wikitext": {"*": str(content)}}},
                )
        return out

    def ask(self, query: str) -> Dict[str, Any]:
        """SMW 语义查询（可选路径，实测不少属性并不存在，需容错）。"""
        url = f"{self.api_url}?action=ask&format=json&query={quote(query)}"
        data = self._json(url, f"ask:{query[:40]}")
        return ((data or {}).get("query") or {}).get("results") or {}

    def search(self, term: str, limit: int = 10) -> List[str]:
        """全站搜索：用来判断「某项资料到底有没有」。"""
        url = (
            f"{self.api_url}?action=query&format=json&list=search"
            f"&srsearch={quote(term)}&srlimit={int(limit)}"
        )
        data = self._json(url, f"search:{term[:30]}")
        if not data:
            return []
        items = (data.get("query") or {}).get("search") or []
        return [str(item.get("title") or "") for item in items]


# ----------------------------------------------------------------------
# 模板解析
# ----------------------------------------------------------------------


def strip_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text or "")


def _split_top_level(body: str, sep: str = "|") -> List[str]:
    """按顶层分隔符切分，忽略 `{{…}}` 与 `[[…]]` 内部的同名符号。"""
    parts: List[str] = []
    depth_brace = 0
    depth_link = 0
    current: List[str] = []
    index = 0
    while index < len(body):
        chunk = body[index:index + 2]
        if chunk == "{{":
            depth_brace += 1
            current.append(chunk)
            index += 2
            continue
        if chunk == "}}":
            depth_brace = max(0, depth_brace - 1)
            current.append(chunk)
            index += 2
            continue
        if chunk == "[[":
            depth_link += 1
            current.append(chunk)
            index += 2
            continue
        if chunk == "]]":
            depth_link = max(0, depth_link - 1)
            current.append(chunk)
            index += 2
            continue
        char = body[index]
        if char == sep and depth_brace == 0 and depth_link == 0:
            parts.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def _expand_inner_templates(value: str, rounds: int = 3) -> str:
    """把值里嵌套的 `{{…}}` 展开成它的第一个有效片段（尽量保留可读内容）。"""
    text = value
    for _ in range(rounds):
        start = text.find("{{")
        if start < 0:
            return text
        depth = 0
        end = -1
        index = start
        while index < len(text) - 1:
            pair = text[index:index + 2]
            if pair == "{{":
                depth += 1
                index += 2
                continue
            if pair == "}}":
                depth -= 1
                index += 2
                if depth == 0:
                    end = index
                    break
                continue
            index += 1
        if end < 0:
            return text[:start]
        inner = text[start + 2:end - 2]
        segments = [seg for seg in _split_top_level(inner) if seg.strip()]
        replacement = ""
        for segment in segments[1:] if len(segments) > 1 else segments:
            if "=" in segment:
                continue
            replacement = segment.strip()
            break
        if not replacement and segments:
            replacement = segments[0].strip()
        text = text[:start] + replacement + text[end:]
    return text


def clean_value(value: str) -> str:
    """把模板值里的 wiki 标记清掉，留下人能读、模型能引用的纯文本。"""
    text = strip_comments(str(value or ""))
    text = _extract_refs(text)
    text = _expand_inner_templates(text)
    text = _LINK_RE.sub(r"\2", text)
    text = _PLAIN_LINK_RE.sub(r"\1", text)
    text = _EXT_LINK_RE.sub(r"\2（\1）", text)
    # 换行标签必须先换成分隔符再清标签，否则「第一行<br>第二行」会粘成「第一行第二行」
    text = re.sub(r"<br\s*/?>", "；", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = _BOLD_RE.sub("", text)
    text = text.replace("'''", "").replace("''", "")
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"^\s*[|!]\s*", "", text)
    return text.strip(" \t|\n")


def normalize_key(value: str) -> str:
    """字段名/模板名归一化：BWIKI 实测写成 `{{弧盘 \\n|…}}`，名字里带换行与空格。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _extract_refs(text: str) -> str:
    return _REF_RE.sub("", text)


def iter_templates(text: str) -> Iterable[Dict[str, Any]]:
    """按出现顺序产出文本里的顶层模板：`{"name": …, "fields": [(k, v)], "positional": […]}`。"""
    body = strip_comments(text or "")
    index = 0
    length = len(body)
    while index < length - 1:
        start = body.find("{{", index)
        if start < 0:
            return
        depth = 0
        cursor = start
        end = -1
        while cursor < length - 1:
            pair = body[cursor:cursor + 2]
            if pair == "{{":
                depth += 1
                cursor += 2
                continue
            if pair == "}}":
                depth -= 1
                cursor += 2
                if depth == 0:
                    end = cursor
                    break
                continue
            cursor += 1
        if end < 0:
            return
        inner = body[start + 2:end - 2]
        segments = _split_top_level(inner)
        name = normalize_key(clean_value(segments[0])) if segments else ""
        fields: List[Tuple[str, str]] = []
        positional: List[str] = []
        for segment in segments[1:]:
            if "=" in segment:
                key, _, raw = segment.partition("=")
                key = normalize_key(clean_value(key))
                if key:
                    fields.append((key, clean_value(raw)))
                    continue
            value = clean_value(segment)
            if value:
                positional.append(value)
        yield {"name": name, "fields": fields, "positional": positional}
        index = start + 2  # 继续扫描内部模板（嵌套内容常常也有用）


def template_fields(
    text: str,
    templates: Optional[Sequence[str]] = None,
    skip_fields: Optional[Iterable[str]] = None,
    max_value: int = _TEMPLATE_MAX_VALUE,
) -> List[Tuple[str, str]]:
    """取出模板字段（键, 值）序列，按模板出现顺序拼接，同名键保留第一个非空值。"""
    wanted = {normalize_key(name) for name in (templates or []) if str(name).strip()}
    skip = set(_SKIP_FIELDS) | {normalize_key(name) for name in (skip_fields or [])}
    pairs: List[Tuple[str, str]] = []
    seen: Dict[str, int] = {}
    for template in iter_templates(text):
        name = template["name"]
        if not name or name.startswith("#") or name.startswith("!"):
            continue
        if wanted and name not in wanted:
            continue
        if not wanted and (":" in name or name.lower() in {"施工中", "需要帮助", "stub"}):
            continue
        for key, value in template["fields"]:
            if not key or key in skip or not value:
                continue
            if _SKIP_FIELD_RE.match(key):
                continue
            if key in seen:
                # 同名键以更长的值为准（模板里常见「占位空值 + 真值」两段）
                existing_index = seen[key]
                if len(value) > len(pairs[existing_index][1]):
                    pairs[existing_index] = (key, value[:max_value])
                continue
            seen[key] = len(pairs)
            pairs.append((key, value[:max_value]))
    return pairs


def build_page_text(title: str, pairs: Sequence[Tuple[str, str]], source_name: str = "") -> str:
    """把模板字段渲染成一段可检索的正文（进 chunks 当证据用）。"""
    lines = [f"{title}（结构化字段，来自 {source_name or 'BWIKI'}）"]
    lines.extend(f"{key}：{value}" for key, value in pairs)
    return "\n".join(lines)


def facts_from_pairs(
    title: str,
    pairs: Sequence[Tuple[str, str]],
    url: str = "",
    source_name: str = "",
) -> List[Dict[str, Any]]:
    """每个字段一条原子条目：字段名→值，没有启发式，也没有模型改写。"""
    facts: List[Dict[str, Any]] = []
    for key, value in pairs:
        if not value or len(value) < 2:
            continue
        answer = f"{title} 的{key}为：{value}"
        if len(answer) > _TEMPLATE_MAX_VALUE:
            answer = answer[:_TEMPLATE_MAX_VALUE] + "…"
        facts.append(
            {
                "title": f"{title}·{key}"[:40],
                "answer": answer,
                "tags": f"{key}, {source_name or 'BWIKI'}, 结构化字段",
                "extraction": "api",
            }
        )
    return facts


# 模板默认值判定：同一个分类里，某字段在多数页面取值完全相同，
# 就认为它是模板占位值而不是实体属性（实测 角色图鉴.最高生命 全部是 145784、
# 最高攻击 全部是 8424、物理防御/法术防御 全是 80/76）。
CONSTANT_MIN_PAGES = 4      # 少于这么多页时不做判定（样本太小，容易误杀）
CONSTANT_SHARE = 0.8        # 取值集中度阈值


def constant_fields(
    pairs_by_page: Mapping[str, Sequence[Tuple[str, str]]],
    min_pages: int = CONSTANT_MIN_PAGES,
    share: float = CONSTANT_SHARE,
) -> Dict[str, str]:
    """找出「同分类里取值恒定」的字段 → {字段: 那个恒定值}（模板默认值）。

    必须剔除的理由：照单全收的话，知识库里会多出
    「哈尼娅的最高攻击 = 8424」「九原的最高攻击 = 8424」这种**看起来精确、其实全错**
    的条目（这两个数其实是模板占位值）。宁可少几条，也不能让模型自信地答错。
    """
    counter: Dict[str, Dict[str, int]] = {}
    for pairs in pairs_by_page.values():
        for key, value in pairs:
            if not value:
                continue
            counter.setdefault(key, {})
            counter[key][value] = counter[key].get(value, 0) + 1
    constant: Dict[str, str] = {}
    for key, values in counter.items():
        total = sum(values.values())
        if total < min_pages:
            continue
        value, count = max(values.items(), key=lambda item: item[1])
        if count / total >= share:
            constant[key] = value
    return constant


_TAUTOLOGY_STRIP = "\"'「」『』《》〈〉（）() \u3000"


def is_tautological(value: str, title: str) -> bool:
    """字段值去掉引号/书名号后等于页面标题 → 这条原子条目零信息量。

    实测 `{{弧盘}}` 里 `弧盘名=X` 而页面标题就是 X、角色模板里 `名称/称号` 同理，
    入库会得到「X 的弧盘名是 X」这种废话条目：既占检索名额、又可能把真正带数值的
    `描述` 字段挤出证据列表（评测里真发生过一次）。
    """
    clean = value.strip().strip(_TAUTOLOGY_STRIP).strip()
    return bool(clean) and clean == title.strip()


def default_cache_dir() -> Optional[Path]:
    """缓存目录：数据目录下的 cache/wiki_api（不可写时返回 None，只是没有缓存）。"""
    try:
        from . import paths

        target = paths.data_dir() / "cache" / "wiki_api"
    except Exception:  # noqa: BLE001
        return None
    return target
