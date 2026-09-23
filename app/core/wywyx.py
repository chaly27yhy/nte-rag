"""玩一玩游戏网（m.wywyx.com）角色图鉴适配器 —— 内置的第二个来源。

## 背景

`app/core/consistency.py` 的跨源投票要求「同一槽位有 ≥2 个独立域名给出同值」，
而内置资料库此前每个断言都只来自 BWIKI 一个域名（实测 593 条里
wiki.biligame.com 486 条、其余域名各管各的标题），
于是 `multi`/`conflict` **恒为 0**——投票逻辑等于从未被真实数据检验过。

## 站点实测结论（2026-09-22）

- 页面是服务端渲染的静态 HTML，数值直接写在正文里（无需浏览器渲染）；
- robots.txt 返回 404（站点未设抓取限制）；
- 正文里有固定结构的「初始面板」：生命值 / 攻击力 / 防御力（外加暴击率 5%、暴击伤害 50%，
  全站恒定，属于模板占位值，刻意不入库）；
- 每页顶部一行集中给出：`战斗定位：…  异能名：「…」  生日：…月…日`；
- **生日与 BWIKI 逐条比对 10/10 完全一致**（早雾 11月7日、哈索尔 8月29日、白藏 11月23日、
  娜娜莉 8月20日、法帝娅 10月31日、阿德勒 9月25日、埃德嘉 10月7日、哈尼娅 3月27日、
  海月 3月9日、翳 1月10日），这是它能作为第二个来源的依据；
- **「初始面板」与 BWIKI 的 `|生命=`/`|攻击=` 不是同一口径**（早雾 1360/78 vs BWIKI 1320/40；
  BWIKI 自己 1280～9279 跨了 7 倍）。因此这里把面板字段命名为
  「初始生命 / 初始攻击 / 初始防御」，与 BWIKI 的「生命 / 攻击」**分槽位存放**，
  只用口径无歧义的「生日」参与跨源投票——把不同口径硬对齐会制造假冲突
  （这一点在跨源投票上线初期实测过，11 + 19 组候选全是假阳性）。

## 字段与条目形态

输出与 BWIKI 结构化路径同构（`wiki_api.facts_from_pairs`）：
`标题 = "<角色>·<字段>"`、`答案 = "<角色> 的<字段>为：<值>"`、`extraction = api`。
因此两个来源的「生日」会落到同一槽位，能被 consistency 投票看见。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .fetch import extract_links

# 页面标题形如「异环埃德嘉角色图鉴」（列表页上的锚文本还带日期后缀）
NAME_RE = re.compile(r"异环(.{1,6}?)角色图鉴")
# 「战斗定位：生存 打击 异能名：「芬尼根守灵夜」 生日：10月7日」——三个字段挤在一行
ROLE_RE = re.compile(r"战斗定位[：:]\s*([^\n]{1,40}?)\s*(?=(?:异能名|生日)[：:]|$)")
ABILITY_QUOTED_RE = re.compile(r"异能名[：:]\s*[「『\"“]([^」』\"”\n]{1,20})[」』\"”]")
ABILITY_BARE_RE = re.compile(r"异能名[：:]\s*([^\s「『\"”（(]{1,20})")
BIRTHDAY_RE = re.compile(r"生日[：:]\s*([0-9]{1,2})\s*月\s*([0-9]{1,2})\s*日")
PANEL_START = "初始面板"
PANEL_END = "简介"
PANEL_FIELDS: Sequence[Tuple[str, str]] = (
    ("生命值", "初始生命"),
    ("攻击力", "初始攻击"),
    ("防御力", "初始防御"),
)
# 锚文本里带「异环…角色图鉴」的链接 = 同游戏的其他角色页，用来发现新页面
CHARACTER_LINK_RE = re.compile(r"异环.{1,6}角色图鉴")
DEFAULT_LINK_PATTERN = r"/wiki/\d+\.html"


def character_name(text: str, fallback: str = "") -> str:
    """从正文/标题里取角色名。取不到返回 fallback（通常是页面标题）。"""
    match = NAME_RE.search(str(text or ""))
    if match:
        return match.group(1).strip()
    title = str(fallback or "")
    match = NAME_RE.search(title)
    return match.group(1).strip() if match else ""


def panel_block(text: str) -> str:
    """截出「初始面板」到「简介」之间的片段。

    必须先切片再取数值：正文里 5 级觉醒写着「生命上限提高 20%」，
    全局搜索「生命值」会把这类描述混进来。

    **结束标记缺失时判失败（返回空串）**，不再把「初始面板」之后的整页正文
    当面板：站点改模板或删掉「简介」标题时，全页数字搜索会产出「初始生命=20」
    这种看着精确、带着 api 级可信度、还能在跨源投票里胜出的错数字。
    拿不到面板字段可以接受（dropped_reason 会给出原因），错数字不可以。
    """
    body = str(text or "")
    start = body.find(PANEL_START)
    if start < 0:
        return ""
    rest = body[start + len(PANEL_START):]
    end = rest.find(PANEL_END)
    if end < 0:
        return ""
    return rest[:end]


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def affiliation(text: str, name: str) -> str:
    """取「<角色> <所属>」那一行（角色页顶部第二行）。"""
    if not name:
        return ""
    pattern = re.compile(rf"^[ \t]*{re.escape(name)}[ \t]+(\S[^\n]{{0,20}})$", re.MULTILINE)
    match = pattern.search(str(text or ""))
    return _clean(match.group(1)) if match else ""


def parse_character(text: str, name: str = "") -> List[Tuple[str, str]]:
    """解析角色页正文 → [(字段, 值)]，顺序稳定（便于断言与 diff）。"""
    body = str(text or "")
    holder = name or character_name(body)
    pairs: List[Tuple[str, str]] = []

    birthday = BIRTHDAY_RE.search(body)
    if birthday:
        pairs.append(("生日", f"{int(birthday.group(1))}月{int(birthday.group(2))}日"))

    ability = ABILITY_QUOTED_RE.search(body) or ABILITY_BARE_RE.search(body)
    if ability:
        pairs.append(("异能", _clean(ability.group(1))))

    role = ROLE_RE.search(body)
    if role:
        pairs.append(("战斗定位", _clean(role.group(1))))

    block = panel_block(body)
    for label, field in PANEL_FIELDS:
        match = re.search(rf"{label}[^0-9]{{0,8}}([0-9]{{1,7}})", block)
        if match:
            pairs.append((field, match.group(1)))

    belonging = affiliation(body, holder)
    if belonging:
        pairs.append(("所属", belonging))

    return [(key, value) for key, value in pairs if value]


def character_links(html: str, url: str, pattern: str = DEFAULT_LINK_PATTERN) -> List[Dict[str, str]]:
    """从页面 HTML 里挑出「同游戏其他角色页」的链接（锚文本含『异环…角色图鉴』）。

    站点没有按游戏过滤的索引页（实测 https://m.wywyx.com/wiki/ 只列全站最新内容），
    所以发现路径是：已知角色页 → 页面底部的相关图鉴链接 → 新角色页。
    """
    found: List[Dict[str, str]] = []
    seen = set()
    for link in extract_links(html, url, pattern=pattern):
        label = _clean(link.get("title") or "")
        if not CHARACTER_LINK_RE.search(label):
            continue
        if link["url"] in seen:
            continue
        seen.add(link["url"])
        found.append({"url": link["url"], "title": label, "name": character_name(label)})
    return found


def merge_frontier(queue: List[str], links: Sequence[Dict[str, str]], seen: Sequence[str]) -> int:
    """把新发现的链接追加到待抓队列（去重、跳过已抓）。返回新增条数。"""
    known = set(seen)
    added = 0
    for link in links:
        target = str(link.get("url") or "")
        if not target or target in known:
            continue
        known.add(target)
        queue.append(target)
        added += 1
    return added


def describe() -> Dict[str, Any]:
    """给报告与断言用的口径快照。"""
    return {
        "site": "m.wywyx.com",
        "fields": [field for _label, field in PANEL_FIELDS],
        "extra_fields": ["生日", "异能", "战斗定位", "所属"],
        "cross_source_field": "生日",
        "note": "初始面板与 BWIKI 的 生命/攻击 口径不同，刻意分槽位存放",
    }


def summarize(pages: Sequence[Dict[str, Any]]) -> str:
    """一行摘要，打印在抓取日志里。"""
    names = [str(page.get("name") or "") for page in pages]
    fields = sum(int(page.get("fields") or 0) for page in pages)
    return f"玩一玩角色图鉴：{len(names)} 个角色、{fields} 个字段（{ '、'.join(names[:6]) }…）"


def dropped_reason(text: str, name: str, pairs: Sequence[Tuple[str, str]]) -> Optional[str]:
    """页面为什么不算有效角色页：供 on_drop 回调给用户一个具体原因。"""
    if not name:
        return "没有解析出角色名（可能不是角色图鉴页）"
    if not panel_block(text) and not pairs:
        if PANEL_START in str(text or ""):
            # 区分「模板改了」与「这根本不是角色页」：前者属于有意跳过面板字段
            return (f"有『{PANEL_START}』但找不到『{PANEL_END}』结束标记"
                    "（页面模板可能已改），为避免错数字本轮不取面板字段")
        return f"既没有『{PANEL_START}』也没有其他结构化字段"
    return None
