"""已证伪来源的撤回名单。

和「预置黑名单」的分工：
- `app/core/quality.py` 的 `BASELINE_BLOCKED_DOMAINS` 按**域名**整站屏蔽
  （字典站、视频站），在发请求之前就拦掉；
- 这里按**具体页面**撤回：站点本身正常，只有这一页被复核判定为不可信。

撤回一处来源要同时管住三件事，少一件就修不干净：
1. 不再抓取——`ingest.store_page()` 拿到名单里的 URL 直接拒收，连切片都不存（手动添加的也拒）。
2. 已经入库的内容不再被检索——`KnowledgeBase.revoke_source()` 把该来源的文档、分块与条目
   置为 `revoked`，检索层只取 `active`/`conflict`，自然看不到。软撤回不删数据，审计痕迹留着。
3. 种子导入时跳过，并且老用户的库里要补做一次——`load_seed()` 逐条过滤，
   `apply_revocations()` 在启动时按名单指纹补跑迁移。种子文件本身不动：它的哈希被文档钉住，
   改种子会改变所有人的 `seed_fingerprint`、触发整份重导，而撤回是代码层的事。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict
from urllib.parse import unquote, urlsplit, urlunsplit

# 页面 URL（原样写，比较时会归一化）→ 撤回原因（会显示在拒收提示里）
REVOKED_PAGES: Dict[str, str] = {
    "https://wiki.biligame.com/yh/薄荷": (
        "该页已证伪：技能整段抄自《银与血》角色「莱夏」的页、人物故事抄自本站「早雾」页、"
        "生日与 CV 与本站「娜娜莉」页相同（2026-09-22 脏数据审计），整页不采信"
    ),
}

PAUSED_BWIKI_REASON = "施工中，复核发现数据错误，暂不采信"
_BWIKI_MERGE_TAG = re.compile(r"并入:\s*wiki\.biligame\.com(?=[\s|,，;；]|$)", re.IGNORECASE)


def paused_reason(url: str) -> str:
    """暂时停用 BWIKI 异环专区。其他专区与历史撤回名单各自独立。"""
    try:
        parts = urlsplit(str(url or "").strip())
        path = unquote(parts.path).rstrip("/")
        host = (parts.hostname or "").rstrip(".")
        if host == "wiki.biligame.com" and (path == "/yh" or path.startswith("/yh/")):
            return PAUSED_BWIKI_REASON
    except ValueError:
        pass
    return ""


def source_paused(url: str, tags: str = "") -> bool:
    """旧行的合并文本无法按来源拆分，也先排除，等待回查独立原文。"""
    return bool(paused_reason(url) or _BWIKI_MERGE_TAG.search(str(tags or "")))

# 撤回后写入元数据表的键名：名字改了要一起改这里
FINGERPRINT_META_KEY = "curation_fingerprint"


def canonical_url(url: str) -> str:
    """把 URL 归一成可比较的形式。

    去掉锚点、查询串与末尾斜杠，百分号解码，主机名转小写。站点常以
    `%E8%96%84%E8%8D%B7` 这种编码形式入库，而名单是人手写的汉字——
    不归一就会漏掉，这是这张名单最容易失效的地方。
    """
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.lower().rstrip("/")
    path = unquote(parts.path or "")
    while len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


_REVOKED_CANON: Dict[str, str] = {canonical_url(key): value for key, value in REVOKED_PAGES.items()}

_MOEGIRL_GAME_URL = "https://zh.moegirl.org.cn/异环"
_MOEGIRL_BANNER_PAGES = {_MOEGIRL_GAME_URL, f"{_MOEGIRL_GAME_URL}/名词词典"}
_MOEGIRL_13_BANNER = (
    "1.3版本「雾中朔望星回」现正运营中!"
    "(简中服)2026年9月3日维护结束后~2026年9月24日05:59(utc+8)\n"
    "(国际及港澳台服)2026年9月9日维护结束后~2026年9月30日05:59(utc+8)"
)


_REVOKED_FACTS = {
    (_MOEGIRL_GAME_URL, "异环1.3版本运营时间"),
    ("https://www.gamersky.com/handbook/202412/1853017.shtml", "《异环》支持平台"),
    ("https://www.gamersky.com/handbook/202604/2129310.shtml", "异环游戏平台"),
    ("https://www.gamersky.com/handbook/202604/2129549.shtml", "《异环》支持的游戏平台"),
    ("https://www.gamersky.com/handbook/202604/2129607.shtml", "异环支持平台与游戏类型"),
}


def revoked_fact(url: str, title: str) -> bool:
    """排除从阶段横幅或重复站点页脚误取的旧事实。"""
    return (canonical_url(url), title) in _REVOKED_FACTS


_GAMERSKY_FOOTER = re.compile(
    r"\n(?:更多相关内容请关注[:：]异环专区\s*\n)?"
    r"本文是否解决了您的问题\s*\n游戏平台[:：]pc/ps5/android/ios\b",
    re.IGNORECASE,
)


def strip_gamersky_footer(url: str, text: str) -> str:
    """保留攻略正文，删去含旧平台清单与广告的已确认页脚。"""
    if not text or "本文是否解决了您的问题" not in text:
        return text
    if urlsplit(canonical_url(url)).hostname != "www.gamersky.com":
        return text
    footer = _GAMERSKY_FOOTER.search(text)
    return text[:footer.start()].rstrip() if footer else text


def strip_retired_banner(url: str, text: str) -> str:
    """组织问答证据时省略已复核的旧横幅与页脚。"""
    if "现正运营中!" in text and canonical_url(url) in _MOEGIRL_BANNER_PAGES:
        return text.replace(_MOEGIRL_13_BANNER, "\n", 1)
    return strip_gamersky_footer(url, text)


def revoked_reason(url: str) -> str:
    """命中撤回名单时返回原因，否则返回空串。"""
    if not _REVOKED_CANON:
        return ""
    return _REVOKED_CANON.get(canonical_url(url), "")


def is_revoked(url: str) -> bool:
    return bool(revoked_reason(url))


def registry_fingerprint() -> str:
    """名单自身的指纹：变了才对已有库补跑一次撤回迁移。"""
    payload = json.dumps(sorted(REVOKED_PAGES.items()), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def apply_revocations(kb: Any, meta_key: str = FINGERPRINT_META_KEY) -> Dict[str, Any]:
    """对已有库补做撤回（启动时调用，幂等）。

    名单是代码的一部分，但被证伪的内容可能早就进了用户的库（种子导入带来的，
    或者旧版本抓回来的）。种子指纹没变时 `load_seed()` 会直接早退，所以撤回必须
    单独跑一次迁移，否则老用户升级后照样会看到「两个说法互相矛盾」。
    有失败就不写指纹，下次启动重试——和 `seed_fingerprint` 的教训一致：
    写了指纹等于宣告「这件事已经做完了」，而它其实没做完。
    """
    fingerprint = registry_fingerprint()
    report: Dict[str, Any] = {
        "applied": False,
        "skipped": False,
        "documents": 0,
        "chunks": 0,
        "facts": 0,
        "failed": 0,
        "fingerprint": fingerprint,
    }
    try:
        current = kb.get_meta(meta_key, "")
    except Exception:
        current = ""
    if current == fingerprint:
        report["skipped"] = True
        return report
    for url in REVOKED_PAGES:
        try:
            counts = kb.revoke_source(url)
        except Exception as error:  # 数据库损坏/锁冲突都不该让启动失败
            logging.warning("[curation] 撤回 %s 失败：%s", url, error)
            report["failed"] += 1
            continue
        for key in ("documents", "chunks", "facts"):
            report[key] += int(counts.get(key, 0) or 0)
    if report["failed"]:
        return report
    try:
        kb.set_meta(meta_key, fingerprint)
    except Exception as error:
        logging.warning("[curation] 撤回指纹写入失败：%s", error)
        report["failed"] += 1
        return report
    report["applied"] = True
    return report
