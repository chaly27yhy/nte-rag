"""知识库检索诊断：直接查询本地库，查看事实与原文片段的命中情况。

用途
----
- 判断「为什么这个问题答不上来」：是没抓到资料，还是分词/阈值问题；
- 不依赖模型，也不需要联网，纯本地检索；
- 可指定自定义数据目录，便于检查种子库或别人的库。

用法：
    python tools\\kb_probe.py 薄荷
    python tools\\kb_probe.py "1.4版本前瞻" --limit 5
    python tools\\kb_probe.py "角色" --data-dir .seed_check
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库检索诊断")
    parser.add_argument("question", help="要检索的问题/关键词")
    parser.add_argument("--data-dir", default="", help="数据目录（默认使用程序的用户数据目录）")
    parser.add_argument(
        "--use-seed",
        action="store_true",
        help="在临时的 .probe_data 目录里导入种子知识库后检索（用于核对「资料里到底有没有」）",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--tokens", action="store_true", help="打印分词结果")
    args = parser.parse_args()

    from app.core import chunk as chunkmod

    if args.tokens:
        print("索引分词：", chunkmod.tokenize(args.question)[:40])
        print("查询分词：", chunkmod.tokenize_query(args.question)[:40])

    if args.use_seed:
        import os
        import shutil

        from app.core.ingest import load_seed

        probe_dir = Path(".probe_data").resolve()
        os.environ["NTE_RAG_DATA_DIR"] = str(probe_dir)
        probe_dir.mkdir(parents=True, exist_ok=True)
        from app.config import Config

        config = Config(path=probe_dir / "config.json")
        config.apply_dev_env(explicit=True)

    if args.data_dir:
        import os

        os.environ["NTE_RAG_DATA_DIR"] = str(Path(args.data_dir).resolve())

    from app.core.store import KnowledgeBase

    kb = KnowledgeBase()
    if args.use_seed and kb.stats()["documents"] == 0:
        seed = Path(__file__).resolve().parents[1] / "seed" / "seed_kb.json"
        if seed.exists():
            report = load_seed(kb, config, seed)
            print(f"[已导入种子知识库：{report['documents']} 篇文档 / {report['facts']} 条条目]")
    stats = kb.stats()
    print(f"库：{stats['db_path']}（{stats['db_size_kb']} KB，FTS5={stats['fts_enabled']}）")
    print(f"文档 {stats['documents']} 篇 / 切片 {stats['chunks']} / 条目 {stats['facts']}（冲突 {stats['conflicts']}）")

    facts = kb.search_facts(args.question, limit=args.limit)
    chunks = kb.search_chunks(args.question, limit=args.limit)
    best = max([f["relevance"] for f in facts] + [c["relevance"] for c in chunks] + [0.0])

    print(f"\n== 知识条目（{len(facts)}）==")
    for fact in facts:
        print(f"[{fact['relevance']:.3f}] {fact['title']}　（{fact['source_type']}, 置信度 {fact['confidence']}）")
        print(f"        {fact['answer'][:160]}")

    print(f"\n== 原文片段（{len(chunks)}）==")
    for piece in chunks:
        preview = piece["text"][:150].replace("\n", " ")
        print(f"[{piece['relevance']:.3f}] {piece['title'][:40]}　{piece['url'][:70]}")
        print(f"        {preview}")

    print(f"\n最佳相关性：{best:.3f}")
    from app.config import Config

    threshold = Config().get("answer", "web_trigger_score", 0.35)
    print(f"联网触发阈值：{threshold}　=>　{'会触发联网' if best < threshold else '仅用本地即可'}")
    kb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
