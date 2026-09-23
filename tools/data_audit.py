"""知识库脏数据审计：跨实体文案复制 / 实体归属错位 / 占位碎片 / 跨标题重复 / 跨作品污染。

`dedupe` 只处理「同一实体·同一字段」的重复与数值冲突，管不了
「A 的字段里写着 B 的内容」这类**错位**——它既不重复也不冲突，只能靠对照实体名、
页面来源和字段值分布查出来。

    python tools\\data_audit.py                                     # 审计 seed/seed_kb.json，打印摘要
    python tools\\data_audit.py --file seed\\seed_kb.json --out .audit\\report.txt
    python tools\\data_audit.py --apply                             # 执行人工裁定过的清理（删确证错位 + 打存疑标记）

审计类别（A/B/C 是「该修」的，D/E/F/G 只提示、不做自动处置）：
    A 跨实体整段复制   不同实体的两条事实，正文（去模板前缀后，≥40 字）相似度 ≥ 0.99
    B 实体归属错位     正文里自己的名字出现 0 次、别的实体出现 ≥ 2 次
    C 占位/碎片答案    空、< 2 字、纯标点，或光秃秃一个百分比（例如「40.0%」）
    D 跨标题重复答案   不同标题的两条事实正文完全相同（≥ 8 字）——同一说法本就可能属于多个实体，只提示
    E 跨作品污染       正文出现其它游戏/作品的专有名词（复用 app.core.quality.FOREIGN_ENTITIES）
    F 唯一性字段撞值   CV/生日/技能名这类字段在两个实体上取了同一个值（提示人工看一眼）
    G 跨游戏词命中     正文出现 CROSS_GAME_TERMS 里登记过的别的作品的专有名词/技能名（带出处）

`初始生命=1330` 这类**纯数字**是正常的字段值，不算碎片答案。

--apply 只执行「人工裁定过」的规则（见下方 REMOVE_FACTS / FLAG_SOURCES），不做自动推断删除：
删除会丢真数据，标注可以撤回，代价不对称。

中文结果写 UTF-8 报告文件（控制台 GBK 直接打印会乱码）。
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.quality import FOREIGN_ENTITIES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".audit"

COPY_THRESHOLD = 0.99
COPY_MIN_CHARS = 40
OTHER_MIN_MENTIONS = 2
PLACEHOLDER_CHARS = 2
DUP_MIN_CHARS = 8
# 这些字段在不同角色间本来就该各有各的值，撞值要人看一眼
UNIQUE_FIELDS = ("CV", "生日", "异能", "异能/契约", "奥义1", "战技1", "特性1", "普攻1", "人物故事")

# ----------------------------------------------------------------------
# 别的作品的专有名词 / 技能名（命中即「串味」，带出处）
# ----------------------------------------------------------------------
# 2026-09-22 实证：BWIKI 异环 wiki 的「薄荷」页把《银与血》（银与绯，BWIKI 上是 yyf wiki）
# 角色「星灭者-莱夏」的整套技能抄了进来 —— 奥义「美梦破灭顷刻」、战技「第四墙破障」、
# 特性「元气祷告」、状态「元气虚像」全部对得上，而且薄荷页的奥义正文直接写着
# 「我方角色每施放1次奥义，莱夏的奥义消耗减1」。
# 对照证据：https://www.9game.cn/news/10892950.html 《银与血星灭者莱夏技能介绍》
CROSS_GAME_TERMS: dict[str, str] = {
    "银与血": "《银与血》（银与绯）",
    "银与绯": "《银与血》（银与绯）",
    "莱夏": "《银与血》角色「星灭者-莱夏」",
    "第四墙破障": "《银与血》莱夏 战技",
    "美梦破灭顷刻": "《银与血》莱夏 奥义",
    "元气祷告": "《银与血》莱夏 特性",
    "元气虚像": "《银与血》莱夏 特性状态",
    "血源爆发": "《银与血》的技能体系",
}

# ----------------------------------------------------------------------
# 人工裁定过的清理规则（每条都带证据，脚本不做自动推断删除）
# ----------------------------------------------------------------------

# 确证错位 → 直接删除
_YINXUE_KIT_REASON = (
    "BWIKI 异环 wiki 的薄荷页把《银与血》（银与绯）角色「星灭者-莱夏」的整套技能抄了进来："
    "奥义「美梦破灭顷刻」、战技「第四墙破障」、特性「元气祷告」、状态「元气虚像」逐项对得上，"
    "薄荷页奥义正文还写着「莱夏的奥义消耗减1」"
    "（对照 https://www.9game.cn/news/10892950.html 《银与血星灭者莱夏技能介绍》），"
    "属跨游戏污染，不是《异环》薄荷的数据"
)
REMOVE_FACTS: dict[str, str] = {
    "薄荷·战技1": _YINXUE_KIT_REASON,
    "薄荷·战技1效果": _YINXUE_KIT_REASON,
    "薄荷·战技1进阶1": _YINXUE_KIT_REASON,
    "薄荷·战技1进阶2": _YINXUE_KIT_REASON,
    "薄荷·普攻1效果": _YINXUE_KIT_REASON,
    "薄荷·特性1": _YINXUE_KIT_REASON,
    "薄荷·特性1效果": _YINXUE_KIT_REASON,
    "薄荷·奥义1": _YINXUE_KIT_REASON,
    "薄荷·奥义1效果": _YINXUE_KIT_REASON,
    "薄荷·奥义1进阶1": _YINXUE_KIT_REASON,
    "薄荷·奥义1进阶2": _YINXUE_KIT_REASON,
    "薄荷·人物故事": (
        "与「早雾·人物故事」整段相同（正文相似度 0.99，正文里「薄荷」出现 0 次、「早雾」8 次、"
        "「娜娜莉」5 次），系 BWIKI 薄荷页复制了早雾页的人物故事"
    ),
    "薄荷·CV": (
        "BWIKI 薄荷页的 CV「宋媛媛」与娜娜莉页完全相同，且该页生日也误写成娜娜莉的 8月20日"
        "（已由维护者裁定改为玩一玩的 6月1日），属模板残留；薄荷真实 CV 目前没有可靠来源"
    ),
}

# 字段级存疑 → 追加「存疑:…」标签（保留内容，回答与体检时可见）
FLAG_FACTS: dict[str, str] = {
    "薄荷·类型": "存疑:来源页（BWIKI薄荷页）已证伪，无法判断该字段是否也来自模板",
    "薄荷·战斗类型": "存疑:来源页（BWIKI薄荷页）已证伪，无法判断该字段是否也来自模板",
    "薄荷·弧盘适配": "存疑:来源页（BWIKI薄荷页）已证伪，无法判断该字段是否也来自模板",
    "薄荷（角色图鉴）": "存疑:取自 BWIKI 角色图鉴页，与已证伪的薄荷页取值一致，待独立来源复核",
    "九原·奥义1进阶2": (
        "存疑:与已证伪的 BWIKI 薄荷页「奥义1进阶2」逐字相同，且与九原自己的「奥义1进阶1」"
        "倍率写法不一致（九原是灵属性却写成「物理」），疑似同一处模板残留"
    ),
}

# 审计发现过、但已被维护者裁定**更正**的条目：不再标存疑，值也改成了交叉验证值。
# 留这张表，审计报告才能回答「当初那条撞值后来怎么处理的」。
REVIEWED_AND_FIXED: dict[str, str] = {
    "九原·CV": (
        "2026-09-22 维护者裁定：原「与娜娜莉页 CV 撞值」是模板残留/录入错位，"
        "已按萌娘百科/百度百科交叉验证更正为「张安琪（中）/田中理惠（日）」（tools/manual_facts.py 的 FACT_UPDATES）"
    ),
    "娜娜莉·CV": (
        "2026-09-22 同日补充为「宋媛媛（中）/竹达彩奈（日）」，与九原不再共用同一个名字"
    ),
}

# 页面级存疑 → 追加「存疑:…」标签（保留内容，回答与体检时可见）
FLAG_SOURCES: dict[str, str] = {
    "https://wiki.biligame.com/yh/%E8%96%84%E8%8D%B7": (
        "存疑:BWIKI薄荷页施工中，已确认整段技能抄自《银与血》莱夏、人物故事抄自早雾页，"
        "生日与 CV 与娜娜莉页相同；剩余字段（类型/战斗类型/弧盘适配）待独立来源复核"
    ),
}
FLAG_NOTE = "（2026-09-22 脏数据审计）"


def _option(args: list, name: str, default: str = "") -> str:
    if name in args:
        index = args.index(name)
        if index + 1 < len(args):
            return args[index + 1]
    return default


def strip_template(answer: str) -> str:
    """去掉「实体 的<字段>为：」这类模板前缀，只看正文。"""
    text = str(answer or "")
    head, sep, tail = text.partition("：")
    if sep and len(head) <= 40:
        return tail.strip()
    head, sep, tail = text.partition(":")
    if sep and len(head) <= 40:
        return tail.strip()
    return text.strip()


def _bigrams(text: str) -> Counter:
    clean = re.sub(r"\s+", "", text)
    return Counter(clean[i:i + 2] for i in range(max(0, len(clean) - 1)))


def _similarity(left: Counter, right: Counter) -> float:
    if not left or not right:
        return 0.0
    return 2 * sum((left & right).values()) / (sum(left.values()) + sum(right.values()))


def entities_of(facts: list) -> set:
    """只在「实体·字段」标题里取实体名。

    表格事实的标题形如「XXX（页面名）」，括号里是来源页名而不是实体，混进来会产生
    「浔的人物故事里出现了『方斯』」这类假阳性（方斯是货币）。
    """
    names = set()
    for fact in facts:
        title = str(fact.get("title") or "")
        if "·" in title:
            names.add(title.partition("·")[0].strip())
    return {name for name in names if len(name) >= 2}


def audit(facts: list) -> dict:
    entities = entities_of(facts)
    report = {
        "facts": len(facts),
        "entities": sorted(entities),
        "A_copies": [],
        "B_misattribution": [],
        "C_placeholder": [],
        "D_duplicate": [],
        "E_foreign": [],
        "F_collision": [],
        "G_crossgame": [],
    }

    bodies = []
    for fact in facts:
        title = str(fact.get("title") or "")
        entity = title.partition("·")[0].strip() if "·" in title else ""
        body = strip_template(fact.get("answer"))
        bodies.append((fact, title, entity, body, _bigrams(body)))

    # A 跨实体整段复制
    for i in range(len(bodies)):
        fact_a, title_a, ent_a, body_a, gram_a = bodies[i]
        if len(body_a) < COPY_MIN_CHARS:
            continue
        for j in range(i + 1, len(bodies)):
            fact_b, title_b, ent_b, body_b, gram_b = bodies[j]
            if ent_a == ent_b or len(body_b) < COPY_MIN_CHARS:
                continue
            score = _similarity(gram_a, gram_b)
            if score >= COPY_THRESHOLD:
                report["A_copies"].append({
                    "score": round(score, 4),
                    "left": title_a,
                    "right": title_b,
                    "left_url": fact_a.get("source_url", ""),
                    "right_url": fact_b.get("source_url", ""),
                    "body": body_a[:200],
                })

    # B 实体归属错位
    for fact, title, entity, body, _gram in bodies:
        if not entity:
            continue
        own = body.count(entity)
        others = {
            name: body.count(name)
            for name in entities
            if name != entity and len(name) >= 2 and body.count(name) >= OTHER_MIN_MENTIONS
        }
        if own == 0 and others:
            report["B_misattribution"].append({
                "title": title,
                "others": others,
                "url": fact.get("source_url", ""),
                "body": body[:200],
            })

    # C 占位/碎片答案（纯数字是正常字段值，不算碎片）
    for fact, title, _entity, body, _gram in bodies:
        if not body:
            continue
        fragment = (
            len(body) < PLACEHOLDER_CHARS
            or re.fullmatch(r"[\W_]+", body) is not None
            or re.fullmatch(r"\d+(\.\d+)?%", body) is not None
        )
        if fragment:
            report["C_placeholder"].append({
                "title": title,
                "body": body,
                "url": fact.get("source_url", ""),
            })

    # D 跨标题重复答案
    grouped: dict[str, list] = defaultdict(list)
    for fact, title, _entity, body, _gram in bodies:
        key = re.sub(r"\s+", "", body)
        if len(key) >= DUP_MIN_CHARS:
            grouped[key].append({"title": title, "url": fact.get("source_url", "")})
    for key, group in grouped.items():
        if len({item["title"] for item in group}) > 1:
            report["D_duplicate"].append({"body": key[:200], "items": group})

    # E 跨作品污染
    for fact, title, _entity, body, _gram in bodies:
        hits = sorted({name for name in FOREIGN_ENTITIES if name in body})
        if hits:
            report["E_foreign"].append({"title": title, "hits": hits, "url": fact.get("source_url", "")})

    # F 唯一性字段撞值
    field_values: dict[tuple, list] = defaultdict(list)
    for fact, title, _entity, body, _gram in bodies:
        if "·" not in title:
            continue
        entity, _, field = title.partition("·")
        if field in UNIQUE_FIELDS:
            field_values[(field, body)].append(entity.strip())
    for (field, value), names in sorted(field_values.items()):
        if len(names) > 1:
            report["F_collision"].append({"field": field, "value": value[:60], "entities": sorted(names)})

    # G 跨游戏词命中（别的作品的专有名词/技能名）
    for fact, title, _entity, body, _gram in bodies:
        blob = f"{title} {body}"
        hits = sorted(
            f"{term}→{note}" for term, note in CROSS_GAME_TERMS.items() if term in blob
        )
        if hits:
            report["G_crossgame"].append({"title": title, "hits": hits, "url": fact.get("source_url", "")})

    report["counts"] = {
        key: len(report[key])
        for key in (
            "A_copies",
            "B_misattribution",
            "C_placeholder",
            "D_duplicate",
            "E_foreign",
            "F_collision",
            "G_crossgame",
        )
    }
    return report


def render(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"事实总数 {report['facts']}，实体 {len(report['entities'])} 个")
    lines.append("类别计数：" + "，".join(f"{key}={value}" for key, value in report["counts"].items()))

    lines.append("\n=== A 跨实体整段复制（≥0.99） ===")
    for index, item in enumerate(report["A_copies"], start=1):
        lines.append(f"[{index:02d}] {item['score']} | {item['left']} <-> {item['right']}")
        lines.append(f"     {item['left_url']}")
        lines.append(f"     {item['right_url']}")
        lines.append(f"     {item['body']}")

    lines.append("\n=== B 实体归属错位（自己 0 次、别人 ≥2 次） ===")
    for index, item in enumerate(report["B_misattribution"], start=1):
        lines.append(f"[{index:02d}] {item['title']} | 别人={item['others']} | {item['url']}")
        lines.append(f"     {item['body']}")

    lines.append("\n=== C 占位/碎片答案 ===")
    for index, item in enumerate(report["C_placeholder"], start=1):
        lines.append(f"[{index:02d}] {item['title']} | {item['body']!r} | {item['url']}")

    lines.append("\n=== D 跨标题重复答案 ===")
    for index, item in enumerate(report["D_duplicate"], start=1):
        lines.append(f"[{index:02d}] {item['body']}")
        for entry in item["items"]:
            lines.append(f"     {entry['title']} | {entry['url']}")

    lines.append("\n=== E 跨作品污染 ===")
    for index, item in enumerate(report["E_foreign"], start=1):
        lines.append(f"[{index:02d}] {item['title']} | {item['hits']} | {item['url']}")

    lines.append("\n=== F 唯一性字段撞值 ===")
    for index, item in enumerate(report["F_collision"], start=1):
        lines.append(f"[{index:02d}] {item['field']} = {item['value']!r} -> {item['entities']}")

    lines.append("\n=== G 跨游戏词命中（别的作品的专有名词/技能名） ===")
    for index, item in enumerate(report["G_crossgame"], start=1):
        lines.append(f"[{index:02d}] {item['title']} | {item['hits']} | {item['url']}")
    return "\n".join(lines)


def _tags_to_text(tags) -> str:
    if isinstance(tags, (list, tuple)):
        return ", ".join(str(item).strip() for item in tags if str(item).strip())
    return str(tags or "")


def _flag(fact: dict, note: str) -> bool:
    """给事实追加「存疑:…」标签；已经标过就不重复加。返回是否改动。"""
    tags = _tags_to_text(fact.get("tags"))
    marker = note + FLAG_NOTE
    if marker in tags:
        return False
    fact["tags"] = f"{tags}, {marker}" if tags else marker
    return True


def apply_cleanup(payload: dict) -> dict:
    """按 REMOVE_FACTS / FLAG_FACTS / FLAG_SOURCES 就地清理，返回统计。"""
    facts = payload.get("facts") or []
    kept: list[dict] = []
    removed: list[dict] = []
    flagged = 0
    flagged_titles: list[str] = []
    for fact in facts:
        title = str(fact.get("title") or "")
        if title in REMOVE_FACTS:
            removed.append({"title": title, "reason": REMOVE_FACTS[title], "url": fact.get("source_url", "")})
            continue
        changed = False
        title_note = FLAG_FACTS.get(title)
        if title_note and _flag(fact, title_note):
            changed = True
        source_note = FLAG_SOURCES.get(str(fact.get("source_url") or ""))
        if source_note and _flag(fact, source_note):
            changed = True
        if changed:
            flagged += 1
            flagged_titles.append(title)
        kept.append(fact)
    payload["facts"] = kept
    return {
        "removed": removed,
        "flagged": flagged,
        "flagged_titles": flagged_titles,
        "before": len(facts),
        "after": len(kept),
    }


def main() -> int:
    args = sys.argv[1:]
    seed_path = Path(_option(args, "--file", str(ROOT / "seed" / "seed_kb.json")))
    out_path = _option(args, "--out")
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    facts = payload.get("facts") or []

    if "--apply" in args:
        stats = apply_cleanup(payload)
        # --apply 会真的删条目，先留一份 .bak（种子文件是发货内容，删掉只能靠 git 找回）。
        backup = seed_path.with_suffix(seed_path.suffix + ".bak")
        backup.write_text(seed_path.read_text(encoding="utf-8"), encoding="utf-8")
        seed_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"清理完成：事实 {stats['before']} → {stats['after']}（删除 {len(stats['removed'])} 条、打存疑标记 {stats['flagged']} 条）")
        print(f"原文件已备份为：{backup}")
        for item in stats["removed"]:
            print(f"  - 删除 {item['title']}：{item['reason']}")
        if stats["flagged_titles"]:
            print("  - 打存疑标记：" + "、".join(stats["flagged_titles"]))
        facts = payload.get("facts") or []

    report = audit(facts)
    text = render(report)
    print(f"审计完成：{seed_path}")
    print("类别计数：" + "，".join(f"{key}={value}" for key, value in report["counts"].items()))
    if out_path:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"报告已写入 {target}")
    else:
        WORK.mkdir(parents=True, exist_ok=True)
        target = WORK / "report.txt"
        target.write_text(text, encoding="utf-8")
        print(f"报告已写入 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
