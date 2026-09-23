"""数据质量：模板垃圾过滤、信息密度、跨游戏污染检测。

职责是模板垃圾过滤、信息密度与「其它游戏专有名词」的判别（有回归断言钉住，见 `tools/quality_check.py`）。

设计原则
--------
- **只做规则能确定的事**，拿不准的一律放过：漏掉一点噪声可以接受，误杀正常内容不行；
- 每个判定都要给出**可读的原因**，写进更新日志，便于回溯与调参；
- 不依赖模型，零运行成本。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

# ----------------------------------------------------------------------
# 1. 模板化噪声（攻略站/资讯站的固定文案）
# ----------------------------------------------------------------------

# 逐行匹配：命中即整行丢弃
_LINE_NOISE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^\s*扫码(关注|下载|查看)",
        r"^\s*长按(识别)?二维码",
        r"^\s*扫描二维码",
        r"^\s*(点击|戳)(这里|下方|上方|查看|阅读原文)",
        r"^\s*关注(我们|公众号|官方)",
        r"^\s*(更多|相关)(精彩|内容|攻略|推荐)",
        r"^\s*(责任编辑|编辑|作者|来源|版权|转载请注明|声明)\s*[:：]",
        r"^\s*(本文|本站).{0,10}(转载|整理|发布)",
        r"^\s*免责声明",
        r"^\s*(上一[篇页]|下一[篇页]|返回列表|回到顶部)",
        r"^\s*(广告|赞助内容|推广)",
        r"^\s*下载\s*(九游|3DM|游民|TapTap|APP)",
        r"^\s*[\-—=*·.]{3,}\s*$",          # 分隔线
        r"^\s*\d+\s*/\s*\d+\s*$",          # 页码
        # BWIKI 全站公告横幅：几乎所有页面都有，且会掩盖「这页其实是空壳」的事实
        r"本WIKI编辑权限开放",
        r"欢迎收藏起来防止迷路",
        r"目前WIKI正在初步建设中",
        r"WIKI编辑权限暂时无人",
        r"WIKI留言板|WIKI交流群",
        r"本页面正在\s*施工中",
        r"点击此处\s*协助编辑",
        r"刷新当前页缓存",
    )
]

# 整页判定：出现次数达到阈值才判定为垃圾（避免误杀偶尔提及）
_PAGE_NOISE_HINTS = [
    (re.compile(r"二维码|扫码关注|扫码下载"), 3, "页面主要是二维码/扫码引导"),
    (re.compile(r"关注(公众号|官方微信)"), 3, "页面主要是公众号引导"),
    (re.compile(r"点击(下载|安装).{0,8}(APP|客户端|游戏)"), 3, "页面主要是下载引导"),
]

# 正文里几乎没有实义内容（视频页、图集页常见）
_LOW_VALUE_TITLE_HINTS = (
    "视频",
    "图集",
    "图片",
    "直播回放",
    "预告片",
    "高清壁纸",
)

# ----------------------------------------------------------------------
# 2. 跨游戏/跨作品污染检测
# ----------------------------------------------------------------------

# 其它作品的专有名词：出现这些说明内容很可能「串味」了
# （实测九游的异环攻略里出现了「绝区零」的角色，属于典型的 AI 生成污染）
FOREIGN_ENTITIES: Sequence[str] = (
    "绝区零",
    "原神",
    "崩坏3",
    "崩坏三",
    "星穹铁道",
    "崩坏：星穹铁道",
    "鸣潮",
    "明日方舟",
    "碧蓝航线",
    "公主连结",
    "塞尔达",
    "艾尔登法环",
    "黑神话",
    "幻塔",
    "光遇",
    "第五人格",
    "阴阳师",
    "Fate/Grand Order",
    "FGO",
    "妮姬",
    "尘白禁区",
    "白荆回廊",
    "无限暖暖",
    "洛克王国",
)

_PUNCT_RE = re.compile(r"[\s\u3000，。、；：！？…—－·（）()\[\]【】《》<>\"'`,.;:!?\-_/\\|~@#$%^&*+=]+")


@dataclass
class PageVerdict:
    """一个页面的质量判定结果。"""

    ok: bool = True
    reason: str = ""
    cleaned_text: str = ""
    removed_lines: int = 0
    foreign_entities: List[str] = field(default_factory=list)
    info_density: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "removed_lines": self.removed_lines,
            "foreign_entities": self.foreign_entities,
            "info_density": round(self.info_density, 3),
        }


def strip_boilerplate(text: str) -> Tuple[str, int]:
    """按行剔除模板噪声，返回 (清理后的文本, 删掉的行数)。"""
    if not text:
        return "", 0
    kept: List[str] = []
    removed = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if any(pattern.search(stripped) for pattern in _LINE_NOISE):
            removed += 1
            continue
        kept.append(line)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return cleaned, removed


def info_density(text: str) -> float:
    """信息密度：去掉空白与标点后，实义字符占比。"""
    if not text:
        return 0.0
    meaningful = len(_PUNCT_RE.sub("", text))
    return meaningful / max(1, len(text))


def find_foreign_entities(text: str, threshold: int = 4) -> List[str]:
    """找出文中提及的「其它作品」专有名词；命中种类达到阈值即判定为跨作品污染。"""
    if not text:
        return []
    hits = {name for name in FOREIGN_ENTITIES if name in text}
    if len(hits) >= threshold:
        return sorted(hits)
    return []


def validate_page(
    title: str,
    text: str,
    min_chars: int = 150,
    min_density: float = 0.35,
    foreign_threshold: int = 4,
    foreign_ratio: float = 0.02,
) -> PageVerdict:
    """页面级质量校验（入库前调用）。

    依次判断：长度 → 模板噪声 → 信息密度 → 跨作品污染。
    任何一项不过就给出原因，由调用方决定丢弃还是降权。
    """
    from . import tables as table_mod

    verdict = PageVerdict()
    cleaned, removed = strip_boilerplate(text)
    verdict.cleaned_text = cleaned
    verdict.removed_lines = removed
    verdict.info_density = info_density(cleaned)

    if len(cleaned) < min_chars:
        verdict.ok = False
        verdict.reason = f"清理后正文过短（{len(cleaned)} 字 < {min_chars}）"
        return verdict

    for pattern, count, reason in _PAGE_NOISE_HINTS:
        if len(pattern.findall(cleaned)) >= count:
            verdict.ok = False
            verdict.reason = reason
            return verdict

    # 表格页面天然「低信息密度」（每个单元格都带 | 与空格填充），
    # 用密度阈值去卡它必然误杀——角色图鉴、弧盘图鉴这类页面正是数值来源。
    # 因此只要页面以表格为主，就跳过密度判定。
    looks_tabular = table_mod.table_density(cleaned) >= 0.25
    if not looks_tabular and verdict.info_density < min_density:
        verdict.ok = False
        verdict.reason = f"信息密度过低（{verdict.info_density:.2f} < {min_density}），疑似导航或图集页"
        return verdict

    # 标题本身是视频/图集类，且正文与标题高度重复 → 没有可用信息
    normalized_title = re.sub(r"\s+", "", title or "")
    if normalized_title and any(hint in normalized_title for hint in _LOW_VALUE_TITLE_HINTS):
        body = re.sub(r"\s+", "", cleaned)
        if normalized_title and body.count(normalized_title) >= 3:
            verdict.ok = False
            verdict.reason = "疑似视频/图集页：正文几乎只是标题的重复"
            return verdict

    foreign = find_foreign_entities(cleaned, threshold=foreign_threshold)
    if foreign:
        # 只有当这些词在正文里占比也不低时才算污染，避免「顺带提一句」
        hits = sum(cleaned.count(name) for name in foreign)
        if hits / max(1, len(cleaned)) >= foreign_ratio:
            verdict.ok = False
            verdict.foreign_entities = foreign
            verdict.reason = f"疑似跨作品内容污染（出现 {'、'.join(foreign[:5])} 等其它作品专有名词）"
            return verdict

    return verdict


def is_boilerplate_line(line: str) -> bool:
    stripped = (line or "").strip()
    return bool(stripped) and any(pattern.search(stripped) for pattern in _LINE_NOISE)


# ----------------------------------------------------------------------
# 3. 来源黑名单（用户可配置 + 预置基线）
# ----------------------------------------------------------------------

# 预置基线黑名单：不用用户配置就拦掉的站点。只放「整站都是字词/字典正文」这类
# 与游戏资料无关、又长又密、能骗过所有启发式判定的站点——实测「异环 地图 区域 探索」
# 这个主题抓回来的是「单个汉字『异』」的字典页，还被当成有效证据存进了知识库。
# 刻意不封 baike.baidu.com：那里有正经的《异环》词条，百科本身是可能命中的来源；
# 只封它的汉语子域（hanyu.baidu.com）。
BASELINE_BLOCKED_DOMAINS: Sequence[str] = (
    "hanyuguoxue.com",     # 汉语国学
    "hgcha.com",           # 汉语查
    "zdic.net",            # 汉典
    "hanyu.baidu.com",     # 百度汉语
    "chazidian.com",       # 查字典
    "cidianwang.com",      # 词典网
    "guoxuedashi.net",     # 国学大师
)

# 视频页：抓到的「正文」只有标题与推荐列表，对知识库没有价值
_VIDEO_ONLY_HOSTS: Sequence[str] = (
    "b23.tv",          # B 站短链，必定指向视频
    "youtu.be",
    "v.douyin.com",
    "ixigua.com",
)
_VIDEO_PATH_HINTS: Sequence[Tuple[str, Sequence[str]]] = (
    ("bilibili.com", ("/video/",)),
    ("youtube.com", ("/watch", "/shorts")),
    ("douyin.com", ("/video/",)),
    ("v.qq.com", ("/x/",)),
    ("youku.com", ("/v_show",)),
    ("iqiyi.com", ("/v_",)),
    ("acfun.cn", ("/v/",)),
    ("twitch.tv", ("/videos",)),
    ("kuaishou.com", ("/short-video",)),
)


def _host_matches(host: str, domain: str) -> bool:
    domain = str(domain or "").strip().lower().lstrip(".")
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


def is_video_url(url: str) -> bool:
    """是否视频页（B 站/YouTube/抖音这类）：抓到的正文只是标题和推荐列表。"""
    host = host_of(url)
    if not host:
        return False
    for domain in _VIDEO_ONLY_HOSTS:
        if _host_matches(host, domain):
            return True
    try:
        from urllib.parse import urlparse

        path = (urlparse(url).path or "").lower()
        query = (urlparse(url).query or "").lower()
    except Exception:
        path, query = "", ""
    for domain, hints in _VIDEO_PATH_HINTS:
        if _host_matches(host, domain) and any(hint in path or hint in query for hint in hints):
            return True
    return False


def baseline_block_reason(url: str) -> str:
    """预置基线的拦截原因；没命中返回空字符串。"""
    host = host_of(url)
    if not host:
        return ""
    for domain in BASELINE_BLOCKED_DOMAINS:
        if _host_matches(host, domain):
            return f"该域名在预置黑名单里（{domain}：字词/字典正文，与游戏资料无关）"
    if is_video_url(url):
        return "视频页：抓到的正文只有标题与推荐列表，没有可用内容"
    return ""


def host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def normalize_domains(values: Sequence[str]) -> List[str]:
    """把用户填的域名列表规整一下：去空、去协议、去路径、转小写、去重保序。

    允许用户直接粘贴网址（https://www.9game.cn/yihuan/... ），
    这里自动提取出 9game.cn 这样的主域。
    """
    result: List[str] = []
    for raw in values or []:
        item = str(raw or "").strip().lower()
        if not item or item.startswith("#"):
            continue
        item = re.sub(r"^[a-z]+://", "", item)      # 去协议
        item = item.split("/", 1)[0]                 # 去路径
        item = item.split("?", 1)[0].split("#", 1)[0]
        item = item.split("@")[-1].split(":")[0]     # 去端口/认证
        if item.startswith("www."):
            item = item[4:]
        if item and item not in result:
            result.append(item)
    return result


def is_blocked(url: str, blocked_domains: Sequence[str], include_baseline: bool = True) -> bool:
    """判断 URL 是否命中黑名单。支持子域：填 9game.cn 可拦 a.9game.cn。

    ``include_baseline=True``（默认）时还会带上预置基线（字词/字典站、视频页）。
    传 False 只按用户配置判定，供「只想看用户自己填了什么」的场景使用。
    """
    if include_baseline and baseline_block_reason(url):
        return True
    host = host_of(url)
    if not host:
        return False
    for domain in blocked_domains or ():
        if _host_matches(host, domain):
            return True
    return False


# ----------------------------------------------------------------------
# 4. 主题相关性（入库闸门）
# ----------------------------------------------------------------------

_THEME_SPLIT_RE = re.compile(r"[\s\u3000,，、;；|/\\+]+")


def theme_terms(topics: Sequence[str], min_len: int = 2) -> Tuple[str, ...]:
    """从更新主题里取出「所有主题共有的词」，作为入库时的主题关键词。

    开箱的 5 个主题是「异环 角色 图鉴 技能」「异环 主线 剧情 章节」……，
    交集就是「异环」。取交集而不是并集，是为了不把「剧情」「技能」这类通用词
    当成主题词；交集为空（或没配主题）时返回空元组，调用方据此放行——
    漏拦一些噪声可以接受，误杀正常内容不行。
    """
    groups: List[set] = []
    for topic in topics or ():
        words = {w for w in _THEME_SPLIT_RE.split(str(topic or "")) if len(w) >= min_len}
        if words:
            groups.append(words)
    if not groups:
        return ()
    common = set.intersection(*groups)
    return tuple(sorted(common))


def theme_mismatch(title: str, text: str, terms: Sequence[str]) -> str:
    """标题或正文里一个主题词都没有时给出原因；命中则返回空字符串。"""
    usable = [str(term) for term in terms or () if str(term or "").strip()]
    if not usable:
        return ""
    haystack = f"{title or ''}\n{text or ''}"
    if any(term in haystack for term in usable):
        return ""
    return f"与主题无关（标题与正文都没有出现主题词：{'、'.join(usable)}）"
