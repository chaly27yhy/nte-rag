"""版本与生效时间：把「这条信息属于哪个版本、什么时候生效」变成可检索的字段。

游戏资料库里最贵的错误是「旧版本的数值被当成当前值」：官方公告几乎都写明了版本号和
生效日期（「1.3版本前瞻…于2026年8月13日更新后上线」），但抽取阶段把这些信息丢掉了，
证据里只剩一个孤零零的数字，模型无从判断它是不是过时、属于哪一版。

两条保守原则：
1. 日期要么**带年份**（`2026年8月13日` / `2026-08-13` / `2026.08.13`），要么来自站点 URL
   （官方站文章链接形如 `/20260909/…`）。单独出现的「8月13日」不猜年份；
   但当链接已经给出年份、且「月日」贴着「版本更新/上线」这类语境时，两者可以合起来
   还原出真实生效日（见 `_roll_forward`，跨年公告顺延一年，超出窗口就放弃）。
2. 全部用正则，不调模型：可离线复跑、可断言、不花钱。识别不准时的兜底是
   「留空」而不是「猜一个」，未标日期的条目在回答期照旧参与检索。

`facts.version` / `facts.effective_from` 只影响排序与证据标注，不隐藏任何旧条目
——版本化最怕把「用户问的就是旧版本」这种情况一并删掉。
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, List, Tuple

# 「版本1.3」「V1.4」「ver 1.3」「1.3版本」「1.3版」「1.3前瞻」「1.3上线」
# 不匹配「1.5倍」「12.00%」这类数值——它们没有版本语境。
VERSION_RE = re.compile(
    r"(?:版本|ver\.?|v)\s*(\d{1,2}\.\d{1,2})"
    r"|(\d{1,2}\.\d{1,2})\s*(?:版本|版|前瞻|上线|更新|开启|正式)",
    re.I,
)

# 完整日期：2026年8月13日 / 2026-08-13 / 2026/8/13 / 2026.08.13
_DATE_FULL_RE = re.compile(r"(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})")

# 站点 URL 里的发布日期：https://…/20260909/xxxx.html
_URL_DATE_RE = re.compile(r"/(20\d{2})(\d{2})(\d{2})(?:[/_]|$)")

# 日期是否「贴着」时间语境：实测种子里「异环版本更新时间线」这类条目正文列了四五个日期，
# 随便挑一个挂上去会给出「生效于 2026-04-23」这种看着确定、其实无意义的标签。
_DATE_CONTEXT_RE = re.compile(r"版本|上线|更新|维护|开启|开放|公测|生效|活动时间|前瞻|截止")
_CONTEXT_WINDOW = 30
# 正文里出现这么多个不同日期，就说明它讲的是一段时间线/排期表，不是「某天生效的某件事」。
_MAX_DATES_IN_TEXT = 2

_MARKS_RE = re.compile(r"[「」『』“”\"'《》【】()（）\s]")


def _iso(year: int, month: int, day: int) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}"


def _valid_date(month: int, day: int) -> bool:
    # 做真正的历法校验：月/日粗筛挡不住「2026年2月31日」这类不存在的日期，
    # 而它会作为「生效于」进 facts 与提示词，看起来还很精确。
    # 年份未知时用闰年 2000，让 2 月 29 日判为合法（边缘情况宁可宽松）。
    try:
        datetime.date(2000, month, day)
    except ValueError:
        return False
    return True


def extract_version(*texts: Any) -> str:
    """取文字里提到的版本号，形如 `1.3`；提到多个时取最高的那个。

    取最高的理由：一个页面/一段文字提到多个版本时（「1.3版本…1.4版本前瞻」），
    它覆盖到的信息通常以最新版本为准。识别不到返回空串。
    """
    found: List[Tuple[int, int]] = []
    for text in texts:
        for match in VERSION_RE.finditer(str(text or "")):
            value = match.group(1) or match.group(2) or ""
            major, _, minor = value.partition(".")
            try:
                found.append((int(major), int(minor)))
            except ValueError:  # pragma: no cover - 正则已保证是数字
                continue
    if not found:
        return ""
    major, minor = max(found)
    return f"{major}.{minor}"


def version_sort_key(value: Any) -> Tuple[int, ...]:
    """把版本号转成可比较的数字元组（`"1.10"` → `(1, 10)`，`"1.9"` → `(1, 9)`）。

    `extract_version` 产出的是不补零的字符串（"1.3"、"1.10"），直接按字符串比大小
    会得到 `"1.10" < "1.9"`——**把旧版本当成更新的版本**。凡是要在「同槽位的两条」
    之间比版本（排序、投票选代表）都必须用这个函数，不要直接比字符串。

    识别不到数字时返回空元组 `()`，它比任何非空元组都小，所以「没有版本号」的条目
    永远排在「有版本号」的后面——这正是想要的方向（有版本信息的更具体）。
    """
    numbers = re.findall(r"\d+", str(value or ""))
    return tuple(int(n) for n in numbers) if numbers else ()


def _dates_in_text(text: Any) -> List[Tuple[str, int]]:
    """返回 [(ISO 日期, 位置)]，只保留合法的完整日期。"""
    found: List[Tuple[str, int]] = []
    for match in _DATE_FULL_RE.finditer(str(text or "")):
        year, month, day = (int(group) for group in match.groups())
        if _valid_date(month, day):
            found.append((_iso(year, month, day), match.start()))
    return found


def _url_date(url: Any) -> str:
    match = _URL_DATE_RE.search(str(url or ""))
    if not match:
        return ""
    year, month, day = (int(group) for group in match.groups())
    return _iso(year, month, day) if _valid_date(month, day) else ""


# 正文里的「8月13日」「8-13日」（没有年份）。只在与链接年份配合时才用，绝不单独猜年份。
# 「.」不算分隔符：`1.3版本` 会被误读成 1 月 3 日——实测踩过这个坑。
_MONTH_DAY_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日?|(?<!\d)(\d{1,2})\s*[-/]\s*(\d{1,2})\s*日")


def _month_days_in_text(text: Any) -> List[Tuple[str, int]]:
    found: List[Tuple[str, int]] = []
    for match in _MONTH_DAY_RE.finditer(str(text or "")):
        month, day = (int(group) for group in match.groups() if group)
        if _valid_date(month, day):
            found.append((f"{month:02d}-{day:02d}", match.start()))
    return found


def _roll_forward(published: str, month_day: str) -> str:
    """把「8月13日」放到链接日期的年份里；年终公告写的次月日期顺延一年。

    实测场景：公告链接是 `/20260808/`（8 月 8 日发布），正文写「将于8月13日版本更新后开启」
    —— 8-13 落在发布日之后，取同年，这正是 1.3 版本的真实生效日；反过来，12 月底发的公告
    说「1月5日上线」，同年 1-5 比发布日早了近一年，明显是跨年，顺延一年（只允许往后 60 天内）。

    其它情况一律放弃，宁可退回「发布于」：同年日期早于发布日超过 45 天（多半是文章顺带提到
    的旧活动）、或者顺延后离发布日太远（发布日 8 月却说「6月1日」，那是过去的事，不是明年）。
    """
    try:
        base = datetime.date.fromisoformat(str(published)[:10])
    except ValueError:
        return ""
    month, day = int(str(month_day)[:2]), int(str(month_day)[3:])
    same_year = _safe_date(base.year, month, day)
    if same_year:
        delta = (same_year - base).days
        if -45 <= delta <= 400:
            return same_year.isoformat()
    next_year = _safe_date(base.year + 1, month, day)
    if next_year:
        delta = (next_year - base).days
        if 0 < delta <= 60:
            return next_year.isoformat()
    return ""


def _safe_date(year: int, month: int, day: int) -> Any:
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def extract_date_info(*texts: Any, url: str = "") -> Dict[str, str]:
    """日期 + 它是哪一类日期。返回 `{"date": ISO 或 "", "kind": "effective"/"published"/""}`。

    必须区分两类，否则会写出假信息：实测种子里「1.3版本更新时间」这条正文只写
    「将于8月13日版本更新后开启」（没有年份），日期若直接从公告链接 `/20260808/` 取，
    那是**公告发布日 8-8**；标成「生效于 2026-08-08」模型就可能照抄成
    「1.3 版本 8 月 8 日生效」——比不标日期更糟。

    判定顺序（每一步都以「不猜」为先）：
    1. 正文里不同日期超过 2 个 → 这讲的是一段时间线/排期表（实测种子里
       「异环版本更新时间线」列了 1.0 到 1.3 的日期），挑任何一个都是误导；
    2. 优先取**贴着时间语境**（版本/上线/更新/维护/活动时间…30 字内）的日期，
       并列时取最早的那个——公告里「8月13日上线，9月3日结束」的生效时间是前者；
    3. 正文只有「8月13日」这种缺年份的写法时，用链接日期补年份（见 `_roll_forward`），
       补出来算 `effective`——这是真实生效日，比「发布于」有用得多；
    4. 正文一个日期都没有时，退回 URL 里的 `/YYYYMMDD/`，标成 `published`。
    """
    for text in texts:
        dates = _dates_in_text(text)
        if not dates:
            continue
        if len({iso for iso, _ in dates}) > _MAX_DATES_IN_TEXT:
            date = _url_date(url)  # 时间线/排期表：不挑日期，宁可退回链接日期
            return {"date": date, "kind": "published" if date else ""}
        body = str(text or "")
        keywords = [m.start() for m in _DATE_CONTEXT_RE.finditer(body)]
        near = [
            (distance, iso)
            for iso, position in dates
            for distance in ([min(abs(position - k) for k in keywords)] if keywords else [10 ** 6])
            if distance <= _CONTEXT_WINDOW
        ]
        if near:
            return {"date": min(near)[1], "kind": "effective"}
        if len({iso for iso, _ in dates}) == 1:
            return {"date": dates[0][0], "kind": "effective"}
    published = _url_date(url)
    if published:
        upgrade = _month_day_from_context(*texts, published=published)
        if upgrade:
            return {"date": upgrade, "kind": "effective"}
        return {"date": published, "kind": "published"}
    return {"date": "", "kind": ""}


def _month_day_from_context(*texts: Any, published: str = "") -> str:
    """正文里贴着时间语境的「月日」＋链接年份 → 完整日期；拿不准返回空。"""
    for text in texts:
        body = str(text or "")
        month_days = _month_days_in_text(body)
        if not month_days or len({md for md, _ in month_days}) > 1:
            continue  # 多个不同月日（时间线）不挑
        keywords = [m.start() for m in _DATE_CONTEXT_RE.finditer(body)]
        if not keywords:
            continue
        for month_day, position in month_days:
            if min(abs(position - k) for k in keywords) > _CONTEXT_WINDOW:
                continue
            rolled = _roll_forward(published, month_day)
            if rolled:
                return rolled
    return ""


def extract_effective_from(*texts: Any, url: str = "") -> str:
    """只要日期（见 `extract_date_info`），保留给断言与脚本用。"""
    return extract_date_info(*texts, url=url)["date"]


def fact_meta(title: str = "", answer: str = "", url: str = "") -> Dict[str, str]:
    """一条知识条目的版本/日期信息，直接对应 facts 表的三列。"""
    info = extract_date_info(title, answer, url=url)
    return {
        "version": extract_version(title, answer),
        "effective_from": info["date"],
        "date_kind": info["kind"],
    }


def slot_key(entity: str, title: str) -> str:
    """「同槽位」键：同一实体的同一字段（`「我们。」·描述` → `我们。|描述`）。

    槽位不同就不是同一条信息（「描述」vs「效果」），槽位相同而版本不同才是
    「同一件事的两个版本」，这时才该让新版本排在前面。`entity` 由调用方
    （`store.entity_of`）先归一化，避免两套归一化规则漂移。
    """
    attr = str(title or "")
    if "·" in attr:
        attr = attr.split("·")[-1]
    attr = _MARKS_RE.sub("", attr).strip()
    head = str(entity or "").strip()
    if not attr:
        return head
    return f"{head}|{attr}" if head else attr


def describe() -> Dict[str, Any]:
    """给文档/界面用的规则快照（改正则时同步这里）。"""
    return {
        "version_pattern": VERSION_RE.pattern,
        "full_date_pattern": _DATE_FULL_RE.pattern,
        "url_date_pattern": _URL_DATE_RE.pattern,
        "rules": [
            "只认带年份的完整日期，或站点 URL 里的 /YYYYMMDD/；「8月13日」不猜年份",
            "正文里的日期是 effective（生效于），URL 退回的是 published（发布于）——两者不能混",
            "同一段文字里多个完整日期取最早（公告的生效时间）",
            "不同日期超过 2 个的按时间线处理，不挑日期",
            "多个版本号取最高（页面通常覆盖到最新版本）",
        ],
    }
