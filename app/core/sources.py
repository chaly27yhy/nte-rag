"""内置数据源目录与抓取连接器。

数据源结论来自实测（见 docs/data_sources_cn.md），关键约束：
- 只使用「服务端渲染（SSR）」或「JSON API」的源，本项目不带浏览器渲染；
- wikipedia.org / fandom.com 在本机网络不可达，灰机 wiki 主机级 403，一律不收录；
- BWIKI 走 MediaWiki API（其 robots 未禁 api.php），萌娘百科走条目页 HTML
  （其 robots 禁止 /*action=，所以刻意不用它的 API），两者合规路径不同；
- 九游攻略存在疑似 AI 生成的事实污染，**已整体移除**（见下方 DEFAULT_SOURCES
  附近的历史说明），本文件不再收录 9game 域名。

连接器类型（source.kind）：
- page        ：单页抓取 + 正文抽取
- list        ：列表页（可翻页）→ 枚举详情页 → 逐篇抓取
- mw_allpages ：MediaWiki API 枚举全站页面，再逐页取正文
- mw_api      ：MediaWiki API 取模板字段（分类成员 → 逐页 wikitext → 字段→值）
- wywyx       ：玩一玩游戏网角色图鉴（静态 HTML → 固定字段；唯一的第二个来源域名）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence
from urllib.parse import quote, urljoin

from . import secrets, wiki_api, wywyx
from .fetch import Fetcher, extract_content, extract_links

# ----------------------------------------------------------------------
# 来源分类：数据来源 / 叙事·世界观来源
# ----------------------------------------------------------------------
# 维护者 2026-09-22 拍板：官网「角色介绍」这类纯叙事文章单独立一类，
# 只用于世界观 / 剧情 / 角色背景问答与引用，**不参与任何数值/字段投票**。
#
# 为什么不靠「值不像数值」这条已有的启发式来挡：叙事文本里也会出现数字
# （「五大城区」「三条规则」），一旦被当成字段值参与投票，就会和真正的
# 数值字段挤进同一个槽位。所以这里用**来源分类**显式排除，而不是靠值形态猜。
SOURCE_CLASS_DATA = "data"
SOURCE_CLASS_NARRATIVE = "narrative"
NARRATIVE_LABEL = "叙事/世界观"
# 落进 tags 的标记：一致性投票按这个标记跳过，引用时按 NARRATIVE_LABEL 标注来源类型
NARRATIVE_TAG = f"{NARRATIVE_LABEL}（不参与字段投票）"
# 标题规则兜住未来新增的角色介绍类文章（官网目前没有可枚举的独立文章列表，
# 该板块实体就在 main.html 的「角色介绍」区，详见 docs/consistency_review.md）
NARRATIVE_TITLE_PATTERN = re.compile(
    r"角色介绍|角色档案|人物介绍|角色设定|人物设定|世界观|设定集|背景故事|人物故事|"
    r"剧情|前传|番外|访谈"
)


def is_narrative_title(title: str) -> bool:
    """标题像不像「纯叙事」内容（角色介绍 / 世界观 / 剧情）。"""
    text = (title or "").strip()
    if not text:
        return False
    return bool(NARRATIVE_TITLE_PATTERN.search(text))


def source_class_of(source: Any) -> str:
    """取数据源 / 页面条目的来源分类，缺省为数据来源。"""
    value = ""
    if isinstance(source, dict):
        value = str(source.get("source_class") or "")
    else:
        value = str(getattr(source, "source_class", "") or "")
    return value or SOURCE_CLASS_DATA


def is_narrative(source: Any) -> bool:
    return source_class_of(source) == SOURCE_CLASS_NARRATIVE


# ----------------------------------------------------------------------
# 内置数据源目录
# ----------------------------------------------------------------------

DEFAULT_SOURCES: List[Dict[str, Any]] = [
    {
        "id": "official_lore",
        "name": "官网·世界观与角色设定（叙事/世界观）",
        "kind": "page",
        "url": "https://yh.wanmei.com/main.html",
        "source_type": "official",
        "priority": 0,
        "enabled": True,
        # 2026-09-21 人工审核裁定：单独立「叙事/世界观来源」，不参与数值/字段投票
        "source_class": SOURCE_CLASS_NARRATIVE,
        "no_vote": True,
        "note": "官方角色设定长文与城区介绍（首页「角色介绍」板块），服务端渲染，实测 200；"
                "按 2026-09-21 人工审核裁定归入叙事/世界观来源，只作答世界观/剧情/角色背景，不参与字段投票",
    },
    {
        "id": "official_news",
        "name": "官网·新闻公告列表",
        "kind": "list",
        "url": "https://yh.wanmei.com/news/index.html",
        "source_type": "official",
        "priority": 1,
        "enabled": True,
        "link_pattern": r"/news/[a-z]+/\d{8}/\d+\.html",
        "page_pattern": "index{p}.html",
        "max_pages": 3,
        "max_items": 24,
        "note": "官网新闻 26 页分页，robots.txt 为空文件（无限制）",
    },
    {
        "id": "official_broad",
        "name": "官网·游戏公告",
        "kind": "list",
        "url": "https://yh.wanmei.com/news/gamebroad/index.html",
        "source_type": "official",
        "priority": 1,
        "enabled": True,
        "link_pattern": r"/news/gamebroad/\d{8}/\d+\.html",
        "page_pattern": "index{p}.html",
        "max_pages": 2,
        "max_items": 20,
        "note": "版本更新与维护公告，回答「最近更新了什么」的关键来源",
    },
    # 结构化 API 源：直接读 MediaWiki 的模板字段，而不是抓渲染后的 HTML。
    # 实测：
    #   · 弧盘条目页的 wikitext 里「描述」字段含 30.00% / 36 点 / 15 秒 / 商城 18 元礼包
    #     这类具体数值，而渲染后的弧盘图鉴表格只有「效果：详见描述」；
    #   · `action=ask` 的 SMW 属性大多不存在（`?最高攻击`/`?最高生命` 返回 45 条全空值），
    #     所以主路径是「分类成员 → 逐页 wikitext → 模板字段」，SMW 只当补充。
    # 请求量与 WAF：每个条目 1 次请求、串行且间隔 ≥5 秒（Fetcher 的 slow_hosts），
    # 并且 WikiApi 会落盘缓存，重跑直接读缓存、被 567 打断后下次能续上。
    {
        "id": "bwiki_api_arc",
        "name": "BWIKI·弧盘（结构化字段）",
        "kind": "mw_api",
        "url": "https://wiki.biligame.com/yh/api.php",
        "page_base": "https://wiki.biligame.com/yh/",
        "category": "弧盘",
        "templates": ["弧盘"],
        "source_type": "wiki",
        "priority": 0,
        "enabled": True,
        "max_items": 46,
        "note": "46 个弧盘条目的模板字段；数值最完整的一路（提取方式记为 api）",
    },
    {
        "id": "bwiki_api_character",
        "name": "BWIKI·角色（结构化字段）",
        "kind": "mw_api",
        "url": "https://wiki.biligame.com/yh/api.php",
        "page_base": "https://wiki.biligame.com/yh/",
        "category": "角色",
        "templates": ["角色图鉴"],
        "source_type": "wiki",
        "priority": 0,
        "enabled": True,
        "max_items": 24,
        "note": "角色模板字段：稀有度/战斗类型/异能属性/生日/CV/角色简介",
    },
    {
        "id": "bwiki_characters",
        "name": "BWIKI·角色图鉴",
        "kind": "page",
        "url": "https://wiki.biligame.com/yh/角色图鉴",
        "source_type": "wiki",
        "priority": 1,
        "enabled": True,
        "container": ".mw-parser-output",
        "note": "角色基础数据表（名称/稀有度/属性/类型）",
    },
    # 下面这些是「图鉴/数值」类页面，是数值型问答（攻击力、概率、稀有度）的主要来源。
    # 实测：只抓角色图鉴一个页面时，知识库里完全没有弧盘/装备的数值，
    # 导致「噬心诡刃满级攻击力」这类问题答不出来。
    {
        "id": "bwiki_arc",
        "name": "BWIKI·弧盘图鉴",
        "kind": "page",
        "url": "https://wiki.biligame.com/yh/弧盘图鉴",
        "source_type": "wiki",
        "priority": 1,
        "enabled": True,
        "container": ".mw-parser-output",
        "note": "弧盘（武器）数值表——弧盘攻击力/副属性类问题的关键来源",
    },
    {
        "id": "bwiki_cartridge",
        "name": "BWIKI·卡带图鉴",
        "kind": "page",
        "url": "https://wiki.biligame.com/yh/卡带图鉴",
        "source_type": "wiki",
        "priority": 2,
        "enabled": True,
        "container": ".mw-parser-output",
        "note": "卡带数值表",
    },
    {
        "id": "bwiki_items",
        "name": "BWIKI·道具图鉴",
        "kind": "page",
        "url": "https://wiki.biligame.com/yh/道具图鉴",
        "source_type": "wiki",
        "priority": 2,
        "enabled": True,
        "container": ".mw-parser-output",
        "note": "道具清单与用途",
    },
    # 已删除 4 个「抓不到有效内容」的 BWIKI 源（不再以停用状态留在目录里，
    # 免得更新页给用户一排永远抓不出东西的灰色开关）：
    #   · 装备图鉴   → 301 到弧盘图鉴，正文逐字相同（5856 字 / 44 行弧盘表），留着只会重复入库
    #   · 异象图鉴   → 静态页面只有 258 字站点横幅，正文由 JS 动态加载
    #   · 成就列表   → 同上，只有站点横幅
    #   · 角色/属性  → 实为「模板:角色/属性」，数字（20/40/50/60/70/80）没有行标签，
    #                  6 种抽取策略都还原不出「哪一行属于哪个角色」
    # 抓取失败与冷却的可见性由 fetch.Fetcher 负责（见 tools/quality_check.py 第【6】节）。
    # 全站枚举放在图鉴之后：实测 BWIKI 抓 ~190 次后开始返回 567（WAF），
    # 万一再被 WAF 拦下，先入库的必须是「弧盘/装备数值」这类高价值页面。
    {
        "id": "bwiki_allpages",
        "name": "BWIKI·异环全站页面",
        "kind": "mw_allpages",
        "url": "https://wiki.biligame.com/yh/api.php",
        "source_type": "wiki",
        "priority": 1,
        "enabled": True,
        "max_items": 40,
        "container": ".mw-parser-output",
        "note": "MediaWiki API 枚举；robots 未禁 api.php；注意其未安装 prop=extracts，故用 action=parse",
    },
    {
        "id": "moegirl_yihuan",
        "name": "萌娘百科·异环",
        "kind": "page",
        "url": "https://zh.moegirl.org.cn/异环",
        "source_type": "wiki",
        "priority": 1,
        "enabled": True,
        "container": ".mw-parser-output",
        "note": "走条目页而非 API：其 robots 禁止 /*action=，条目路径未禁",
    },
    {
        "id": "moegirl_glossary",
        "name": "萌娘百科·异环名词词典",
        "kind": "page",
        "url": "https://zh.moegirl.org.cn/异环/名词词典",
        "source_type": "wiki",
        "priority": 2,
        "enabled": True,
        "container": ".mw-parser-output",
        "note": "异象/异能者/维特海默值等术语定义，实体对齐的黄金数据",
    },
    {
        "id": "gamersky_handbook",
        "name": "游民星空·异环攻略手册",
        "kind": "list",
        "url": "https://www.gamersky.com/z/neverness-to-everness/handbook/",
        "source_type": "community",
        "priority": 3,
        "enabled": True,
        # 实测：详情页真实形态是 /handbook/<年月>/<id>.shtml（本页约 155 条），
        # 旧规则 /z/neverness-to-everness/<id>.shtml 匹配不到任何链接。
        "link_pattern": r"/handbook/\d{6}/\d+\.shtml",
        "max_pages": 1,
        "max_items": 15,
        "note": "攻略正文数千字，质量稳定",
    },
    {
        "id": "3dm_guides",
        "name": "3DM·异环攻略合集",
        "kind": "list",
        "url": "https://shouyou.3dmgame.com/zt/203713_gl_all_5/",
        "source_type": "community",
        "priority": 3,
        "enabled": True,
        "link_pattern": r"shouyou\.3dmgame\.com/gl/\d+\.html",
        "max_pages": 1,
        "max_items": 12,
        "note": "列表页内嵌全文，正文可直接抽取",
    },
    # 已移除「九游·异环攻略」：
    # 实测其内容存在疑似 AI 生成的事实污染（例如把《异环》与《绝区零》的角色混为一谈），
    # 这类来源会直接污染知识库且难以自动识别，因此不再收录。
    # 少一个来源的代价小于错误信息进入知识库的代价。
    # 第二个来源：跨源投票需要「同一槽位有第二个独立域名」，否则
    # consistency 的 multi/conflict 恒为 0（实测 593 条里 486 条同属 wiki.biligame.com），
    # 投票逻辑等于从未被真实数据验证过。选它的依据见 app/core/wywyx.py 开头。
    {
        "id": "wywyx_characters",
        "name": "玩一玩·角色图鉴（第二来源）",
        "kind": "wywyx",
        "url": "https://m.wywyx.com/wiki/578640.html",
        "seed_urls": [
            "https://m.wywyx.com/wiki/578621.html",  # 零
            "https://m.wywyx.com/wiki/578622.html",  # 早雾
            "https://m.wywyx.com/wiki/578624.html",  # 哈索尔
            "https://m.wywyx.com/wiki/578633.html",  # 白藏
            "https://m.wywyx.com/wiki/578637.html",  # 娜娜莉
            "https://m.wywyx.com/wiki/578638.html",  # 法帝娅
            "https://m.wywyx.com/wiki/578639.html",  # 阿德勒
            "https://m.wywyx.com/wiki/578640.html",  # 埃德嘉
            "https://m.wywyx.com/wiki/578641.html",  # 哈尼娅
            "https://m.wywyx.com/wiki/578642.html",  # 海月
            "https://m.wywyx.com/wiki/578643.html",  # 薄荷
            "https://m.wywyx.com/wiki/578644.html",  # 翳
        ],
        "link_pattern": r"/wiki/\d+\.html",
        "source_type": "wiki",
        "priority": 0,
        "enabled": True,
        "max_items": 40,
        "note": "robots.txt 404（未设限制）；生日与 BWIKI 逐条一致 10/10，"
                "初始面板与 BWIKI 的 生命/攻击 口径不同故分槽位存放（只让生日参与跨源投票）",
    },
]

# BWIKI 施工期标记（2026-09-21 人工审核裁定）
# 原话要点：BWIKI 目前处于施工状态、编辑权限没有 24 小时审核，但**不整站降权**
# （会误伤已稳定的字段）；处理方式是全站标记「施工中/可靠性待确认」，生命/攻击等
# 数值字段一旦冲突一律做**字段级存疑**、不参与投票、不跨源比对，施工结束后抽样复核
# 再决定是否调权重。所以这里只给源打标记，权重仍是 trust.SOURCE_SCORE["wiki"] = 0.75。
CONSTRUCTION_DOMAINS = ("wiki.biligame.com",)
CONSTRUCTION_NOTE = (
    "施工中/可靠性待确认（2026-09-21 人工审核裁定：不整站降权，数值字段冲突做字段级存疑、"
    "不参与投票、不跨源比对，施工结束后抽样复核）"
)


def _mark_under_construction() -> None:
    for item in DEFAULT_SOURCES:
        url = str(item.get("url") or item.get("page_base") or "")
        if not any(domain in url for domain in CONSTRUCTION_DOMAINS):
            continue
        item["under_construction"] = True
        note = str(item.get("note") or "")
        if CONSTRUCTION_NOTE not in note:
            item["note"] = f"{note}；{CONSTRUCTION_NOTE}" if note else CONSTRUCTION_NOTE


_mark_under_construction()

SOURCE_BY_ID = {item["id"]: item for item in DEFAULT_SOURCES}

# Wiki 的维护/元页面：枚举全站时会混进来（沙盒、样式调整器、模板、分类……），
# 它们没有游戏内容价值，入库只会污染检索结果。
# 刻意使用「整名匹配」而不是子串匹配——「共存测试」这类真实条目不能被误杀。
_WIKI_META_PATTERN = re.compile(
    # san…box 覆盖 Sandbox / Sanbox（BWIKI 实际存在的拼写错误）/ Sandbox:主页参考 等变体
    r"^(san\w{0,3}box.*|沙盒.*|测试页面.*|test\s*page.*|"
    # 「创建弧盘」这类新建条目的辅助页在分类里与真实条目混在一起（实测存在）
    r"创建.*|编辑.*|"
    r"widget|gadget|common\.css|common\.js|"
    r"wiki样式.*|样式调整器|"
    r"mediawiki(\s*汇总页面)?.*|汇总页面|"
    r"(模板|template|分类|category|帮助|help|用户|user|talk|讨论|widget|模块|module|"
    r"特殊|special|文件|file)\s*:.*|"
    r"首页|main\s*page)$",
    re.IGNORECASE,
)


def is_wiki_meta_title(title: str) -> bool:
    """判断是否为 wiki 维护页面（应当跳过，不入知识库）。"""
    clean = (title or "").strip()
    if not clean:
        return True
    return bool(_WIKI_META_PATTERN.match(clean))


@dataclass
class PageItem:
    """一个待入库的页面。"""

    url: str
    title: str = ""
    text: str = ""
    source_id: str = ""
    source_type: str = "community"
    published: str = ""
    quality: str = "normal"
    meta: Dict[str, Any] = field(default_factory=dict)
    # 来源分类（data / narrative）与「不参与字段投票」标记：由 iter_source_pages
    # 统一标注，叙事类内容不进入一致性投票
    source_class: str = SOURCE_CLASS_DATA
    no_vote: bool = False


def get_sources(include_disabled: bool = False) -> List[Dict[str, Any]]:
    return [dict(item) for item in DEFAULT_SOURCES if include_disabled or item.get("enabled", True)]


def get_source(source_id: str) -> Optional[Dict[str, Any]]:
    item = SOURCE_BY_ID.get(source_id)
    return dict(item) if item else None


# ----------------------------------------------------------------------
# 连接器
# ----------------------------------------------------------------------

# allpages 枚举的分页上限（L2）；aplimit=50，40 跳 = 最多扫 2000 个标题，
# 足够覆盖正常 wiki，又能保证「服务端一直吐 continue」不会变成死循环。
_ALLPAGES_MAX_HOPS = 40


def _enough_for_constant_check(parsed: Mapping[str, Any], wanted: Sequence[str]) -> bool:
    """判断这批样本是否足以做「同分类取值恒定 = 模板占位值」推断（L1）。

    被 WAF 截断的批次可能正好剩 4 页（= CONSTANT_MIN_PAGES），而这时真正逐实体的
    字段（那 4 页都是「稀有度=S」）会被当成占位值整批剥掉 —— 这一批报成功却静默缺
    数据。所以要求解析成功页数覆盖本批 wanted 的至少八成。
    """
    needed = max(wiki_api.CONSTANT_MIN_PAGES, (len(wanted) * 4 + 4) // 5)
    return len(parsed) >= needed


def _report_drop(on_drop: Optional[Any], reason: str) -> None:
    """把「这一页没抓到」明确报出去。

    以前这里是 `return None`——于是 BWIKI 返回 HTTP 567 时，8 个页面
    静默消失，更新日志只写「失败 0，跳过 5」，用户完全看不出发生过什么。
    """
    if not on_drop:
        return
    try:
        on_drop(reason)
    except Exception:
        pass


def _annotate_page(source: Dict[str, Any], item: PageItem) -> PageItem:
    """给页面打上来源分类。

    两条规则：① 数据源自己声明 narrative（官网「角色介绍」板块）→ 整源叙事；
    ② 标题命中 `NARRATIVE_TITLE_PATTERN`（「【角色介绍】某某」这类文章）→ 单篇叙事。
    第二种是给未来留的口子：官网目前没有可枚举的独立文章列表，但栏目一变，
    这里就能自动把新文章挡在字段投票之外，不需要再改代码。
    """
    klass = source_class_of(source)
    if klass == SOURCE_CLASS_DATA and is_narrative_title(item.title):
        klass = SOURCE_CLASS_NARRATIVE
    item.source_class = klass
    item.no_vote = bool(source.get("no_vote")) or klass == SOURCE_CLASS_NARRATIVE
    item.meta.setdefault("source_class", klass)
    if item.no_vote:
        item.meta.setdefault("no_vote", True)
    return item


def iter_source_pages(
    source: Dict[str, Any],
    fetcher: Fetcher,
    on_progress: Optional[Any] = None,
    limit: Optional[int] = None,
    on_drop: Optional[Any] = None,
) -> Iterator[PageItem]:
    """按 source.kind 产出可入库的页面。"""

    def progress(message: str) -> None:
        if on_progress:
            try:
                on_progress(message)
            except Exception:
                pass

    def pages(kind: str, max_items: int) -> Iterator[PageItem]:
        if kind == "page":
            item = _fetch_page(source, source["url"], fetcher, on_drop=on_drop)
            if item:
                yield item
            return

        if kind == "list":
            detail_links = _enumerate_list(source, fetcher, progress)
            progress(f"{source['name']}：发现 {len(detail_links)} 个页面")
            for index, link in enumerate(detail_links[:max_items]):
                progress(f"{source['name']}：抓取第 {index + 1}/{min(len(detail_links), max_items)} 篇")
                item = _fetch_page(
                    source, link["url"], fetcher, fallback_title=link.get("title", ""), on_drop=on_drop
                )
                if item:
                    yield item
            return

        if kind == "mw_allpages":
            titles = _mw_allpages(source, fetcher, max_items)
            progress(f"{source['name']}：枚举到 {len(titles)} 个条目")
            if not titles:
                _report_drop(on_drop, f"{source['name']}：条目列表为空（枚举请求可能被站点拦截）")
            for index, title in enumerate(titles):
                progress(f"{source['name']}：抓取第 {index + 1}/{len(titles)} 条")
                item = _mw_parse_page(source, title, fetcher, on_drop=on_drop)
                if item:
                    yield item
            return

        if kind == "mw_api":
            yield from _mw_api_pages(source, fetcher, max_items, progress, on_drop)
            return

        if kind == "wywyx":
            yield from _wywyx_pages(source, fetcher, max_items, progress, on_drop)
            return

        progress(f"未知的数据源类型：{kind}")

    kind = (source.get("kind") or "page").lower()
    max_items = int(limit or source.get("max_items") or 50)
    for page in pages(kind, max_items):
        yield _annotate_page(source, page)


def _mw_api_pages(
    source: Dict[str, Any],
    fetcher: Fetcher,
    max_items: int,
    progress: Any,
    on_drop: Optional[Any] = None,
) -> Iterator[PageItem]:
    """结构化 API 路径：分类成员 → 逐页 wikitext → 模板字段 → 原子条目。

    与 `mw_allpages` 的区别：那条路抓渲染后的正文再交给模型摘写，
    这条路直接读「字段名=值」，数值不会被概括、也不会被模型漏掉。
    """
    api = wiki_api.WikiApi(fetcher, source["url"], cache_dir=wiki_api.default_cache_dir())
    category = str(source.get("category") or "")
    titles = [title for title in api.category_members(category, limit=500) if title]
    usable = [title for title in titles if not is_wiki_meta_title(title)]
    progress(f"{source['name']}：分类「{category}」下 {len(usable)} 个条目")
    if not usable:
        # 「枚举为空」有三种完全不同的原因，必须分开说，
        # 否则站点限流与「分类名写错了」在日志里长得一模一样（实测踩过）。
        if api.last_error:
            reason = f"接口报错：{api.last_error}"
        elif titles:
            reason = f"分类下 {len(titles)} 个条目全被判定为维护页"
        else:
            reason = "接口返回正常但该分类下 0 个条目（分类名可能已改）"
        _report_drop(on_drop, f"{source['name']}：分类「{category}」没有可用条目（{reason}）")
        return
    page_base = str(source.get("page_base") or source["url"].rsplit("/api.php", 1)[0] + "/")
    templates = source.get("templates") or []
    wanted = usable[:max_items]
    # 批量取原文：BWIKI 的 WAF 每个冷却窗口只放行几次请求，
    # 逐页 action=parse 会让 46 页条目在第 8 次请求就被 567 拦下（实测）。
    # 一次请求 20 页，46 页 = 3 次请求；已缓存过的页面连请求都不用发。
    texts = api.wikitext_many(wanted)
    failed = len(api.batch_failures)
    progress(
        f"{source['name']}：批量取回 {len(texts)}/{len(wanted)} 页原文"
        + (f"（{failed} 页因请求失败未取到，失败不写缓存，下次更新会自动重试）" if failed else "")
    )
    if not texts:
        _report_drop(
            on_drop,
            f"{source['name']}：批量取原文全部失败（{api.last_error or '原因未知'}）",
        )
        return
    # 先一次性解析所有页面，再判定「同分类里取值恒定」的字段（模板默认值）。
    # 实测 角色图鉴 的 最高生命/最高攻击/物理防御/法术防御 全部页面同值，
    # 它们是模板占位值：入库会变成「某某的最高攻击=8424」这种看似精确的错答案。
    parsed: Dict[str, list] = {}
    for title in wanted:
        raw = texts.get(title, "")
        if not raw:
            continue
        parsed[title] = wiki_api.template_fields(
            raw, templates=templates, skip_fields=source.get("skip_fields")
        )
    constants = wiki_api.constant_fields(parsed) if _enough_for_constant_check(parsed, wanted) else {}
    if constants:
        names = "、".join(f"{key}({value[:12]})" for key, value in list(constants.items())[:6])
        progress(
            f"{source['name']}：{len(constants)} 个字段在同分类里取值恒定，按模板默认值剔除（{names}）"
        )
    for index, title in enumerate(wanted):
        progress(f"{source['name']}：解析第 {index + 1}/{len(wanted)} 条")
        raw = texts.get(title, "")
        if not raw:
            # 「整批请求失败」和「这一页确实不存在/为空」必须分开说：
            # 前者重跑就能补上（失败不写缓存），后者永远取不到；
            # 混在一起会把站点限流误报成「页面可能不存在」。
            failure = api.batch_failures.get(title, "")
            if failure:
                _report_drop(on_drop, f"{title}：批量接口请求失败（{failure}）")
            else:
                _report_drop(on_drop, f"{title}：批量接口未返回该页原文（可能不存在或为空页）")
            continue
        tautologies = 0
        pairs = []
        for key, value in parsed.get(title, []):
            if key in constants:
                continue
            if wiki_api.is_tautological(value, title):
                # 「X 的弧盘名是 X」这类条目零信息量，还会挤占检索名额。
                tautologies += 1
                continue
            pairs.append((key, value))
        if tautologies:
            progress(f"{source['name']}：{title} 剔除 {tautologies} 个「值等于标题」的废话字段")
        if len(pairs) < 2:
            if parsed.get(title):
                _report_drop(
                    on_drop,
                    f"{title}：去掉模板默认值字段后剩余信息过少（原 {len(parsed[title])} 个字段）",
                )
            else:
                _report_drop(on_drop, f"{title}：未解析到模板字段（模板名可能变了）")
            continue
        text = wiki_api.build_page_text(title, pairs, source.get("name", ""))
        # 阈值刻意低（40 字）：字段少的小条目也值得入库成原子条目，
        # 正文这一层由 store_page 的 150 字门槛去决定要不要切块。
        if len(text) < 40:
            _report_drop(on_drop, f"{title}：结构化字段过少（{len(text)} 字），已跳过")
            continue
        yield PageItem(
            url=page_base + quote(title),
            title=title,
            text=text,
            source_id=source.get("id", ""),
            source_type=source.get("source_type", "wiki"),
            published="",
            quality=source.get("quality", "normal"),
            meta={
                "source_name": source.get("name", ""),
                "api_facts": wiki_api.facts_from_pairs(
                    title, pairs, source_name=source.get("name", "")
                ),
                "api_fields": len(pairs),
            },
        )


def _wywyx_pages(
    source: Dict[str, Any],
    fetcher: Fetcher,
    max_items: int,
    progress: Any,
    on_drop: Optional[Any] = None,
) -> Iterator[PageItem]:
    """第二来源路径：玩一玩角色图鉴（静态 HTML → 固定字段 → 原子条目）。

    与 `mw_api` 的两点不同：
    - 发现靠链接：站点没有按游戏过滤的索引页（实测全是全站最新内容），
      所以从已知角色页出发，顺页面里的「异环…角色图鉴」链接走到新角色页；
    - 每页只发一次请求：同一份 HTML 既抽正文（`extract_content`）又抽链接，
      不额外为发现页面再抓一遍。
    """
    seeds = [str(url) for url in (source.get("seed_urls") or []) if url]
    entry = str(source.get("url") or "")
    if entry and entry not in seeds:
        seeds.insert(0, entry)
    if not seeds:
        _report_drop(on_drop, f"{source.get('name', '')}：没有配置任何入口页（seed_urls 为空）")
        return
    pattern = str(source.get("link_pattern") or wywyx.DEFAULT_LINK_PATTERN)
    containers = (source["container"],) if source.get("container") else ()
    queue: List[str] = list(seeds)
    seen: set = set()
    emitted = 0
    summary: List[Dict[str, Any]] = []
    while queue and emitted < max_items:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            html = fetcher.get_html(url)
        except Exception as error:  # noqa: BLE001
            # 抓取失败必须留痕：站点改版/风控与「页面真的不存在」在处理上完全不同，
            # 静默跳过会让种子文件悄悄少掉几个角色而没人发现（实测踩过 8 页静默丢失）。
            _report_drop(on_drop, f"{url}：抓取失败（{secrets.scrub(str(error))[:90]}）")
            continue
        text, title, published = extract_content(html, url, containers)
        wywyx.merge_frontier(queue, wywyx.character_links(html, url, pattern), seen)
        name = wywyx.character_name(text, fallback=title)
        pairs = wywyx.parse_character(text, name=name)
        reason = wywyx.dropped_reason(text, name, pairs)
        if reason:
            _report_drop(on_drop, f"{url}：{reason}")
            continue
        if len(pairs) < 2:
            _report_drop(on_drop, f"{url}：结构化字段过少（{len(pairs)} 个），已跳过")
            continue
        emitted += 1
        summary.append({"name": name, "fields": len(pairs)})
        progress(f"{source['name']}：第 {emitted} 个角色 {name}（{len(pairs)} 个字段）")
        yield PageItem(
            url=url,
            title=name,
            text=wiki_api.build_page_text(name, pairs, source.get("name", "")),
            source_id=source.get("id", ""),
            source_type=source.get("source_type", "wiki"),
            published=published,
            quality=source.get("quality", "normal"),
            meta={
                "source_name": source.get("name", ""),
                "api_facts": wiki_api.facts_from_pairs(
                    name, pairs, url=url, source_name=source.get("name", "")
                ),
                "api_fields": len(pairs),
            },
        )
    if summary:
        progress(wywyx.summarize(summary))


def _fetch_page(
    source: Dict[str, Any],
    url: str,
    fetcher: Fetcher,
    fallback_title: str = "",
    on_drop: Optional[Any] = None,
) -> Optional[PageItem]:
    # 站点适配的关键：把数据源声明的正文容器传下去，
    # 否则中文 wiki 的左侧上百条导航会被当成正文入库。
    containers = (source["container"],) if source.get("container") else ()
    result = fetcher.fetch(url, container_selectors=containers)
    if not result.ok:
        reason = result.error or (f"HTTP {result.status}" if result.status else "没有响应")
        _report_drop(on_drop, f"{url}：{reason}")
        return None
    if len(result.text) < 120:
        _report_drop(on_drop, f"{url}：正文过短（{len(result.text)} 字），已跳过")
        return None
    return PageItem(
        url=result.final_url or url,
        title=result.title or fallback_title,
        text=result.text,
        source_id=source.get("id", ""),
        source_type=source.get("source_type", "community"),
        published=result.published,
        quality=source.get("quality", "normal"),
        meta={"source_name": source.get("name", ""), "http_status": result.status},
    )


def _enumerate_list(source: Dict[str, Any], fetcher: Fetcher, progress: Any) -> List[Dict[str, str]]:
    """列表页（含翻页）→ 详情页链接，按 URL 去重。"""
    base_url = source["url"]
    pattern = source.get("link_pattern", "")
    max_pages = int(source.get("max_pages", 1))
    page_pattern = source.get("page_pattern", "")
    collected: Dict[str, Dict[str, str]] = {}

    for page_index in range(1, max_pages + 1):
        if page_index == 1 or not page_pattern:
            url = base_url
        else:
            directory, _, filename = base_url.rpartition("/")
            candidate = page_pattern.replace("{p}", str(page_index)).replace("{page}", str(page_index))
            if candidate.endswith(".html") and filename.endswith(".html"):
                url = f"{directory}/{candidate}"
            else:
                url = urljoin(base_url if base_url.endswith("/") else base_url + "/", candidate)
        try:
            html = fetcher.get_html(url)
        except Exception as error:  # noqa: BLE001
            progress(f"{source['name']}：列表页失败 {secrets.scrub(str(error))[:80]}")
            continue
        links = extract_links(html, url, pattern=pattern)
        for link in links:
            collected.setdefault(link["url"], link)
        if not links:
            break
    return list(collected.values())


def _mw_allpages(source: Dict[str, Any], fetcher: Fetcher, max_items: int) -> List[str]:
    """用 MediaWiki API 枚举条目名（分页拉取）。

    失败时抛出带原因的错误，交给上层写进更新日志——不静默返回空列表，
    否则用户会以为「这个 wiki 没有内容」。
    """
    api = source["url"]
    titles: List[str] = []
    continuation: Dict[str, Any] = {}
    # 迭代上限（L2）：服务端若持续返回 continue、而返回的标题全被维护页过滤掉，
    # `len(titles) < max_items` 就永远成立 —— 每个单次请求都成功，没有任何超时会
    # 触发，于是同步死循环，卡死整轮更新并持续烧 WAF 预算。
    for _hop in range(_ALLPAGES_MAX_HOPS):
        params = {
            "action": "query",
            "list": "allpages",
            "aplimit": "50",
            "apnamespace": "0",
            "format": "json",
            "formatversion": "2",
        }
        params.update(continuation)
        try:
            payload = fetcher.get_json(api, params=params)
        except Exception as error:  # noqa: BLE001
            if titles:
                break  # 已经拿到一部分，保留成果
            raise RuntimeError(f"枚举 {source.get('name', api)} 失败：{secrets.scrub(str(error))}") from error
        before = len(titles)
        for entry in ((payload or {}).get("query") or {}).get("allpages") or []:
            title = entry.get("title")
            if not title:
                continue
            if is_wiki_meta_title(title):
                continue  # 跳过维护页（沙盒、模板、分类等）
            titles.append(title)
            if len(titles) >= max_items:
                break
        if len(titles) >= max_items:
            break
        following = payload.get("continue") if isinstance(payload, dict) else None
        if not following or len(titles) == before or following == continuation:
            # 没有后续分页 / 这一页一条可用标题都没新增 / continue 游标原地踏步：
            # 三种都不该再请求下去。
            break
        continuation = following
    return titles


def _mw_parse_page(
    source: Dict[str, Any],
    title: str,
    fetcher: Fetcher,
    on_drop: Optional[Any] = None,
) -> Optional[PageItem]:
    """用 action=parse 取某个条目的渲染 HTML，再抽正文。

    刻意不用 prop=extracts —— BWIKI 未安装该扩展。
    """
    api = source["url"]
    try:
        payload = fetcher.get_json(
            api,
            params={
                "action": "parse",
                "page": title,
                "prop": "text",
                "format": "json",
                "formatversion": "2",
                "redirects": "1",
            },
        )
    except Exception as error:
        _report_drop(on_drop, f"{source.get('name', '')}/{title}：{secrets.scrub(str(error))[:120]}")
        return None
    html = ((payload or {}).get("parse") or {}).get("text")
    if not html:
        _report_drop(on_drop, f"{source.get('name', '')}/{title}：接口没有返回正文")
        return None
    containers = [source["container"]] if source.get("container") else []
    text, parsed_title, published = extract_content(html, api, container_selectors=containers)
    if len(text) < 120:
        _report_drop(
            on_drop,
            f"{source.get('name', '')}/{title}：抽到的正文过短（{len(text)} 字），容器选择器可能失效",
        )
        return None
    # 第二道闸：渲染后的标题可能和枚举到的标题不一样（实测枚举「Feeling」
    # 渲染出来是「Wiki样式调整器」），所以要用真实标题再判一次。
    # 必须用 `parsed_title or title`：抽取不出标题时 parsed_title 是空串，
    # 而空串会被 is_wiki_meta_title 判为「维护页」——那会把正经内容页误杀
    # （实测「《异环》全平台公测现已开启！」「「倾世之雨」」就是这样被丢掉的）。
    effective_title = parsed_title or title
    if is_wiki_meta_title(effective_title):
        _report_drop(on_drop, f"{source.get('name', '')}/{title}：维护页（{effective_title}），已跳过")
        return None
    page_url = f"{api.rsplit('/api.php', 1)[0]}/{quote(title.replace(' ', '_'))}"
    return PageItem(
        url=page_url,
        title=parsed_title or title,
        text=text,
        source_id=source.get("id", ""),
        source_type=source.get("source_type", "wiki"),
        published=published,
        quality=source.get("quality", "normal"),
        meta={"source_name": source.get("name", ""), "wiki_title": title},
    )

