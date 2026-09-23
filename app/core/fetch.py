"""网页抓取与正文抽取。

要点
----
- 有礼貌：默认读取并遵守 robots.txt（按 UA 判定），被禁止的 URL 直接跳过并记录原因。
- 有兜底：trafilatura 抽取失败时，回落到「去掉脚本/导航后取最长文本块」的启发式。
- 有上限：单页最多读取 max_bytes，避免误抓大文件把内存打满。
- 站点适配：支持指定正文容器选择器（例如 BWIKI 与萌娘百科的 .mw-parser-output），
  这是中文 wiki 抓取的关键，否则左侧上百条导航会被当成正文入库。
"""

from __future__ import annotations

import ipaddress
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from . import secrets
from . import quality

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 NTE-RAG/1.0"
)

# 需要退避重试的状态码：限流 + 网关/服务端临时故障。
# 567 是腾讯 EdgeOne WAF 的「请求被拦截」码，实测 BWIKI 在连续请求后会返回它，
# 通常过一会儿自行恢复，因此按可重试处理并给出明确提示。
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 524, 567})

# 命中这些状态不是「临时抽风」，而是站点在拦截抓取：进入域级冷却
COOLDOWN_STATUSES = frozenset({403, 429, 567})

# 这些域名的页面重、反爬严，单独放慢节奏（实测 BWIKI 连续抓 190 页后开始返回 567）
SLOW_HOSTS = ("bwiki.cn", "wiki.biligame", "moegirl.org", "huijiwiki")
RETRY_DELAYS = (2.0, 5.0, 10.0)

# 站点声明的 Crawl-delay 超过这个值就不抓了（少抓一页好过按错误的节奏持续请求）。
MAX_CRAWL_DELAY = 30.0

# 域级冷却与限速状态**按进程共享**：过去这两张表挂在 Fetcher 实例上，
# 而更新器（autoupdate 每次新建一个）与聊天的联网补充（rag 每次新建一个）
# 各持一份，更新器被 WAF 拦下后得到的 600 秒冷却，聊天那条路完全看不见，
# 于是同一分钟内又从另一个 fetcher 打过去。现在按主机名共享，锁也共用。
_SHARED_COOLDOWN: Dict[str, float] = {}
_SHARED_LAST_REQUEST: Dict[str, float] = {}
_SHARED_LOCK = threading.RLock()

# robots.txt 缓存同样按进程共享：过去它挂在 RobotsPolicy 实例上，而
# Fetcher 每构造一个就新建一份 policy —— rag 每次回答都会新建 Fetcher，
# 于是「每回答一次就多打一次不走限速的 /robots.txt」，共享冷却在
# robots 这条路上被整个绕过。共享后同一站点在一次对话里只取一次（TTL 见下文）。
_SHARED_ROBOTS: Dict[str, Any] = {}
_SHARED_ROBOTS_LOCK = threading.RLock()


def _host_of(url: Any) -> str:
    """取 URL 的主机名；无法解析时返回空串，绝不向外抛异常。

    `urlparse("http://[abc").hostname` 会抛 `ValueError: Invalid IPv6 URL`，
    而主机名被用在冷却表、限速表这些「只为记录」的地方——为了取一个字符串
    而把整个抓取流程打断并不值得（实测 cooldown_left("http://[abc") 会直接抛）。
    """
    try:
        return urlparse(str(url or "")).hostname or ""
    except ValueError:
        return ""


def _is_local_host(host: str) -> bool:
    """判断主机名是否指向本机（localhost / 回环 / 私有网段）。

    robots.txt 只对「别人的站点」有意义。本机地址要么是本项目自带的测试服务，
    要么是用户自建的接口，它们不会有 /robots.txt；而「读不到 robots
    就不抓」这条规则会把它们全部挡死（自检里的本地 http.server 就撞在这里）。
    """
    name = (host or "").strip().lower().strip("[]")
    if not name:
        return False
    if name in ("localhost", "localhost.localdomain") or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


class BlockedError(RuntimeError):
    """目标站点触发了访问限制（WAF / 限流），需要稍后重试。"""

# 正文容器候选：按优先级尝试
CONTENT_SELECTORS: Sequence[str] = (
    ".mw-parser-output",
    "#mw-content-text",
    "article",
    ".article-content",
    ".content",
    "#content",
    ".post-content",
    ".detail-content",
    "#artibody",
    ".news_content",
)

_STRIP_SELECTORS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "iframe",
    ".nav",
    ".menu",
    ".sidebar",
    ".advertisement",
    ".ad",
    ".comment",
    ".comments",
    ".related",
    ".breadcrumb",
    ".toolbar",
    ".mw-editsection",
    ".catlinks",
    ".printfooter",
    "#toc",
    ".toc",
)


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    ok: bool = False
    title: str = ""
    text: str = ""
    published: str = ""
    site: str = ""
    error: str = ""
    skipped: bool = False
    bytes_read: int = 0
    # 正文在 max_bytes 处被截断：以前静默 break，超长页面被当成完整页
    # 入库/分块/建索引，尾部内容丢失且没有任何提示。
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "ok": self.ok,
            "title": self.title,
            "published": self.published,
            "site": self.site,
            "error": self.error,
            "skipped": self.skipped,
            "truncated": self.truncated,
            "chars": len(self.text or ""),
        }


ROBOTS_TTL_OK = 1800.0        # 读到有效规则：缓存 30 分钟
ROBOTS_TTL_MISSING = 6 * 3600.0  # 明确 404 / 空文件：缓存 6 小时（确认过的「无规则」）
ROBOTS_TTL_ERROR = 300.0      # 拉取失败（超时 / 5xx / 网络错误）：只缓存 5 分钟，之后重试


class _RobotsEntry:
    """一台主机 robots.txt 的缓存项：状态 + 原文 + 取回时刻。"""

    __slots__ = ("status", "parser", "fetched_at")

    def __init__(self, status: str, parser: Optional[RobotFileParser], fetched_at: float) -> None:
        self.status = status                    # "ok" / "missing" / "error"
        self.parser = parser
        self.fetched_at = fetched_at


class RobotsPolicy:
    """按主机缓存 robots.txt 解析结果，严格遵守各站规则。

    状态分三种，缓存时长不同：

    - ``ok``      —— 读到有效规则，按规则判定（缓存 30 分钟）；
    - ``missing`` —— 明确 404 / 空文件，即站点未声明规则，视为允许（缓存 6 小时）；
    - ``error``   —— 拉取失败（超时、5xx、连接错误）。**这类不确定状态一律判为
      「不允许抓取」**：拿不到 rules 时最保守的做法就是不抓，缓存 5 分钟后重试。
      以前这里把「拉取失败」和「404」一起当成无限制，等于一次网络抖动就把整站
      翻成可任意抓取。
    """

    def __init__(self, user_agent: str = DEFAULT_UA, timeout: float = 10) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        # 缓存与锁进程共享：见文件顶部 _SHARED_ROBOTS 的说明。
        self._cache: Dict[str, _RobotsEntry] = _SHARED_ROBOTS
        self._lock = _SHARED_ROBOTS_LOCK

    def _load(self, scheme: str, host: str) -> Optional[_RobotsEntry]:
        key = f"{scheme}://{host}"
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and not self._stale(entry, now):
                return entry
        parser: Optional[RobotFileParser] = None
        status = "error"
        try:
            response = httpx.get(
                f"{key}/robots.txt",
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
                follow_redirects=True,
            )
            if response.status_code == 200 and response.text.strip():
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
                status = "ok"
            elif response.status_code in (404, 410) or response.status_code == 200:
                # 404/410：站点没有 robots.txt；200 但空文件：同样等于没声明规则。
                status = "missing"
            else:
                status = "error"
        except Exception:
            parser = None
            status = "error"
        entry = _RobotsEntry(status=status, parser=parser, fetched_at=now)
        with self._lock:
            self._cache[key] = entry
        return entry

    def _stale(self, entry: _RobotsEntry, now: float) -> bool:
        if entry.status == "ok":
            ttl = ROBOTS_TTL_OK
        elif entry.status == "missing":
            ttl = ROBOTS_TTL_MISSING
        else:
            ttl = ROBOTS_TTL_ERROR
        return (now - entry.fetched_at) >= ttl

    def note(self, url: str) -> str:
        """给界面/日志用的简短说明，解释为什么这个 URL 被判成不可抓。"""
        try:
            parts = urlparse(url)
        except Exception:
            return "robots.txt 判定失败（URL 无法解析），已保守跳过"
        if not parts.scheme or not parts.hostname:
            return "robots.txt 判定失败（URL 缺少主机名），已保守跳过"
        entry = self._load(parts.scheme, parts.hostname)
        if entry is None or entry.status == "error":
            return "无法读取该站 robots.txt（网络错误或超时），按最保守处理：本轮不抓取该站"
        if entry.status == "missing":
            return "robots.txt 不存在（该站未声明任何规则），允许抓取"
        return "robots.txt 禁止抓取该地址"

    def allowed(self, url: str) -> bool:
        try:
            parts = urlparse(url)
        except Exception:
            return False
        if not parts.scheme or not parts.hostname:
            return False
        if _is_local_host(parts.hostname):
            # 本机地址不适用 robots.txt：没有「站点」可言，而且本机服务多半只是
            # 测试/自建接口，根本没有 /robots.txt。否则「读不到 robots 就不抓」
            # 会把所有 localhost 用例（自检里的本地 http.server）一起挡掉。
            return True
        entry = self._load(parts.scheme, parts.hostname)
        if entry is None or entry.status == "error":
            # 规则读不到（超时 / 5xx / 网络错误）就不抓：越权抓取的代价大于少抓一页。
            return False
        if entry.status == "missing":
            # 明确 404 / 空文件：站点根本没有 robots.txt，也就没有任何规则可违反
            # （RFC 9309：没有规则即不限制）。以前这里把 missing 也一并判成
            # 「不允许」，于是官网这类「有 robots.txt 但内容为空」的站点永远抓不到，
            # 报错却是「robots.txt 不存在，允许抓取」——行为和文案互相矛盾。
            return True
        if entry.parser is None:
            return False
        try:
            return bool(entry.parser.can_fetch(self.user_agent, url))
        except Exception:
            return False

    def crawl_delay(self, url: str) -> float:
        try:
            parts = urlparse(url)
            entry = self._load(parts.scheme, parts.hostname or "")
            if entry is None or entry.parser is None:
                return 0.0
            delay = entry.parser.crawl_delay(self.user_agent)
            return float(delay) if delay else 0.0
        except Exception:
            return 0.0


class Fetcher:
    """带 robots 检查、限速与正文抽取的抓取器。"""

    def __init__(
        self,
        user_agent: str = DEFAULT_UA,
        timeout: float = 25,
        max_bytes: int = 3_000_000,
        respect_robots: bool = True,
        min_interval: float = 2.0,
        blocked_domains: Optional[Sequence[str]] = None,
        cooldown_seconds: float = 600.0,
        slow_hosts: Optional[Sequence[str]] = None,
        slow_min_interval: float = 5.0,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = float(timeout)
        self.max_bytes = int(max_bytes)
        self.respect_robots = respect_robots
        self.min_interval = float(min_interval)
        self.blocked_domains = quality.normalize_domains(blocked_domains or ())
        # 被 WAF 拦下（HTTP 567/429/403）之后，整个域冷却一段时间不再打扰——
        # 既降低对方压力，也让「这些页面这次没抓到」变成可见的一次跳过，
        # 而不是几十次无意义的重试。
        self.cooldown_seconds = float(cooldown_seconds)
        self.slow_hosts = [item.lower() for item in (slow_hosts or ())]
        self.slow_min_interval = float(slow_min_interval)
        self.robots = RobotsPolicy(user_agent=self.user_agent, timeout=min(10, timeout))
        # 共享状态：见文件顶部 _SHARED_* 的说明
        self._last_request = _SHARED_LAST_REQUEST
        self._cooldown = _SHARED_COOLDOWN
        self._lock = _SHARED_LOCK
        self.retries = len(RETRY_DELAYS)
        self.headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        }
        # 连接池复用：过去每次尝试都新建 httpx.Client（`_get_with_retry` 甚至
        # 用模块级 httpx.get），一次抓取 200 页 × 最多 4 次尝试就是几百次 TLS
        # 握手，而对方正是按连接数判定是否在爬取。这里复用一个 client，见 reset_client()。
        self._client: Optional[httpx.Client] = None

    # ------------------------------------------------------------------

    def client(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(
                    timeout=self.timeout, follow_redirects=True, headers=self.headers
                )
            return self._client

    def reset_client(self) -> None:
        """丢弃并关闭当前连接池（换代理/换 UA 或收尾时用）。"""
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不该影响调用方
                pass

    def close(self) -> None:
        self.reset_client()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @staticmethod
    def _retryable(status: int) -> bool:
        return status in RETRY_STATUSES

    @staticmethod
    def _status_hint(status: int) -> str:
        if status == 567:
            return "目标站点触发了访问限制（HTTP 567，站点 WAF 拦截），请稍后再试"
        if status == 429:
            return "请求过于频繁被限流（HTTP 429），请稍后再试"
        if status >= 500:
            return f"目标站点服务端临时错误（HTTP {status}）"
        return f"HTTP {status}"

    def _backoff(self, attempt: int, url: str) -> None:
        """第 attempt+1 次尝试前的等待：丢连接、睡退避、再限速。

        连接池里可能留着一条已经死掉的 keep-alive，复用它会立刻再失败一次，
        所以退避前先丢掉连接池。
        """
        self.reset_client()
        time.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
        self._throttle(url)

    # ------------------------------------------------------------------

    def _host_interval(self, host: str) -> float:
        for token in self.slow_hosts:
            if token and token in host:
                return max(self.min_interval, self.slow_min_interval)
        return self.min_interval

    def cooldown_left(self, url: str) -> float:
        """该域名还有多少秒处于冷却期（0 表示可以正常抓取）。"""
        host = _host_of(url)
        with self._lock:
            until = self._cooldown.get(host, 0.0)
        return max(0.0, until - time.time())

    def _enter_cooldown(self, url: str, status: int) -> None:
        if status not in COOLDOWN_STATUSES:
            return
        host = _host_of(url)
        if not host:
            return
        with self._lock:
            self._cooldown[host] = time.time() + self.cooldown_seconds

    def _throttle(self, url: str) -> None:
        host = _host_of(url)
        try:
            declared = self.robots.crawl_delay(url)
        except Exception:  # noqa: BLE001 - 限速不该因为取 robots 失败而中断
            declared = 0.0
        # 只在 robots 明确要求时才真的按它睡：读不到 robots 时 crawl_delay 返回 0，
        # 因此这里不会出现「因为网络抖动而每页多睡 30 秒」。
        delay = max(self._host_interval(host), declared)
        # 预订「下一次可发请求的时刻」：过去是「锁内读 last → 锁外 sleep(wait)
        # → 锁内写 now」，两个线程会算出同一个 wait，睡完之后连续各发一次请求——
        # 每主机限速在并发下等于没生效。现在在锁内一次算清并写回，锁外只负责睡。
        with self._lock:
            earliest = self._last_request.get(host, 0.0) + delay
            now = time.time()
            wait = earliest - now
            # 站点用 Crawl-delay 声明的节奏必须睡满：过去是 time.sleep(min(wait, 10))，
            # 声明 Crawl-delay: 60 的站点会被按 10 秒爬——这不只是不礼貌，还会直接
            # 招来 WAF 封禁。真遇到这么慢的站点，放弃这一页并把原因说清楚，
            # 而不是偷偷用更快的节奏抓完。
            if wait > MAX_CRAWL_DELAY:
                raise BlockedError(
                    f"该站要求的抓取间隔为 {wait:.0f} 秒（超过 {MAX_CRAWL_DELAY:.0f} 秒上限），本轮跳过"
                )
            self._last_request[host] = earliest if wait > 0 else now
        if wait > 0:
            time.sleep(wait)

    def _hop_guard(self, original: str, final: str, ignore_robots: bool) -> str:
        """重定向落点检查：主机变了就重新过一遍闸门。返回原因，空串表示放行。

        黑名单、robots、私网判定过去只在**初始 URL** 上做过，而 httpx 默认跟随
        重定向（client() 里 follow_redirects=True）：一个 301/302 就能把抓取送到
        被屏蔽的域、robots 禁止抓的位置，甚至送到本机服务上（本应用自己就跑着
        一个 FastAPI），然后把 body 拉进知识库。这里在拿到最终 URL 后补一次同口径检查。
        """
        origin_host = _host_of(original)
        final_host = _host_of(final)
        if not final_host or final_host == origin_host:
            return ""
        if quality.is_blocked(final, self.blocked_domains, include_baseline=False):
            return f"该地址重定向到你的来源黑名单域名（{final_host}），已丢弃"
        baseline = quality.baseline_block_reason(final)
        if baseline:
            return f"该地址重定向到已屏蔽的站点（{final_host}）：{baseline}"
        if _is_local_host(final_host) and not _is_local_host(origin_host):
            return f"该地址重定向到本机/内网地址（{final_host}），已丢弃"
        if self.respect_robots and not ignore_robots and not self.robots.allowed(final):
            return f"该地址重定向到 robots.txt 不允许抓取的位置（{final_host}），已丢弃"
        return ""

    def fetch(
        self,
        url: str,
        ignore_robots: bool = False,
        container_selectors: Sequence[str] = (),
    ) -> FetchResult:
        result = FetchResult(url=url)
        result.site = _host_of(url)

        # 用户黑名单优先于一切：命中即跳过，不发起任何请求
        if quality.is_blocked(url, self.blocked_domains, include_baseline=False):
            result.skipped = True
            result.error = "该域名在你的来源黑名单中，已跳过"
            return result

        # 预置基线（字典站、视频页）：同样在发请求之前拦掉，理由写清楚
        baseline = quality.baseline_block_reason(url)
        if baseline:
            result.skipped = True
            result.error = baseline
            return result

        # 冷却判定放在 robots 之前：既然已经决定避让这个站点，就不该再为它发任何请求
        # （读 /robots.txt 本身也是一次请求）。顺序固定下来还有个副作用是有益的——
        # 这道门变成**可离线复现**的，不再取决于当时能不能读到 robots.txt。
        left = self.cooldown_left(url)
        if left > 0:
            result.skipped = True
            result.error = f"站点此前触发了访问限制，正在冷却中（约 {int(left)} 秒后恢复），本轮跳过"
            return result

        if self.respect_robots and not ignore_robots and not self.robots.allowed(url):
            result.skipped = True
            result.error = self.robots.note(url)
            return result

        try:
            self._throttle(url)
        except BlockedError as error:
            # 该站要求的间隔太长（见 _throttle 的上限说明）：放弃这一页
            result.skipped = True
            result.error = str(error)
            return result
        last_status = 0
        for attempt in range(self.retries + 1):
            if attempt:
                self._backoff(attempt, url)
            try:
                client = self.client()
                with client.stream("GET", url) as response:
                        result.status = response.status_code
                        result.final_url = str(response.url)
                        last_status = response.status_code
                        # 重定向落点检查：见 _hop_guard 的说明
                        hop = self._hop_guard(url, result.final_url, ignore_robots)
                        if hop:
                            result.error = hop
                            return result
                        if response.status_code in COOLDOWN_STATUSES:
                            # WAF 拦截 / 限流不是瞬时抖动，重试只会让封禁更深：
                            # 立即进入域级冷却并返回，不再把同一个 URL 打 4 遍。
                            result.error = self._status_hint(response.status_code)
                            self._enter_cooldown(url, response.status_code)
                            return result
                        if self._retryable(response.status_code) and attempt < self.retries:
                            continue
                        if response.status_code >= 400:
                            result.error = self._status_hint(response.status_code)
                            self._enter_cooldown(url, response.status_code)
                            return result
                        # Content-Type 的大小写不保证（实测有站点返回 `TEXT/HTML`）：
                        # 先小写再按类型分流。JSON 绝不能过 HTML 抽取（wikitext 原文里
                        # 带 <br>、<span>，会被当成标签把尾部整段吃掉）；反过来
                        # `text/plain` 大多是纯文本接口，走 HTML 抽取只会得到空正文。
                        content_type = response.headers.get("content-type", "").lower()
                        if "json" in content_type:
                            kind = "json"
                        elif "html" in content_type or "xml" in content_type:
                            kind = "html"
                        elif "text" in content_type:
                            kind = "text"
                        else:
                            result.error = f"不支持的内容类型：{content_type[:60]}"
                            return result
                        buffer = bytearray()
                        # 整体时限：httpx 的标量 timeout 是「每个阶段」的超时，
                        # 服务器每 30 秒滴一个字节也能永远吊住这次抓取。这里给整次
                        # 请求一个墙钟预算，超了就当作超时丢弃这一页。
                        deadline = time.monotonic() + self.timeout
                        for piece in response.iter_bytes():
                            buffer.extend(piece)
                            if len(buffer) >= self.max_bytes:
                                # 标记被截断：以前这里静默 break，超限的长页面
                                # 被当成完整页入库，尾部内容丢失而调用方毫无察觉。
                                result.truncated = True
                                break
                            if time.monotonic() > deadline:
                                raise httpx.ReadTimeout("响应体读取超出整体时限")
                        encoding = response.encoding
                result.bytes_read = len(buffer)
                raw = bytes(buffer)
                result.final_url = result.final_url or url
                if kind == "json":
                    result.text = _decode(raw, encoding)
                    result.title = ""
                    result.published = ""
                    result.ok = bool(result.text)
                    if not result.ok:
                        result.error = "接口返回了空内容"
                    return result
                if kind == "text":
                    result.text = _decode(raw, encoding).strip()
                    result.title = ""
                    result.published = ""
                    result.ok = bool(result.text)
                    if not result.ok:
                        result.error = "接口返回了空内容"
                    return result
                html = _decode(raw, encoding)
                text, title, published = extract_content(
                    html, result.final_url, container_selectors=tuple(container_selectors)
                )
                result.text = text
                result.title = title
                result.published = published
                result.ok = bool(text)
                if not result.ok:
                    result.error = "未能从页面中抽取到正文"
                return result
            except httpx.TimeoutException:
                if attempt < self.retries:
                    continue
                result.error = "请求超时"
                return result
            except httpx.HTTPError as error:
                if attempt < self.retries:
                    continue
                result.error = secrets.scrub(f"网络错误：{error}")
                return result
            except Exception as error:  # noqa: BLE001
                result.error = secrets.scrub(f"抓取失败：{error}")
                return result
        if last_status:
            result.error = self._status_hint(last_status)
            self._enter_cooldown(url, last_status)
        return result

    def _get_with_retry(
        self,
        url: str,
        headers: Dict[str, str],
        params: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        """带退避重试的 GET，供 JSON/HTML 接口复用。"""
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            if attempt:
                self._backoff(attempt, url)
            try:
                # 复用连接池：这里是所有 MediaWiki / 玩一玩接口流量走的路径，
                # 每次尝试新建 client 会让一次抓取多出几千次 TLS 握手。
                # 边收边判大小：过去用 client.get() 先把整个响应读进内存、再比较
                # len(response.content)，于是 max_bytes 这道内存上限对这两条主路径
                # 实际上不生效——一个超大或无限滴灌的风控页就能把进程吃满。
                client = self.client()
                with client.stream("GET", url, headers=headers, params=params) as streamed:
                    buffer = bytearray()
                    for piece in streamed.iter_bytes():
                        buffer.extend(piece)
                        if len(buffer) > self.max_bytes:
                            raise BlockedError(
                                f"接口返回内容过大（超过 {self.max_bytes // 1000} KB），已放弃解析"
                            )
                    # 重新包装成「已读完」的 Response：流式读过之后原对象的 .content
                    # 不再可用（httpx 会抛 StreamConsumed），而调用方习惯用
                    # .content / .json() / .encoding / .url，所以显式构造一个。
                    response = httpx.Response(
                        status_code=streamed.status_code,
                        headers=streamed.headers,
                        content=bytes(buffer),
                        request=streamed.request,
                    )
            except httpx.HTTPError as error:
                last_error = error
                continue
            if response.status_code in COOLDOWN_STATUSES:
                # 与 fetch() 一致：WAF/限流码立即冷却，不做退避重试
                self._enter_cooldown(url, response.status_code)
                raise BlockedError(self._status_hint(response.status_code))
            if self._retryable(response.status_code) and attempt < self.retries:
                last_error = BlockedError(self._status_hint(response.status_code))
                continue
            if response.status_code >= 400:
                self._enter_cooldown(url, response.status_code)
                raise BlockedError(self._status_hint(response.status_code))
            return response
        if isinstance(last_error, BlockedError):
            raise last_error
        raise BlockedError(secrets.scrub(f"请求失败：{last_error}"))

    def _guard(self, url: str, ignore_robots: bool) -> None:
        if quality.is_blocked(url, self.blocked_domains, include_baseline=False):
            raise RuntimeError("该域名在你的来源黑名单中，已跳过")
        baseline = quality.baseline_block_reason(url)
        if baseline:
            raise RuntimeError(baseline)
        # 冷却检查必须放在这里，不能只在 fetch() 里查：
        # get_json()/get_html() 走的是 _get_with_retry()，只经过 _guard()，
        # 而 MediaWiki API 与玩一玩这类来源正是走这两条路。过去站点已经进入
        # 600 秒冷却后，批量 API 仍会持续请求，越抓越被封。
        # 顺序上它排在 robots 之前：冷却中不为该站发任何请求（/robots.txt 也算一次）。
        left = self.cooldown_left(url)
        if left > 0:
            raise BlockedError(
                f"站点此前触发了访问限制，正在冷却中（约 {int(left)} 秒后恢复），本轮跳过"
            )
        if self.respect_robots and not ignore_robots and not self.robots.allowed(url):
            raise RuntimeError(self.robots.note(url))

    def get_json(self, url: str, ignore_robots: bool = False, params: Optional[Dict[str, Any]] = None) -> Any:
        self._guard(url, ignore_robots)
        response = self._get_with_retry(
            url, {**self.headers, "Accept": "application/json,text/plain,*/*"}, params
        )
        # 重定向落点检查：接口这条路径同样可能被 301/302 带到别处
        hop = self._hop_guard(url, str(response.url), ignore_robots)
        if hop:
            raise BlockedError(hop)
        # 返回体也要有上限：接口正常时都很小，但风控页/错误页可能是个大 HTML。
        if len(response.content) > self.max_bytes:
            raise BlockedError(f"接口返回内容过大（超过 {self.max_bytes // 1000} KB），已放弃解析")
        try:
            return response.json()
        except ValueError as error:
            # 风控拦截页通常是 HTML：过去这里直接抛 json.JSONDecodeError，
            # 调用方只能看到「Expecting value: line 1 column 1」这种与真实原因无关的报错。
            head = _decode(response.content[:200], response.encoding)
            raise BlockedError(
                f"接口未返回 JSON（可能是风控拦截页），已跳过：{secrets.scrub(head)}"
            ) from error

    def get_html(self, url: str, ignore_robots: bool = False) -> str:
        self._guard(url, ignore_robots)
        response = self._get_with_retry(url, self.headers)
        hop = self._hop_guard(url, str(response.url), ignore_robots)
        if hop:
            raise BlockedError(hop)
        if len(response.content) > self.max_bytes:
            raise BlockedError(f"页面过大（超过 {self.max_bytes // 1000} KB），已跳过")
        return _decode(response.content, response.encoding)


def _decode(raw: bytes, encoding: Optional[str] = None) -> str:
    """稳健解码：优先声明编码，其次 charset_normalizer，最后 utf-8 容错。"""
    if encoding and encoding.lower() not in ("iso-8859-1",):
        try:
            return raw.decode(encoding, errors="replace")
        except Exception:
            pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    for candidate in ("utf-8", "gb18030", "gbk", "big5"):
        try:
            return raw.decode(candidate)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _clean_soup(soup: Any) -> None:
    for selector in _STRIP_SELECTORS:
        for node in soup.select(selector):
            node.decompose()


def pick_container(soup: Any, selectors: Sequence[str] = CONTENT_SELECTORS):
    """挑出正文容器：优先用户指定选择器，其次通用候选，最后退回 body。"""
    for selector in selectors:
        node = soup.select_one(selector)
        if node is not None and len(node.get_text(strip=True)) > 200:
            return node
    for selector in CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if node is not None and len(node.get_text(strip=True)) > 200:
            return node
    return soup.body or soup


def extract_content(
    html: str,
    url: str = "",
    container_selectors: Sequence[str] = (),
) -> tuple[str, str, str]:
    """返回 (正文, 标题, 发布时间)。"""
    if not html:
        return "", "", ""
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = _clean_inline(soup.title.string)
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = _clean_inline(og_title["content"]) or title
    h1 = soup.find("h1")
    if h1 and not title:
        title = _clean_inline(h1.get_text())

    published = ""
    for attrs in (
        {"property": "article:published_time"},
        {"name": "publishdate"},
        {"name": "pubdate"},
        {"itemprop": "datePublished"},
        {"name": "weibo: article:create_at"},
    ):
        node = soup.find("meta", attrs=attrs)
        if node and node.get("content"):
            published = _clean_inline(node["content"])
            break
    if not published:
        time_node = soup.find("time")
        if time_node:
            published = _clean_inline(time_node.get("datetime") or time_node.get_text())

    text = ""
    selectors = tuple(container_selectors) if container_selectors else ()
    # 1) 指定/通用容器 + trafilatura（对容器内 HTML 再抽一次，效果最好）
    container = pick_container(soup, selectors) if selectors else None
    try:
        import trafilatura

        source_html = str(container) if container is not None else html
        extracted = trafilatura.extract(
            source_html,
            url=url or None,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
        if extracted:
            text = extracted
    except Exception:
        text = ""

    # 2) 容器纯文本兜底
    if len(text.strip()) < 80:
        node = container if container is not None else pick_container(soup, selectors)
        clone = BeautifulSoup(str(node), "lxml")
        _clean_soup(clone)
        text = _blocks_to_text(clone)

    # 3) 整页兜底
    if len(text.strip()) < 80:
        clone = BeautifulSoup(html, "lxml")
        _clean_soup(clone)
        text = _blocks_to_text(clone)

    return _tidy(text), title, published


def _blocks_to_text(node: Any) -> str:
    """把块级元素拼成带换行的纯文本，保留段落结构便于切块。"""
    blocks: List[str] = []
    for element in node.find_all(["h1", "h2", "h3", "h4", "h5", "p", "li", "td", "th", "div", "br", "tr"]):
        if element.name == "br":
            continue
        if element.find(["p", "li", "div", "td"]):
            continue  # 只取叶子块，避免重复
        content = _clean_inline(element.get_text(" "))
        if content and len(content) > 1:
            if element.name in ("h1", "h2", "h3", "h4", "h5"):
                blocks.append(f"\n{content}\n")
            else:
                blocks.append(content)
    return "\n".join(blocks)


def _clean_inline(text: str) -> str:
    """压缩空白并去掉残留标签。

    部分站点（如萌娘百科）的 og:title 里带原始模板片段
    （`<span class="mw-page-title-main">异环</span>`），必须再剥一层标签，
    否则标题会被写进知识库并污染检索结果。
    """
    if not text:
        return ""
    clean = re.sub(r"<[^>]{0,200}>", " ", text)
    clean = clean.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", clean).strip()


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(lines).strip()


# ----------------------------------------------------------------------
# 列表页链接枚举
# ----------------------------------------------------------------------


def build_fetcher(config: Any = None, **overrides: Any) -> Fetcher:
    """从配置构造抓取器。

    统一在这里读取「来源黑名单」等设置，避免各处构造 Fetcher 时漏掉，
    导致黑名单只在某些路径生效。
    """
    blocked: Sequence[str] = ()
    timeout = 25.0
    cooldown = 600.0
    slow_min_interval = 5.0
    if config is not None:
        try:
            blocked = config.get("auto_update", "blocked_domains", []) or []
            timeout = float(config.get("search", "timeout", 20)) + 10
        except Exception:
            pass
        try:
            cooldown = float(config.get("fetch", "cooldown_seconds", 600))
            slow_min_interval = float(config.get("fetch", "wiki_min_interval", 5))
        except Exception:
            pass
    options: Dict[str, Any] = {
        "timeout": timeout,
        "blocked_domains": blocked,
        "cooldown_seconds": cooldown,
        "slow_hosts": SLOW_HOSTS,
        "slow_min_interval": slow_min_interval,
    }
    options.update(overrides)
    return Fetcher(**options)


def extract_variants(
    html: str,
    url: str = "",
    container_selectors: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """用多种策略分别抽取正文，返回各自的字符数与预览。

    用于诊断「某站点只抽到很短正文」的问题：一眼看出是 trafilatura 漏抽、
    容器选择器不对，还是页面确实没有正文（例如纯 JS 渲染）。
    """
    from bs4 import BeautifulSoup

    variants: List[Dict[str, Any]] = []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    def add(name: str, text: str) -> None:
        clean = _tidy(text or "")
        variants.append({"strategy": name, "chars": len(clean), "preview": clean[:240]})

    # 1) 整页 trafilatura
    try:
        import trafilatura

        add("trafilatura:整页", trafilatura.extract(
            html, url=url or None, include_comments=False, include_tables=True, favor_recall=True
        ) or "")
    except Exception as error:
        add("trafilatura:整页", f"[失败] {error}")

    # 2) 指定容器 + trafilatura
    for selector in container_selectors or ():
        node = soup.select_one(selector)
        if node is None:
            add(f"容器 {selector}", "[未找到该容器]")
            continue
        try:
            import trafilatura

            add(
                f"trafilatura:容器 {selector}",
                trafilatura.extract(
                    str(node), url=url or None, include_comments=False, include_tables=True, favor_recall=True
                ) or "",
            )
        except Exception as error:
            add(f"trafilatura:容器 {selector}", f"[失败] {error}")
        clone = BeautifulSoup(str(node), "lxml")
        _clean_soup(clone)
        add(f"纯文本:容器 {selector}", _blocks_to_text(clone))

    # 3) 整页纯文本
    clone = BeautifulSoup(html, "lxml")
    _clean_soup(clone)
    add("纯文本:整页", _blocks_to_text(clone))

    # 4) body 原始文本长度（判断是否 JS 渲染空壳）
    body_text = soup.body.get_text(" ", strip=True) if soup.body else ""
    variants.append({"strategy": "body 原始文本", "chars": len(body_text), "preview": body_text[:240]})
    return variants


def extract_links(html: str, base_url: str, pattern: str = "", min_title_len: int = 4) -> List[Dict[str, str]]:
    """从列表页里取出候选详情页链接（带标题），用于后续逐篇抓取。

    `pattern` 来自数据源配置（可信的本地文件，不是用户输入），但一条写坏的
    嵌套量词正则足以把一次抓取卡死。所以这里给模式长度、每一步检视的 URL
    长度和候选总数都设了上限——超过就当作「没有匹配」。
    """
    from bs4 import BeautifulSoup

    _MAX_PATTERN = 500
    _MAX_ANCHORS = 5000
    _MAX_URL = 2048

    soup = BeautifulSoup(html, "lxml")
    results: List[Dict[str, str]] = []
    seen: set[str] = set()
    regex = None
    if pattern:
        if len(pattern) > _MAX_PATTERN:
            raise ValueError(f"链接匹配表达式过长（上限 {_MAX_PATTERN} 字符），已拒绝")
        regex = re.compile(pattern)
    for anchor in soup.find_all("a", href=True):
        if len(seen) >= _MAX_ANCHORS:
            break
        href = anchor["href"].strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        absolute = urljoin(base_url, href)
        if len(absolute) > _MAX_URL:
            continue
        if regex and not regex.search(absolute):
            continue
        if absolute in seen:
            continue
        title = _clean_inline(anchor.get_text(" ")) or _clean_inline(anchor.get("title", ""))
        if len(title) < min_title_len:
            continue
        seen.add(absolute)
        results.append({"url": absolute, "title": title})
    return results
