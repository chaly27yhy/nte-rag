"""开场推荐问题：每次打开页面 / 每次更新完知识库，随机抽一批「知识库答得出来」的问题。

放在后端是因为候选池要从整张 facts 表里随机取（用户库里几百上千条），
前端只能拿到 50 条文档列表，随机性和覆盖面都差很多。
这里只做「把事实标题套成问句」，不调模型、不联网，因此可以离线单测。
"""

from __future__ import annotations

import random
import re
from typing import Any, Dict, List, Optional

# 站点维护/记号类标题，套成问句不成句，直接跳过；
# 新闻标题（「8月18日不停服更新」）也不适合当推荐问题——它本身就是一条结论，
# 用户点进去只会得到「是的，8月18日更新了」这种没有信息量的回答。
_SKIP_PATTERN = re.compile(
    r"(scrolltoc|bilibili|bwiki|编辑|模板|分类|沙盒|测试页面|样式|菜单|导航|页脚|侧边栏|"
    r"^\d+$|^\d+月\d+日|停服|维护|补偿|公告|前瞻|签到|网页活动)",
    re.I,
)
# 标题末尾的「（角色图鉴）」这类来源括注：问句里不需要
_PAREN_TAIL = re.compile(r"[（(][^（()）]{0,14}[）)]\s*$")

# 按标题形态选问句模板（顺序有意义：先匹配先命中）
_ASK_RULES = (
    # 标题本身就是问句（「X是什么」「X有哪些」）→ 原样加上问号，不要再套一层
    (re.compile(r"(是什么|有哪些|有谁|怎么样|能不能|好不好|吗|呢)\s*[？?]?$"), "{t}？"),
    (re.compile(r"(有谁|有哪些|一览|名单|列表|排行|排名|哪个|哪种|哪把|哪张|哪套)"), "{t}？"),
    (re.compile(r"(时间|日期|安排|截止|什么时候|多久|几号)"), "{t}是什么时候？"),
    (re.compile(
        r"(价格|售价|原价|折扣|多少钱|多少|数量|数值|攻击力|防御力|生命值|概率|保底|上限|倍率|消耗|收益)"
    ), "{t}是多少？"),
    (re.compile(r"(怎么|如何|怎样|能否|是否|为什么|什么用|干什么|在哪)"), "{t}？"),
)
_DEFAULT_ASK = "「{t}」是什么？"

# 知识库刚建好、条目很少时的兜底问题（它们同时也会进入随机池）
EVERGREEN: List[Dict[str, str]] = [
    {"ask": "异环的全平台公测是什么时候开启的？", "label": "公测时间"},
    {"ask": "游戏里有哪些可操作角色？", "label": "角色一览"},
    {"ask": "异环支持哪些游戏平台？", "label": "支持平台"},
    {"ask": "弧盘是做什么用的？", "label": "弧盘系统"},
    {"ask": "卡带系统怎么用？", "label": "卡带系统"},
    {"ask": "异象和异能者分别是什么？", "label": "核心设定"},
]

_VERSION_IN_TITLE = re.compile(r"(\d+)\.(\d+)")

# 快捷问题按钮上直接显示**完整**标题：按字数硬截会出现「异环游戏基本介…」这种半截词，
# 用户反馈就是「推荐问题后半部分显示不出来」。所以只接受长度合理的标题，
# 超长的（多半是「XX汇总/合辑」这类噪声标题）直接不参与推荐。
_CHIP_TITLE_MAX = 20


def _clean_title(title: str) -> str:
    text = (title or "").strip()
    for _ in range(2):
        stripped = _PAREN_TAIL.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    return text.rstrip("？?！!。.：:；; 　")


def question_for(title: str) -> str:
    """把一条事实的标题套成用户会想点的问句；套不出来返回空串。"""
    core = _clean_title(title)
    if len(core) < 4 or _SKIP_PATTERN.search(core):
        return ""
    for pattern, template in _ASK_RULES:
        if pattern.search(core):
            return template.format(t=core)
    return _DEFAULT_ASK.format(t=core)


def label_for(title: str, limit: int = _CHIP_TITLE_MAX) -> str:
    """快捷问题按钮上的短标签。

    原则上**不截断**（中文按字数截会把词切断，用户看到的「半截问题」就是这么来的）；
    只有在标题短于这个长度时才会走到截断分支，而超长标题在 build_starter_asks
    里已经被整条跳过，不会显示成半截词。
    """
    core = _clean_title(title)
    return core if len(core) <= limit else core[:limit] + "…"


def latest_version(documents: Any) -> str:
    """官方公告标题里出现过的**最大**版本号（如 '1.4'）。

    不能取最新那条：文档接口按更新时间倒序，旧版本的补丁公告可能比新版本的前瞻
    更新得更晚（实测 1.3 的公告排在 1.4 前瞻前面），取第一条会把版本停在 1.3。
    """
    best = None
    for doc in documents or []:
        title = (doc.get("title") or "") if isinstance(doc, dict) else str(doc)
        if "版本" not in title:
            continue
        hit = _VERSION_IN_TITLE.search(title)
        if not hit:
            continue
        value = (int(hit.group(1)), int(hit.group(2)))
        if best is None or value > best:
            best = value
    return ".".join(str(part) for part in best) if best else ""


def build_starter_asks(kb, count: int = 3, rng: Optional[random.Random] = None) -> List[Dict[str, str]]:
    """随机抽 count 个推荐问题（每次调用都不同，除非知识库只有这么几条）。"""
    picker = rng or random.Random()
    pool: List[Dict[str, str]] = []
    seen = set()

    def add(ask: str, label: str) -> None:
        if ask and ask not in seen:
            seen.add(ask)
            pool.append({"ask": ask, "label": label})

    try:
        version = latest_version(kb.list_documents(source_type="official", limit=50))
    except Exception:
        version = ""
    if version:
        add(f"异环 {version} 版本更新了什么内容？", f"{version} 版本")

    try:
        facts = kb.sample_facts(limit=80)
    except Exception:
        facts = []
    for fact in facts:
        title = fact.get("title", "") if isinstance(fact, dict) else str(fact)
        if len(_clean_title(title)) > _CHIP_TITLE_MAX:
            continue          # 长标题截断后会变成半截词，直接跳过
        add(question_for(title), label_for(title))

    for item in EVERGREEN:
        add(item["ask"], item["label"])

    picker.shuffle(pool)
    return pool[: max(1, int(count))]
