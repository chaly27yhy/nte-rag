"""从 BWIKI 模板原文生成「结构化字段」专项题集（离线、可复跑）。

背景
----
`eval/eval_set.json` 的题目是从知识库自动生成的 —— **知识库里有什么，就只考什么**
（天花板效应）。所以「模板字段」这条新链路必须有自己的题集，否则打分永远是 1.0，
看不出到底有没有变好。`eval/eval_tables.json` 当初为表格抽取而建，做法相同。

和自动出题的区别：**答案不是模型编的，而是直接从缓存里的模板原文读出来的**，
所以 `review.status` 直接标 `ok`；需要人复核的只有「问法是否自然」。
另外脚本会做**区分度守卫**：如果某个答案在基线种子里已经存在（说明老链路也答得上），
这道题就被跳过 —— 否则新旧对比会被「本来就答得上的题」冲淡。

    .venv\\Scripts\\python.exe tools\\build_api_eval.py                       # 用默认参数生成
    .venv\\Scripts\\python.exe tools\\build_api_eval.py --base-seed seed\\seed_kb.before_api.json
    .venv\\Scripts\\python.exe tools\\build_api_eval.py --limit-chars 6 --limit-arcs 8
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import paths  # noqa: E402
from app.core import wiki_api  # noqa: E402
from app.core.sources import get_source  # noqa: E402

# 只挑「图鉴表格里根本没有」的字段：
# 角色图鉴表格有 稀有度/属性/类型/战斗类型，弧盘表格只有 名/稀有度/效果/描述，
# 所以生日、CV、所属、基础攻击/生命、适用分类、获取途径只能从模板原文拿到。
# （最高生命/最高攻击 不在此列：实测它们是模板默认值，全站同值，脚本会再过滤一次。）
CHAR_FIELDS: List[Tuple[str, str]] = [
    ("生日", "角色「{name}」的生日是什么时候？"),
    ("CV", "角色「{name}」的配音（CV）是谁？"),
    ("所属", "角色「{name}」属于哪个组织/阵营？"),
    ("攻击", "角色「{name}」的基础攻击是多少？"),
    ("生命", "角色「{name}」的基础生命是多少？"),
]
ARC_FIELDS: List[Tuple[str, str]] = [
    ("适用分类", "弧盘「{name}」的适用分类是什么？"),
    ("获取途径", "弧盘「{name}」的获取途径是什么？"),
]
# 描述里的数值是最容易在 HTML 链路里丢掉的部分（图鉴表格只写「详见描述」）
DESC_FIELD = "描述"
_POINT_RE = re.compile(r"\d+(?:\.\d+)?%|\d+\s*(?:点|秒)")


class _OfflineFetcher:
    """只读缓存：批量接口拿不到的页面就直接算缺，绝不联网。"""

    def fetch(self, url: str, **_kw: object) -> object:  # noqa: D102
        class _Result:
            ok = False
            status = 0
            error = "离线模式（只读缓存）"
            text = ""

        return _Result()


def _clean_points(value: str) -> List[str]:
    return [item.strip() for item in str(value or "").split("；") if item.strip()]


def build(args: argparse.Namespace) -> int:
    base_text = ""
    if args.base_seed:
        base_path = Path(args.base_seed)
        if not base_path.is_absolute():
            base_path = paths.project_root() / base_path
        if base_path.exists():
            base_text = base_path.read_text(encoding="utf-8")
            print(f"区分度守卫：答案已存在于 {base_path.name} 的题目会被跳过")
        else:
            print(f"基线种子不存在：{base_path}（跳过区分度守卫）")

    api = wiki_api.WikiApi(_OfflineFetcher(), "https://wiki.biligame.com/yh/api.php",
                           cache_dir=wiki_api.default_cache_dir())
    items: List[Dict[str, Any]] = []
    skipped: List[str] = []
    missing_cache: List[str] = []

    def add(name: str, question: str, points: List[str], value_for_guard: str,
            category: str, difficulty: str, page_base: str) -> None:
        points = [point for point in points if point]
        if not points:
            return
        if base_text and value_for_guard and value_for_guard in base_text:
            skipped.append(f"{name}（答案已在基线种子里）")
            return
        items.append({
            "question": question,
            "expected_points": points,
            "expected_sources": [page_base + quote(name)],
            "category": category,
            "difficulty": difficulty,
            "answerable": True,
            "review": {
                "status": "ok",
                "comment": "答案直接读自 BWIKI 模板原文（缓存），非模型生成",
            },
            "id": f"api{len(items) + 1:03d}",
        })

    plans = [
        ("bwiki_api_character", args.limit_chars, CHAR_FIELDS, "角色"),
        ("bwiki_api_arc", args.limit_arcs, ARC_FIELDS, "弧盘"),
    ]
    for source_id, limit, fields, category in plans:
        source = get_source(source_id) or {}
        page_base = str(source.get("page_base") or "")
        category_name = str(source.get("category") or "")
        templates = source.get("templates") or []
        if not page_base or not category_name:
            print(f"！数据源配置不全，跳过：{source_id}")
            continue
        members = sorted({
            title for title in api.category_members(category_name, limit=500)
            if title and not title.startswith(("模板:", "预设:", "创建"))
        })
        titles = members[:limit]
        texts = api.wikitext_many(titles)
        print(f"{source_id}：缓存命中 {len(texts)}/{len(titles)} 页（分类下共 {len(members)} 页）")
        page_pairs: Dict[str, list] = {}
        for title in titles:
            raw = texts.get(title, "")
            if not raw:
                missing_cache.append(title)
                continue
            page_pairs[title] = wiki_api.template_fields(
                raw, templates=templates, skip_fields=source.get("skip_fields")
            )
        # 模板默认值字段（同分类里取值恒定）绝不能拿来出题：
        # 实测 角色图鉴.最高攻击 全部是 8424，问「九原的最高攻击」答案本身就是错的。
        constants = wiki_api.constant_fields(page_pairs)
        if constants:
            print(f"  模板默认值字段（不出题）：{'、'.join(sorted(constants))}")
        for title in titles:
            pairs = page_pairs.get(title) or []
            if not pairs:
                continue
            values = {str(key).strip(): str(value or "").strip()
                      for key, value in pairs
                      if key not in constants
                      and not wiki_api.is_tautological(str(value or ""), title)}
            for field, pattern in fields:
                value = values.get(field, "")
                if not value:
                    continue
                add(title, pattern.format(name=title), _clean_points(value), value,
                    category, "medium", page_base)
            description = values.get(DESC_FIELD, "")
            points = _POINT_RE.findall(description)[:3]
            if points and category == "弧盘":
                # 描述里的数值（30.00% / 36点 / 15秒）是最容易在 HTML 链路里丢的部分：
                # 弧盘图鉴表格那一列只写「详见描述」。
                add(title, f"弧盘「{title}」的效果描述里提到了哪些数值？",
                    points, description, category, "hard", page_base)

    payload = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": ("结构化字段（BWIKI 模板原文）专项题集：答案读自缓存原文，"
                 "已用基线种子做区分度守卫（答案已存在的题被剔除）。"),
        "items": items,
    }
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = paths.project_root() / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if missing_cache:
        print(f"！{len(missing_cache)} 页不在缓存里，已跳过：{'、'.join(missing_cache[:6])}")
    for note in skipped:
        print(f"  - 跳过 {note}")
    print(f"items: {len(items)}  skipped: {len(skipped)}  -> {out_path}")
    return 0 if items else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="生成结构化字段专项题集")
    parser.add_argument("--out", default=str(Path("eval") / "eval_api.json"))
    parser.add_argument("--limit-chars", type=int, default=5, help="取多少个角色")
    parser.add_argument("--limit-arcs", type=int, default=6, help="取多少个弧盘")
    parser.add_argument("--base-seed", default=str(Path("seed") / "seed_kb.before_api.json"),
                        help="基线种子文件，用于区分度守卫（空字符串关闭）")
    return build(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
