"""近似重复/冲突体检：直接扫种子文件或本地库，列出同标题下的重复与数值冲突。

合并是**入库时**生效的，历史数据（老种子里已有的重复条目）
不会自己消失。所以需要一个能对着现有资料跑、给出可核查清单的入口。

    python tools\\dedupe_report.py                          # 扫 seed/seed_kb.json
    python tools\\dedupe_report.py --file seed\\seed_kb.prev.json
    python tools\\dedupe_report.py --file seed\\seed_kb.json --merge   # 就地合并种子文件里的重复
    python tools\\dedupe_report.py --data-dir .eval_data --merge   # 扫本地库并真合并
    python tools\\dedupe_report.py --out .dedupe\\report.txt

中文结果写 UTF-8 报告文件（控制台 GBK 直接打印会乱码）。
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config  # noqa: E402
from app.core import dedupe  # noqa: E402
from app.core.store import KnowledgeBase  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".dedupe"


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
            "tags": fact.get("tags", ""),
            "status": "active",
        })
    return facts


def main() -> int:
    args = sys.argv[1:]
    seed_file = Path(_option(args, "--file", str(ROOT / "seed" / "seed_kb.json")))
    data_dir = _option(args, "--data-dir", "")
    merge = "--merge" in args
    out_file = Path(_option(args, "--out", str(WORK / "report.txt")))
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

    groups = dedupe.duplicate_groups(facts, include_conflict=True)
    duplicates = [group for group in groups if group["action"] == "duplicate"]
    conflicts = [group for group in groups if group["action"] == "conflict"]

    say(f"来源：{source_label}")
    say(f"条目总数：{len(facts)}")
    say(f"重复分组：{len(duplicates)}　冲突分组：{len(conflicts)}")
    by_extraction: dict = {}
    for fact in facts:
        key = fact.get("extraction") or "（空）"
        by_extraction[key] = by_extraction.get(key, 0) + 1
    say("提取方式分布：" + "　".join(f"{key}={value}" for key, value in sorted(by_extraction.items())))
    say("")

    for index, group in enumerate(groups, start=1):
        say(f"[{index}] {group['action']}｜{group['title']}｜{group['reason']}")
        for answer, url in zip(group["answers"], group["sources"]):
            say(f"      · {(answer or '')[:110]}")
            say(f"        来源：{url or '（无）'}")
        if merge and kb is not None and group["action"] == "duplicate":
            rows = [fact for fact in facts if int(fact["id"]) in set(group["ids"])]
            keeper = max(rows, key=lambda row: (float(row.get("confidence") or 0), len(row.get("answer") or "")))
            for row in rows:
                if int(row["id"]) == int(keeper["id"]):
                    continue
                kb.update_fact(int(row["id"]), status="superseded")
                say(f"      合并：{row['id']} → {keeper['id']}（旧条目置为 superseded）")
    if conflicts:
        say("")
        say("冲突条目不会自动合并：数值对不上时谁对不知道，已保留两条并标记「冲突:」待人工复核。")

    # 种子文件也要能就地合并：只靠导入时合并的话，老用户升级 exe 时
    # `ensure_seed()` 看到指纹没变就不会重新导入（api.py:68-90），修好的合并逻辑轮不到他。
    if merge and kb is None:
        payload = json.loads(seed_file.read_text(encoding="utf-8"))
        result = dedupe.merge_seed_facts(payload.get("facts") or [])
        say("")
        if result["merged"]:
            payload["facts"] = result["facts"]
            note = str(payload.get("note") or "").strip()
            if "近似重复已在种子内合并" not in note:
                payload["note"] = f"{note}（同标题近似重复已在种子内合并）".strip()
            # 就地改的是发货用的种子文件，先留一份 .bak（合并结果不可逆）。
            backup = seed_file.with_suffix(seed_file.suffix + ".bak")
            backup.write_text(seed_file.read_text(encoding="utf-8"), encoding="utf-8")
            seed_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            say(f"已写回种子文件：合并 {result['merged']} 条 → {len(result['facts'])} 条（原文件已备份为 {backup.name}）")
            for title, answer in result["groups"]:
                say(f"      · {title} → {(answer or '')[:110]}")
        else:
            say("种子文件里没有可合并的重复条目（未改动文件）")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(lines), encoding="utf-8")
    if kb is not None:
        kb.close()
    print(f"facts={len(facts)} duplicate_groups={len(duplicates)} conflict_groups={len(conflicts)}"
          f" report -> {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
