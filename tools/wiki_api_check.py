"""结构化接口（BWIKI 模板字段）端到端诊断。

条目里的数值（30.00% / 36 点 / 15 秒 / 商城 18 元礼包）只存在于
条目页模板里，渲染后的图鉴表格只有「效果：详见描述」。这条链路一旦断掉，
知识库不会报错、只会「数值都不见了」，所以必须能单独跑、单独看。

    python tools\\wiki_api_check.py --limit 3          # 小样本连通性
    python tools\\wiki_api_check.py --limit 46         # 全量（约 4 分钟，站点限速 5 秒/次）
    python tools\\wiki_api_check.py --list             # 只看有哪些接口源
    python tools\\wiki_api_check.py --source bwiki_api_character --limit 24
    python tools\\wiki_api_check.py --source bwiki_api_arc --limit 46 \\
        --interval 15 --wait 600 --rounds 8            # 被 WAF 拦时放慢并续跑

中文结果写进 UTF-8 报告文件（控制台是 GBK，直接打印会变成乱码），
默认 `.wiki_api_check/report.txt`，可用 `--out` 改路径。
缓存落在 <data-dir>/cache/wiki_api，重跑直接读缓存、被站点限流打断时也能续上。
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config  # noqa: E402
from app.core import wiki_api  # noqa: E402
from app.core.fetch import build_fetcher  # noqa: E402
from app.core.ingest import ingest_sources  # noqa: E402
from app.core.sources import get_source, get_sources  # noqa: E402
from app.core.store import KnowledgeBase  # noqa: E402

WORK = Path(".wiki_api_check")


def main() -> int:
    args = sys.argv[1:]

    def option(name: str, default: str = "") -> str:
        if name in args:
            index = args.index(name)
            if index + 1 < len(args):
                return args[index + 1]
        return default

    lines: list = []

    def say(message: str = "") -> None:
        lines.append(message)

    api_sources = [item for item in get_sources() if item.get("kind") == "mw_api"]
    out_file = Path(option("--out", str(WORK / "report.txt")))

    if "--category" in args:
        # 只查分类成员：用来确认分类名到底叫什么（实测「角色」返回 0，「角色图鉴」才有）
        names = [option("--category")]
        if "--also" in args:
            names.extend(option("--also").split(","))
        fetcher = build_fetcher(Config(path=WORK / "config.json"))
        api = wiki_api.WikiApi(fetcher, api_sources[0]["url"], cache_dir=wiki_api.default_cache_dir())
        for name in names:
            members = api.category_members(name.strip(), limit=200)
            say(f"Category:{name.strip()} → {len(members)} 条：{'、'.join(members[:12])}"
                f"{'　错误：' + api.last_error if not members and api.last_error else ''}")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("\n".join(lines), encoding="utf-8")
        print(f"report -> {out_file}")
        return 0

    if "--list" in args or not api_sources:
        for item in api_sources:
            say(f"{item['id']}　{item['name']}　分类={item.get('category')}　模板={item.get('templates')}")
        Path(option("--out", str(WORK / "report.txt"))).parent.mkdir(parents=True, exist_ok=True)
        Path(option("--out", str(WORK / "report.txt"))).write_text("\n".join(lines), encoding="utf-8")
        print(f"api sources: {len(api_sources)} -> report written")
        return 0

    source_id = option("--source", api_sources[0]["id"])
    source = get_source(source_id)
    if not source:
        print(f"unknown source: {source_id}")
        return 2

    limit = int(option("--limit", "3"))
    offline = "--offline" in args
    wait_seconds = float(option("--wait", "0"))
    rounds = int(option("--rounds", "1"))
    if wait_seconds > 0:
        rounds = max(rounds, 2)
    out_file = Path(option("--out", str(WORK / "report.txt")))
    say(f"数据源：{source['name']}（{source_id}）")
    say(f"分类：{source.get('category')}　模板：{source.get('templates')}　上限：{limit}（每页一次请求，间隔 5 秒）")

    config = Config(path=WORK / "config.json")
    fetcher = build_fetcher(config)
    interval = float(option("--interval", "0"))
    if interval > 0:
        # 站点进入 WAF 惩罚期时，默认的 5 秒间隔会被连续拒绝；
        # 放慢到 15~20 秒往往能恢复「每轮前进几页」的节奏。
        fetcher.slow_min_interval = interval
        fetcher.min_interval = interval
    if offline:
        fetcher.min_interval = 0.0
        fetcher.slow_min_interval = 0.0

    cache_dir = wiki_api.default_cache_dir()
    cache_files = len(list(cache_dir.glob("*.json"))) if cache_dir and cache_dir.exists() else 0
    say(f"缓存目录：{cache_dir}（已有 {cache_files} 个条目缓存）")
    say("")

    shutil.rmtree(WORK / "kb", ignore_errors=True)
    (WORK / "kb").mkdir(parents=True, exist_ok=True)
    kb = KnowledgeBase(db_file=WORK / "kb" / "knowledge.db")

    def progress(message: str, **extra: object) -> None:
        say(f"  · {message}")

    try:
        import time

        for round_index in range(1, rounds + 1):
            if rounds > 1:
                say(f"—— 第 {round_index}/{rounds} 轮（缓存命中即不发请求）——")
            report = ingest_sources(
                kb,
                config,
                fetcher=fetcher,
                source_ids=[source_id],
                per_source_limit=limit,
                max_facts_per_page=0,      # 不用模型：这条链路本来就是确定性的
                on_progress=progress,
            )
            say("")
            say(f"本轮结果：页面 {report['pages']}｜结构化条目 {report['api_facts_added']} 条"
                f"｜表格条目 {report['table_facts_added']} 条｜被过滤 {report['filtered']} 页"
                f"｜抓取失败 {report['dropped']}")
            for item in report.get("errors", [])[:10]:
                say(f"  ！{item}")
            blocked = [item for item in report.get("errors", []) if "冷却" in item or "567" in item]
            if not blocked or round_index == rounds or wait_seconds <= 0:
                break
            # 站点 WAF 触发后整域冷却 600 秒；这里明着等，等完继续用缓存续跑，
            # 免得因一次拦截丢掉整轮进度。
            say(f"  站点限流中，等待 {wait_seconds:.0f} 秒后继续（缓存已保留）…")
            time.sleep(wait_seconds)
    except KeyboardInterrupt:
        say("  收到中断，已保留缓存与已入库条目")
    finally:
        rows = kb.list_facts(limit=300)
        api_rows = [row for row in rows if (row.get("extraction") or "") == "api"]
        say("")
        say(f"库内条目 {len(rows)} 条，其中结构化接口 {len(api_rows)} 条")
        for row in api_rows[:8]:
            say(f"  · [{row['confidence']:.2f}] {row['title']} → {(row['answer'] or '')[:100]}")
        if api_rows:
            confidences = sorted(float(row["confidence"]) for row in api_rows)
            say(f"可信度区间：{confidences[0]:.2f} ~ {confidences[-1]:.2f}")
            say(f"提取方式分布：table={sum(1 for r in rows if r.get('extraction') == 'table')}"
                f" api={len(api_rows)}"
                f" llm={sum(1 for r in rows if r.get('extraction') == 'llm')}"
                f" 空={sum(1 for r in rows if not r.get('extraction'))}")
            titles = sorted({(row['title'] or '').split('·')[0] for row in api_rows})
            say(f"覆盖条目 {len(titles)} 个：{'、'.join(titles[:20])}")
        kb.close()

        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("\n".join(lines), encoding="utf-8")
        cached = len(list(cache_dir.glob("*.json"))) if cache_dir and cache_dir.exists() else 0
        print(f"api facts: {len(api_rows)}  cache: {cached}  report -> {out_file}")
    return 0 if api_rows else 1


if __name__ == "__main__":
    sys.exit(main())
