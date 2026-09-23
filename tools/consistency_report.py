"""跨来源一致性体检：同一件事在几个独立站点上说法一样吗？

和 `dedupe_report.py` 的分工：
- `dedupe_report.py` 管的是**同一条事实**有没有被抄成两条（重复/合并）；
- 本工具管的是**同一个槽位**（实体+字段）在不同独立来源上的数值是否一致——
  ≥2 个域名说法一致 → 记「多源确认」(+0.08)；说法不一致且没有版本/日期区分 → 标冲突、两条都留。

判例默认**只报告不落盘**（`--apply` 才写回库），因为假阳性代价高：
早期用「实体 + 多值」朴素比法跑过 11 + 19 组候选，**全是假阳性**。
所以这里的口径加了四道闸门（同槽位 / 值型答案 / 至少两位数字 / 版本不同不算冲突），
并且把每一个判例连同来源域名一起打出来，供人工过一遍再决定是否落盘。

    python tools\\consistency_report.py                          # 扫 seed/seed_kb.json，只报告
    python tools\\consistency_report.py --verdict conflict        # 只看冲突判例
    python tools\\consistency_report.py --data-dir .eval_data     # 扫本地库
    python tools\\consistency_report.py --data-dir .eval_data --apply   # 真写回（加多源确认/标冲突）
    python tools\\consistency_report.py --out .consistency\\report.txt

中文结果写 UTF-8 报告文件（控制台 GBK 直接打印会乱码）。
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import consistency  # noqa: E402
from app.core.store import KnowledgeBase  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".consistency"


def _option(args: list, name: str, default: str = "") -> str:
    if name in args:
        index = args.index(name)
        if index + 1 < len(args):
            return args[index + 1]
    return default


def load_seed_facts(path: Path) -> list:
    payload = json.loads(path.read_text(encoding="utf-8"))
    facts = []
    for index, fact in enumerate(payload.get("facts") or [], start=1):
        facts.append({
            "id": index,
            "title": fact.get("title", ""),
            "answer": fact.get("answer", ""),
            "source_url": fact.get("source_url", ""),
            "source_type": fact.get("source_type", ""),
            "confidence": float(fact.get("confidence") or 0.0),
            "extraction": fact.get("extraction", ""),
            "version": fact.get("version", ""),
            "effective_from": fact.get("effective_from", ""),
            "tags": fact.get("tags", ""),
            "status": "active",
        })
    return facts


def main() -> int:
    args = sys.argv[1:]
    seed_file = Path(_option(args, "--file", str(ROOT / "seed" / "seed_kb.json")))
    data_dir = _option(args, "--data-dir", "")
    out_file = Path(_option(args, "--out", str(WORK / "report.txt")))
    only = _option(args, "--verdict", "all")
    show = int(_option(args, "--show", "30") or 30)
    min_domains = int(_option(args, "--min-domains", str(consistency.MIN_DOMAINS)) or 2)
    apply_changes = "--apply" in args
    lines: list = []

    def say(message: str = "") -> None:
        lines.append(message)

    kb = None
    if data_dir:
        base = Path(data_dir)
        db_file = base / "kb" / "knowledge.db"
        if not db_file.exists():
            db_file = base / "knowledge.db"
        kb = KnowledgeBase(db_file=db_file)
        facts = kb.list_facts(limit=100000)
        source_label = f"本地库 {db_file}"
    else:
        if not seed_file.exists():
            print(f"seed not found: {seed_file}")
            return 2
        facts = load_seed_facts(seed_file)
        source_label = f"种子文件 {seed_file}"

    verdicts = consistency.vote(facts, min_domains=min_domains)
    summary = consistency.summarize(verdicts)
    changes = consistency.apply(kb, verdicts, dry_run=True) if kb is not None else None

    rule = consistency.describe()
    say(f"来源：{source_label}")
    say(f"条目总数：{len(facts)}　参与投票的槽位：{summary['slots']}")
    say(
        f"多源一致 {summary['multi']}　冲突 {summary['conflict']}　"
        f"版本不同（不判冲突）{summary['versioned']}　单一来源 {summary['single']}"
    )
    if changes is not None:
        say(
            f"若不落盘直接写回，将：加「多源确认」{changes['confirmed']} 条、"
            f"标记冲突 {changes['conflicts']} 条（已标记过 {changes['skipped']} 条不动）"
        )
    say("")
    say("判定口径（宁缺勿错，四道闸门）：")
    for item in rule["rules"]:
        say(f"  - {item}")
    say("")

    wanted = [v for v in verdicts if only == "all" or v["verdict"] == only]
    for index, item in enumerate(wanted[:show], start=1):
        say(f"[{index}] {item['verdict']}｜{item['slot']}｜{item['title']}")
        for fact in item["facts"]:
            value = str(fact["answer"] or "").replace("\n", " ")[:90]
            say(f"      · {value}")
            say(f"        来源：{fact['source_url'] or '（无）'}　域名 {fact['domain'] or '（无）'}"
                f"　可信度 {fact['confidence']}　id={fact['id']}")
        if item["verdict"] == consistency.VERDICT_VERSIONED:
            say(f"      版本/生效时间：{'　'.join(item['version_keys'])}")
    if len(wanted) > show:
        say(f"…… 另有 {len(wanted) - show} 组未列出（用 --show N 调整）")

    if apply_changes:
        if kb is None:
            say("")
            say("⚠️ 只有 --data-dir 的本地库能写回；种子文件请改完再用 seed_builder 重建。")
        else:
            applied = consistency.apply(kb, verdicts, dry_run=False)
            say("")
            say(f"已写回：多源确认 +{applied['confirmed']}、冲突标记 +{applied['conflicts']}、"
                f"跳过（已标记）{applied['skipped']}")
    else:
        say("")
        say("（默认只报告不落盘；确认判例无误后加 --apply 写回本地库。）")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(lines), encoding="utf-8")
    if kb is not None:
        kb.close()
    print(
        f"facts={len(facts)} slots={summary['slots']} multi={summary['multi']} "
        f"conflict={summary['conflict']} versioned={summary['versioned']} "
        f"single={summary['single']} report -> {out_file}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
