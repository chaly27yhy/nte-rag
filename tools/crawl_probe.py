"""数据源抓取探针：单源试抓，打印每页抽取结果，用于判断站点是否改版。

用法：
    python tools\\crawl_probe.py --list
    python tools\\crawl_probe.py --source official_lore
    python tools\\crawl_probe.py --source official_news --limit 3 --show 200
    python tools\\crawl_probe.py --url https://example.com/article --container .content

只做抓取与抽取，不写入知识库，便于在开发机上安全排查。
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.fetch import Fetcher, extract_variants  # noqa: E402
from app.core.sources import get_source, get_sources, iter_source_pages  # noqa: E402


def print_table_analysis(text: str, title: str, raw_limit: int = 0) -> int:
    """打印一段正文的表格解析结果，返回抽出的条目数。

    `--url`、`--source`、`--from-seed` 三个入口共用，避免只有一条路径能验证表格。
    """
    from app.core import tables as table_mod

    body = text or ""
    print(f"表格行占比：{table_mod.table_density(body):.2f}")
    if raw_limit:
        raw_rows = [line for line in body.splitlines() if table_mod.is_table_row(line)]
        print(f"\n-- 原始表格行（前 {raw_limit} 行，用于核对结构）--")
        for line in raw_rows[:raw_limit]:
            sep = "  ← 判定为分隔行" if table_mod.is_separator_row(line) else ""
            print(f"   {line[:110]}{sep}")
    parsed = table_mod.parse_tables(body)
    print(f"解析出 {len(parsed)} 张表")
    for index, (header, rows) in enumerate(parsed[:3], start=1):
        print(f"\n== 表 {index}：{len(header)} 列 × {len(rows)} 行 ==")
        print(f"   表头：{header}")
        for row in rows[:3]:
            print(f"   行  ：{row[:4]}")
    facts = table_mod.extract_table_facts(body, title or "页面")
    print(f"\n抽出条目 {len(facts)} 条")
    for fact in facts[:5]:
        print(f"   · {fact['title']}")
        print(f"     {fact['answer'][:120]}")
    return len(facts)


def main() -> int:
    parser = argparse.ArgumentParser(description="数据源抓取探针")
    parser.add_argument("--list", action="store_true", help="列出内置数据源")
    parser.add_argument("--source", help="内置数据源 id")
    parser.add_argument("--url", help="直接抓取某个网址")
    parser.add_argument("--container", default="", help="正文容器选择器，例如 .mw-parser-output")
    parser.add_argument("--compare", action="store_true", help="对比多种正文抽取策略（诊断用）")
    parser.add_argument("--links", action="store_true", help="列出页面链接形态统计（修 link_pattern 用）")
    parser.add_argument(
        "--find",
        default="",
        help="在抓到的正文里查找关键词并打印上下文（诊断「这个数值到底在不在页面上」）",
    )
    parser.add_argument(
        "--from-seed",
        action="store_true",
        help="配合 --tables：改为解析种子知识库里的切片（离线，不受站点限流影响）",
    )
    parser.add_argument(
        "--tables",
        action="store_true",
        help="解析页面里的表格并打印（诊断「表格为什么没变成条目」）",
    )
    parser.add_argument(
        "--mw-pages",
        action="store_true",
        help="配合 --url 指向 MediaWiki api.php：列出全站页面标题（不抓正文，用于发现图鉴类页面）",
    )
    parser.add_argument("--limit", type=int, default=3, help="最多抓取页数")
    parser.add_argument("--show", type=int, default=160, help="每页打印正文字符数")
    parser.add_argument(
        "--no-robots",
        action="store_false",
        dest="respect_robots",
        default=True,
        help="不检查 robots.txt（仅用于本地排查；默认遵守，别拿它去抓生产站点）",
    )
    args = parser.parse_args()

    if args.list:
        for source in get_sources(include_disabled=True):
            flag = "启用" if source.get("enabled") else "停用"
            print(f"{source['id']:<22} [{flag}] {source['source_type']:<9} {source['name']}\n{'':<24}{source['url']}")
        return 0

    fetcher = Fetcher(timeout=30, respect_robots=args.respect_robots)

    if args.url or args.from_seed:
        if args.tables and args.from_seed:
            import json

            from app.core import tables as table_mod

            seed_path = Path(__file__).resolve().parents[1] / "seed" / "seed_kb.json"
            if not seed_path.exists():
                print(f"找不到种子知识库：{seed_path}（请先运行 tools\\seed_builder.py）")
                return 2
            payload = json.loads(seed_path.read_text(encoding="utf-8"))
            total_tables = 0
            total_facts = 0
            for document in payload.get("documents") or []:
                text = "\n".join(document.get("chunks") or [])
                density = table_mod.table_density(text)
                parsed = table_mod.parse_tables(text)
                facts = table_mod.extract_table_facts(text, document.get("title", ""))
                total_tables += len(parsed)
                total_facts += len(facts)
                if parsed or density > 0.05:
                    print(f"\n【{document.get('title', '')[:44]}】表格行占比 {density:.2f}，"
                          f"解析出 {len(parsed)} 张表 → {len(facts)} 条条目")
                    for header, rows in parsed[:2]:
                        print(f"    表头（{len(header)} 列 × {len(rows)} 行）：{header}")
                        for row in rows[:2]:
                            print(f"      {row[:4]}")
            print(f"\n合计：{total_tables} 张表，可抽出 {total_facts} 条表格条目")
            if total_facts:
                print("\n示例条目：")
                for document in payload.get("documents") or []:
                    for fact in table_mod.extract_table_facts("\n".join(document.get("chunks") or []), document.get("title", ""))[:2]:
                        print(f"  · {fact['title']}")
                        print(f"    {fact['answer'][:120]}")
            return 0
        if args.url and args.tables:
            result = fetcher.fetch(args.url, container_selectors=(args.container,) if args.container else ())
            print(f"URL: {args.url}　正文 {len(result.text or '')} 字符")
            print_table_analysis(result.text or "", result.title or "页面", raw_limit=6)
            return 0
        if args.mw_pages:
            from app.core.sources import _mw_allpages

            titles = _mw_allpages(
                {"url": args.url, "name": args.url}, fetcher, max(1, args.limit * 20)
            )
            print(f"共 {len(titles)} 个页面：")
            for index, title in enumerate(titles, start=1):
                print(f"  {index:>4}. {title}")
            return 0
        if args.find:
            result = fetcher.fetch(args.url, container_selectors=(args.container,) if args.container else ())
            print(f"URL: {args.url}")
            print(f"状态 {result.status}　标题 {result.title}　正文字符 {len(result.text or '')}")
            keywords = [k.strip() for k in args.find.split(",") if k.strip()]
            for keyword in keywords:
                hits = [line.strip() for line in (result.text or "").splitlines() if keyword in line]
                print(f"\n== 关键词「{keyword}」命中 {len(hits)} 行 ==")
                for line in hits[:8]:
                    print(f"   {line[:220]}")
                if not hits:
                    print("   （正文中未出现——说明该页没有这个数值，或抽取时被丢掉了）")
            return 0
        if args.links:
            from collections import Counter
            from urllib.parse import urlparse, urljoin

            from bs4 import BeautifulSoup

            html = fetcher.get_html(args.url)
            soup = BeautifulSoup(html, "lxml")
            patterns: Counter = Counter()
            samples: dict = {}
            for anchor in soup.find_all("a", href=True):
                href = urljoin(args.url, anchor["href"].strip())
                if not href.startswith("http"):
                    continue
                path = urlparse(href).path
                # 把数字段归一化，便于看出链接模板
                normalized = re.sub(r"\d+", "{N}", path)
                patterns[normalized] += 1
                samples.setdefault(normalized, href)
            print(f"HTML 长度：{len(html)}，链接模板统计（Top 20）：")
            for pattern, count in patterns.most_common(20):
                print(f"{count:5}  {pattern}")
                print(f"       例：{samples[pattern]}")
            return 0
        if args.compare:
            html = fetcher.get_html(args.url)
            selectors = [args.container] if args.container else [".mw-parser-output", "article", ".content"]
            print(f"HTML 长度：{len(html)}")
            for variant in extract_variants(html, args.url, tuple(selectors)):
                print(f"\n[{variant['strategy']}] {variant['chars']} 字符")
                print("  " + (variant["preview"] or "").replace("\n", " ")[: args.show])
            return 0
        result = fetcher.fetch(args.url)
        print(f"URL      : {args.url}")
        print(f"状态     : {result.status}  错误: {result.error or '无'}")
        print(f"标题     : {result.title}")
        print(f"发布时间 : {result.published}")
        print(f"正文字符 : {len(result.text)}")
        print("-" * 60)
        print((result.text or "")[: args.show * 4])
        return 0 if result.ok else 1

    if not args.source:
        parser.print_help()
        return 2

    source = get_source(args.source)
    if not source:
        print(f"未找到数据源：{args.source}")
        return 2

    print(f"数据源：{source['name']}（{source['kind']}）")
    print(f"地址  ：{source['url']}")

    def progress(message: str) -> None:
        print(f"  · {message}")

    count = 0
    failures = 0
    dropped: list = []
    for page in iter_source_pages(
        source, fetcher, on_progress=progress, limit=args.limit, on_drop=dropped.append
    ):
        count += 1
        preview = (page.text or "").replace("\n", " ")[: args.show]
        print(f"\n[{count}] {page.title or '(无标题)'}")
        print(f"    URL   : {page.url}")
        print(f"    字符数: {len(page.text or '')}  时间: {page.published or '-'}")
        print(f"    预览  : {preview}")
        if len(page.text or "") < 200:
            failures += 1
        if args.tables:
            print("    ---- 表格解析 ----")
            print_table_analysis(page.text or "", page.title or "", raw_limit=4)

    for reason in dropped[:10]:
        print(f"  ⚠️ 抓取失败已跳过：{reason}")
    if len(dropped) > 10:
        print(f"  ⚠️ 另有 {len(dropped) - 10} 个页面被跳过")

    print(f"\n共取得 {count} 页，其中疑似过短 {failures} 页，抓取失败跳过 {len(dropped)} 页")
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main())
