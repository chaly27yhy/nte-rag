"""可信度派生分：把「这条信息有多可信」从模型自评改成可解释的四因子加权。

为什么要有这个模块
------------------
改版前 `facts.confidence` 直接来自抽取提示词里模型自己给的数字，
实测 230 条种子里 **208 条（90.4%）落在 0.90 以上**，中位数 0.90 —— 没有区分度，
而 `store.py` 的排序又是 `relevance = 覆盖度×0.75 + confidence×0.25`，
等于让模型的自我评分直接参与排序。这里改成按来源、提取方式、多源一致性、时效新鲜度派生：

    trust = 0.45×来源等级 + 0.25×提取方式 + 0.20×多源一致性 + 0.10×时效新鲜度

四因子都是入库时**已知的客观事实**（来源类型、走的是规则还是模型、有没有第二来源、
页面发布时间），因此可以逐条解释、可以写进日志，也可以被断言测试。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

# 权重（改了要同步 docs/architecture.md 第 4.1 节）
W_SOURCE = 0.45
W_EXTRACTION = 0.25
W_CONSISTENCY = 0.20
W_FRESHNESS = 0.10

# 来源等级：官方 > 手工策展 > 维基 > 种子 > 社区
SOURCE_SCORE: Dict[str, float] = {
    "official": 1.0,
    "manual": 0.95,
    "wiki": 0.75,
    "seed": 0.6,
    "community": 0.5,
}
SOURCE_SCORE_DEFAULT = 0.5

# 提取方式：结构化接口 > 确定性表格 > 模型抽取
EXTRACTION_SCORE: Dict[str, float] = {
    "api": 1.0,        # BWIKI action=ask / 模板字段，字段名→值，无启发式
    "table": 0.85,     # 网页表格按分隔行解析（有启发式，但数值不被改写）
    "llm": 0.6,        # 模型摘写，可能概括或漏数
    "manual": 0.95,    # 用户手写
}
EXTRACTION_DEFAULT = 0.6

# 多源一致性
CONSISTENCY_SCORE: Dict[str, float] = {
    "multi": 1.0,      # 至少两个不同来源都写了同一件事
    "single": 0.6,     # 只有一个来源
    "conflict": 0.2,   # 检出冲突，谁对不知道
}
CONSISTENCY_DEFAULT = 0.6

# 时效新鲜度：半年内 1.0 / 一年内 0.7 / 更久 0.4 / 无日期 0.7（中性）
#
# FRESH_UNKNOWN 与 FRESH_YEAR 同值是**刻意的校准结果，不是笔误**：读不到日期时，
# 「刚发布的页面没有日期字段」和「确实是一年前的旧资料」无法分辨，只能给中性分。
# 要改它就必须同时重算内置库 seed/seed_kb.json 里 685 条事实的 confidence，否则
# 内置数据与新抓数据不可比 —— 那属于数据级改动，要单独处理。
FRESH_RECENT = 1.0
FRESH_YEAR = 0.7
FRESH_OLD = 0.4
FRESH_UNKNOWN = 0.7

# 时效敏感条目（版本、活动、价格、概率、保底）没有日期时额外扣分
TIME_SENSITIVE_PENALTY = 0.7

_TIME_SENSITIVE = re.compile(
    # `up` 必须用 ASCII 字母边界而不是 \b：中文是「词字符」，`角色up池` 里
    # 「色」与「u」之间没有 \b，用 \bup\b 反而漏掉最常见的写法；同时
    # 前后排除英文字母即可挡掉 setup / backup / group 这类误命中。
    r"版本|更新|活动|限时|时间|日期|公测|测试|招募|预约|价格|折扣|原价|概率|保底|概率提升|卡池|(?<![A-Za-z])up(?![A-Za-z])",
    re.I,
)


def is_time_sensitive(title: str, answer: str = "") -> bool:
    """是否是「会过期」的条目（版本公告、活动时间、价格、概率保底…）。"""
    return bool(_TIME_SENSITIVE.search(f"{title or ''} {answer or ''}"))


def _parse_date(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text[: len(text)], fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    # 带时区偏移的 ISO 串（+08:00）用 fromisoformat 兜底
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def freshness_score(published_at: Any, now: Optional[datetime] = None, time_sensitive: bool = False) -> float:
    """页面发布时间越新分越高；没有日期给中性值，时效敏感条目再打折。"""
    published = _parse_date(published_at)
    if published is None:
        return round(FRESH_UNKNOWN * (TIME_SENSITIVE_PENALTY if time_sensitive else 1.0), 4)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = current - published
    if age < timedelta(0):
        age = timedelta(0)          # 未来时间当作最新
    if age <= timedelta(days=183):
        score = FRESH_RECENT
    elif age <= timedelta(days=365):
        score = FRESH_YEAR
    else:
        score = FRESH_OLD
    if time_sensitive:
        score *= TIME_SENSITIVE_PENALTY
    return round(score, 4)


def trust_breakdown(
    source_type: str = "",
    extraction: str = "llm",
    sources: int = 1,
    conflict: bool = False,
    published_at: Any = "",
    title: str = "",
    answer: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, float]:
    """返回四个因子与总分的明细，便于日志与断言。"""
    source_key = (source_type or "").strip().lower()
    source = SOURCE_SCORE.get(source_key, SOURCE_SCORE_DEFAULT)
    extract = EXTRACTION_SCORE.get((extraction or "").strip().lower(), EXTRACTION_DEFAULT)
    if conflict:
        consistency = CONSISTENCY_SCORE["conflict"]
    elif int(sources or 1) >= 2:
        consistency = CONSISTENCY_SCORE["multi"]
    else:
        consistency = CONSISTENCY_SCORE["single"]
    fresh = freshness_score(
        published_at, now=now, time_sensitive=is_time_sensitive(title, answer)
    )
    total = W_SOURCE * source + W_EXTRACTION * extract + W_CONSISTENCY * consistency + W_FRESHNESS * fresh
    return {
        "source": round(source, 4),
        "extraction": round(extract, 4),
        "consistency": round(consistency, 4),
        "freshness": round(fresh, 4),
        # 下限 0.05 是防御性钳位：按当前权重与各因子取值，最坏组合（社区站 +
        # 模型抽取 + 检出冲突 + 过期且时效敏感）约 0.443，实际够不到 0.05。
        # 留着是为了将来调 W_* 权重时不会算出 0 或负数。
        "total": round(max(0.05, min(1.0, total)), 4),
    }


def compute_trust(
    source_type: str = "",
    extraction: str = "llm",
    sources: int = 1,
    conflict: bool = False,
    published_at: Any = "",
    title: str = "",
    answer: str = "",
    penalty: float = 1.0,
    now: Optional[datetime] = None,
) -> float:
    """派生可信度（0.05–1.0）。penalty 用于「低质量页面」这类额外打折。"""
    detail = trust_breakdown(
        source_type=source_type,
        extraction=extraction,
        sources=sources,
        conflict=conflict,
        published_at=published_at,
        title=title,
        answer=answer,
        now=now,
    )
    return round(max(0.05, min(1.0, detail["total"] * float(penalty or 1.0))), 4)


def trust_tag(detail: Dict[str, float], extraction: str = "llm") -> str:
    """写进 facts.tags 的可解释标记，便于用 SQL 抽查与回归排查。"""
    return (
        f"提取:{extraction or 'llm'} "
        f"可信度:{detail['total']:.2f}"
        f"(来源{detail['source']:.2f}/提取{detail['extraction']:.2f}"
        f"/一致{detail['consistency']:.2f}/时效{detail['freshness']:.2f})"
    )


def describe() -> Dict[str, Any]:
    """给界面/文档用的参数快照（新增字段时同步这里）。"""
    return {
        "weights": {
            "source": W_SOURCE,
            "extraction": W_EXTRACTION,
            "consistency": W_CONSISTENCY,
            "freshness": W_FRESHNESS,
        },
        "source": dict(SOURCE_SCORE),
        "extraction": dict(EXTRACTION_SCORE),
        "consistency": dict(CONSISTENCY_SCORE),
    }
