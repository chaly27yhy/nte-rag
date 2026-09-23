"""运行评测集，输出可比较的质量指标（`eval/eval_set.json`）。

指标说明
--------
- **要点覆盖率**：答案覆盖了 expected_points 里多少比例（由模型逐条判分）
- **完整覆盖**：要点全中的题目数
- **引用命中率**：答案引用的来源里是否包含 expected_sources（检验检索链）
- **拒答正确率**：对「资料确实没有答案」的题，是否说明资料未涵盖而不是编造
- **平均延迟**

这组数字用于判断一次质量改动让答案变好还是变坏。

用法：
    python tools\\run_eval.py                      # 全部（跳过 review.status=drop）
    python tools\\run_eval.py --limit 20           # 只跑前 20 题
    python tools\\run_eval.py --web                # 允许联网补齐（默认只用本地知识库）
    python tools\\run_eval.py --tag before-fix      # 报告文件名带上标记，便于前后对比
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config  # noqa: E402
from app.core import paths  # noqa: E402
from app.core.llm import build_llm  # noqa: E402
from app.core.rag import RagEngine  # noqa: E402
from app.core.store import KnowledgeBase  # noqa: E402

def split_scored(results: List[Dict[str, Any]]) -> tuple:
    """把结果分成「计分 / 知识库缺口（不计分）/ 应拒答」三组。

    `kb_gap` 题（已核实任何可爬来源都查不到该数据）若算进要点覆盖率分母，
    分数会随单次判分波动（q035 实测在 2/2 与 1/2 之间跳），看起来像数据退化。
    因此缺口题不计入覆盖率，但**仍计入编造统计**——库里没有 ≠ 可以编。
    该函数是纯函数，`tools/quality_check.py` 直接对它做断言。
    """
    answerable = [r for r in results if r.get("answerable") and r.get("coverage") is not None]
    gap = [r for r in answerable if (r.get("tags") or {}).get("kb_gap")]
    scored = [r for r in answerable if not (r.get("tags") or {}).get("kb_gap")]
    refusal = [r for r in results if not r.get("answerable")]
    return scored, gap, refusal


GRADE_SYSTEM = """你是严格的评测员。我会给你一道题、若干「期望要点」、以及助手的回答。

**第一步：逐条判断回答是否覆盖了每个期望要点**
- 只要回答了该要点的实质内容即算覆盖（措辞不同没关系）；
- 数值、名称、时间与要点不一致 → 不算覆盖；
- 回答里没提到 → 不算覆盖。

**第二步：判断 is_refusal**
回答是否表达了「资料未涵盖 / 无法确定 / 没有找到相关信息」（而不是给出了答案）。

**第三步：判断 extra_facts —— 只关心「编造」，不要因为回答更丰富就报警**
- "none"：回答没有超出期望要点的具体事实；
- "same_kind"：回答补充了**同一类**的正确信息（例如期望要点列了 2 个地区、回答列了 3 个；
  或补充了同一件事的相关细节）。**这不算编造**，因为出题时期望要点往往写得不全；
- "unsupported"：回答里出现了**与期望要点矛盾**、又**在【参考资料】里找不到依据**的具体事实
  （尤其是精确数值、时间、人名）。

**关键：判断 "unsupported" 前必须先看【参考资料】。**
我会把助手当时看到的资料原文一并给你。只要那些「多出来的细节」能在参考资料里找到出处
（哪怕是参考资料里的一句话、一个日期、一个名称），就属于**有依据的补充**，
应当算 "same_kind" 而不是 "unsupported"。只有参考资料里完全没有、且看起来像编造的，才给 "unsupported"。

**请从严把握 "unsupported"**：误报会直接误导使用者。

只输出 JSON，不要解释。格式：
{"covered":[true,false],"is_refusal":false,"extra_facts":"none","comment":"简短理由"}"""


def grade(
    llm,
    question: str,
    points: List[str],
    answer: str,
    answerable: bool,
    evidence: str = "",
) -> Dict[str, Any]:
    evidence_block = f"\n\n【参考资料（助手当时看到的原文）】\n{evidence[:6000]}" if evidence else ""
    if answerable and points:
        user = (
            f"【题目】{question}\n\n"
            f"【期望要点】\n" + "\n".join(f"{i + 1}. {p}" for i, p in enumerate(points)) + "\n\n"
            f"【助手回答】\n{answer[:4000]}"
            f"{evidence_block}"
        )
    else:
        # 不可回答的题没有要点，只判断是否拒答与是否编造
        user = (
            f"【题目】{question}\n\n"
            f"【期望要点】（无——这题资料里没有答案，助手应当说明未涵盖）\n\n"
            f"【助手回答】\n{answer[:4000]}"
            f"{evidence_block}"
        )
    try:
        payload = llm.chat_json(
            [{"role": "system", "content": GRADE_SYSTEM}, {"role": "user", "content": user}],
            temperature=0,
            max_tokens=700,
        )
    except Exception as error:  # noqa: BLE001
        return {"covered": [], "is_refusal": False, "extra_facts": "unknown", "comment": f"判分失败：{error}"}
    if not isinstance(payload, dict):
        return {"covered": [], "is_refusal": False, "extra_facts": "unknown", "comment": "判分返回格式异常"}
    covered = payload.get("covered")
    if not isinstance(covered, list):
        covered = []
    extra = str(payload.get("extra_facts") or "none")
    if extra not in ("none", "same_kind", "unsupported", "unknown"):
        # 兼容旧字段名
        legacy = str(payload.get("hallucination") or "none")
        extra = {"possible": "same_kind", "likely": "unsupported"}.get(legacy, "none")
    return {
        "covered": [bool(item) for item in covered],
        "is_refusal": bool(payload.get("is_refusal")),
        "extra_facts": extra,
        "comment": str(payload.get("comment") or "")[:200],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="运行评测集")
    parser.add_argument("--set", default="eval/eval_set.json")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0 = 全部）")
    parser.add_argument("--web", action="store_true", help="允许联网补齐（默认只用本地知识库）")
    parser.add_argument("--include-pending", action="store_true", help="连未审核的题也跑（默认也跑，仅 drop 跳过）")
    parser.add_argument("--tag", default="", help="报告文件名标记，便于前后对比")
    parser.add_argument(
        "--data-dir",
        default=".eval_data",
        help="评测使用的数据目录（默认 .eval_data，与你的日常使用数据隔离）",
    )
    parser.add_argument("--no-seed", action="store_true", help="不自动导入种子知识库")
    parser.add_argument(
        "--seed",
        default="",
        help="A/B 对比用：指定要导入的种子文件（默认用 seed/seed_kb.json）。"
        "换种子时必须配一个全新的 --data-dir，否则会读到上一次导入的旧库。",
    )
    args = parser.parse_args()

    # 评测必须跑在一个「有内容的」知识库上。
    # 默认用独立的 .eval_data 目录并导入种子库，避免污染日常数据、也避免对着空库评测。
    import os

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = paths.project_root() / data_dir
    os.environ["NTE_RAG_DATA_DIR"] = str(data_dir)

    root = paths.project_root()
    set_path = Path(args.set)
    if not set_path.is_absolute():
        set_path = root / set_path
    if not set_path.exists():
        print(f"找不到评测集：{set_path}\n请先运行：python tools\\build_eval_set.py")
        return 2

    payload = json.loads(set_path.read_text(encoding="utf-8"))
    items = [
        item for item in (payload.get("items") or [])
        if (item.get("review") or {}).get("status") != "drop"
    ]
    if args.limit:
        items = items[: args.limit]
    if not items:
        print("没有可执行的题目")
        return 2

    config = Config()
    config.apply_dev_env(explicit=True)
    if not config.get_secret("key_enc", "llm") or not config.get("llm", "model", ""):
        print("需要先在 .env 中配置模型（答题与判分都要用）")
        return 2
    llm = build_llm(config)
    kb = KnowledgeBase()

    # 空库（首次运行）时自动导入随程序分发的种子知识库
    stats = kb.stats()
    if stats["documents"] == 0 and not args.no_seed:
        from app.core.ingest import load_seed

        seed = Path(args.seed) if args.seed else paths.seed_kb_path()
        if not seed.is_absolute():
            seed = root / seed
        if seed.exists():
            report = load_seed(kb, config, seed)
            print(f"已导入种子知识库：{seed.name} → {report['documents']} 篇文档 / {report['facts']} 条条目")
            stats = kb.stats()
        else:
            print(f"警告：找不到种子知识库 {seed}，检索结果可能为空")
    elif args.seed:
        print(
            f"注意：数据目录 {data_dir} 已有 {stats['documents']} 篇文档，"
            f"本次不会导入 --seed（换种子请换一个全新的 --data-dir）"
        )

    engine = RagEngine(config, kb)

    reviewed = sum(1 for item in items if (item.get("review") or {}).get("status") in ("ok", "fixed"))
    print("=" * 74)
    print(f"评测集：{set_path.name}　题目 {len(items)} 道（其中已人工审核 {reviewed} 道）")
    print(f"模式  ：{'允许联网补齐' if args.web else '仅本地知识库'}")
    print(f"知识库：{stats['db_path']}")
    print(f"        {stats['documents']} 篇文档 / {stats['chunks']} 段原文 / {stats['facts']} 条条目")
    print("=" * 74)

    results: List[Dict[str, Any]] = []
    execution_errors: List[str] = []
    started_all = time.time()
    for index, item in enumerate(items, start=1):
        question = item["question"]
        answerable = bool(item.get("answerable", True))
        points = item.get("expected_points") or []
        try:
            answer = engine.answer(question, allow_web=args.web)
            text = answer.text
            citations = [c.get("url") for c in (answer.citations or []) if c.get("url")]
            latency = answer.latency_ms
            best_score = float((answer.retrieval or {}).get("best_score") or 0.0)
        except Exception as error:  # noqa: BLE001
            text, citations, latency, best_score = f"[执行失败] {error}", [], 0, 0.0
            # 单题失败不该中断整轮，但也不能让「全部失败」看起来像「评测通过」：
            # 记下每条，收尾按这个清单决定退出码（见文件末尾）。
            execution_errors.append(f"[{item.get('id') or index}] {type(error).__name__}: {error}")

        # 把助手当时看到的资料一并交给判分模型，否则「有依据的补充」会被误判成编造。
        # answer.retrieval 只是统计摘要（chunks 是**数量**不是列表），
        # 所以要重新按同样的问题检索一次（本地检索是确定性的，结果一致）。
        # 这一步只是判分辅助，绝不能因为它出错而让整轮评测变 0 分，故单独兜底。
        evidence_text = ""
        try:
            evidence_text = engine.render_context(engine.build_evidence(engine.retrieve(question)))
        except Exception:  # noqa: BLE001
            evidence_text = ""

        verdict = grade(llm, question, points, text, answerable, evidence=evidence_text)
        covered = verdict["covered"]
        if points:
            hit = sum(1 for flag in covered[: len(points)] if flag)
            coverage = hit / len(points)
        else:
            coverage = None

        expected_sources = [url for url in (item.get("expected_sources") or []) if url]
        source_hit = None
        if expected_sources:
            source_hit = any(any(exp[:60] in (cit or "") for cit in citations) for exp in expected_sources)

        results.append(
            {
                "id": item.get("id"),
                "question": question,
                "answerable": answerable,
                "category": item.get("category"),
                "difficulty": item.get("difficulty"),
                "coverage": coverage,
                "points_total": len(points),
                "points_covered": sum(1 for flag in covered[: len(points)] if flag),
                "source_hit": source_hit,
                "is_refusal": verdict["is_refusal"],
                "extra_facts": verdict["extra_facts"],
                "best_score": round(best_score, 4),
                "latency_ms": latency,
                "tags": {
                    "time_sensitive": bool(item.get("time_sensitive")),
                    "kb_gap": bool(item.get("kb_gap")),
                },
                "review_comment": str((item.get("review") or {}).get("comment") or "")[:300],
                "comment": verdict["comment"],
                "answer_preview": text[:200],
            }
        )
        mark = "·"
        if answerable:
            mark = "✅" if (coverage or 0) >= 0.999 else ("⚠️" if (coverage or 0) >= 0.5 else "❌")
        else:
            mark = "✅" if verdict["is_refusal"] else "❌"
        print(f"  [{index:>3}/{len(items)}] {mark} {question[:44]}"
              f"　覆盖 {results[-1]['points_covered']}/{results[-1]['points_total']}"
              f"　{latency}ms")

    # 见 split_scored 的注释：缺口题不计入覆盖率分母，但仍计入编造统计。
    scored_results, gap_results, refusal_results = split_scored(results)
    answerable_results = scored_results + gap_results
    source_results = [r for r in results if r["source_hit"] is not None]

    def rate(values: List[bool]) -> float:
        return round(sum(1 for v in values if v) / len(values), 4) if values else 0.0

    # 「编造」只在有期望要点的可回答题上统计才有意义；
    # 应拒答题没有要点，任何具体回答都会被误判，所以单独处理。
    # 另外把「同类补充」(same_kind) 排除在外：出题时期望要点常常写不全
    # 因此回答更丰富 ≠ 编造。
    halluc_answerable = [r for r in answerable_results if r["extra_facts"] == "unsupported"]
    same_kind = [r for r in answerable_results if r["extra_facts"] == "same_kind"]
    suspicion = [r for r in refusal_results if not r["is_refusal"] and r["best_score"] >= 0.30]
    flagged = [
        r for r in results
        if (r.get("tags") or {}).get("kb_gap") or (r.get("tags") or {}).get("time_sensitive")
    ]

    metrics = {
        "题目总数": len(results),
        "可回答题数": len(scored_results),
        "缺口题数（不计分）": len(gap_results),
        "应拒答题数": len(refusal_results),
        "要点覆盖率": round(statistics.mean([r["coverage"] for r in scored_results]), 4) if scored_results else 0.0,
        "完整覆盖题数": sum(1 for r in scored_results if r["coverage"] >= 0.999),
        "完整覆盖率": rate([r["coverage"] >= 0.999 for r in scored_results]),
        "引用命中率": rate([bool(r["source_hit"]) for r in source_results]),
        "拒答正确率": rate([bool(r["is_refusal"]) for r in refusal_results]),
        "疑似编造题数": len(halluc_answerable),
        "缺口题编造题数": sum(1 for r in gap_results if r["extra_facts"] == "unsupported"),
        "缺口题平均覆盖（仅记录）": round(statistics.mean([r["coverage"] for r in gap_results]), 4) if gap_results else 0.0,
        "同类补充题数": len(same_kind),
        "平均延迟ms": int(statistics.mean([r["latency_ms"] for r in results])) if results else 0,
        "总耗时秒": round(time.time() - started_all, 1),
    }

    print("\n" + "=" * 74)
    print("评测结果")
    print("=" * 74)
    for key, value in metrics.items():
        print(f"  {key:<14} {value}")

    weak = sorted(
        [r for r in scored_results if r["coverage"] < 0.5],
        key=lambda r: r["coverage"],
    )[:8]
    if weak:
        print("\n最需要改进的题目（覆盖不足一半）：")
        for row in weak:
            print(f"  · [{row['id']}] {row['question'][:52]}")
            print(f"      覆盖 {row['points_covered']}/{row['points_total']}　判分说明：{row['comment'][:90]}")
    if refusal_results:
        bad_refusal = [r for r in refusal_results if not r["is_refusal"]]
        if bad_refusal:
            print("\n应拒答题里「给出了答案」的题：")
            for row in bad_refusal[:8]:
                hint = ""
                if row["best_score"] >= 0.30:
                    hint = f"　⚠️ 知识库相关性 {row['best_score']:.2f}，很可能是题目标签错了（其实答得出来），请人工确认"
                print(f"  · [{row['id']}] {row['question'][:52]}{hint}")
                print(f"      回答开头：{row['answer_preview'][:88]}")
        if suspicion:
            print(f"\n其中 {len(suspicion)} 道「标签可能标错」——这类题不会计入编造统计，"
                  f"但请到 eval_set.json 里把标签改为 answerable: true 并补上期望要点。")

    if flagged:
        print(f"\n⚠️ 有 {len(flagged)} 道题被人工标注为「知识库缺口 / 时效敏感」"
              f"（标签对当前快照成立，但知识库更新后会失效）：")
        for row in flagged:
            tag = "知识库缺口" if (row.get("tags") or {}).get("kb_gap") else "时效敏感"
            extra = ""
            if (row.get("tags") or {}).get("kb_gap") and row.get("coverage") is not None:
                extra = f"　本次覆盖 {row['coverage']:.2f}（不计入覆盖率）"
            print(f"  · [{row['id']}]【{tag}】{row['question'][:46]}{extra}")
            if row.get("review_comment"):
                print(f"      {row['review_comment'][:110]}")
        print("  → 知识库缺口题不计入「要点覆盖率」，但计入「疑似编造」；"
              "补齐来源或改进抽取后，请把 kb_gap 去掉。")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    # --tag 会被直接拼进文件名，先剔掉路径分隔符与点号，避免 `--tag ..\..\x` 把报告写到 eval/ 之外。
    safe_tag = re.sub(r"[^0-9A-Za-z_-]+", "-", args.tag) if args.tag else ""
    tag = f"-{safe_tag}" if safe_tag else ""
    report_path = root / "eval" / f"report{tag}-{stamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "eval_set": set_path.name,
                "mode": "web" if args.web else "local",
                "reviewed_count": reviewed,
                "metrics": metrics,
                "gap_ids": [r["id"] for r in gap_results],
                "results": results,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n详细报告：{report_path}")
    kb.close()
    if execution_errors:
        print(f"\n❌ 有 {len(execution_errors)} / {len(items)} 道题在检索或调用模型时抛了异常，"
              f"报告里的分数不能用来判断质量，本次以退出码 1 结束：")
        for line in execution_errors[:20]:
            print(f"   · {line}")
        if len(execution_errors) > 20:
            print(f"   …… 另有 {len(execution_errors) - 20} 条，详见报告里的 [执行失败] 文本")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
