"""人工录入层：把「用户确认过、但抓不到可靠来源」的事实与人工裁定标注写进种子。

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

可信度：两条人工事实都是 `source_type="manual"` + `extraction="manual"`，按
`app/core/trust.py` 的公式 = 0.45×0.95 + 0.25×0.95 + 0.20×0.6 + 0.10×时效。
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import json
import sys
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
NOTE_SUFFIX = "；另含 %d 条人工录入事实（tools/manual_facts.py，2026-09-22 用户确认）"
NOTE_MARK = "；另含 "


# --------------------------------------------------------------------------- #
# 人工录入的事实：用户确认、但公开可抓来源拿不到
# --------------------------------------------------------------------------- #
MANUAL_FACTS: List[Dict[str, Any]] = [
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
        "why": (
            "用户 2026-09-22 说「摄心特刊保底次数与其它特刊保底次数一致」；"
            "17173 转载的官方公众号公告写明 6 次/8 次保底、保底次数在所有限定特刊之间完全共享。"
            "概率数值任何来源都查不到，所以题面里不含概率。"
        ),
    },
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
            "原「与娜娜莉页 CV 撞值」是模板残留/录入错位。"
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
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
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
            published_at=spec.get("published_at", ""),
            title=title,
            answer=answer,
        ),
        "extraction": str(spec.get("extraction", "manual")),
        "version": str(meta.get("version", "")),
        "effective_from": str(meta.get("effective_from", "")),
        "date_kind": str(meta.get("date_kind", "")),
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
        for key in ("answer", "source_url", "source_type", "extraction", "confidence", "version"):
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
