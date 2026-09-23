"""离线核对评测集的「期望要点」在本地知识库里到底有没有依据（不花钱、不联网）。

背景
----
评测集的题目大多是从知识库自动生成的，但生成之后知识库还会变（换种子、加结构化字段、
剔除模板垃圾）。一旦某题的期望要点在库里根本不存在，这题就会**稳定地**答不出来，
看起来像「检索变差了」，其实是题与库不匹配。人工逐题核对 45 道题很费时间，这里用两个
可解释的信号先筛一遍，把候选人交给人工确认：

1. **数字缺失**：要点里的数字（如 `86000`、`1.23%`）在库中出现不了 —— 数值型要点的强信号；
2. **措辞缺失**：要点的字符二元组在库中的覆盖率低于阈值 —— 换个说法写的事实会被误报，
   所以只当提示，不当结论。

输出分三档：`有据` / `部分缺失` / `疑似缺口`。它**只做筛查**：
`--include-drop` 可以把 review.status=drop 的题也纳入核对。

用法：
    python tools\\kb_gap_report.py
    python tools\\kb_gap_report.py --set eval\\eval_set.json --min-support 0.7
    python tools\\kb_gap_report.py --include-drop --json-out eval\\kb_gap-20260922.json
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import paths  # noqa: E402

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(text: str) -> List[str]:
    """抽出文本里的数字，并做与 app/core/dedupe.py 一致的规范化。

    不规范化的后果是**假缺口**：库里若写的是 `86,000`、要点写 `86000`，
    直接比字符串就会报「数字缺失」，把有据的题误判成源侧缺口。
    千分位去掉、小数尾零去掉，让 `86,000`/`1.20%` 与 `86000`/`1.2%` 可比。
    """
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    out: List[str] = []
    for token in NUMBER_RE.findall(text):
        if "." in token:
            token = token.rstrip("0").rstrip(".") or "0"
        out.append(token)
    return out


def _bigrams(text: str) -> set:
    """字符二元组集合（中文短文本最稳的粗粒度重合度度量）。"""
    compact = "".join(ch for ch in text if not ch.isspace())
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def build_corpus(seed: Dict[str, Any]) -> Dict[str, Any]:
    """把种子库摊平成「一段文本 + 二元组集合 + 数字集合」。"""
    parts: List[str] = []
    for fact in seed.get("facts") or []:
        parts.append(f"{fact.get('title') or ''} {fact.get('answer') or ''}")
    for doc in seed.get("documents") or []:
        parts.append(str(doc.get("title") or ""))
        parts.extend(str(chunk) for chunk in (doc.get("chunks") or []))
    text = "\n".join(parts)
    return {
        "text": text,
        "bigrams": _bigrams(text),
        "numbers": set(numbers_in(text)),
        "chars": len(text),
    }


def judge_point(point: str, corpus: Dict[str, Any], min_support: float) -> Dict[str, Any]:
    numbers = numbers_in(point)
    missing_numbers = [n for n in numbers if n not in corpus["numbers"]]
    grams = _bigrams(point)
    support = (
        sum(1 for gram in grams if gram in corpus["bigrams"]) / len(grams) if grams else 0.0
    )
    if missing_numbers:
        verdict = "数字缺失"
    elif support < min_support:
        verdict = "措辞缺失"
    else:
        verdict = "有据"
    return {
        "point": point,
        "verdict": verdict,
        "support": round(support, 3),
        "missing_numbers": missing_numbers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="核对评测集要点在本地库里有没有依据")
    parser.add_argument("--set", default="eval/eval_set.json", help="评测集路径")
    parser.add_argument("--seed", default="", help="种子库路径（默认用 seed/seed_kb.json）")
    parser.add_argument("--min-support", type=float, default=0.6, help="措辞覆盖率阈值")
    parser.add_argument("--include-drop", action="store_true", help="把 review.status=drop 的题也纳入")
    parser.add_argument("--json-out", default="", help="把结果写成 JSON")
    args = parser.parse_args()

    root = paths.project_root()
    set_path = Path(args.set)
    if not set_path.is_absolute():
        set_path = root / set_path
    seed_path = Path(args.seed) if args.seed else paths.seed_kb_path()
    if not seed_path.is_absolute():
        seed_path = root / seed_path
    if not set_path.exists() or not seed_path.exists():
        print(f"找不到文件：{set_path} / {seed_path}")
        return 2

    payload = json.loads(set_path.read_text(encoding="utf-8"))
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    corpus = build_corpus(seed)

    items = payload.get("items") or []
    if not args.include_drop:
        items = [i for i in items if (i.get("review") or {}).get("status") != "drop"]

    print("=" * 74)
    print(f"评测集：{set_path.name}　题目 {len(items)} 道")
    print(f"知识库：{seed_path.name}　{len(seed.get('facts') or [])} 条条目 / "
          f"{len(seed.get('documents') or [])} 篇文档 / {corpus['chars']} 字")
    print("=" * 74)

    rows: List[Dict[str, Any]] = []
    for item in items:
        points = item.get("expected_points") or []
        checked = [judge_point(str(p), corpus, args.min_support) for p in points]
        if not points:
            overall = "无要点"
        elif all(c["verdict"] == "有据" for c in checked):
            overall = "有据"
        elif any(c["verdict"] == "有据" for c in checked):
            overall = "部分缺失"
        else:
            overall = "疑似缺口"
        rows.append(
            {
                "id": item.get("id"),
                "question": item.get("question"),
                "status": (item.get("review") or {}).get("status"),
                "kb_gap": bool(item.get("kb_gap")),
                "overall": overall,
                "points": checked,
            }
        )

    for row in rows:
        mark = {"有据": "✅", "部分缺失": "⚠️", "疑似缺口": "❌", "无要点": "·"}[row["overall"]]
        print(f"\n{mark} [{row['id']}] {row['question']}")
        for point in row["points"]:
            detail = f"覆盖 {point['support']:.2f}"
            if point["missing_numbers"]:
                detail += f"　缺数字 {'/'.join(point['missing_numbers'])}"
            flag = {"有据": "·", "措辞缺失": "?", "数字缺失": "!"}[point["verdict"]]
            print(f"    {flag} {point['point'][:56]}　（{point['verdict']}，{detail}）")

    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["overall"]] = counts.get(row["overall"], 0) + 1
    print("\n" + "=" * 74)
    print("汇总：" + "　".join(f"{key} {value}" for key, value in sorted(counts.items())))

    suspects = [r for r in rows if r["overall"] == "疑似缺口" or r["overall"] == "部分缺失"]
    if suspects:
        print("\n需要人工确认的题（敲定后到评测集里补 kb_gap + review.comment）：")
        for row in suspects:
            bad = [p["point"] for p in row["points"] if p["verdict"] != "有据"]
            print(f"  · [{row['id']}] {row['question'][:44]}　→ {'；'.join(bad)[:80]}")
    print("\n注意：本工具只做筛查（数字缺失是强信号，措辞缺失可能只是换了说法），"
          "结论要由人工核对后写进评测集；`--include-drop` 可把 drop 的题也纳入。")

    if args.json_out:
        out = Path(args.json_out)
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {"eval_set": set_path.name, "seed": seed_path.name, "rows": rows},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"\n明细已写入：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
