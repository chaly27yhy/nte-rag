"""人工录入层：把维护者确认、逐字段复核的事实与人工裁定标注写进种子。

背景
----
内置种子由 `tools/seed_builder.py` 抓取产出，重跑会把种子整体覆盖。用户口述确认的
数据（例如「噬心诡刃满级攻击力 570」）如果任何可抓来源都拿不到，就必须有一条
**可复跑**的落地路径，否则每次重建种子都会丢。同时把「这条是人工写进去的、证据是
什么」写进 tags，避免以后有人把它当成抓来的数据。

数据改在**种子文件**里而不是导入时再加：`app/server/api.py:68-90` 的 `ensure_seed()`
按种子文件指纹决定是否重新导入，种子文件不变时更新逻辑对老用户不生效。

    python tools\\manual_facts.py --show     # 打印人工层现状（只读，不改文件）
    python tools\\manual_facts.py --apply    # 把 MANUAL_FACTS / NOTE_UPDATES 落到种子
    python tools\\manual_facts.py --check    # 校验种子与人工层一致（断言用，退出码 0/1）

可信度按 `app/core/trust.py` 计算。有发布日期的条目按复核当天计算时效，以便复跑同一份种子。
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import trust  # noqa: E402
from app.core import versioning  # noqa: E402
from app.core.sources import NARRATIVE_TAG  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEED = ROOT / "seed" / "seed_kb.json"

# 与 tools/seed_builder.py 写出的键顺序保持一致（比对/更新都按这个顺序来）
FACT_KEYS = (
    "title",
    "answer",
    "topic",
    "tags",
    "source_url",
    "source_type",
    "confidence",
    "extraction",
    "version",
    "effective_from",
    "date_kind",
)

# 追加到 payload["note"] 末尾的说明（可重复执行，先按这个前缀剥掉旧尾巴）
NOTE_SUFFIX = "；另含 %d 条人工录入事实（tools/manual_facts.py，含维护者确认与网页复核）"
NOTE_MARK = "；另含 "

# 日期按国服公告；每篇摘要与精确检索所需的字段共用同一个来源。
CN_15_ANNOUNCEMENTS = (
    (
        "2026-11-04", "264868", "《异环》1.5版本「致电，空想迷数」更新公告",
        "国服 1.5「致电，空想迷数」于 2026 年 11 月 5 日停服维护后上线，当天预定维护时段为北京时间 06:00–11:00。"
        "首期开放曳尔沐和真红的限定棋盘及两项弧盘研募。"
        "本版加入正篇「再会于数海」、异象巡礼「贵腐巢母」和曳尔沐的羁遇玩法，扩展了资源溢出存储、"
        "养成目标、资源获取进度与地图图钉管理。活动日期、奖励条件和数值见同源结构化条目。",
        (
            ("2026-11-05", "国服1.5版本名称、正式上线与停服维护时间", "版本时间",
             "国服 1.5 版本名为「致电，空想迷数」，于 2026 年 11 月 5 日版本维护完成后开放。"
             "官宣的停服维护安排为北京时间当天 06:00–11:00，结束时间视维护进度而定。"),
            ("2026-11-05", "国服1.5版本维护与修复补偿", "维护补偿",
             "2026 年 11 月 5 日 06:00 前已创建角色的鉴定师，符合版本维护与问题修复两项补偿的领取条件。"
             "两项均为 300 环石，由维护结束后的游戏内邮件发放。"),
            ("2026-11-05", "1.5首期·曳尔沐与真红限定棋盘", "限定棋盘",
             "从 2026 年 11 月 5 日维护后到 2026 年 11 月 26 日 05:59，S 级曳尔沐可从「电子人之梦」获取，"
             "S 级真红在「破晓前」返场。两个棋盘均包含薄荷、埃德嘉、阿德勒；需先完成序章中前往伊波恩古董店的正篇任务。"),
            ("2026-11-05", "1.5首期·静候彩铃与穿过胭红蜃景弧盘研募", "限定弧盘",
             "从 2026 年 11 月 5 日维护后到 2026 年 11 月 26 日 05:59，「静候彩铃」可用三重钥匙或回音钥匙参加「回音特刊」获取，"
             "「穿过胭红蜃景」可用三重钥匙或决意钥匙参加「决意特刊」获取。两项均需完成序章中前往伊波恩古董店的正篇任务。"),
            ("2026-11-05", "1.5·再会于数海、贵腐巢母解锁", "剧情与副本",
             "1.5 更新后，通关正篇「魔女」可解锁正篇「再会于数海」，通关正篇「雾巢游戏」可解锁异象巡礼「贵腐巢母」。"
             "曳尔沐的都市偕游和居家邀约需要先获得该角色并达到指定羁遇等级。"),
            ("2026-11-05", "1.5·本性像素与都市活力溢出存储上限", "资源机制",
             "1.5 更新后，本性像素溢出会存入「盒装相纸」，存储上限为 2400 点。"
             "都市活力溢出会存入「后备活力」，存储上限为 1000 点。"),
            ("2026-11-05", "1.5·真红威慑凝视与轨外之境", "角色机制",
             "1.5 更新后，真红每次进入或重新挑战「轨外之境」时，被动「威慑凝视」直接以满层状态开始。"),
            ("2026-11-05", "1.5·环期赠礼与运通全城、争锋赏宴·攻坚", "限时活动",
             "三项活动从 2026 年 11 月 5 日维护后开始，均在 2026 年 12 月 17 日 05:59 截止。"
             "「环期赠礼」累计签到 7 天得 10 个质实骰子。运通全城限时任务产出库霸，活动商店可换 C001，提高商誉可拿 Ori-LV03。"
             "争锋赏宴·攻坚通关挑战有环石等奖励，称号「盛宴·执锐」的显示效果随总积分变化。"),
            ("2026-11-05", "1.5·所行皆坦途盲盒与坦途车辆", "限时盲盒",
             "2026 年 11 月 5 日维护后至 2026 年 12 月 17 日 05:59，可用方斯或环石抽取「所行皆坦途」。"
             "其中包含坦途及试作型陆地霸者001，抽取成本随次数增加，抽满 15 次可拿齐奖励。"),
            ("2026-11-05", "1.5·千年虫之谜时装折扣", "限时折扣",
             "曳尔沐时装「千年虫之谜」长期在售。公告列的原价为 3280 异晶，"
             "首发折扣价为 2680 异晶，折扣时段为 2026 年 11 月 5 日维护后至 2026 年 12 月 17 日 05:59。"),
            ("2026-11-05", "1.5·集市分福活动与累计进度奖励", "限时活动",
             "本期「集市分福」从 2026 年 11 月 5 日维护后到 2026 年 12 月 17 日 05:59。"
             "累计进度达到 3000 可解锁配饰「小小的朋友」，达到 5100 可解锁交互物品「流动占卜屋」。"),
            ("2026-11-05", "1.5·环期赏令开启条件与结束时间", "限时活动",
             "完成主线「憩息时间」可参加 1.5 的环期赏令。开放期是 2026 年 11 月 5 日维护后至 2026 年 12 月 16 日 23:59，"
             "完成环期任务获得历练值、提高赏令等级。"),
            ("2026-11-09", "1.5·像素溢出活动时间与双倍额度", "限时活动",
             "像素溢出于 2026 年 11 月 9 日 05:00 开始、2026 年 11 月 16 日 04:59 结束。"
             "在公告指定的四类玩法消耗本性像素可拿双倍奖励。每日双倍额度为 120，可累积，整个活动最多 840。"),
            ("2026-11-13", "1.5·征途坦荡活动与TX纪念币", "限时活动",
             "征途坦荡从 2026 年 11 月 13 日 10:00 开始，至 2026 年 12 月 17 日 05:59。"
             "参与可得 TX 纪念币，能在活动商店兑换环石、浮冰影券、方斯等。"),
            ("2026-11-16", "1.5·渔获大作战时间与捕鱼地点", "限时活动",
             "渔获大作战于 2026 年 11 月 16 日 05:00 开始，至 2026 年 12 月 17 日 05:59。"
             "参加地点在纳库佩达公园的「海上钓客」捕鱼点，提高捕鱼等级可拿环石、浮冰影券等奖励。"),
            ("2026-11-16", "1.5·夺金大作战时间与方斯加成", "限时活动",
             "夺金大作战于 2026 年 11 月 16 日 05:00 开始、2026 年 11 月 30 日 04:59 结束。"
             "这段时间在「粉爪大劫案」获取方斯的效率为平时的 2 倍。"),
        ),
    ),
    (
        "2026-11-06", "264932", "《异环》11月6日不停服更新公告",
        "国服 1.5 在 11 月 6 日安排不停服更新。本次涉及未确认的梦、曳尔沐觉醒、即刻落槌、贵腐巢母、"
        "坦途车库显示、家具和养成指南等，也修正了部分崩溃情形。",
        (
            ("2026-11-06", "1.5·11月6日不停服更新安排", "维护排期",
             "国服定于 2026 年 11 月 6 日 02:00–06:00 进行不停服更新，时间为公告预估的北京时间，"
             "期间如遇短暂断线可重新登录。"),
            ("2026-11-06", "1.5·11月6日曳尔沐觉醒与玩法修复", "角色修复",
             "11 月 6 日更新修正了「404 遥望一座孤岛」激活后错视幻觉消耗层数计数不准导致的暴击伤害异常，"
             "并修复即刻落槌中「神秘来电」的场外来电可能覆盖自身已知藏品的问题。"),
            ("2026-11-06", "1.5·未确认的梦与贵腐巢母修复", "剧情与副本修复",
             "11 月 6 日更新修复了「再会于数海」中做「未确认的梦」时角色醒来可能掉进地下的问题，"
             "以及多人「贵腐巢母」副本里能够异常召车的问题。"),
        ),
    ),
    (
        "2026-11-13", "265046", "《异环》11月13日不停服更新公告",
        "国服 1.5 在 11 月 13 日安排不停服更新。曳尔沐的部分日语语音在本次上线，"
        "同时修复了剧情重复播放、运通全城信誉分、同城派送进度、部分角色表现和界面显示；背包中的旧钱包道具会同步新版名称。",
        (
            ("2026-11-13", "1.5·11月13日不停服更新安排", "维护排期",
             "国服 2026 年 11 月 13 日不停服更新的预计时段为北京时间 02:00–06:00；"
             "实际结束时间视更新进度而定。"),
            ("2026-11-13", "1.5·曳尔沐11月13日日语配音与即刻落槌解锁", "角色更新",
             "11 月 13 日更新实装了曳尔沐在邀约、即刻落槌及部分互动动作中的日语配音。"
             "即刻落槌中如果此前遇到曳尔沐解锁异常，更新后可再次完成解锁条件。"),
            ("2026-11-13", "1.5·遗失的钱包更名为团三郎的恶作剧", "道具更新",
             "2026 年 11 月 13 日更新把「遗失的钱包」改名为「团三郎的恶作剧」，并换上新版图标和描述。"
             "背包内已有的道具会同步更新，原有数量与使用功能保持一致。"),
            ("2026-11-13", "1.5·运通全城信誉分与同城派送修复", "活动修复",
             "11 月 13 日更新修复了运通全城部分订单完成后信誉分未增加的问题。"
             "同一公告也修复了同城派送的订单状态异常，以及完成订单后老旧邮箱未激活或升级进度未增加的问题。"),
        ),
    ),
    (
        "2026-11-18", "265103", "《异环》TGA 2026提名答谢",
        "《异环》获 TGA 2026 年度最佳移动游戏提名。国服官方公告列出了全服环石答谢的邮件发放时间与领用资格。",
        (
            ("2026-11-18", "1.5·TGA 2026提名答谢邮件与领取条件", "限时奖励",
             "国服于 2026 年 11 月 18 日 15:00（UTC+8）通过游戏内邮件发放 1000 环石作为 TGA 2026 提名答谢。"
             "公告要求在 1.5 版本结束前创建角色并解锁邮件功能。"),
        ),
    ),
)


def _announcement_url(day: str, article_id: str) -> str:
    return f"https://yh.wanmei.com/m/news/gamebroad/{day.replace('-', '')}/{article_id}.html"


def _announcement_docs() -> List[Dict[str, Any]]:
    return [
        {
            "url": _announcement_url(day, article_id),
            "title": f"{title}（复核摘要）",
            "site": "yh.wanmei.com",
            "source_type": "official",
            "published_at": day,
            "content_hash": hashlib.sha1(summary.encode("utf-8")).hexdigest(),
            "meta": {"source_name": "《异环》国服官网", "content_kind": "reviewed_summary"},
            "chunks": [summary],
        }
        for day, article_id, title, summary, _ in CN_15_ANNOUNCEMENTS
    ]


# --------------------------------------------------------------------------- #
# 人工录入的事实：维护者确认或逐字段复核的公开资料
# --------------------------------------------------------------------------- #
MANUAL_FACTS: List[Dict[str, Any]] = [
    {
        "title": "异环国服公测支持平台",
        "answer": (
            "《异环》国服于 2026 年 4 月 23 日正式公测。完美世界年报确认首发覆盖 "
            "Windows PC、安卓（Android）、iOS、Mac、鸿蒙（HarmonyOS）与 PlayStation 5（PS5）。"
        ),
        "topic": "游戏平台",
        "tags": "人工录入, 官宣复核, 国服, 公测平台",
        "source_url": "https://static.cninfo.com.cn/finalpage/2026-04-28/1225207317.PDF",
        "source_type": "official",
        "published_at": "2026-04-28",
        "reviewed_at": "2026-11-20",
        "why": "年报确认国服已经公测及六个平台，国服官网定档公告列出同样的平台。",
    },
    {
        "title": "噬心诡刃·满级攻击力",
        "answer": (
            "噬心诡刃（1.3版本限定S级弧盘） 的满级攻击力为：570（初始 37）。"
        ),
        "topic": "人工录入·弧盘数值",
        "tags": (
            "人工录入, 用户确认, 限定S级弧盘, 摄心特刊, "
            "证据:神ゲー攻略(kamigame.jp)「喰心刃」页 攻撃力 37～570"
        ),
        "source_url": "https://kamigame.jp/nte/page/435479019140427436.html",
        "published_at": "",  # 该页没有日期 → 时效按「未知」0.7 计
        "why": (
            "用户 2026-09-22 口述「噬心诡刃满级攻击力是570」；"
            "神ゲー攻略的「喰心刃」页独立给出 上昇ステ 攻撃力 37 ～ 570。"
        ),
    },
    {
        "title": "限定特刊·保底次数",
        "answer": (
            "限定特刊（弧盘研募计划，含1.3版本的「摄心特刊」） 的保底次数为："
            "所有「限定特刊」共享同一保底次数并永久累计——至多6次研募必定通过保底获得S级弧盘，"
            "至多8次研募必定通过保底获得当期限定S级弧盘；"
            "开启1次「限定特刊」共可获得10个任意品质的弧盘奖励，"
            "保底次数可以继承到后续「限定特刊」。"
        ),
        "topic": "人工录入·抽卡保底",
        "tags": (
            "人工录入, 用户确认, 限定特刊, 保底机制, 摄心特刊, "
            "证据:官方公众号转载(17173)2026-06-16「追猎特刊」弧盘研募计划公告"
        ),
        "source_url": "https://news.17173.com/content/06162026/171559836.shtml",
        "published_at": "2026-06-16",  # 公告日期，用于算时效
        "reviewed_at": "2026-09-22",
        "why": (
            "用户 2026-09-22 说「摄心特刊保底次数与其它特刊保底次数一致」；"
            "17173 转载的官方公众号公告写明 6 次/8 次保底、保底次数在所有限定特刊之间完全共享。"
            "概率数值任何来源都查不到，所以题面里不含概率。"
        ),
    },
    {
        "title": "不屈之绵·弧盘类型",
        "answer": "「不屈之绵」的弧盘适用分类为等离子类。这里的分类指弧盘类型。",
        "topic": "弧盘类型",
        "tags": "人工录入, 网页复核, 弧盘分类, 区服:来源未注明, 版本:来源未注明",
        "source_url": "https://wiki.mysqil.com/weapons/buquzhimian",
        "source_type": "wiki",
        "why": "核对环世全鉴及异环信息站的类型字段；仅收录类型。",
    },
    {
        "title": "成功的第一步·稀有度",
        "answer": "国服公测资料将弧盘「成功的第一步」列为 B 级。",
        "topic": "弧盘稀有度",
        "tags": "人工录入, 网页复核, 弧盘稀有度, 区服:国服, 口径:公测资料",
        "source_url": "https://m.ali213.net/news/gl2604/1766607.html",
        "source_type": "community",
        "published_at": "2026-04-23",
        "reviewed_at": "2026-11-19",
        "why": "核对游侠手游的公测日条目及异环弧盘 Wiki 的稀有度。",
    },
    {
        "title": "成功的第一步·变轨效果",
        "answer": (
            "国服公测资料记载，弧盘「成功的第一步」的基础调弧效果为：装备者释放变轨技能后，"
            "自身攻击力提升 12%，持续 10 秒，20 秒内最多触发一次。"
        ),
        "topic": "弧盘变轨效果",
        "tags": "人工录入, 网页复核, 变轨技能, 区服:国服, 口径:公测资料的基础调弧",
        "source_url": "https://m.ali213.net/news/gl2604/1766607.html",
        "source_type": "community",
        "published_at": "2026-04-23",
        "reviewed_at": "2026-11-19",
        "why": "核对游侠手游的 12%／10 秒／20 秒与异环弧盘 Wiki 的精炼区间和触发条件。",
    },
    {
        "title": "「银河暂留」·公测签到奖励",
        "answer": (
            "据国服公测期间的签到攻略，S 级弧盘「银河暂留」曾作为公测登录活动第 10 天的奖励发放。"
            "当前能否领取以及当前获取途径待核实。"
        ),
        "topic": "弧盘历史获取",
        "tags": "人工录入, 网页复核, 弧盘获取途径, 签到奖励, 区服:国服, 口径:公测活动历史",
        "source_url": "https://www.taptap.cn/moment/796429909030338680",
        "source_type": "community",
        "why": "TapTap 公测攻略和 NTE 异环攻略站均明确列出第 10 天；只采用历史口径。",
    },
    {
        "title": "「银河暂留」·弧盘类型",
        "answer": "公开弧盘图鉴将「银河暂留」的弧盘类型（适用分类）列为等离子类。",
        "topic": "弧盘类型",
        "tags": "人工录入, 网页复核, 弧盘分类, 版本:来源未注明, 区服:来源未注明",
        "source_url": "https://www.ntestation.com/%E9%93%B6%E6%B2%B3%E6%9A%82%E7%95%99",
        "source_type": "wiki",
        "why": "异环信息站的类型与 GameWith 简中等离子、英文 Plasma 一致。",
    },
    {
        "title": "「银河暂留」·1阶水星图",
        "answer": (
            "按公开图鉴，弧盘「银河暂留」的 1 阶效果「水星图」使装备者的魂属性异能伤害提高 12%。"
            "造成魂属性异能伤害时叠加暴击伤害，每层提高 2%，持续 5 秒，最多 10 层，每 0.1 秒至多叠一层。"
        ),
        "topic": "弧盘效果",
        "tags": "人工录入, 网页复核, 弧盘阶数:1, 版本:来源未注明, 区服:来源未注明",
        "source_url": "https://www.ntestation.com/%E9%93%B6%E6%B2%B3%E6%9A%82%E7%95%99",
        "source_type": "wiki",
        "why": "异环信息站的首档读数与 GameWith 英文图鉴的默认调弧档吻合。",
    },
    {
        "title": "「最后一朵玫瑰」·弧盘类型",
        "answer": "公开弧盘图鉴将「最后一朵玫瑰」的弧盘类型（适用分类）列为液态类。",
        "topic": "弧盘类型",
        "tags": "人工录入, 网页复核, 弧盘分类, 版本:来源未注明, 区服:来源未注明",
        "source_url": "https://www.ntestation.com/%E6%9C%80%E5%90%8E%E4%B8%80%E6%9C%B5%E7%8E%AB%E7%91%B0",
        "source_type": "wiki",
        "why": "异环信息站的类型与 GameWith 简中液态、英文 Liquid 一致。",
    },
    {
        "title": "「最后一朵玫瑰」·1阶落拓玫瑰",
        "answer": (
            "按公开图鉴，弧盘「最后一朵玫瑰」的 1 阶效果「落拓玫瑰」使装备者攻击力提高 14%。"
            "装备者造成持续伤害时可叠加「暗棘」，每层增加 6% 暴击伤害，持续 3 秒。"
            "每 0.3 秒至多叠一层，最多 10 层，继续叠加会刷新持续时间，施放变轨技能会立即获得 10 层。"
            "装备者伤害已倾陷的敌人时，可延长该敌人的倾陷 3 秒，每次倾陷最多触发一次。"
        ),
        "topic": "弧盘效果",
        "tags": "人工录入, 网页复核, 弧盘阶数:1, 版本:来源未注明, 区服:来源未注明",
        "source_url": "https://www.ntestation.com/%E6%9C%80%E5%90%8E%E4%B8%80%E6%9C%B5%E7%8E%AB%E7%91%B0",
        "source_type": "wiki",
        "why": "异环信息站的首档读数与 GameWith 英文图鉴的默认调弧档吻合。",
    },
    {
        "title": "「银河暂留」·1.5版本水星图公告复核",
        "answer": (
            "核对 8 月 10 日至 11 月 19 日的国服版本更新和维护公告，其中未列出「银河暂留」或「水星图」的数值调整。"
            "回答国服 1.5 版本的 1 阶效果时，可按已核公告沿用公开图鉴的同阶数值。"
        ),
        "topic": "弧盘公告复核",
        "tags": "人工录入, 公告复核, 国服, 弧盘阶数:1, 复核截止:2026-11-19",
        "source_url": "https://yh.wanmei.com/m/news/gamebroad/20261104/264868.html",
        "why": "逐条核对1.3至1.5的版本与维护公告，原始数值见异环信息站和 GameWith。公告范围见 docs/data_sources_cn.md。",
    },
    {
        "title": "「最后一朵玫瑰」·1.5版本落拓玫瑰公告复核",
        "answer": (
            "核对 8 月 10 日至 11 月 19 日的国服版本更新和维护公告，1.4 版本公告列出「最后一朵玫瑰」返场研募，"
            "所查公告未列出「落拓玫瑰」的数值调整。回答国服 1.5 版本的 1 阶效果时，可按已核公告沿用公开图鉴的同阶数值。"
        ),
        "topic": "弧盘公告复核",
        "tags": "人工录入, 公告复核, 国服, 弧盘阶数:1, 复核截止:2026-11-19",
        "source_url": "https://yh.wanmei.com/m/news/gamebroad/20261104/264868.html",
        "why": "逐条核对1.3至1.5的版本与维护公告，原始数值见异环信息站和 GameWith。公告范围见 docs/data_sources_cn.md。",
    },
    {
        "title": "「我们。」·弧盘类型",
        "answer": "弧盘「我们。」的类型（适用分类）为等离子类，稀有度为 B 级。",
        "topic": "弧盘类型",
        "tags": "人工录入, 网页复核, 弧盘分类, 等离子",
        "source_url": "https://www.ntestation.com/%E6%88%91%E4%BB%AC%E3%80%82",
        "source_type": "wiki",
        "why": "异环信息站标为B级等离子，GameWith简中及英文页面一致。",
    },
    {
        "title": "「我们。」·1阶原核体",
        "answer": "弧盘「我们。」的 1 阶效果「原核体」使装备者的普通攻击伤害提高 12%。",
        "topic": "弧盘效果",
        "tags": "人工录入, 网页复核, 弧盘阶数:1, 普通攻击伤害",
        "source_url": "https://www.ntestation.com/%E6%88%91%E4%BB%AC%E3%80%82",
        "source_type": "wiki",
        "why": "异环信息站列出的首档为普攻伤害12%，GameWith英文默认档一致。",
    },
    {
        "title": "「电音」狂欢·弧盘类型",
        "answer": "弧盘「电音」狂欢的类型（适用分类）为液态类，稀有度为 B 级。",
        "topic": "弧盘类型",
        "tags": "人工录入, 网页复核, 弧盘分类, 电音狂欢, 液态",
        "source_url": "https://gamewith.ai/nte/zh-CN/arc/real-music",
        "source_type": "wiki",
        "why": "GameWith 简中图鉴的B级液态与呗果的弧盘记录一致。",
    },
    {
        "title": "「电音」狂欢·1阶变轨技能伤害",
        "answer": "弧盘「电音」狂欢的 1 阶效果使装备者的变轨技能伤害提高 12%。",
        "topic": "弧盘效果",
        "tags": "人工录入, 网页复核, 弧盘阶数:1, 电音狂欢, 变轨技能伤害",
        "source_url": "https://gamewith.net/nte/74425",
        "source_type": "wiki",
        "why": "简中图鉴确认伤害种类，GameWith 英文默认档和 Neverness 1阶均为变轨技能伤害12%。",
    },
    {
        "title": "「我们。」·1.5版本普攻伤害公告复核",
        "answer": (
            "核对 8 月 10 日至 11 月 19 日的国服版本更新和维护公告，所查公告未列出「我们。」或「原核体」的数值调整。"
            "回答国服 1.5 版本的 1 阶效果时，可按已核公告沿用公开图鉴的同阶数值。"
        ),
        "topic": "弧盘公告复核",
        "tags": "人工录入, 公告复核, 国服, 弧盘阶数:1, 复核截止:2026-11-19",
        "source_url": "https://yh.wanmei.com/m/news/gamebroad/20261104/264868.html",
        "why": "逐条核对国服18则版本与维护公告；原值和核对范围见 docs/data_sources_cn.md。",
    },
    {
        "title": "「电音」狂欢·1.5版本变轨技能伤害公告复核",
        "answer": (
            "核对 8 月 10 日至 11 月 19 日的国服版本更新和维护公告，所查公告未列出弧盘「电音」狂欢的数值调整。"
            "回答国服 1.5 版本的 1 阶效果时，可按已核公告沿用公开图鉴的同阶数值。"
        ),
        "topic": "弧盘公告复核",
        "tags": "人工录入, 公告复核, 国服, 弧盘阶数:1, 电音狂欢, 复核截止:2026-11-19",
        "source_url": "https://yh.wanmei.com/m/news/gamebroad/20261104/264868.html",
        "why": "逐条核对国服18则版本与维护公告；原值和核对范围见 docs/data_sources_cn.md。",
    },
    {
        "title": "九原·中文配音",
        "answer": "《异环》角色九原的中文配音演员是张安琪。",
        "topic": "角色中文配音",
        "tags": "人工录入, 网页复核, 中文配音, 中配, CV",
        "source_url": "https://www.ntestation.com/%E4%B9%9D%E5%8E%9F",
        "source_type": "wiki",
        "why": "异环信息站的中文配音字段与环世全鉴、番组计划的角色记录一致。",
    },
    {
        "title": "娜娜莉·中文配音",
        "answer": "海天配音工作室发布的《异环》参配名单列出，角色娜娜莉的中文配音演员是宋媛媛。",
        "topic": "角色中文配音",
        "tags": "人工录入, 网页复核, 配音工作室署名, 中文配音, 中配, CV",
        "source_url": "https://www.bilibili.com/video/BV19gNo68E78/",
        "source_type": "community",
        "published_at": "2026-07-18",
        "reviewed_at": "2026-11-19",
        "why": "海天工作室发布的参配名单明确对应角色与演员，异环信息站的中文配音字段佐证。",
    },
    {
        "title": "哈尼娅·生日（图鉴复核）",
        "answer": "《异环》角色哈尼娅的生日是3月27日。",
        "topic": "角色生日",
        "tags": "人工录入, 网页复核, 生日",
        "source_url": "https://www.ntestation.com/%E5%93%88%E5%B0%BC%E5%A8%85",
        "source_type": "wiki",
        "why": "异环信息站的生日字段与 HN 玩新聞的角色档案一致。",
    },
    {
        "title": "哈尼娅·中文配音",
        "answer": "《异环》角色哈尼娅的中文配音演员是苏子芜。",
        "topic": "角色中文配音",
        "tags": "人工录入, 网页复核, 中文配音, 中配, CV",
        "source_url": "https://www.ntestation.com/%E5%93%88%E5%B0%BC%E5%A8%85",
        "source_type": "wiki",
        "why": "异环信息站的中文配音字段与环世全鉴、前瞻配音名单整理一致。",
    },
    {
        "title": "哈索尔·生日（图鉴复核）",
        "answer": "《异环》角色哈索尔的生日是8月29日。",
        "topic": "角色生日",
        "tags": "人工录入, 网页复核, 生日",
        "source_url": "https://www.ntestation.com/%E5%93%88%E7%B4%A2%E5%B0%94",
        "source_type": "wiki",
        "why": "异环信息站的生日字段与游侠网的角色资料、玩家当日生日分享一致。",
    },
    {
        "title": "哈索尔·中文配音",
        "answer": "海天配音工作室发布的《异环》参配名单列出，角色哈索尔的中文配音演员是洪海天。",
        "topic": "角色中文配音",
        "tags": "人工录入, 网页复核, 配音工作室署名, 中文配音, 中配, CV",
        "source_url": "https://www.bilibili.com/video/BV19gNo68E78/",
        "source_type": "community",
        "published_at": "2026-07-18",
        "reviewed_at": "2026-11-19",
        "why": "海天工作室发布的参配名单明确对应角色与演员，异环信息站的中文配音字段佐证。",
    },
    {
        "title": "埃德嘉·生日（图鉴复核）",
        "answer": "《异环》角色埃德嘉的生日是10月7日。",
        "topic": "角色生日",
        "tags": "人工录入, 网页复核, 生日",
        "source_url": "https://www.ntestation.com/%E5%9F%83%E5%BE%B7%E5%98%89",
        "source_type": "wiki",
        "why": "异环信息站的生日字段与角色一览资料一致。",
    },
    {
        "title": "埃德嘉·中文配音",
        "answer": "《异环》角色埃德嘉的中文配音演员是四白。",
        "topic": "角色中文配音",
        "tags": "人工录入, 网页复核, 中文配音, 中配, CV",
        "source_url": "https://www.ntestation.com/%E5%9F%83%E5%BE%B7%E5%98%89",
        "source_type": "wiki",
        "why": "异环信息站的中文配音字段与前瞻配音名单整理一致。",
    },
    {
        "title": "真红·战斗定位",
        "answer": "异环信息站将角色真红的战斗定位列为主力输出。",
        "topic": "角色战斗定位",
        "tags": "人工录入, 网页复核, 战斗定位, 战斗类型, 主C",
        "source_url": "https://www.ntestation.com/%E7%9C%9F%E7%BA%A2",
        "source_type": "wiki",
        "why": "异环信息站的战斗定位字段为主力输出，环世全鉴的主C分类与之对应。",
    },
    {
        "title": "娜娜莉·战斗定位（图鉴复核）",
        "answer": "异环信息站将角色娜娜莉的战斗定位列为主力输出、追加攻击。也就是输出型角色，主要负责输出，并有追加攻击机制。",
        "topic": "角色战斗定位",
        "tags": "人工录入, 网页复核, 战斗定位, 战斗类型, 主C",
        "source_url": "https://www.ntestation.com/%E5%A8%9C%E5%A8%9C%E8%8E%89",
        "source_type": "wiki",
        "why": "异环信息站直接给出两项战斗定位；环世全鉴的主C分类支持输出定位，技能资料支持追加攻击。",
    },
    {
        "title": "国服1.4版本上线·9月24日维护",
        "answer": (
            "2026年9月24日版本维护完成后，国服开启1.4版本「祷歌为谁而诵」。"
            "按国服1.3和1.4的官方排期，9月24日该次维护前仍为1.3「雾中朔望星回」。"
            "公告计划维护时段为06:00–11:00，实际完成时间以开服情况为准。"
        ),
        "topic": "国服版本切换",
        "tags": "人工录入, 网页复核, 国服, 1.3版本, 1.4版本, 版本上线, 版本维护",
        "source_url": "https://yh.wanmei.com/news/gamebroad/20260923/264278.html",
        "source_type": "official",
        "why": "官网1.4更新公告明确以9月24日维护完成为开启条件，与1.3公告及维护者确认的旧版本适用范围一致。",
    },
    {
        "title": "8月20日不停服更新·排球之星修复",
        "answer": (
            "2026年8月20日《异环》不停服更新公告列出「排球之星」的四项修复："
            "① 赢得比赛后，残虹播放胜利动作时发生时装穿模。② 排球皮肤「海特洛日出」偶尔不显示拖尾。"
            "③ 男角色抛球时排球偶尔异常抖动。④ 解锁新球员时弹窗偶尔重复出现。"
        ),
        "topic": "玩法修复公告",
        "tags": "人工录入, 网页复核, 排球之星, 不停服更新, 8月20日, 修复",
        "source_url": "https://yh.wanmei.com/news/gamebroad/20260819/263646.html",
        "source_type": "official",
        "why": "官网公告修复列表第15至18条均明确点名排球之星，拦网手感属于另一栏的优化。",
    },
    {
        "title": "九原·1级属性详情",
        "answer": (
            "《异环》1.5 版本中，维护者实拍的九原 1 级角色面板显示生命值 1290、攻击力 82、防御力 75、体力值 200。"
            "进阶读数为暴击率 5.00%、暴击伤害 50.00%、充能效率 100.00%、环合强度 0、倾陷强度 0、"
            "治疗加成 0.00%、受治疗加成 0.00%。截图未列出弧盘、空幕和技能等级。"
        ),
        "topic": "角色1级面板",
        "tags": "人工录入, 客户端实拍复核, 版本:1.5, 采集日期:2026-11-19, 角色等级:1, 初始面板",
        "source_url": "https://m.wywyx.com/wiki/578618.html",
        "why": "版本和角色等级由维护者确认。公开的玩一玩历史快照佐证生命、攻击、防御三项。",
    },
    {
        "title": "甲硬币·道具类型",
        "answer": "维护者提供的《异环》国服 1.5 版本客户端道具详情把「甲硬币」标为货币。",
        "topic": "道具类型",
        "tags": "人工录入, 客户端实拍复核, 区服:国服, 版本:1.5, 采集日期:2026-11-19",
        "source_url": "https://www.ntestation.com/%E8%B4%A7%E5%B8%81",
        "why": "实拍的道具类型为货币，异环信息站的货币条目可公开佐证。",
    },
    {
        "title": "甲硬币·客户端获取途径",
        "answer": (
            "维护者提供的《异环》国服 1.5 版本客户端实拍中，「甲硬币」道具详情列出四个获取入口："
            "开启材料自选箱、胡迪尼的魔术舞台·硬币记、猎人交易所、世界探索。"
        ),
        "topic": "道具获取途径",
        "tags": "人工录入, 客户端实拍复核, 区服:国服, 版本:1.5, 采集日期:2026-11-19",
        "source_url": "https://gamewith.net/nte/74479",
        "why": "四个入口及中文名称取自国服实拍；GameWith 的英文攻略可公开佐证对应途径。",
    },
    {
        "title": "不屈之绵·80级弧盘面板",
        "answer": (
            "维护者提供的《异环》1.5 版本客户端实拍中，80 级 S 级等离子弧盘「不屈之绵」"
            "的弧盘面板显示固定攻击力 512，百分比攻击力 27.50%。"
        ),
        "topic": "弧盘面板",
        "tags": "人工录入, 客户端实拍复核, 版本:1.5, 采集日期:2026-11-19, 弧盘等级:80",
        "source_url": "https://wiki.mysqil.com/weapons/buquzhimian",
        "why": "实拍的 80 级弧盘面板两项读数与环世全鉴对应档位一致。",
    },
    {
        "title": "不屈之绵·1阶拳击糖",
        "answer": (
            "维护者提供的《异环》1.5 版本客户端实拍中，「不屈之绵」的 1 阶效果「拳击糖」只对等离子适配者生效："
            "装备者造成的伤害增加 22.00%，对生命值低于 50% 的敌人，增伤提高至 28.00%。"
        ),
        "topic": "弧盘效果",
        "tags": "人工录入, 客户端实拍复核, 版本:1.5, 采集日期:2026-11-19, 弧盘阶数:1",
        "source_url": "https://wiki.mysqil.com/weapons/buquzhimian",
        "why": "实拍确认了效果生效条件、阶数和三项数值，环世全鉴的 1 阶数值一致。",
    },
] + [
    {
        "title": title,
        "answer": answer,
        "topic": topic,
        "tags": "人工录入, 公告复核, 区服:国服, 版本:1.5",
        "source_url": _announcement_url(day, article_id),
        "source_type": "official",
        "published_at": day,
        "reviewed_at": "2026-11-20",
        "version": "1.5",
        "effective_from": effective_from,
    }
    for day, article_id, _, _, facts in CN_15_ANNOUNCEMENTS
    for effective_from, title, topic, answer in facts
]


# --------------------------------------------------------------------------- #
# 人工裁定的标注：口径澄清（用户 2026-09-22 明确「生命攻击口径是角色初始数值」）
# --------------------------------------------------------------------------- #
CALIBER_NOTE = (
    "口径:角色初始数值（BWIKI 模板 生命/攻击 即角色初始面板，用户 2026-09-22 确认）；"
    "但 BWIKI 站内数值不一致（小吱与玩一玩一致，早雾/九原/浔对不上），维持不跨源比对"
)
NOTE_UPDATES: List[Dict[str, Any]] = [
    {
        "titles": [
            "小吱·生命",
            "小吱·攻击",
            "早雾·生命",
            "早雾·攻击",
            "九原·生命",
            "九原·攻击",
            "浔·生命",
            "浔·攻击",
        ],
        "old": "口径未知（BWIKI模板字段口径与玩一玩1级面板不同，不跨源比对）",
        "new": CALIBER_NOTE,
        "why": "用户 2026-09-22 澄清口径＝角色初始数值；BWIKI 自身数值不一致，所以仍不跨源比对。",
    },
]


# --------------------------------------------------------------------------- #
# 事实更正：用户确认后**原地改正**的既有事实（不是新增，而是替换错误值）
# --------------------------------------------------------------------------- #
CV_EVIDENCE = "证据:萌娘百科/百度百科交叉验证（2026-09-22 用户确认）"
FACT_UPDATES: List[Dict[str, Any]] = [
    {
        "title": "九原·CV",
        "answer": "九原 的CV为：张安琪（中）/田中理惠（日）",
        "drop_prefixes": ("存疑:",),  # 撞值存疑是录入错位造成的，撤销
        "add_tags": (CV_EVIDENCE, "人工更正"),
        "why": (
            "用户 2026-09-22 裁定「直接恢复，不再标存疑」：萌娘百科与百度百科交叉验证"
            "九原中配张安琪、日配田中理惠，与娜娜莉（宋媛媛/竹达彩奈）完全不同，"
            "旧 BWIKI 曾给九原写入宋媛媛，资料未说明错记原因。"
        ),
    },
    {
        "title": "娜娜莉·CV",
        "answer": "娜娜莉 的CV为：宋媛媛（中）/竹达彩奈（日）",
        "drop_prefixes": ("存疑:",),
        "add_tags": (CV_EVIDENCE,),
        "why": (
            "用户 2026-09-22 给出交叉验证值：娜娜莉中配宋媛媛、日配竹达彩奈。"
            "同日九原页被改正，两者不再共用同一个名字。"
        ),
    },
]

# --------------------------------------------------------------------------- #
# 叙事/世界观来源：官网 main.html 的「角色介绍」板块文本不参与任何字段投票
# --------------------------------------------------------------------------- #
# 用户 2026-09-22 裁定 ③：官网「角色介绍」类纯叙事文章单独立「叙事/世界观来源」。
# 实测（.probe_lore*.py）官网没有可枚举的独立文章列表，这类内容就是 main.html
# 的「角色介绍」板块，因此按 URL 精确圈定，而不是按标题模糊匹配。
NARRATIVE_URLS = ("https://yh.wanmei.com/main.html",)
NARRATIVE_DOC_META = {"source_class": "narrative", "no_vote": True}


# --------------------------------------------------------------------------- #
# 标签小工具（种子里 tags 是 ", " 连接的字符串）
# --------------------------------------------------------------------------- #
def split_tags(text: Any) -> List[str]:
    return [item.strip() for item in str(text or "").split(",") if item.strip()]


def join_tags(items: List[str]) -> str:
    return ", ".join(items)


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def load_payload(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_payload(payload: Dict[str, Any], path: Path) -> None:
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n"
    )


def _tags_to_text(tags: Any) -> str:
    if isinstance(tags, (list, tuple)):
        return ", ".join(str(item).strip() for item in tags if str(item).strip())
    return str(tags or "").strip()


def build_fact(spec: Dict[str, Any]) -> Dict[str, Any]:
    """按种子的事实 schema 造一条人工事实（含版本/日期推导与可信度）。"""
    title = str(spec["title"])
    answer = str(spec["answer"])
    url = str(spec.get("source_url", ""))
    meta = versioning.fact_meta(title, answer, url)
    published_at = spec.get("published_at", "")
    reviewed_at = (
        datetime.fromisoformat(spec["reviewed_at"]).replace(tzinfo=timezone.utc) if published_at else None
    )
    fact = {
        "title": title,
        "answer": answer,
        "topic": str(spec.get("topic", "人工录入")),
        "tags": _tags_to_text(spec.get("tags", "")),
        "source_url": url,
        "source_type": str(spec.get("source_type", "manual")),
        "confidence": trust.compute_trust(
            source_type=str(spec.get("source_type", "manual")),
            extraction=str(spec.get("extraction", "manual")),
            sources=1,
            conflict=False,
            published_at=published_at,
            title=title,
            answer=answer,
            now=reviewed_at,
        ),
        "extraction": str(spec.get("extraction", "manual")),
        "version": str(spec.get("version") or meta.get("version", "")),
        "effective_from": str(spec.get("effective_from") or meta.get("effective_from", "")),
        "date_kind": "effective" if spec.get("effective_from") else str(meta.get("date_kind", "")),
    }
    return fact


def fact_index(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(fact.get("title", "")): fact
        for fact in payload.get("facts", [])
        if isinstance(fact, dict)
    }


def _sync_note(payload: Dict[str, Any]) -> str:
    """把「另含 N 条人工录入事实」写进 payload["note"]（幂等）。"""
    base = str(payload.get("note", ""))
    cut = base.find(NOTE_MARK)
    if cut >= 0:
        base = base[:cut]
    payload["note"] = base + (NOTE_SUFFIX % len(MANUAL_FACTS))
    return payload["note"]


def _fix_tags(tags: Any, drop_prefixes: Tuple[str, ...], add_tags: Tuple[str, ...]) -> Tuple[str, bool]:
    """按规则改标签；返回 (新标签, 是否有改动)。"""
    items = split_tags(tags)
    kept = [
        item
        for item in items
        if not any(item.startswith(prefix) for prefix in drop_prefixes)
    ]
    changed = len(kept) != len(items)
    for extra in add_tags:
        if extra not in kept:
            kept.append(extra)
            changed = True
    return join_tags(kept), changed


def apply_manual(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把 MANUAL_FACTS / FACT_UPDATES / NOTE_UPDATES / 叙事标注落到 payload（原地改）。"""
    facts = payload.setdefault("facts", [])
    index = fact_index(payload)
    report: Dict[str, Any] = {
        "added": [],
        "updated": [],
        "unchanged": [],
        "noted": [],
        "fixed": [],
        "missing": [],
        "narrative_facts": [],
        "narrative_docs": [],
    }

    documents = payload.setdefault("documents", [])
    docs_by_url = {doc["url"]: doc for doc in documents}
    for doc in _announcement_docs():
        # 重抓时可能已有同一篇的原文，原文优先。重新应用时可以修订已有摘要。
        existing = docs_by_url.get(doc["url"])
        if existing is None:
            documents.append(doc)
        elif (existing.get("meta") or {}).get("content_kind") == "reviewed_summary":
            existing.update(doc)

    for spec in MANUAL_FACTS:
        fresh = build_fact(spec)
        current = index.get(fresh["title"])
        if current is None:
            facts.append(fresh)
            index[fresh["title"]] = fresh
            report["added"].append(fresh["title"])
            continue
        diff = {
            key: (current.get(key), fresh[key])
            for key in FACT_KEYS
            if current.get(key) != fresh[key]
        }
        if diff:
            current.update(fresh)  # 键顺序不变，只改值
            report["updated"].append((fresh["title"], sorted(diff)))
        else:
            report["unchanged"].append(fresh["title"])

    for rule in FACT_UPDATES:
        fact = index.get(str(rule["title"]))
        if fact is None:
            report["missing"].append(str(rule["title"]))
            continue
        changed = False
        answer = str(rule.get("answer") or "")
        if answer and fact.get("answer") != answer:
            fact["answer"] = answer
            changed = True
        tags, tags_changed = _fix_tags(
            fact.get("tags"),
            tuple(rule.get("drop_prefixes") or ()),
            tuple(rule.get("add_tags") or ()),
        )
        if tags_changed:
            fact["tags"] = tags
            changed = True
        if changed:
            report["fixed"].append(str(rule["title"]))

    for rule in NOTE_UPDATES:
        for title in rule["titles"]:
            fact = index.get(title)
            if fact is None:
                report["missing"].append(title)
                continue
            tags = _tags_to_text(fact.get("tags"))
            new = str(rule["new"])
            if new in tags:
                continue
            old = str(rule.get("old", ""))
            if old and old in tags:
                tags = tags.replace(old, new)
            else:
                tags = f"{tags}, {new}" if tags else new
            fact["tags"] = tags
            report["noted"].append(title)

    # 叙事/世界观来源：事实打标 + 文档 meta 标类（用户 2026-09-22 裁定 ③）
    for fact in facts:
        if str(fact.get("source_url", "")) not in NARRATIVE_URLS:
            continue
        items = split_tags(fact.get("tags"))
        if NARRATIVE_TAG not in items:
            items.append(NARRATIVE_TAG)
            fact["tags"] = join_tags(items)
            report["narrative_facts"].append(str(fact.get("title", "")))
    for doc in payload.get("documents", []):
        if str(doc.get("url", "")) not in NARRATIVE_URLS:
            continue
        meta = doc.setdefault("meta", {})
        if any(meta.get(key) != value for key, value in NARRATIVE_DOC_META.items()):
            meta.update(NARRATIVE_DOC_META)
            report["narrative_docs"].append(str(doc.get("url", "")))

    _sync_note(payload)
    return report


def check(payload: Dict[str, Any]) -> List[Tuple[str, bool, str]]:
    """校验种子与人工层一致（断言用）。返回 (名称, 是否通过, 说明) 列表。"""
    results: List[Tuple[str, bool, str]] = []
    index = fact_index(payload)

    for expected in _announcement_docs():
        matching = [doc for doc in payload.get("documents", []) if doc["url"] == expected["url"]]
        valid = (
            len(matching) == 1
            and matching[0]["source_type"] == "official"
            and bool(matching[0]["chunks"])
            and (
                (matching[0].get("meta") or {}).get("content_kind") != "reviewed_summary"
                or matching[0] == expected
            )
        )
        results.append((f"官方公告来源有且仅有一篇：{expected['title']}", valid, expected["url"]))

    for spec in MANUAL_FACTS:
        fresh = build_fact(spec)
        title = fresh["title"]
        current = index.get(title)
        if current is None:
            results.append((f"人工事实存在：{title}", False, "种子里找不到"))
            continue
        results.append(
            (
                f"人工事实存在：{title}",
                True,
                f"可信度 {current.get('confidence')}",
            )
        )
        keys = ("answer", "source_url", "source_type", "extraction", "confidence", "version")
        if spec.get("effective_from"):
            keys += ("effective_from", "date_kind")
        for key in keys:
            same = current.get(key) == fresh[key]
            results.append(
                (
                    f"人工事实字段 {title}.{key}",
                    same,
                    "" if same else f"{current.get(key)!r} != {fresh[key]!r}",
                )
            )
        manual_tag = "人工录入" in _tags_to_text(current.get("tags"))
        results.append((f"人工事实带「人工录入」标：{title}", manual_tag, ""))

    for rule in NOTE_UPDATES:
        for title in rule["titles"]:
            tags = _tags_to_text((index.get(title) or {}).get("tags"))
            has_new = str(rule["new"]) in tags
            has_old = bool(rule.get("old")) and str(rule["old"]) in tags
            results.append(
                (
                    f"口径标注已更新：{title}",
                    has_new and not has_old,
                    "" if has_new and not has_old else f"tags={tags}",
                )
            )

    for rule in FACT_UPDATES:
        title = str(rule["title"])
        fact = index.get(title)
        if fact is None:
            results.append((f"事实更正已落地：{title}", False, "种子里找不到"))
            continue
        answer_ok = fact.get("answer") == rule.get("answer")
        results.append(
            (
                f"事实更正答案：{title}",
                answer_ok,
                "" if answer_ok else f"{fact.get('answer')!r}",
            )
        )
        items = split_tags(fact.get("tags"))
        stale = [
            item
            for item in items
            if any(item.startswith(prefix) for prefix in (rule.get("drop_prefixes") or ()))
        ]
        results.append(
            (
                f"事实更正已撤销旧存疑：{title}",
                not stale,
                "" if not stale else "、".join(stale),
            )
        )
        have = [tag for tag in (rule.get("add_tags") or ()) if tag in items]
        results.append(
            (
                f"事实更正带证据标：{title}",
                len(have) == len(rule.get("add_tags") or ()),
                "" if have else f"tags={fact.get('tags')}",
            )
        )

    # 叙事/世界观来源：该标的都标了、不该标的一个没多
    narrative_titles = []
    untagged = []
    for fact in payload.get("facts", []):
        tagged = NARRATIVE_TAG in split_tags(fact.get("tags"))
        is_narrative_url = str(fact.get("source_url", "")) in NARRATIVE_URLS
        if is_narrative_url and tagged:
            narrative_titles.append(str(fact.get("title", "")))
        elif is_narrative_url or tagged:
            untagged.append(f"{fact.get('title')}")
    results.append(
        (
            "叙事来源事实已打「不参与字段投票」标",
            bool(narrative_titles) and not untagged,
            f"已标 {len(narrative_titles)} 条；异常 {untagged}",
        )
    )
    narrative_docs = [
        doc
        for doc in payload.get("documents", [])
        if str(doc.get("url", "")) in NARRATIVE_URLS
    ]
    docs_ok = bool(narrative_docs) and all(
        all(doc.get("meta", {}).get(key) == value for key, value in NARRATIVE_DOC_META.items())
        for doc in narrative_docs
    )
    results.append(
        (
            "叙事来源文档 meta 标了 source_class",
            docs_ok,
            f"{len(narrative_docs)} 篇",
        )
    )
    other_narrative = [
        fact.get("title")
        for fact in payload.get("facts", [])
        if NARRATIVE_TAG in split_tags(fact.get("tags"))
        and str(fact.get("source_url", "")) not in NARRATIVE_URLS
    ]
    results.append(
        (
            "没有把新闻/公告误标成叙事",
            not other_narrative,
            "、".join(map(str, other_narrative)),
        )
    )

    note = str(payload.get("note", ""))
    results.append(
        (
            "种子 note 记录人工录入条数",
            (NOTE_SUFFIX % len(MANUAL_FACTS)) in note,
            note,
        )
    )

    stale = [
        fact.get("title")
        for fact in payload.get("facts", [])
        if "口径未知" in _tags_to_text(fact.get("tags"))
    ]
    results.append(("种子里不再有「口径未知」旧标", not stale, ", ".join(map(str, stale))))
    return results


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    seed = DEFAULT_SEED
    if "--file" in args:
        position = args.index("--file")
        if position + 1 >= len(args):
            print("--file 后面要跟一个种子文件路径")
            return 2
        seed = Path(args[position + 1])
    payload = load_payload(seed)
    facts = payload.get("facts", [])

    if "--show" in args or not ({"--apply", "--check"} & set(args)):
        print(f"种子：{seed}")
        print(f"事实：{len(facts)} 条 / 文档：{len(payload.get('documents', []))} 篇")
        index = fact_index(payload)
        for spec in MANUAL_FACTS:
            fresh = build_fact(spec)
            current = index.get(fresh["title"])
            state = "已录入" if current else "缺失"
            print(f"  [{state}] {fresh['title']}　可信度 {fresh['confidence']}")
            if current:
                print(f"      答案：{current.get('answer')}")
            print(f"      为什么：{spec.get('why', '')}")
        for rule in NOTE_UPDATES:
            done = sum(
                1
                for title in rule["titles"]
                if str(rule["new"]) in _tags_to_text((index.get(title) or {}).get("tags"))
            )
            print(f"  [标注 {done}/{len(rule['titles'])}] {rule['why']}")
        for rule in FACT_UPDATES:
            fact = index.get(str(rule["title"])) or {}
            same = fact.get("answer") == rule.get("answer")
            print(f"  [更正 {'已落地' if same else '待落地'}] {rule['title']}")
            print(f"      应为：{rule['answer']}")
            if fact:
                print(f"      现状：{fact.get('answer')}")
            print(f"      为什么：{rule.get('why', '')}")
        narrative = [
            fact.get("title")
            for fact in facts
            if NARRATIVE_TAG in split_tags(fact.get("tags"))
        ]
        print(f"  [叙事来源 {len(narrative)} 条] {NARRATIVE_URLS[0]}　不参与字段投票")
        for title in narrative:
            print(f"      · {title}")
        return 0

    if "--apply" in args:
        report = apply_manual(payload)
        save_payload(payload, seed)
        print(f"已写回种子：{seed}")
        print(f"  新增 {len(report['added'])}：{'、'.join(report['added']) or '无'}")
        print(
            f"  更新 {len(report['updated'])}："
            + ("；".join(f"{t}({','.join(k)})" for t, k in report["updated"]) or "无")
        )
        print(f"  事实更正 {len(report['fixed'])}：{'、'.join(report['fixed']) or '无'}")
        print(f"  口径标注 {len(report['noted'])} 条：{'、'.join(report['noted']) or '无'}")
        print(
            f"  叙事来源标注 {len(report['narrative_facts'])} 条事实 / "
            f"{len(report['narrative_docs'])} 篇文档"
        )
        print(f"  事实总数：{len(payload.get('facts', []))}")
        if report["missing"]:
            print(f"  ⚠️ 找不到的事实：{'、'.join(report['missing'])}")
        return 0

    failures = [(name, detail) for name, ok, detail in check(payload) if not ok]
    total = len(check(payload))
    for name, detail in failures:
        print(f"  ❌ {name}　{detail}")
    print(f"人工层校验：{total - len(failures)}/{total} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
