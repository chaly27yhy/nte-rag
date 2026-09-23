"""结构化字段体检：哪些字段真的在区分实体，哪些只是模板默认值。

背景（实测踩到的坑）
--------------------
BWIKI 的角色图鉴模板里，`最高生命` / `最高攻击` 这些字段**每个角色都是同一个数**
（145784 / 8424），它们是模板占位值，不是角色属性。如果照单全收，知识库里就会多出
「哈尼娅的最高攻击 = 8424」「九原的最高攻击 = 8424」这种**看起来精确、其实全错**的条目 ——
这正是最危险的一类脏数据（比缺数据更糟：模型会自信地答错）。

所以判定规则是：一个字段如果在**同一分类的多数页面里取值完全相同**（默认 ≥80%、至少 4 页），
就按模板默认值处理，不入库、也不写进页面正文。

    .venv\\Scripts\\python.exe tools\\field_report.py                  # 读缓存目录
    .venv\\Scripts\\python.exe tools\\field_report.py --share 0.6 --min-pages 3
    .venv\\Scripts\\python.exe tools\\field_report.py --cache data\\cache\\wiki_api --out .field_report\\report.txt

--caliber：BWIKI 的「生命/攻击」和玩一玩的「初始生命/初始攻击」能不能当同一个指标？
----------------------------------------------------------------------------
薄荷生日那次裁定后留下的第三个待查项就是这个。结论是**不能**（所以这 8 条槽位永久
标注「口径未知、不跨源比对」）；实测：小吱两边完全相同（1280/83），
但九原/浔差 4~10 倍且倍数不成比例，`最高生命/最高攻击` 又是全页同值的模板常量，
反推不出「BWIKI 填的是哪一档」。

    .venv\\Scripts\\python.exe tools\\field_report.py --caliber
    .venv\\Scripts\\python.exe tools\\field_report.py --caliber --seed seed\\seed_kb.json
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import wiki_api  # noqa: E402

WORK = Path(".field_report")
# 用哪个模板解析：一个 wiki 上不同分类用不同模板，字段集合不同，
# 谁解析出的字段多就用谁（同一页一般只对应一个信息框模板）。
CANDIDATE_TEMPLATES = ["角色图鉴", "弧盘"]
# --caliber 用的字段对：(种子里的字段, BWIKI 模板里的字段)
CALIBER_FIELDS = (("初始生命", "生命"), ("初始攻击", "攻击"))
FACT_NUMBER_RE = re.compile(r"[：:]\s*(-?\d+(?:\.\d+)?)")


def _load_pages(cache_dir: Path) -> Dict[str, str]:
    """把缓存里的 {页面标题: wikitext} 读出来（兼容批量与单页两种写法）。"""
    pages: Dict[str, str] = {}
    for path in sorted(cache_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        # 缓存文件的真实结构是 {"url":…, "fetched_at":…, "data":{…}}，
        # 里面的 data 才是接口原始响应（早期版本按裸响应解析，一页也读不出来）
        payload = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        parse = payload.get("parse") or {}
        title = str(parse.get("title") or "")
        wikitext = ((parse.get("wikitext") or {}).get("*")) or ""
        if title and wikitext:
            pages[title] = wikitext
            continue
        # action=query（批量）的原样响应：query.pages 里带 revisions。
        # MediaWiki 标准形态是**对象**（pageid → 页面），老版本是数组，两种都要吃。
        raw_pages = (payload.get("query") or {}).get("pages") or []
        if isinstance(raw_pages, dict):
            raw_pages = list(raw_pages.values())
        for page in raw_pages:
            if not isinstance(page, dict):
                continue
            name = str(page.get("title") or "")
            revisions = page.get("revisions") or []
            content = ""
            if revisions:
                content = ((revisions[0].get("slots") or {}).get("main") or {}).get("*") \
                    or revisions[0].get("*") or ""
            if name and content:
                pages[name] = content
    return pages


def analyze(pages: Dict[str, str], share: float, min_pages: int) -> List[Dict[str, Any]]:
    """按模板分组做字段值分布统计。"""
    groups: Dict[str, List[Tuple[str, List[Tuple[str, str]]]]] = defaultdict(list)
    for title, raw in pages.items():
        best: List[Tuple[str, str]] = []
        best_template = ""
        for template in CANDIDATE_TEMPLATES:
            pairs = wiki_api.template_fields(raw, templates=[template])
            if len(pairs) > len(best):
                best, best_template = pairs, template
        if best_template and len(best) >= 2:
            groups[best_template].append((title, best))

    report: List[Dict[str, Any]] = []
    mismatches: List[str] = []
    for template, items in sorted(groups.items()):
        values: Dict[str, Counter] = defaultdict(Counter)
        for title, pairs in items:
            for key, value in pairs:
                values[key][value] += 1
            # 串页自检：模板里的「名称/姓名/称号/弧盘名」必须等于页面标题。
            # 批量接口取原文时如果张冠李戴，这里会立刻暴露（实测未出现，但必须能测）。
            for key in ("名称", "姓名", "称号", "弧盘名"):
                if key in dict(pairs) and dict(pairs)[key] != title:
                    mismatches.append(f"{title}：{key}={dict(pairs)[key]}")
        rows = []
        for key, counter in values.items():
            total = sum(counter.values())
            top_value, top_count = counter.most_common(1)[0]
            rows.append({
                "field": key,
                "pages": total,
                "distinct": len(counter),
                "top_value": top_value,
                "top_share": round(top_count / total, 3) if total else 0.0,
                "constant": total >= min_pages and top_count / total >= share,
            })
        rows.sort(key=lambda row: (-row["top_share"], row["field"]))
        report.append({"template": template, "pages": len(items), "fields": rows})
    return report, mismatches


def _seed_values(seed_path: Path, field: str) -> Dict[str, float]:
    """从种子里取 `<实体>·<字段>` 的数值（答案形如「小吱 的初始生命为：1280」）。"""
    try:
        payload = json.loads(seed_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    suffix = "·" + field
    values: Dict[str, float] = {}
    for fact in payload.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        title = str(fact.get("title") or "")
        if not title.endswith(suffix):
            continue
        match = FACT_NUMBER_RE.search(str(fact.get("answer") or ""))
        if match:
            values[title[: -len(suffix)]] = float(match.group(1))
    return values


def caliber_report(pages: Dict[str, str], seed_path: Path, share: float, min_pages: int) -> List[str]:
    """口径对照：BWIKI 角色图鉴的 生命/攻击 vs 玩一玩角色页的 初始生命/初始攻击。"""
    bwiki: Dict[str, Dict[str, str]] = {}
    for title, raw in pages.items():
        fields = dict(wiki_api.template_fields(raw, templates=["角色图鉴"]))
        if fields.get("生命") or fields.get("攻击"):
            bwiki[title] = fields
    seed_values = {field: _seed_values(seed_path, field) for field, _ in CALIBER_FIELDS}

    lines: List[str] = [
        "口径对照：BWIKI 角色图鉴模板的「生命/攻击」 vs 玩一玩角色页的「初始生命/初始攻击」",
        f"（缓存里带角色的页 {len(bwiki)} 张；种子 {seed_path}）",
        "问题：这两组数能不能当同一个指标跨源比对？",
        "",
        f"{'实体':<10}{'BWIKI生命':>10}{'BWIKI攻击':>10}  |{'玩一玩初始生命':>14}{'初始攻击':>10}  |{'比值':>16}  判定",
    ]
    entities = sorted(set(bwiki) | set(seed_values["初始生命"]))
    common = matched = 0
    for name in entities:
        fields = bwiki.get(name) or {}
        b_life = str(fields.get("生命") or "").strip()
        b_atk = str(fields.get("攻击") or "").strip()
        s_life = seed_values["初始生命"].get(name)
        s_atk = seed_values["初始攻击"].get(name)
        ratio = ""
        if b_life and s_life:
            common += 1
            try:
                life_ratio = float(b_life) / s_life if s_life else 0.0
                atk_ratio = (float(b_atk) / s_atk) if (b_atk and s_atk) else 0.0
            except ValueError:
                ratio, verdict = "无法比较", "非数字"
            else:
                ratio = f"{life_ratio:.2f}× / {atk_ratio:.2f}×"
                # 两列都要看：过去只判生命比率，于是「生命对得上、攻击对不上」也会报「一致」。
                # 某一侧缺攻击值时（b_atk/s_atk 为空）没有可比对象，不算不一致。
                atk_ok = (not b_atk) or (not s_atk) or abs(atk_ratio - 1) < 1e-9
                verdict = "一致" if (abs(life_ratio - 1) < 1e-9 and atk_ok) else "不一致"
                if verdict == "一致":
                    matched += 1
        elif b_life or s_life:
            verdict = "只有单边"
        else:
            verdict = ""
        lines.append(
            f"{name:<10}{b_life or '-':>10}{b_atk or '-':>10}  |"
            f"{(str(int(s_life)) if s_life is not None else '-'):>14}"
            f"{(str(int(s_atk)) if s_atk is not None else '-'):>10}  |{ratio:>16}  {verdict}"
        )

    # 模板常量：同一字段在多数页面取值相同 → 是占位值，不能拿来反推档位。
    constants: List[str] = []
    total_pages = len(bwiki)
    for field in ("生命", "攻击", "最高生命", "最高攻击"):
        counter = Counter(str((fields or {}).get(field) or "").strip() for fields in bwiki.values())
        counter.pop("", None)
        seen = sum(counter.values())
        if not counter or seen < min_pages:
            continue
        top_value, top_count = counter.most_common(1)[0]
        if top_count / seen >= share:
            constants.append(f"{field}={top_value}（{top_count}/{seen} 页同值）")
    lines += [
        "",
        f"共同实体 {common} 个：两边一致 {matched}，不一致 {common - matched}",
        f"全页同值（模板常量，反推不出档位）：{'、'.join(constants) or '无'}",
    ]
    if not common:
        lines.append("结论：没有可对照的共同实体，无法判断口径。")
    elif matched == common:
        lines.append("结论：两边完全一致，可以当同一个指标跨源比对。")
    else:
        lines.append(
            "结论：两边不一致且比值不成比例 → 不能当同一个指标，"
            "这 8 条槽位维持「口径未知（不跨源比对）」。"
        )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="结构化字段分布体检")
    parser.add_argument("--cache", default=str(Path("data") / "cache" / "wiki_api"))
    parser.add_argument("--share", type=float, default=0.8, help="取值集中度阈值（默认 0.8）")
    parser.add_argument("--min-pages", type=int, default=4, help="至少多少页才判定（默认 4）")
    parser.add_argument("--out", default="")
    parser.add_argument("--caliber", action="store_true",
                        help="口径对照：BWIKI 生命/攻击 vs 玩一玩 初始生命/初始攻击")
    parser.add_argument("--seed", default=str(Path("seed") / "seed_kb.json"))
    args = parser.parse_args()

    cache_dir = Path(args.cache)
    if not cache_dir.exists():
        print(f"缓存目录不存在：{cache_dir}")
        return 2
    pages = _load_pages(cache_dir)
    if not pages:
        print(f"缓存里没有页面原文：{cache_dir}")
        return 1

    if args.caliber:
        lines = caliber_report(pages, Path(args.seed), args.share, args.min_pages)
        out_path = Path(args.out) if args.out else WORK / "caliber.txt"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines[4:]))
        print(f"-> {out_path}")
        return 0

    report, mismatches = analyze(pages, args.share, args.min_pages)
    lines: List[str] = [
        f"缓存页面 {len(pages)} 页　阈值：集中度 ≥{args.share} 且页数 ≥{args.min_pages} 视为模板默认值",
        "",
    ]
    constants: List[str] = []
    for group in report:
        lines.append(f"===== 模板 {group['template']}（{group['pages']} 页）=====")
        for row in group["fields"]:
            mark = "默认值" if row["constant"] else "区分实体"
            lines.append(
                f"  [{mark}] {row['field']}：{row['pages']} 页 / {row['distinct']} 个取值"
                f"　最常见 {row['top_value'][:40]!r}（{row['top_share']:.0%}）"
            )
            if row["constant"]:
                constants.append(f"{group['template']}.{row['field']}")
        lines.append("")
    lines.append(f"疑似模板默认值字段（{len(constants)}）：{'、'.join(constants) or '无'}")
    lines.append("")
    # 串页自检：批量取原文时如果标题和内容对不上，这里必须能看见（实测 0 条才算干净）。
    lines.append(f"标题与模板名称不一致（{len(mismatches)}）：{'；'.join(mismatches[:20]) or '无'}")

    out_path = Path(args.out) if args.out else WORK / "report.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"pages: {len(pages)}  constant_fields: {len(constants)}  mismatches: {len(mismatches)}"
          f"  -> {out_path}")
    for name in constants:
        print(f"  ! {name}")
    for item in mismatches:
        print(f"  ? {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
