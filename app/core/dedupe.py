"""近似重复与冲突判定：同一标题下的两条信息，到底是重复、补充，还是打架。

背景
----
现有去重只有 SimHash 汉明距离（`store.find_similar_facts`，阈值 3/4）。它按
64 位指纹比较，对长文本有效，对**短事实**太紧。实测种子里三组漏网重复：

    「《异环》游戏类型」 动作角色扮演游戏 / 开放世界动作角色扮演游戏   （相似 0.89）
    「异环游戏基本介绍」 同一件事的两种写法                            （相似 0.93）
    「共存测试招募截止时间」 两个来源各写一遍                          （相似 0.62）

同时也**不能**盲目降低阈值：种子里大量条目共享标题词（「持续 3 秒」「持续 5 秒」
「持续 10 秒」是不同技能的时长），阈值一松就会互相吃掉。

所以这里的策略是「先同标题、再判关系」：
1. 只在**同标题**的既有条目里找（跨标题的别名归一化是另一件事）；
2. 用归一化后的答案比对，区分四种关系：
   - `same`     两边归一化后完全一样 → 重复
   - `superset` 一方完整包含另一方（「开放世界动作角色扮演游戏」⊃「动作角色扮演游戏」）→ 重复，留更全的
   - `numbers`  两边都带数字且数字集合不同（2480 vs 3280）→ **冲突**，谁对不知道，两条都留
   - `similar`  文本相似度 ≥ 阈值 → 重复，留更长的那条
   - `unrelated` 以上都不是 → 不动
3. 判定与「留谁」都返回理由，写进 tags，可被断言与人工复核。

2026-09-22 补两条规则（真实种子上仍有 2 组漏网）
------------------------------------------------
第 1 批的两组重复，标题完全一样，却都被判成 `unrelated`：

    「《异环》游戏类型」    《异环》是一款动作角色扮演游戏。
                          《异环》是一款开放世界动作角色扮演游戏。   相似 0.79
    「共存测试招募截止时间」 …招募活动将于1月23日11:00关闭，还未参加的鉴定师…
                          …招募截止时间为1月23日 11:00。            相似 0.55

原因是判定太字面：前者是**中间插字**的包含（`shorter in longer` 为假），后者是
**同样的数字换了说法**（数字集合相同 → 不走冲突分支，又够不到相似阈值）。所以加：

- `subset`：短的 bigram 有 ≥ 90% 出现在长的里 → 同一条信息的详略两说，留更全的。
- `same_numbers`：两边数字集合相同、至少有一个两位数以上的数字、且相似度 ≥ 0.45
  → 同一件事换了说法，留更长的那条。

两条都只判「重复」，而重复走的是 `merge_into`（答案取并集，不丢信息），
所以即使判宽了也不会丢事实；真正要守住的是**不把它们判成冲突**。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

# 同标题下答案相似度达到此值即视为重复（chinese bigram Dice）
SIMILAR_MERGE = 0.82
# 「包含」判定时，短的至少要占到长的一半，避免「S」这类极短答案吞掉一切
CONTAINMENT_MIN_RATIO = 0.45
# 中间插字的包含（「动作角色扮演游戏」⊂「开放世界动作角色扮演游戏」）：短的 bigram 有这么大比例
# 出现在长的里，就当作同一条信息的详略两说
CONTAINMENT_BIGRAM_RATIO = 0.9
# 数字集合相同、只是说法不同时的相似度下限（「将于1月23日11:00关闭」vs「截止时间为1月23日 11:00」）
SIMILAR_NUMBERS = 0.45
# 参与「数值一致」判定时至少要有一个两位数以上的数字，避免「3 秒」「3 级」这类小数字误判
NUMERIC_MIN_DIGITS = 2

_PUNCT = re.compile(
    r"[\s，。、；：！？“”‘’（）【】《》「」『』…—·,.;:!?\"'()\[\]<>/\\|~`^&*+=_-]+"
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def normalize_text(text: str) -> str:
    """去掉空白与标点、全角数字归一，只留可比对的内容。"""
    cleaned = str(text or "")
    for source, target in (("０", "0"), ("１", "1"), ("２", "2"), ("３", "3"), ("４", "4"),
                           ("５", "5"), ("６", "6"), ("７", "7"), ("８", "8"), ("９", "9")):
        cleaned = cleaned.replace(source, target)
    return _PUNCT.sub("", cleaned).lower()


def numbers_in(text: str) -> List[str]:
    """抽出归一化数字，去掉千分位与小数尾零（2,480 与 2480 视为同一个）。"""
    values: List[str] = []
    for raw in _NUMBER.findall(str(text or "").replace(",", "")):
        if "." in raw:
            raw = raw.rstrip("0").rstrip(".")
        values.append(raw)
    return values


def _bigrams(text: str) -> set:
    if len(text) < 2:
        return {text} if text else set()
    return {text[index:index + 2] for index in range(len(text) - 1)}


def similarity(left: str, right: str) -> float:
    """中文字符 bigram 的 Dice 系数（0~1），短文本比编辑距离稳。"""
    a, b = _bigrams(normalize_text(left)), _bigrams(normalize_text(right))
    if not a or not b:
        return 1.0 if a == b else 0.0
    return round(2 * len(a & b) / (len(a) + len(b)), 4)


def bigram_containment(left: str, right: str) -> float:
    """短的 bigram 有多大比例出现在长的里（1.0 = 短的像是长的插字变体）。

    `shorter in longer` 只能抓「原样包含」，「动作角色扮演游戏」与
    「开放世界动作角色扮演游戏」这种**中间插字**的包含抓不到，用 bigram 覆盖率补。
    """
    a, b = normalize_text(left), normalize_text(right)
    if len(a) > len(b):
        a, b = b, a
    small, large = _bigrams(a), _bigrams(b)
    if not small or not large:
        return 0.0
    return round(len(small & large) / len(small), 4)


def compare(existing_answer: str, candidate_answer: str) -> Dict[str, Any]:
    """判断两条同标题信息的关系，并说明该留哪一条。"""
    old, new = normalize_text(existing_answer), normalize_text(candidate_answer)
    if not old and not new:
        return {"action": "duplicate", "keep": "existing", "reason": "两边都没有正文"}
    if old == new:
        return {"action": "duplicate", "keep": "existing", "reason": "答案相同"}

    old_numbers, new_numbers = numbers_in(existing_answer), numbers_in(candidate_answer)
    if old_numbers and new_numbers and set(old_numbers) != set(new_numbers):
        return {
            "action": "conflict",
            "keep": "both",
            "reason": "数值不一致（{} vs {}），无法判断哪个新".format(
                "/".join(old_numbers[:4]), "/".join(new_numbers[:4])
            ),
        }

    longer, shorter = (old, new) if len(old) >= len(new) else (new, old)
    keep = "existing" if len(old) >= len(new) else "candidate"
    if shorter and len(shorter) >= CONTAINMENT_MIN_RATIO * len(longer) and shorter in longer:
        return {"action": "duplicate", "keep": keep, "reason": "答案一方包含另一方，保留更完整的一条"}

    # 中间插字的包含：「《异环》是一款动作角色扮演游戏」与「…开放世界动作角色扮演游戏」
    if shorter and len(shorter) >= 2 and len(shorter) >= CONTAINMENT_MIN_RATIO * len(longer):
        coverage = bigram_containment(old, new)
        if coverage >= CONTAINMENT_BIGRAM_RATIO:
            return {
                "action": "duplicate",
                "keep": keep,
                "reason": f"一方几乎是另一方的子集（bigram 覆盖 {coverage:.2f}），保留更完整的一条",
            }

    # 数字集合完全一致、只是换了说法：「将于1月23日11:00关闭…」与「截止时间为1月23日 11:00。」
    if (
        old_numbers
        and new_numbers
        and set(old_numbers) == set(new_numbers)
        and any(len(value) >= NUMERIC_MIN_DIGITS for value in old_numbers)
        and similarity(existing_answer, candidate_answer) >= SIMILAR_NUMBERS
    ):
        return {
            "action": "duplicate",
            "keep": keep,
            "reason": "数值一致（{}），表述不同，保留更完整的一条".format("/".join(old_numbers[:4])),
        }

    score = similarity(existing_answer, candidate_answer)
    if score >= SIMILAR_MERGE:
        return {"action": "duplicate", "keep": keep, "reason": f"答案相似度 {score:.2f}"}
    return {"action": "unrelated", "keep": "both", "reason": f"相似度仅 {score:.2f}"}


def find_duplicate(
    kb: Any,
    title: str,
    answer: str,
    threshold: int = 3,
) -> Optional[Dict[str, Any]]:
    """在既有条目里找同标题的重复/冲突。返回 `{"fact", "action", "keep", "reason"}`。

    SimHash 只作为兜底（跨标题的近似重复靠它，同标题的靠上面的关系判定）。
    """
    conflict_hit: Optional[Dict[str, Any]] = None
    for row in kb.find_facts_by_title(title, limit=6):
        verdict = compare(row.get("answer") or "", answer)
        if verdict["action"] == "duplicate":
            return {"fact": row, **verdict}
        if verdict["action"] == "conflict" and conflict_hit is None:
            conflict_hit = {"fact": row, **verdict}
    if conflict_hit:
        return conflict_hit

    # 跨标题的近似重复：同一件事被写成了不同标题（「早雾的生日」vs「早雾·生日」）。
    # 这段兜底过去是死代码——limit=1 时唯一能返回的行就是同标题的那条，
    # 而它已经被上面的 find_facts_by_title 循环处理过了，于是永远走不到这里。
    # 现在取一小组近邻，并要求标题确实不同才算「跨标题」命中。
    for row in kb.find_similar_facts(f"{title} {answer}", limit=20, threshold=threshold):
        if normalize_text(row.get("title") or "") == normalize_text(title):
            continue
        verdict = compare(row.get("answer") or "", answer)
        if verdict["action"] in ("duplicate", "conflict"):
            return {"fact": row, **verdict}
    return None


def _domain(url: str) -> str:
    match = re.match(r"https?://([^/]+)", str(url or ""))
    return match.group(1) if match else ""


def merge_answers(existing: str, candidate: str) -> str:
    """融合两条答案：把候选里既有答案没有的句子补进去（去重后拼接）。"""
    old, new = str(existing or "").strip(), str(candidate or "").strip()
    if not old:
        return new
    if not new:
        return old
    if normalize_text(new) in normalize_text(old):
        return old
    if normalize_text(old) in normalize_text(new):
        return new
    # 中间插字的子集：并集拼起来会得到「…动作角色扮演游戏；…开放世界动作角色扮演游戏」
    # 这种自相冗余的答案，不如直接留更完整的那条。
    if bigram_containment(old, new) >= CONTAINMENT_BIGRAM_RATIO:
        return old if len(normalize_text(old)) >= len(normalize_text(new)) else new
    parts_old = [part.strip() for part in re.split(r"[。；\n]", old) if part.strip()]
    extra = [
        part.strip()
        for part in re.split(r"[。；\n]", new)
        if part.strip() and normalize_text(part) not in normalize_text(old)
    ]
    if not extra:
        return old
    return "；".join(parts_old + extra)


def merge_into(
    kb: Any,
    existing: Dict[str, Any],
    candidate_answer: str,
    candidate_url: str = "",
    candidate_confidence: float = 0.0,
) -> str:
    """把候选并入既有条目：答案取并集、可信度取高者、追加「并入:域名」标记。

    返回最终写入的答案（便于调用方写日志/断言）。
    """
    merged = merge_answers(existing.get("answer") or "", candidate_answer)
    confidence = max(float(existing.get("confidence") or 0.0), float(candidate_confidence or 0.0))
    tags = existing.get("tags") or ""
    domain = _domain(candidate_url)
    mark = f"并入:{domain}" if domain and f"并入:{domain}" not in tags else ""
    if mark:
        tags = f"{tags} | {mark}".strip(" |")
    fields: Dict[str, Any] = {"answer": merged, "confidence": confidence}
    if tags != (existing.get("tags") or ""):
        fields["tags"] = tags
    # 内容完全相同的二次确认（merged 与 confidence 都没变化）也必须落盘：
    # 否则「并入:<域名>」标记丢失 → consistency 认不出第二个独立来源 → 跨源 multi 恒为 0，
    # 而「两源说法完全一致」恰恰是跨源投票最该确认的判例。
    if (
        merged != (existing.get("answer") or "")
        or confidence > float(existing.get("confidence") or 0.0)
        or tags != (existing.get("tags") or "")
    ):
        kb.update_fact(int(existing["id"]), **fields)
    return merged


def duplicate_groups(facts: Sequence[Dict[str, Any]], include_conflict: bool = False) -> List[Dict[str, Any]]:
    """离线扫描：把同标题下互为重复/冲突的条目分组，供体检报告与断言使用。"""
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for fact in facts:
        buckets.setdefault(normalize_text(fact.get("title") or ""), []).append(fact)
    groups: List[Dict[str, Any]] = []
    for rows in buckets.values():
        if len(rows) < 2:
            continue
        for index, base in enumerate(rows):
            for other in rows[index + 1:]:
                verdict = compare(base.get("answer") or "", other.get("answer") or "")
                if verdict["action"] == "duplicate" or (include_conflict and verdict["action"] == "conflict"):
                    groups.append({
                        "title": base.get("title") or other.get("title") or "",
                        "action": verdict["action"],
                        "reason": verdict["reason"],
                        "ids": [int(base["id"]), int(other["id"])],
                        "answers": [base.get("answer") or "", other.get("answer") or ""],
                        "sources": [base.get("source_url") or "", other.get("source_url") or ""],
                    })
    return groups


def merge_seed_facts(facts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """在**种子文件**里就地合并已知重复（不改动入参，返回新列表）。

    在文件里合并是必需的：`app/server/api.py:ensure_seed()`
    用种子文件指纹决定要不要重新导入（注释原文：「旧实现只看 `seed_loaded=1`，
    升级等于白升」）。如果种子文件一字不改，老用户升级 exe 后指纹相同 →
    不会重新导入 → 新写好的合并逻辑对他完全不生效。所以种子本身必须干净。

    返回 `{"facts": 合并后的列表, "merged": 合并掉的条数, "groups": [(标题, 保留的答案)]}`。
    """
    kept: List[Dict[str, Any]] = []
    merged = 0
    groups: List[tuple] = []
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for fact in facts:
        key = normalize_text(fact.get("title") or "")
        buckets.setdefault(key, []).append(dict(fact))
    for rows in buckets.values():
        if len(rows) == 1:
            kept.append(rows[0])
            continue
        survivors: List[Dict[str, Any]] = []
        for row in rows:
            absorbed = False
            for keeper in survivors:
                verdict = compare(keeper.get("answer") or "", row.get("answer") or "")
                if verdict["action"] != "duplicate":
                    continue
                keeper["answer"] = merge_answers(keeper.get("answer") or "", row.get("answer") or "")
                keeper["confidence"] = max(
                    float(keeper.get("confidence") or 0.0),
                    float(row.get("confidence") or 0.0),
                )
                domain = _domain(row.get("source_url") or "")
                marker = f"并入:{domain}" if domain else "并入:重复"
                tags = str(keeper.get("tags") or "")
                if marker not in tags:
                    keeper["tags"] = f"{tags} {marker}".strip()
                merged += 1
                groups.append((keeper.get("title") or "", keeper.get("answer") or ""))
                absorbed = True
                break
            if not absorbed:
                survivors.append(row)
        kept.extend(survivors)
    return {"facts": kept, "merged": merged, "groups": groups}


def describe() -> Dict[str, float]:
    return {
        "similar_merge": SIMILAR_MERGE,
        "containment_min_ratio": CONTAINMENT_MIN_RATIO,
        "containment_bigram_ratio": CONTAINMENT_BIGRAM_RATIO,
        "similar_numbers": SIMILAR_NUMBERS,
        "numeric_min_digits": NUMERIC_MIN_DIGITS,
    }
