"""生成评测集候选题目（`eval/eval_set.json` 的草稿，人工审核后才算数）。

生成方式
----------------------------------
没有评测集，任何一次质量改动都无法判断是让答案更准还是更钝。
从零手写 60 道题很费时间，因此这里用知识库现有内容**批量生成候选**，
再由人快速审核（改错题、删不合适的题），把人工投入压到最低。

输出 eval/eval_set.json，每道题带：
    question          问题
    expected_points   期望答案要点（2-4 条，必须来自知识库）
    expected_sources  期望来源 URL（用于判断检索是否命中）
    category          分类：角色 / 剧情 / 玩法 / 系统 / 活动 / 术语
    difficulty        easy / medium / hard
    answerable        false 表示「资料里确实没有答案」，用于检验是否该拒答时拒答
    review            人工审核状态（pending / ok / fixed / drop）

用法：
    python tools\\build_eval_set.py --count 60
    python tools\\build_eval_set.py --count 60 --from-db       # 用本地知识库而不是种子文件

    （本工具靠模型生成候选题，需先配好：.env 里 NTE_RAG_DEV_ENV=1 + NTE_RAG_DEV_LLM_*；
      每题的 review.status 需人工审核，默认写入 eval/eval_set.json，会先备份 .bak。）
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import argparse
import json
import os
import random
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config  # noqa: E402
from app.core import paths  # noqa: E402
from app.core.llm import build_llm  # noqa: E402

OUT_DEFAULT = "eval/eval_set.json"

# 临时数据目录（配置 + 知识库）。刻意留在仓库内并被 .gitignore 的 `.eval_*/` 覆盖，
# 绝不能让本工具碰到用户真实的 %APPDATA%\NTE-RAG。
SCRATCH_DIR_NAME = ".eval_build"

GENERATE_SYSTEM = """你是《异环》（Neverness to Everness）知识库的评测集设计者。

我会给你若干条「知识条目」（含标题与内容）。请为它们设计评测题目。

要求：
1. 每题**必须能且仅能**依据给定条目回答；不要出需要外部知识的题。
2. expected_points 是判分要点，2-4 条，必须是条目里明确写出的信息；
   措辞可以概括，但**数值、名称、时间必须与条目完全一致**。
3. 问题要像真实玩家会问的，不要出现「根据上述资料」这类元话术。
4. 覆盖不同难度：
   - easy：直接问单一事实（某角色是谁、某功能在哪）
   - medium：需要综合两三处信息
   - hard：需要区分细节或多个数值
5. 每题给出 used_url 表示它依据哪条条目的来源 URL。
6. 只输出 JSON 数组，不要解释。格式：
[{"question":"…","expected_points":["…"],"category":"角色","difficulty":"easy","used_url":"…"}]"""

UNANSWERABLE_SYSTEM = """你是《异环》（Neverness to Everness）知识库的评测集设计者。

下面会给你一段「当前知识库已有的内容摘要」。请设计若干条
**玩家可能会问、但依据这些内容无法回答**的问题。这类题用于检验助手是否会
在资料不足时老实说「资料未涵盖」，而不是编造答案。

要求：
1. 问题必须是真实玩家关心的（版本后续内容、未公布的角色、未来的联动、内部数值公式等）。
2. 不要问完全不相关的东西（例如与游戏无关的常识）。
3. 只输出 JSON 数组。格式：
[{"question":"…","category":"活动","why_unanswerable":"资料中没有提到…"}]"""


def load_items(use_db: bool, limit: int) -> List[Dict[str, Any]]:
    """取出用于出题的素材：优先用结构化条目，没有就用原文片段。"""
    if use_db:
        from app.core.store import KnowledgeBase

        kb = KnowledgeBase()
        facts = kb.list_facts(limit=limit, status="active")
        kb.close()
        return [
            {
                "title": fact.get("title", ""),
                "answer": fact.get("answer", ""),
                "tags": fact.get("tags", ""),
                "topic": fact.get("topic", ""),
                "url": fact.get("source_url", ""),
                "source_type": fact.get("source_type", ""),
            }
            for fact in facts
        ]

    seed = paths.seed_kb_path()
    if not seed.exists():
        print(f"找不到种子知识库：{seed}")
        return []
    payload = json.loads(seed.read_text(encoding="utf-8"))
    facts = [
        {
            "title": fact.get("title", ""),
            "answer": fact.get("answer", ""),
            "tags": fact.get("tags", ""),
            "topic": fact.get("topic", ""),
            "url": fact.get("source_url", ""),
            "source_type": fact.get("source_type", ""),
        }
        for fact in (payload.get("facts") or [])
    ]
    if facts:
        return facts
    # 没有条目就退回原文片段
    items: List[Dict[str, Any]] = []
    for document in payload.get("documents") or []:
        for index, chunk in enumerate(document.get("chunks") or []):
            items.append(
                {
                    "title": f"{document.get('title', '')}（第 {index + 1} 段）",
                    "answer": chunk,
                    "tags": "",
                    "topic": document.get("source_type", ""),
                    "url": document.get("url", ""),
                    "source_type": document.get("source_type", ""),
                }
            )
    return items


def build_prompt(batch: List[Dict[str, Any]]) -> str:
    blocks = []
    for index, item in enumerate(batch):
        blocks.append(
            f"[{index}] 标题：{item['title']}\n"
            f"    内容：{item['answer'][:400]}\n"
            f"    分类：{item.get('topic') or item.get('tags') or '未分类'}\n"
            f"    来源 URL：{item.get('url', '')}"
        )
    return "知识条目：\n" + "\n\n".join(blocks) + f"\n\n请为以上 {len(batch)} 条各出 1-2 道题。"


ANSWERABILITY_SYSTEM = """你负责核验评测题的标签是否成立。

我会给你一个问题，以及知识库中检索到的「最相关原文」。请判断：
**仅依据这些原文，能否回答该问题？**

- 能给出问题所问的核心信息 → answerable = true
- 原文只沾边、但缺少问题所要的关键信息 → answerable = false
- 注意：原文里如果压根没有提及问题所问的对象/数值/时间，即为 false，不要因为话题相近就判 true。

只输出 JSON：{"answerable": true/false, "reason": "一句话理由"}"""


def verify_unanswerable(items: List[Dict[str, Any]], llm) -> None:
    """核验「资料无法回答」的标签。

    模型出题时只看到一小段摘要，容易把「摘要里没有、但知识库里其实有」的内容判为不可回答。
    12 道候选里有多道实际答得出来——标签错了，评测结果会被误读成「助手在编造」。

    核验方式：检索知识库，把命中的原文交给另一个模型判断「能否据此回答」。
    单纯用相关性分数不可靠（话题相近 ≠ 有答案），须以原文为证据判。
    """
    if not items:
        return
    import os

    from app.core.ingest import load_seed
    from app.core.rag import RagEngine
    from app.core.store import KnowledgeBase

    data_dir = paths.project_root() / SCRATCH_DIR_NAME
    data_dir.mkdir(parents=True, exist_ok=True)   # KnowledgeBase 不会自建父目录
    os.environ["NTE_RAG_DATA_DIR"] = str(data_dir)
    config = Config(path=data_dir / "config.json")
    config.apply_dev_env(explicit=True)
    kb = KnowledgeBase(db_file=data_dir / "knowledge.db")
    if kb.stats()["documents"] == 0:
        seed = paths.seed_kb_path()
        if seed.exists():
            load_seed(kb, config, seed)
    engine = RagEngine(config, kb)

    dropped = 0
    kept = 0
    for item in items:
        try:
            retrieval = engine.retrieve(item["question"])
        except Exception:  # noqa: BLE001
            continue
        best = float(retrieval.get("best_score") or 0)
        chunks = (retrieval.get("chunks") or [])[:4]
        facts = (retrieval.get("facts") or [])[:4]
        evidence = "\n\n".join(
            [f"[原文] {c.get('title', '')}\n{c.get('text', '')[:600]}" for c in chunks]
            + [f"[条目] {f.get('title', '')}\n{f.get('answer', '')[:400]}" for f in facts]
        ) or "（没有检索到任何相关内容）"
        try:
            verdict = llm.chat_json(
                [
                    {"role": "system", "content": ANSWERABILITY_SYSTEM},
                    {"role": "user", "content": f"【问题】{item['question']}\n\n【最相关原文】\n{evidence}"},
                ],
                temperature=0,
                max_tokens=400,
            )
        except Exception:  # noqa: BLE001
            verdict = {}
        can_answer = bool((verdict or {}).get("answerable")) if isinstance(verdict, dict) else False
        reason = str((verdict or {}).get("reason") or "")[:120] if isinstance(verdict, dict) else ""

        if can_answer:
            item["review"]["status"] = "drop"
            item["review"]["comment"] = (
                f"自动核验不通过：知识库原文可以回答该问题（相关性 {best:.2f}）。{reason}"
            )
            dropped += 1
        else:
            item["review"]["comment"] = f"自动核验通过：{reason or '检索不到可回答的原文'}（相关性 {best:.2f}）"
            kept += 1

    kb.close()
    try:
        shutil.rmtree(data_dir, ignore_errors=True)
    except Exception:
        pass
    print(f"  核验「不可回答」标签：{len(items)} 道中 {kept} 道成立、{dropped} 道其实答得出来（已标 drop）")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成评测集候选题目")
    parser.add_argument("--out", default=OUT_DEFAULT)
    parser.add_argument("--count", type=int, default=60, help="目标题目总数（含不可回答题）")
    parser.add_argument("--batch", type=int, default=8, help="每次调用喂多少条素材")
    parser.add_argument("--unanswerable", type=int, default=10, help="生成多少道「资料无法回答」的题")
    parser.add_argument("--from-db", action="store_true", help="用本地知识库而不是种子文件")
    parser.add_argument("--seed", type=int, default=20260921, help="随机种子，保证可复现")
    args = parser.parse_args()

    # 这个工具只需要「读素材」+ 一个放临时配置与库的目录，绝不能碰用户真实的
    # %APPDATA%\NTE-RAG：`Config()` 不带 path 时指向真实 config.json，而 apply_dev_env()
    # 会把 .env 里的开发用 provider / base_url / model **持久化写进去**（覆盖用户日常设置）；
    # `KnowledgeBase()` 更会新建并迁移真实 knowledge.db。做法与 tools/run_eval.py 一致：
    # 先把数据目录钉到仓库内的临时目录，再构造任何对象。
    scratch = paths.project_root() / SCRATCH_DIR_NAME
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["NTE_RAG_DATA_DIR"] = str(scratch)

    config = Config(path=scratch / "config.json")
    config.apply_dev_env(explicit=True)
    if not config.get_secret("key_enc", "llm") or not config.get("llm", "model", ""):
        print("需要先在 .env 中配置模型（本工具靠模型生成候选题）")
        return 2
    llm = build_llm(config)

    items = load_items(args.from_db, limit=400)
    if not items:
        print("没有可用素材，请先运行 tools/seed_builder.py 生成种子知识库")
        return 2
    print(f"素材：{len(items)} 条")

    random.seed(args.seed)
    random.shuffle(items)
    want_answerable = max(1, args.count - args.unanswerable)
    batches = [items[i : i + args.batch] for i in range(0, len(items), args.batch)]

    questions: List[Dict[str, Any]] = []
    for index, batch in enumerate(batches):
        if len(questions) >= want_answerable:
            break
        print(f"  出题中… 第 {index + 1}/{len(batches)} 批（已有 {len(questions)} 题）")
        try:
            payload = llm.chat_json(
                [
                    {"role": "system", "content": GENERATE_SYSTEM},
                    {"role": "user", "content": build_prompt(batch)},
                ],
                temperature=0.4,
                max_tokens=2600,
            )
        except Exception as error:  # noqa: BLE001
            print(f"    本批失败：{error}")
            continue
        if isinstance(payload, dict):
            payload = payload.get("questions") or payload.get("items") or []
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, dict):
                continue
            question = str(row.get("question") or "").strip()
            points = row.get("expected_points") or []
            if not question or not isinstance(points, list) or not points:
                continue
            questions.append(
                {
                    "question": question,
                    "expected_points": [str(p).strip() for p in points if str(p).strip()],
                    "expected_sources": [str(row.get("used_url") or "").strip()] if row.get("used_url") else [],
                    "category": str(row.get("category") or "未分类"),
                    "difficulty": str(row.get("difficulty") or "medium"),
                    "answerable": True,
                    "review": {"status": "pending", "comment": ""},
                }
            )
            if len(questions) >= want_answerable:
                break

    # 「资料无法回答」的题：用于检验拒答行为
    if args.unanswerable > 0:
        print("  生成「资料无法回答」的题目…")
        digest = "\n".join(f"- {item['title']}：{item['answer'][:80]}" for item in items[:60])
        try:
            payload = llm.chat_json(
                [
                    {"role": "system", "content": UNANSWERABLE_SYSTEM},
                    {
                        "role": "user",
                        "content": f"当前知识库内容摘要：\n{digest}\n\n"
                        f"请出 {args.unanswerable} 道上述内容无法回答、但玩家会问的问题。",
                    },
                ],
                temperature=0.5,
                max_tokens=1600,
            )
            if isinstance(payload, dict):
                payload = payload.get("questions") or payload.get("items") or []
            for row in payload if isinstance(payload, list) else []:
                if not isinstance(row, dict):
                    continue
                question = str(row.get("question") or "").strip()
                if not question:
                    continue
                questions.append(
                    {
                        "question": question,
                        "expected_points": [],
                        "expected_sources": [],
                        "category": str(row.get("category") or "未分类"),
                        "difficulty": "hard",
                        "answerable": False,
                        "why_unanswerable": str(row.get("why_unanswerable") or ""),
                        "review": {"status": "pending", "comment": ""},
                    }
                )
        except Exception as error:  # noqa: BLE001
            print(f"    生成失败：{error}")

    # 核验「资料无法回答」的标签：把「知识库里其实有」的题误标为应拒答，
    # 会让评测把正确回答误判成编造。
    verify_unanswerable([item for item in questions if not item["answerable"]], llm)

    for index, item in enumerate(questions, start=1):
        item["id"] = f"q{index:03d}"

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = paths.project_root() / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        # 默认输出就是已经人工审核过的 eval/eval_set.json，而机器生成的题目会把
        # review.status 全部重置成 pending，人工结论（ok / fixed / drop）就只能靠 git 找回。
        # 覆盖前先留一份 .bak（.gitignore 不忽略它，出问题能直接对比）。
        backup = out_path.with_suffix(out_path.suffix + ".bak")
        shutil.copy2(out_path, backup)
        print(f"⚠️ 目标文件已存在，已先备份到：{backup}")
    out_path.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
                "note": (
                    f"主评测集：{len(questions)} 道《异环》中文问答，覆盖角色、剧情、玩法、系统、"
                    "活动、术语、成就等类别。每题按 expected_points 逐条判分，并用 expected_sources "
                    "检验检索是否命中期望来源；answerable 为 false 的题考的是该拒答时能否拒答，"
                    "这类题另附 why_unanswerable 说明拒答依据。review.status 记录审核状态：pending "
                    "为自动生成、尚未审核，ok 为已确认题目与要点正确，fixed 为审核中修正过要点、"
                    "来源或标签，正在参与评测，drop 为剔除、不参与评测。review.comment 记录该题的"
                    "复核结论与理由，kb_gap 题必须在此留下说明。审核结果只写进这两个字段：JSON "
                    "不支持注释，写在结构外面会让文件无法解析。"
                ),
                "items": questions,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    answerable = sum(1 for item in questions if item["answerable"])
    print("\n" + "=" * 62)
    print(f"已生成 {len(questions)} 题（可回答 {answerable}，应拒答 {len(questions) - answerable}）")
    print(f"输出：{out_path}")
    print("\n下一步（人工审核，约 30-60 分钟）：")
    print("  1. 打开该文件，逐题检查 expected_points 是否正确、问题是否自然；")
    print("  2. 把 review.status 改成 ok（可用）/ fixed（已修正）/ drop（删除）；")
    print("  3. 然后运行：python tools\\run_eval.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
