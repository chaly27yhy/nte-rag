"""构建随程序分发的种子知识库（seed/seed_kb.json）。

作用
----
让用户第一次打开 exe 时就有可检索的基础资料，不必等首次联网更新。
构建过程就是一次真实的抓取：按内置数据源目录抓取 → 切块 → （可选）模型抽取条目
→ 导出为 JSON。

用法：
    python tools\\seed_builder.py --out seed\\seed_kb.json
    python tools\\seed_builder.py --out seed\\seed_kb.json --sources official_lore,official_news,moegirl_yihuan --per-source-limit 5
    python tools\\seed_builder.py --out seed\\seed_kb.json --with-llm      # 需先在 .env 配好模型

只抓取服务端渲染页面与公开 API，并遵守各站 robots.txt。
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config  # noqa: E402
from app.core import paths  # noqa: E402
from app.core import dedupe  # noqa: E402
from app.core.fetch import Fetcher  # noqa: E402
from app.core.ingest import ingest_sources, load_seed  # noqa: E402
from app.core.llm import build_llm  # noqa: E402
from app.core.sources import get_sources  # noqa: E402
from app.core.store import KnowledgeBase  # noqa: E402

WORK_DIR = ".seed_build"


def inspect_seed(path: Path, sample: int = 8) -> int:
    """检查已生成的种子知识库：标题是否干净、条目分布、置信度分布。"""
    import collections
    import statistics

    if not path.exists():
        print(f"找不到种子文件：{path}")
        return 2
    payload = json.loads(path.read_text(encoding="utf-8"))
    documents = payload.get("documents") or []
    facts = payload.get("facts") or []

    print(f"文件        : {path}（{path.stat().st_size / 1024:.1f} KB）")
    print(f"生成时间    : {payload.get('generated_at', '-')}")
    print(f"文档 / 切片 : {len(documents)} 篇 / {sum(len(d.get('chunks') or []) for d in documents)} 段")
    print(f"知识条目    : {len(facts)} 条")

    print("\n--- 文档列表（检查标题是否残留 HTML）---")
    dirty = 0
    for document in documents:
        title = document.get("title", "")
        flag = ""
        if "<" in title or ">" in title:
            flag = "  ⚠️ 标题含标签"
            dirty += 1
        print(f"  [{document.get('source_type','?'):<9}] {len(document.get('chunks') or []):>3} 段  {title[:58]}{flag}")
    if dirty:
        print(f"  ⚠️ 共 {dirty} 篇标题仍含 HTML 标签，建议重新抓取")
    else:
        print("  ✅ 所有标题干净")

    if facts:
        print("\n--- 条目来源分布 ---")
        for key, count in collections.Counter(f.get("source_type", "?") for f in facts).most_common():
            print(f"  {key:<10} {count}")
        print("\n--- 按主题（数据源）统计条目 ---")
        for key, count in collections.Counter(f.get("topic", "?") for f in facts).most_common():
            print(f"  {str(key):<28} {count}")
        table_facts = [f for f in facts if str(f.get("title", "")).endswith("）")]
        print(f"\n  其中「表格数值条目」：{len(table_facts)} 条")
        for fact in table_facts[:5]:
            print(f"    · [{fact.get('source_type')}] {fact.get('title')}")
            print(f"      {str(fact.get('answer'))[:110]}")
        print("\n--- 提取方式分布（可信度就是按它派生的）---")
        for key, count in collections.Counter(
            str(f.get("extraction") or "（空，旧版种子）") for f in facts
        ).most_common():
            print(f"  {key:<16} {count}")
        api_facts = [f for f in facts if str(f.get("extraction") or "") == "api"]
        if api_facts:
            print(f"  其中「结构化字段条目」：{len(api_facts)} 条（模板字段，数值不被概括）")
            for fact in api_facts[:5]:
                print(f"    · {fact.get('title')}｜{str(fact.get('answer'))[:90]}")
        confidences = [float(f.get("confidence") or 0) for f in facts]
        print("\n--- 置信度分布 ---")
        print(f"  最低 {min(confidences):.2f}｜最高 {max(confidences):.2f}｜平均 {statistics.mean(confidences):.2f}")
        buckets = collections.Counter()
        for value in confidences:
            if value >= 0.9:
                buckets["0.90-1.00 明确事实"] += 1
            elif value >= 0.7:
                buckets["0.70-0.89 较明确"] += 1
            elif value >= 0.5:
                buckets["0.50-0.69 带不确定措辞"] += 1
            else:
                buckets["<0.50 含混"] += 1
        for key, count in buckets.most_common():
            print(f"  {key:<22} {count}")

        # 版本/时效覆盖率：这决定了回答期能不能告诉用户「这是 1.3 版本的数据」
        dated = [f for f in facts if str(f.get("effective_from") or "")]
        versioned = [f for f in facts if str(f.get("version") or "")]
        print("\n--- 版本与生效时间（回答期按它标注证据）---")
        print(f"  带生效日期 {len(dated)}/{len(facts)}｜带版本号 {len(versioned)}/{len(facts)}")
        published = [f for f in dated if str(f.get("date_kind") or "") == "published"]
        print(f"  其中来自公告链接的发布日（只会标成「发布于」）{len(published)} 条")
        seen_versions = collections.Counter(str(f.get("version")) for f in versioned)
        if seen_versions:
            print("  版本分布：" + "、".join(f"{k}（{v} 条）" for k, v in sorted(seen_versions.items())))

        print(f"\n--- 条目示例（前 {min(sample, len(facts))} 条）---")
        for fact in facts[:sample]:
            print(f"  [{float(fact.get('confidence') or 0):.2f}] {fact.get('title','')}｜{fact.get('tags','')}")
            print(f"        {fact.get('answer','')[:110]}")
            print(f"        来源：{fact.get('source_url','')[:88]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="构建种子知识库")
    parser.add_argument("--out", default=str(Path("seed") / "seed_kb.json"), help="输出文件")
    parser.add_argument("--sources", default="", help="逗号分隔的数据源 id，默认取全部启用的源")
    parser.add_argument("--per-source-limit", type=int, default=8, help="每个数据源最多抓多少页")
    parser.add_argument("--max-facts-per-page", type=int, default=5, help="每页最多抽取多少条目")
    parser.add_argument("--with-llm", action="store_true", help="调用模型抽取结构化条目（需 .env 配好）")
    parser.add_argument("--keep-work", action="store_true", help="保留中间数据库便于排查")
    parser.add_argument("--resume", action="store_true",
                        help="复用上次的工作库接着抓（被站点限流打断时用，配合 WikiApi 缓存）")
    parser.add_argument("--wait", type=int, default=0,
                        help="开始前先等待 N 秒（等站点冷却结束再抓）")
    parser.add_argument("--base", default="",
                        help="先把已有种子导入工作库再增量抓取（被限流时不会比基线更差）")
    parser.add_argument("--inspect", action="store_true", help="只检查已生成的种子文件，不抓取")
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="确认「这次条目变少是有意的」（例如过滤规则变严），不再另存为 .partial.json",
    )
    args = parser.parse_args()

    if args.inspect:
        target = Path(args.out)
        if not target.is_absolute():
            target = paths.project_root() / target
        return inspect_seed(target)

    work = Path(WORK_DIR).resolve()
    resuming = bool(args.resume) and (work / "knowledge.db").exists()
    if work.exists() and not resuming:
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    if resuming:
        print(f"续抓模式：复用 {work / 'knowledge.db'}（上一轮抓到的内容会保留）\n")
    if args.wait > 0:
        print(f"先等待 {args.wait} 秒，让站点冷却结束…")
        time.sleep(args.wait)

    config = Config(path=work / "config.json")
    config.apply_dev_env(explicit=True)
    config.set("kb", "chunk_size", 700, autosave=False)
    config.set("kb", "chunk_overlap", 100, autosave=False)

    kb = KnowledgeBase(db_file=work / "knowledge.db")
    fetcher = Fetcher(timeout=30, min_interval=2.0)

    if args.base:
        base_path = Path(args.base)
        if not base_path.is_absolute():
            base_path = paths.project_root() / base_path
        if base_path.exists():
            loaded = load_seed(kb, config, base_path)
            print(f"已载入基线种子：文档 {loaded['documents']} 篇，条目 {loaded['facts']} 条"
                  f"（重复跳过 {loaded['duplicates']}）\n")
        else:
            print(f"基线种子不存在，忽略：{base_path}\n")

    llm = None
    if args.with_llm:
        if config.get_secret("key_enc", "llm") and config.get("llm", "model", ""):
            llm = build_llm(config)
            print(f"已启用模型抽取：{config.get('llm', 'provider')} / {config.get('llm', 'model')}")
        else:
            print("警告：未在 .env 中找到模型配置，跳过条目抽取（只入库原文）")

    source_ids = [s.strip() for s in args.sources.split(",") if s.strip()] or None
    if source_ids:
        known = {item["id"] for item in get_sources(include_disabled=True)}
        unknown = [s for s in source_ids if s not in known]
        if unknown:
            print(f"未知的数据源 id：{unknown}")
            return 2

    started = time.time()
    print(f"开始抓取（每源上限 {args.per_source_limit} 页）…\n")
    report = ingest_sources(
        kb,
        config,
        fetcher,
        llm=llm,
        source_ids=source_ids,
        on_progress=lambda message, **extra: print(f"  · {message}"),
        per_source_limit=args.per_source_limit,
        max_facts_per_page=args.max_facts_per_page,
    )

    documents = []
    for doc in kb.list_documents(limit=10000):
        chunks = [row["text"] for row in kb.list_chunks(doc["id"])]
        if not chunks:
            print(f"  ! 跳过无切片的文档：{doc['url']}")
            continue
        documents.append(
            {
                "url": doc["url"],
                "title": doc["title"],
                "site": doc["site"],
                "source_type": doc["source_type"],
                "published_at": doc.get("published_at", ""),
                "content_hash": doc.get("content_hash", ""),
                "meta": json.loads(doc.get("meta") or "{}") if isinstance(doc.get("meta"), str) else (doc.get("meta") or {}),
                "chunks": chunks,
            }
        )

    facts = [
        {
            "title": fact["title"],
            "answer": fact["answer"],
            "topic": fact["topic"],
            "tags": fact["tags"],
            "source_url": fact["source_url"],
            "source_type": fact["source_type"],
            "confidence": fact["confidence"],
            # 提取方式必须跟着种子走：导入时要按它重新派生可信度
            # （api 1.0 > table 0.85 > llm 0.6），也是体检报告的分类依据。
            "extraction": fact.get("extraction", ""),
            # 版本与生效时间也跟种子走（由 versioning 在入库时确定性取出）。
            # 少了这两列，exe 内置资料就退化成「看不出是哪个版本的数据」。
            "version": fact.get("version", ""),
            "effective_from": fact.get("effective_from", ""),
            # 日期类型（生效于/发布于）也要跟种子走，否则重建后老库会丢掉这个区分。
            "date_kind": fact.get("date_kind", ""),
            # 状态必须跟着种子走：`conflict` 标记「两个来源口径冲突、互不覆盖」
            # （见 store_api_facts 的 conflict 分支）。旧实现导出时只留
            # status == "active"，第二个来源的不同值会在生成种子这一步被静默丢掉
            # ——玩一玩 `薄荷·生日 = 6月1日` 与 BWIKI `8月20日` 冲突时，
            # 入库时 status=conflict，导出的种子里却只剩 8月20日，
            # 产品里看不到这条分歧。只有 superseded/已废弃的行才该被丢掉。
            "status": fact.get("status") or "active",
        }
        for fact in kb.list_facts(limit=10000)
        if fact.get("status") in ("active", "conflict")
    ]

    # 种子文件本身不留已知重复：`ensure_seed()` 靠文件指纹决定是否重新导入，
    # 文件一字不改时，老用户升级 exe 后指纹相同 → 不会重新导入 →
    # 新写的合并逻辑不会生效（api.py:68-90 注释里那条教训）。
    deduped = dedupe.merge_seed_facts(facts)
    if deduped["merged"]:
        print(f"近似重复合并：去掉 {deduped['merged']} 条 → {len(deduped['facts'])} 条")
        for _title, _answer in deduped["groups"]:
            print(f"  · {_title} → {_answer[:40]}")
    facts = deduped["facts"]

    payload = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": "由 tools/seed_builder.py 抓取生成；仅含服务端渲染页面与公开 API 的正文。",
        "sources": sorted({doc["source_type"] for doc in documents}),
        "documents": documents,
        "facts": facts,
    }

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = paths.project_root() / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 防退化：抓取受站点限流/封禁影响时，本轮条目会明显变少。
    # 直接覆盖会丢掉上一次的种子（BWIKI 被 WAF 拦下时曾出现
    # 223 条 → 110 条、表格条目 79 → 0），因此改为「先并排保存，由人工决定是否替换」。
    previous: dict | None = None
    if out_path.exists():
        try:
            previous = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            previous = None
    previous_facts = len((previous or {}).get("facts") or [])
    regressed = bool(previous_facts) and len(facts) < previous_facts and not args.allow_shrink
    if regressed:
        out_path = out_path.with_name(f"{out_path.stem}.partial.json")
        print(
            f"\n⚠️ 本次只入库 {len(facts)} 条，少于已有种子的 {previous_facts} 条"
            f"（多半是数据源被限流或封禁）。"
            f"\n⚠️ 已保留原种子不动，本次结果另存为：{out_path.name}"
            f"\n⚠️ 确认无误后再手动替换，避免好数据被一次失败的抓取覆盖。"
        )
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    kb.close()
    if not args.keep_work and not args.resume:
        shutil.rmtree(work, ignore_errors=True)

    size_kb = out_path.stat().st_size / 1024
    print("\n" + "=" * 62)
    print(f"抓取页数      : {report['pages']}（失败 {report['failed']}，跳过 {report['skipped']}）")
    conflict_rows = sum(1 for f in facts if f.get("status") == "conflict")
    print(
        f"入库切片      : {report['chunks']}"
        f"（结构化字段条目 {report.get('api_facts_added', 0)}，"
        f"表格数值条目 {report.get('table_facts_added', 0)}，"
        f"多源确认 {report.get('confirmed', 0)}，"
        f"重复合并 {report.get('duplicates', 0)}，"
        f"抓取失败跳过 {report.get('dropped', 0)}，质量过滤 {report.get('filtered', 0)}）"
    )
    print(f"新增知识条目  : {report['facts_added']}（更新 {report['facts_updated']}，冲突 {report['conflicts']}）")
    if conflict_rows:
        print(f"种子里的冲突  : {conflict_rows} 条（status=conflict，两个来源口径打架，已保留、不互相覆盖）")
    print(f"错误          : {len(report['errors'])}")
    for message in report["errors"][:10]:
        print(f"    - {message}")
    print(f"输出文件      : {out_path}（{size_kb:.1f} KB，文档 {len(documents)} 篇，条目 {len(facts)} 条）")
    print(f"耗时          : {time.time() - started:.1f} 秒")
    # 文档数为 0 不等于失败：像玩一玩角色图鉴这种页面正文只有 70–102 字，
    # 会被「正文过短」当文档跳过，但结构化字段（初始生命/生日…）照样入库。
    # 旧实现只看 report["pages"]，于是这种「只有结构化字段」的源永远返回 1，
    # 让自动化脚本把正常的种子（696 条）当成失败。
    got_facts = report.get("api_facts_added", 0) + report.get("table_facts_added", 0) + report["facts_added"]
    if report["pages"] == 0 and got_facts == 0:
        print("\n警告：没有抓到任何页面，请检查网络与数据源配置。")
        return 1
    if report["pages"] == 0:
        print(
            f"\n注意：没有入库正文文档，但拿到了 {got_facts} 条结构化条目"
            f"（页面正文过短被跳过，不影响字段入库）。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
