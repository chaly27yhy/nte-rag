"""知识库体检报告：直接分析 seed/seed_kb.json，输出可复核的数据画像。

用法：
    .venv\\Scripts\\python.exe tools\\kb_report.py
    .venv\\Scripts\\python.exe tools\\kb_report.py --seed seed\\seed_kb.json --json eval\\kb_report.json

检查项：
  1. 规模与来源结构（高等级来源占比）
  2. 可追溯性（是否有来源链接 / 标签 / 抓取时间）
  3. 置信度分布是否真的有区分度
  4. 重复条目（同标题近似答案）——现有 SimHash 阈值抓不住的那类
  5. 冲突候选：同一标题下同单位数值不一致（按标题作用域，噪声低）
  6. 时效敏感条目（限时/截止/活动时间）
  7. 切片长度分布
"""

import argparse
import difflib
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import _console  # noqa: F401  (GBK 控制台安全打印)

ROOT = Path(__file__).resolve().parent.parent

TIER_OF = {"official": "official", "manual": "manual", "wiki": "wiki", "seed": "seed", "community": "community"}

NUM_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(异晶|元|%|％|秒|分钟|小时|天|次|名|个|层|级|抽)")
DATE_PAT = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}\s*年")
TIME_SENSITIVE = re.compile(r"限时|截止|折扣时间|活动时间|将于|下架|上架|维护更新后|开放时间|结束时间|在售")
PUNCT = re.compile(r"[\s，。、；：！？,.;:!?（）()【】\[\]「」『』《》\-—~～·…\"'`]")


def norm(text):
    return PUNCT.sub("", str(text or "")).lower()


def similarity(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def load(seed_path):
    data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    return data


def section(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default=str(ROOT / "seed" / "seed_kb.json"))
    ap.add_argument("--json", default="", help="把报告另存为 JSON")
    args = ap.parse_args()

    data = load(args.seed)
    docs = data.get("documents") or []
    facts = data.get("facts") or []
    chunks = data.get("chunks") or [c for d in docs for c in (d.get("chunks") or [])]
    print(f"种子文件：{args.seed}")
    print(f"生成时间：{data.get('generated_at') or data.get('generated') or '(未记录)'}")
    print(f"规模：{len(docs)} 篇文档 / {len(chunks)} 段切片 / {len(facts)} 条条目"
          f"（{Path(args.seed).stat().st_size / 1024:.1f} KB）")

    # 1 来源结构
    section("【1】来源结构与可追溯性")
    tiers = Counter(TIER_OF.get(str(f.get("source_type") or "?").lower(), str(f.get("source_type") or "?"))
                    for f in facts)
    for name, count in tiers.most_common():
        print(f"  {name:<10} {count:>4} 条  ({count / max(len(facts), 1) * 100:.1f}%)")
    domains = Counter()
    for f in facts:
        url = str(f.get("source_url") or "")
        m = re.search(r"https?://([^/]+)", url)
        domains[m.group(1) if m else "(无来源)"] += 1
    print("  域名 TOP：" + "、".join(f"{d} {c}" for d, c in domains.most_common(8)))
    missing_url = sum(1 for f in facts if not f.get("source_url"))
    missing_tag = sum(1 for f in facts if not f.get("tags"))
    print(f"  无来源链接：{missing_url} 条；无标签：{missing_tag} 条")

    # 2 置信度
    section("【2】置信度分布（是否具备区分度）")
    buckets = Counter()
    confs = []
    for f in facts:
        try:
            c = float(f.get("confidence"))
        except (TypeError, ValueError):
            buckets["缺失"] += 1
            continue
        confs.append(c)
        buckets[f"{int(c * 10) / 10:.1f}"] += 1
    for key in sorted(buckets, reverse=True):
        print(f"  {key:<6} {buckets[key]:>4} 条")
    if confs:
        print(f"  中位 {statistics.median(confs):.2f}，唯一取值 {len(set(confs))} 个")
        top = max(confs)
        high = sum(1 for c in confs if c >= 0.9)
        print(f"  0.90 以上占 {high / len(confs) * 100:.1f}%（说明几乎不区分来源等级）")

    # 3 重复簇
    section("【3】近似重复条目（同标题）")
    by_title = defaultdict(list)
    for f in facts:
        by_title[norm(f.get("title"))].append(f)
    dup_pairs = []
    for key, group in by_title.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                s = similarity(group[i].get("answer"), group[j].get("answer"))
                dup_pairs.append((s, group[i], group[j]))
    dup_pairs.sort(key=lambda x: -x[0])
    if not dup_pairs:
        print("  同标题重复：无")
    for s, a, b in dup_pairs[:15]:
        print(f"  相似度 {s:.2f}  《{a.get('title')}》  [{a.get('source_type')}] vs [{b.get('source_type')}]")
        print(f"      A: {str(a.get('answer'))[:80]}")
        print(f"      B: {str(b.get('answer'))[:80]}")
    print(f"  同标题组数：{sum(1 for g in by_title.values() if len(g) > 1)}，条目对 {len(dup_pairs)}")

    # 4 冲突候选（标题作用域）
    section("【4】冲突候选：同一标题下同单位数值不一致（低噪声口径）")
    conflicts = []
    for key, group in by_title.items():
        values = defaultdict(set)
        for f in group:
            for value, unit in NUM_UNIT.findall(str(f.get("answer") or "")):
                values[unit].add(value)
        for unit, vals in values.items():
            if len(vals) > 1:
                conflicts.append((group[0].get("title"), unit, sorted(vals), group))
    if not conflicts:
        print("  未发现同标题数值冲突")
    for title, unit, vals, group in conflicts[:20]:
        print(f"  《{title}》 单位「{unit}」出现多个值：{vals}")
        for f in group:
            print(f"      [{f.get('source_type')}] {str(f.get('answer'))[:90]}")

    # 5 时效性
    section("【5】时效敏感条目")
    ts = [f for f in facts if TIME_SENSITIVE.search(str(f.get("answer") or ""))]
    dated = [f for f in facts if DATE_PAT.search(str(f.get("answer") or ""))]
    print(f"  含限时/截止/活动时间措辞：{len(ts)} 条（{len(ts) / max(len(facts), 1) * 100:.1f}%）")
    print(f"  答案中出现明确日期：{len(dated)} 条")
    for f in ts[:8]:
        print(f"      [{f.get('source_type')}] {str(f.get('title'))[:30]} — {str(f.get('answer'))[:70]}")

    # 6 长度与短条目
    section("【6】条目与切片长度")
    lens = [len(str(f.get("answer") or "")) for f in facts]
    if lens:
        print(f"  条目长度：中位 {statistics.median(lens):.0f}，均值 {statistics.mean(lens):.0f}，"
              f"最短 {min(lens)}，最长 {max(lens)}")
        print(f"  短于 40 字：{sum(1 for x in lens if x < 40)} 条")
    clens = [len(str(c.get("text") or "")) for c in chunks]
    if clens:
        print(f"  切片长度：中位 {statistics.median(clens):.0f}，均值 {statistics.mean(clens):.0f}，最长 {max(clens)}")

    # 7 表格来源条目
    section("【7】表格/数值条目")
    numeric = [f for f in facts if NUM_UNIT.search(str(f.get("answer") or ""))]
    print(f"  含数值+单位的条目：{len(numeric)} 条（{len(numeric) / max(len(facts), 1) * 100:.1f}%）")

    if args.json:
        payload = {
            "seed": str(args.seed),
            "generated_at": data.get("generated_at"),
            "documents": len(docs),
            "chunks": len(chunks),
            "facts": len(facts),
            "tiers": dict(tiers),
            "domains": dict(domains),
            "missing_url": missing_url,
            "missing_tag": missing_tag,
            "confidence_unique": len(set(confs)),
            "confidence_high_ratio": (sum(1 for c in confs if c >= 0.9) / len(confs)) if confs else None,
            "dup_title_groups": sum(1 for g in by_title.values() if len(g) > 1),
            "dup_pairs": [{"similarity": round(s, 3), "title": a.get("title")} for s, a, _ in dup_pairs],
            "conflicts": [{"title": t, "unit": u, "values": v} for t, u, v, _ in conflicts],
            "time_sensitive": len(ts),
            "dated": len(dated),
            "numeric_facts": len(numeric),
        }
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
