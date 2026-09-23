"""把人工审核批注落实为评测集里的具体修改（一次性迁移，可重复执行）。

背景
----
人工审核 `eval/eval_set.json` 时写下的批注（见 tools/repair_eval_notes.py 迁移到
review.comment）需要落实成具体动作。本脚本把判断结果**显式写成数据**，便于复核：

- `status`：drop（作废）/ fixed（已修正）/ ok（确认可用）/ pending（待定）
- `question`：按批注补上时间锚点等必要上下文
- `time_sensitive`：标签只对当前知识库快照成立，知识库更新后会失效
- `kb_gap`：标签对当前知识库成立，但公开资料里有该数据 → 属于抽取/知识库缺口

判断依据来自各题的 review.comment 批注。

用法：
    python tools\\apply_review_decisions.py            # 预览
    python tools\\apply_review_decisions.py --apply    # 写入
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import paths  # noqa: E402

DEFAULT_SET = "eval/eval_set.json"

_TIME_SENSITIVE_NOTE = (
    "自动分析：本标签对【当前知识库快照】成立（检索确认库内无相关内容）。"
    "你指出公开预告已提及 → 属于知识库过时。建议更新知识库后把 answerable 改为 true，"
    "并补上期望要点。"
)

_KB_GAP_NOTE = (
    "自动分析：知识库只有该物品的获取方式、没有数值（检索最佳命中即是获取说明）。"
    "你指出公开资料有具体数值 → 属于【抽取缺口】（数值/表格内容未被抽到），"
    "建议改进抽取或更新知识库后再改为可回答。"
)

DECISIONS: Dict[str, Dict[str, Any]] = {
    # q001：成就数量与实际不符 → drop
    "q001": {"append": "自动分析：按你的批注保留 drop（成就数量与实际不符）。"},
    # q039：来源攻略只列了两个区，缺其他地区 → pending
    "q039": {
        "status": "pending",
        "append": (
            "自动分析：知识库的来源攻略原文只写了「米格尔区与新赫兰德区」，"
            "属【来源不完整】而非抽取错误。请把 expected_points 补成游戏真实地区列表——"
            "补全后这道题会变成「检验知识库能否回答游戏真实情况」的高价值用例。"
        ),
    },
    # q041/q042：内容正确但缺更新时间 → 补 8月20日锚点
    "q041": {
        "status": "fixed",
        "question": "8月20日不停服更新优化了「排球之星」的哪项操作手感？",
        "append": "自动分析：已按批注补上时间锚点（来源为《异环》8月20日不停服更新公告）。",
    },
    "q042": {
        "status": "fixed",
        "question": "8月20日不停服更新中，「排球之星」修复了哪些问题？",
        "append": "自动分析：已按批注补上时间锚点（同上）。",
    },
    # q046/q048：信息与兑换码已过时 → drop
    "q046": {"status": "drop", "append": "自动分析：按批注「目前已过时信息」标记 drop。"},
    "q048": {"status": "drop", "append": "自动分析：按批注「兑换码已过时」标记 drop。"},
    # q050/q051/q056/q059：公开预告已提及 → time_sensitive（知识库过时）
    "q050": {"time_sensitive": True, "append": _TIME_SENSITIVE_NOTE},
    "q051": {"time_sensitive": True, "append": _TIME_SENSITIVE_NOTE},
    "q056": {"time_sensitive": True, "append": _TIME_SENSITIVE_NOTE},
    "q059": {"time_sensitive": True, "append": _TIME_SENSITIVE_NOTE},
    # q052/q054/q057：公开资料有具体数值、库内只有获取方式 → kb_gap（抽取缺口）
    "q052": {"kb_gap": True, "append": _KB_GAP_NOTE},
    "q054": {"kb_gap": True, "append": _KB_GAP_NOTE},
    "q057": {"kb_gap": True, "append": _KB_GAP_NOTE},
    # q060：已有复刻先例但未进常驻池，与本题结论不冲突 → 维持 drop
    "q060": {"append": "自动分析：你补充的复刻信息与本题结论不冲突，维持 drop。"},
}


def main() -> int:
    parser = argparse.ArgumentParser(description="落实人工审核决定到评测集")
    parser.add_argument("--set", default=DEFAULT_SET)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    path = Path(args.set)
    if not path.is_absolute():
        path = paths.project_root() / path
    if not path.exists():
        print(f"找不到评测集：{path}")
        return 2

    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("items") or []
    by_id = {item.get("id"): item for item in items}

    changed = 0
    print(f"评测集：{path.name}（{len(items)} 道题）\n")
    for item_id, decision in DECISIONS.items():
        item = by_id.get(item_id)
        if item is None:
            print(f"  ⚠️ {item_id} 不存在，跳过")
            continue
        review = item.setdefault("review", {})
        notes = []
        if "status" in decision and review.get("status") != decision["status"]:
            notes.append(f"status {review.get('status')} → {decision['status']}")
            review["status"] = decision["status"]
        if "question" in decision and item.get("question") != decision["question"]:
            notes.append(f"question → {decision['question'][:28]}…")
            item["question"] = decision["question"]
        for field in ("time_sensitive", "kb_gap"):
            if decision.get(field):
                if not item.get(field):
                    notes.append(f"{field}=true")
                item[field] = True
        if decision.get("append"):
            existing = str(review.get("comment") or "")
            marker = decision["append"][:18]
            if marker not in existing:
                review["comment"] = (existing + "　" if existing else "") + decision["append"]
                notes.append("补充说明")
        if notes:
            changed += 1
            print(f"  [{item_id}] " + "；".join(notes))

    print(f"\n共 {changed} 道题需要修改")
    if not args.apply:
        print("（这是预览。加 --apply 才会写入）")
        return 0
    payload["items"] = items
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
