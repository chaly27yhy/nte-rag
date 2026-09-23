"""联网搜索：多提供商可切换，并带「免 Key 兜底」的降级链。

优先级设计
----------
1. 用户在设置里选定的主源（推荐博查，国内可直连）；
2. 主源失败或没结果时，若开启了 free_fallback，则依次尝试
   DuckDuckGo（ddgs 库）→ 必应网页抓取；
3. 全部失败时返回结构化错误，由上层提示用户，而不是静默失败。

所有提供商统一返回 SearchResult，并顺带判定来源类型（official/wiki/community），
用于后续给官方资料更高的排序权重。
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

import httpx

from . import quality
from . import secrets

SEARCH_PROVIDERS = ("bocha", "tavily", "serper", "duckduckgo", "bing", "auto")

PROVIDER_LABELS = {
    "bocha": "博查 Bocha（国内直连，需 Key）",
    "tavily": "Tavily（需 Key，需可访问海外）",
    "serper": "Serper / Google（需 Key，需可访问海外）",
    "duckduckgo": "DuckDuckGo（免 Key）",
    "bing": "必应网页（免 Key）",
    "auto": "自动：主源优先，失败则用免 Key 源",
}

FREE_PROVIDERS = ("duckduckgo", "bing")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_OFFICIAL_HOSTS = (
    "yh.wanmei.com",
    "wanmei.com",
    "nte.perfectworld.com",
    "perfectworld.com",
    "neverness.gg",
)
_WIKI_HOSTS = ("wiki.biligame.com", "biligame.com", "huijiwiki.com", "fandom.com", "wiki.gg")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    provider: str = ""
    published: str = ""
    source_type: str = "community"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SearchError(RuntimeError):
    pass


def classify_source(url: str) -> str:
    """按域名粗判来源类型：官方 / wiki / 社区。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return "community"
    if not host:
        return "community"
    for official in _OFFICIAL_HOSTS:
        if host == official or host.endswith("." + official):
            return "official"
    for wiki in _WIKI_HOSTS:
        if host == wiki or host.endswith("." + wiki) or "wiki" in host:
            return "wiki"
    return "community"


def site_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _clean_text(value: str) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", "", value)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


class SearchClient:
    """统一搜索入口。"""

    def __init__(
        self,
        provider: str = "bocha",
        bocha_key: str = "",
        tavily_key: str = "",
        serper_key: str = "",
        searxng_url: str = "",
        free_fallback: bool = True,
        timeout: float = 20,
        max_results: int = 8,
        blocked_domains: Optional[Sequence[str]] = None,
    ) -> None:
        self.provider = (provider or "bocha").lower()
        self.bocha_key = bocha_key
        self.tavily_key = tavily_key
        self.serper_key = serper_key
        self.searxng_url = searxng_url
        self.free_fallback = free_fallback
        self.timeout = float(timeout)
        self.max_results = int(max_results)
        self.blocked_domains = quality.normalize_domains(blocked_domains or ())
        self.last_errors: List[str] = []

    # ------------------------------------------------------------------

    def search(self, query: str, max_results: Optional[int] = None) -> List[SearchResult]:
        limit = int(max_results or self.max_results)
        self.last_errors = []
        order = self._provider_order()
        collected: List[SearchResult] = []
        for provider in order:
            try:
                results = self._run_provider(provider, query, limit)
            except Exception as error:  # noqa: BLE001
                self.last_errors.append(f"{provider}: {secrets.scrub(str(error))[:200]}")
                continue
            if results:
                collected.extend(results)
            if len(_dedupe(collected)) >= limit:
                break
            if provider not in FREE_PROVIDERS:
                continue
        merged = _dedupe(collected)
        # 用户在黑名单里的域名直接不给出来，避免它们出现在引用里、也避免被抓取
        if self.blocked_domains:
            kept = [item for item in merged if not quality.is_blocked(item.url, self.blocked_domains)]
            if len(kept) != len(merged):
                self.last_errors.append(f"已按来源黑名单过滤 {len(merged) - len(kept)} 条搜索结果")
            merged = kept
        if not merged and self.last_errors:
            raise SearchError("；".join(self.last_errors))
        return merged[: max(limit, 1)]

    def _provider_order(self) -> List[str]:
        primary = self.provider
        if primary == "auto":
            primary = self._first_configured() or "duckduckgo"
        order = [primary]
        if self.free_fallback:
            for free in FREE_PROVIDERS:
                if free not in order:
                    order.append(free)
        return order

    def _first_configured(self) -> str:
        if self.bocha_key:
            return "bocha"
        if self.tavily_key:
            return "tavily"
        if self.serper_key:
            return "serper"
        return "duckduckgo"

    def _run_provider(self, provider: str, query: str, limit: int) -> List[SearchResult]:
        if provider == "bocha":
            return self._bocha(query, limit)
        if provider == "tavily":
            return self._tavily(query, limit)
        if provider == "serper":
            return self._serper(query, limit)
        if provider == "duckduckgo":
            return self._duckduckgo(query, limit)
        if provider == "bing":
            return self._bing(query, limit)
        raise SearchError(f"未知的搜索提供商：{provider}")

    # ------------------------------------------------------------------
    # 各提供商实现
    # ------------------------------------------------------------------

    def _bocha(self, query: str, limit: int) -> List[SearchResult]:
        if not self.bocha_key:
            raise SearchError("未配置博查 API Key")
        response = httpx.post(
            "https://api.bochaai.com/v1/web-search",
            headers={
                "Authorization": f"Bearer {self.bocha_key}",
                "Content-Type": "application/json",
            },
            json={"query": query, "count": limit, "summary": True, "freshness": "noLimit"},
            timeout=self.timeout,
            follow_redirects=True,
        )
        _check(response, "bocha")
        payload = response.json()
        pages = (((payload or {}).get("data") or {}).get("webPages") or {}).get("value") or []
        results: List[SearchResult] = []
        for page in pages:
            url = page.get("url") or page.get("siteUrl") or ""
            if not url:
                continue
            results.append(
                SearchResult(
                    title=_clean_text(page.get("name") or ""),
                    url=url,
                    snippet=_clean_text(page.get("summary") or page.get("snippet") or ""),
                    provider="bocha",
                    published=str(page.get("datePublished") or page.get("dateLastCrawled") or ""),
                    source_type=classify_source(url),
                )
            )
        return results

    def _tavily(self, query: str, limit: int) -> List[SearchResult]:
        if not self.tavily_key:
            raise SearchError("未配置 Tavily API Key")
        response = httpx.post(
            "https://api.tavily.com/search",
            headers={
                "Authorization": f"Bearer {self.tavily_key}",
                "Content-Type": "application/json",
            },
            json={
                "api_key": self.tavily_key,  # 兼容旧版鉴权方式
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
                "include_answer": False,
            },
            timeout=self.timeout,
            follow_redirects=True,
        )
        _check(response, "tavily")
        payload = response.json()
        results: List[SearchResult] = []
        for item in payload.get("results") or []:
            url = item.get("url") or ""
            if not url:
                continue
            results.append(
                SearchResult(
                    title=_clean_text(item.get("title") or ""),
                    url=url,
                    snippet=_clean_text(item.get("content") or ""),
                    provider="tavily",
                    published=str(item.get("published_date") or ""),
                    source_type=classify_source(url),
                )
            )
        return results

    def _serper(self, query: str, limit: int) -> List[SearchResult]:
        if not self.serper_key:
            raise SearchError("未配置 Serper API Key")
        response = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": self.serper_key, "Content-Type": "application/json"},
            json={"q": query, "num": limit, "gl": "cn", "hl": "zh-cn"},
            timeout=self.timeout,
            follow_redirects=True,
        )
        _check(response, "serper")
        payload = response.json()
        results: List[SearchResult] = []
        for item in payload.get("organic") or []:
            url = item.get("link") or ""
            if not url:
                continue
            results.append(
                SearchResult(
                    title=_clean_text(item.get("title") or ""),
                    url=url,
                    snippet=_clean_text(item.get("snippet") or ""),
                    provider="serper",
                    published=str(item.get("date") or ""),
                    source_type=classify_source(url),
                )
            )
        return results

    def _duckduckgo(self, query: str, limit: int) -> List[SearchResult]:
        try:
            from ddgs import DDGS  # type: ignore
        except Exception as error:  # pragma: no cover
            raise SearchError(f"未安装 ddgs 组件：{error}") from error
        results: List[SearchResult] = []
        with DDGS(timeout=self.timeout) as engine:  # type: ignore[call-arg]
            rows = engine.text(query, region="cn-zh", max_results=limit) or []
        for row in rows:
            url = row.get("href") or row.get("url") or ""
            if not url:
                continue
            results.append(
                SearchResult(
                    title=_clean_text(row.get("title") or ""),
                    url=url,
                    snippet=_clean_text(row.get("body") or row.get("snippet") or ""),
                    provider="duckduckgo",
                    source_type=classify_source(url),
                )
            )
        return results

    def _bing(self, query: str, limit: int) -> List[SearchResult]:
        from bs4 import BeautifulSoup

        response = httpx.get(
            "https://www.bing.com/search",
            params={"q": query, "count": max(10, limit), "setlang": "zh-CN", "ensearch": "0"},
            headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=self.timeout,
            follow_redirects=True,
        )
        _check(response, "bing")
        soup = BeautifulSoup(response.text, "lxml")
        results: List[SearchResult] = []
        for block in soup.select("li.b_algo")[: limit * 2]:
            link = block.select_one("h2 a")
            if not link or not link.get("href"):
                continue
            url = link["href"]
            if not url.startswith("http"):
                continue
            caption = block.select_one(".b_caption p") or block.select_one("p")
            results.append(
                SearchResult(
                    title=_clean_text(link.get_text()),
                    url=url,
                    snippet=_clean_text(caption.get_text() if caption else ""),
                    provider="bing",
                    source_type=classify_source(url),
                )
            )
        return results[:limit]

    # ------------------------------------------------------------------

    def test(self, query: str = "异环 游戏") -> Dict[str, Any]:
        """给设置页用的搜索连通性测试。"""
        report: Dict[str, Any] = {"provider": self.provider, "results": [], "errors": [], "ok": False}
        try:
            found = self.search(query, max_results=3)
            report["results"] = [item.to_dict() for item in found]
            report["ok"] = bool(found)
        except Exception as error:  # noqa: BLE001
            report["errors"].append(secrets.scrub(str(error)))
        report["errors"].extend(self.last_errors)
        report["attempted"] = self._provider_order()
        return report


def _check(response: httpx.Response, provider: str) -> None:
    if response.status_code < 400:
        return
    detail = (response.text or "")[:300]
    hints = {
        401: "密钥无效",
        403: "无权限或被风控拦截",
        429: "触发限流",
    }
    hint = hints.get(response.status_code, "")
    raise SearchError(secrets.scrub(f"[{provider}] HTTP {response.status_code} {hint} {detail}"))


def _dedupe(results: Sequence[SearchResult]) -> List[SearchResult]:
    seen: set[str] = set()
    merged: List[SearchResult] = []
    for item in results:
        key = item.url.split("#", 1)[0].rstrip("/")
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
    # 官方 > wiki > 社区，稳定排序保证官方资料优先被抓取
    weight = {"official": 0, "wiki": 1, "community": 2}
    merged.sort(key=lambda item: weight.get(item.source_type, 3))
    return merged


def build_search(config: Any) -> SearchClient:
    section = config.section("search")
    return SearchClient(
        provider=section.get("provider", "bocha"),
        bocha_key=config.get_secret("bocha_key_enc", "search"),
        tavily_key=config.get_secret("tavily_key_enc", "search"),
        serper_key=config.get_secret("serper_key_enc", "search"),
        searxng_url=section.get("searxng_url", ""),
        free_fallback=bool(section.get("free_fallback", True)),
        timeout=section.get("timeout", 20),
        max_results=section.get("max_results", 8),
        blocked_domains=config.get("auto_update", "blocked_domains", []) or [],
    )
