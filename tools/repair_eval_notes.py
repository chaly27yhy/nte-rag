"""把人工写在 eval_set.json 里的中文括注迁移到 review.comment。

背景
----
JSON 不支持注释。人工审核时会在行尾写 `（此处错误，不止4个）` 这样的批注，
结果整个文件无法解析，评测脚本直接报 JSONDecodeError。

本工具做两件事：
1. 只在**字符串之外**识别行尾括注并删除（字符串里的 `（Ctrl+F5）` 这类不受影响）；
2. 把批注文本追加到对应题目的 `review.comment` 里，前面加「复核结论：」，
   同时按批注语义给出**建议状态**（不直接改 answerable，交给人确认）。

用法：
    python tools\\repair_eval_notes.py                 # 先看会改什么（dry-run）
    python tools\\repair_eval_notes.py --apply         # 实际写入
    python tools\\repair_eval_notes.py --apply --in-place   # 覆盖原文件（默认写 .fixed.json）
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import paths  # noqa: E402

DEFAULT_SET = "eval/eval_set.json"

NOTE_PREFIX = "复核结论："

# 按批注语义给出的**建议状态**；不直接改 answerable
_SUGGEST_DROP = ("已过时", "过时", "错误", "不对", "不成立")
_SUGGEST_FIX = ("哪次", "时间线未提及", "不明确", "歧义")


def quote_parity(text: str) -> int:
    """统计未被转义的引号数量（奇数说明游标正处于字符串内部）。"""
    count = 0
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            count += 1
    return count


def strip_trailing_note(line: str) -> Tuple[str, str]:
    """去掉行尾「字符串之外」的括注，返回 (清理后的行, 批注文本)。"""
    body = line.rstrip("\n")
    newline = "\n" if line.endswith("\n") else ""

    candidates = [index for index, char in enumerate(body) if char in "（("]
    for position in reversed(candidates):
        if quote_parity(body[:position]) % 2 == 1:
            continue  # 这个括号在字符串内部，是正文的一部分
        end = -1
        for index in range(position + 1, len(body)):
            if body[index] in "）)":
                end = index
                break
        if end < 0:
            continue
        note = body[position + 1 : end].strip()
        return body[:position].rstrip() + newline, note
    return line, ""


def repair(text: str) -> Tuple[str, List[Tuple[int, int, str]]]:
    """返回 (修复后的文本, [(题目序号, 行号, 批注)])。题目序号从 1 开始。"""
    lines = text.splitlines(keepends=True)
    out: List[str] = []
    notes: List[Tuple[int, int, str]] = []
    item_index = 0
    for lineno, line in enumerate(lines, start=1):
        if line.rstrip() == "  {":          # items 数组里每个题目对象的起始行
            item_index += 1
        cleaned, note = strip_trailing_note(line)
        if note:
            notes.append((item_index, lineno, note))
        out.append(cleaned)
    return "".join(out), notes


def suggest(note: str) -> str:
    for keyword in _SUGGEST_DROP:
        if keyword in note:
            return "drop"
    for keyword in _SUGGEST_FIX:
        if keyword in note:
            return "fixed"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移 eval_set.json 里的中文括注")
    parser.add_argument("--set", default=DEFAULT_SET)
    parser.add_argument("--apply", action="store_true", help="实际写入（默认只预览）")
    parser.add_argument("--in-place", action="store_true", help="覆盖原文件")
    args = parser.parse_args()

    path = Path(args.set)
    if not path.is_absolute():
        path = paths.project_root() / path
    if not path.exists():
        print(f"找不到文件：{path}")
        return 2

    original = path.read_text(encoding="utf-8")
    fixed_text, notes = repair(original)

    print(f"文件：{path}")
    print(f"发现 {len(notes)} 条字符串外批注\n")

    try:
        payload = json.loads(fixed_text)
    except json.JSONDecodeError as error:
        print(f"❌ 清理批注后仍然不是合法 JSON：{error}")
        print("   说明文件里还有别的语法问题（例如漏了逗号），需要人工看一下。")
        if args.apply:
            # 只有显式 --apply 才落盘：预览模式下写文件与「默认只预览」不符。
            preview = path.with_name(path.stem + ".cleaned.json")
            preview.write_text(fixed_text, encoding="utf-8")
            print(f"   已写出清理后的文本供排查：{preview}")
        else:
            print("   （预览模式不写文件；加 --apply 才会把清理后的文本写出来供排查）")
        return 1

    items = payload.get("items") or []
    print(f"✅ 清理后 JSON 合法，共 {len(items)} 道题\n")

    applied: List[Dict[str, Any]] = []
    for item_index, lineno, note in notes:
        if not (1 <= item_index <= len(items)):
            print(f"  第 {lineno} 行：批注找不到对应题目，已跳过")
            continue
        item = items[item_index - 1]
        review = item.setdefault("review", {})
        old = str(review.get("comment") or "").strip()
        review["comment"] = (old + "　" if old else "") + NOTE_PREFIX + note
        hint = suggest(note)
        applied.append(
            {
                "id": item.get("id"),
                "行号": lineno,
                "批注": note,
                "原状态": review.get("status"),
                "建议": hint or "（人工判断）",
            }
        )

    if applied:
        print("批注归位情况：")
        for row in applied:
            mark = "→ drop" if row["建议"] == "drop" else ("→ fixed" if row["建议"] == "fixed" else "")
            print(f"  [{row['id']}] 第 {row['行号']} 行：{row['批注'][:56]}{mark}")
        print()

    if not args.apply:
        print("（这是预览。加 --apply 才会写入）")
        return 0

    payload["items"] = items
    payload["note"] = (
        f"主评测集：{len(items)} 道《异环》中文问答，覆盖角色、剧情、玩法、系统、"
        "活动、术语、成就等类别。每题按 expected_points 逐条判分，并用 expected_sources "
        "检验检索是否命中期望来源；answerable 为 false 的题考的是该拒答时能否拒答，"
        "这类题另附 why_unanswerable 说明拒答依据。review.status 记录审核状态：pending "
        "为自动生成、尚未审核，ok 为已确认题目与要点正确，fixed 为审核中修正过要点、"
        "来源或标签，正在参与评测，drop 为剔除、不参与评测。review.comment 记录该题的"
        "复核结论与理由，kb_gap 题必须在此留下说明。审核结果只写进这两个字段：JSON "
        "不支持注释，写在结构外面会让文件无法解析。"
    )
    target = path if args.in_place else path.with_name(path.stem + ".fixed.json")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入：{target}")
    if not args.in_place:
        print(f"确认无误后可覆盖原文件：Copy-Item '{target}' '{path}' -Force")
    return 0


if __name__ == "__main__":
    sys.exit(main())
