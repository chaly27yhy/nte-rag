"""跨来源一致性投票：把「同一件事在几个独立站点上说法是否一致」变成确定性判断。

多源一致性被写进了可信度公式（`trust.py` 占 0.20 权重），
但入库时喂给它的只有 `sources=1` 这个默认值——0.20 的权重一直空着。
这里负责把它填上，并且刻意做得**很保守**。

「朴素数值冲突检测」早期就被实测否掉：
「实体 + 多值」的朴素比法实测 11 + 19 组候选**全部是假阳性**（同一个商品的原价/折扣价、
不同活动的价格、不同角色各自的数值被放在一起比）。所以这里加了四道闸门：

1. **只比同一个槽位**（`versioning.slot_key` = 实体 + 字段）：`「夏日梦」·价格` 与
   `「织梦者」·价格` 是两个槽位，永远不会互相判冲突。
2. **只比「值型」答案**：短（≤ 40 字）、以数字为主（去掉数字后剩下的汉字 ≤ 6 个）。
   一段散文里的 2480 和另一段散文里的 3280 不构成冲突。
3. **数值要有区分度**：至少一个数字有 2 位以上（`12.5` 算，孤零零的 `3` 不算），
   避免「第 1 章」这种噪声参与投票。
4. **版本/生效时间不同的两条不算冲突**，判为 `versioned`（谁新谁在前，两条都留）——
   这是版本化知识库的正常形态，不是矛盾。

判定的四种结果：
- `multi`：≥2 个独立域名、数值签名完全一致 → 「多源确认」，加分；
- `conflict`：≥2 个独立域名、签名不一致且没有版本/日期区分 → 标 `conflict`，**不覆盖**任一条；
- `versioned`：同槽位但版本/生效时间不同 → 不判冲突，交给 `store._prefer_newest_in_slot` 排序；
- `single`：只有一个独立来源（无结论，不产生任何写入）。

两道**裁决层**的排除规则（2026-09-21 人工审核裁定，与上面的值型闸门无关）：

5. **叙事/世界观来源不投票**：官网「角色介绍」这类纯叙事内容（标签带 `NARRATIVE_TAG`）
   只用于世界观/剧情/角色背景问答与引用，永远不参与数值/字段投票。
   靠来源分类挡，而不是靠「值不像数值」猜——叙事文本里也有数字（「五大城区」）。
6. **施工期站点的数值字段不跨源比对**：`sources.CONSTRUCTION_DOMAINS`（BWIKI）的
   生命/攻击等数值字段直接退出投票，一旦出现冲突只做**字段级存疑**标记
   （`NUMERIC_FIELD_TAG`），不采纳任何一方，也不整站降权（权重维持 0.75）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from . import versioning
from .sources import CONSTRUCTION_DOMAINS, CONSTRUCTION_NOTE, NARRATIVE_TAG

# 「值型」答案的长度上限：超过这个长度就认为是叙述性文字，不参与数值投票
VALUE_MAX_CHARS = 40
# 去掉数字后剩余的汉字上限（例如「最高攻击 8424 点」剩 5 个字）
RESIDUE_MAX_CHARS = 6
# 至少一个数字要有这么多位，才算有区分度
MIN_DIGITS = 2
# 判定为「多源」所需的最少独立域名数
MIN_DOMAINS = 2

VERDICT_MULTI = "multi"
VERDICT_CONFLICT = "conflict"
VERDICT_VERSIONED = "versioned"
VERDICT_SINGLE = "single"

MULTI_SOURCE_TAG = "多源确认"
CONFLICT_TAG_PREFIX = "多源冲突"
MULTI_SOURCE_BONUS = 0.08

# 施工期数值字段的字段级存疑标记（2026-09-21 人工审核裁定）
NUMERIC_FIELD_TAG = "数值字段存疑（施工期，未跨源比对）"
# 哪些字段算「数值字段」：施工期这些字段一律不跨源比对
NUMERIC_FIELD_RE = re.compile(
    r"生命|生命值|攻击|攻击力|防御|防御力|暴击|爆伤|伤害|抗性|速度|数值|加成|上限|初始"
)

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)*")
_NON_WORD_RE = re.compile(r"[\W_]+")


def domain_of(url: Any) -> str:
    """URL 的注册域名（去掉 www. 与端口），用于判断来源是否独立。"""
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        host = urlsplit(text if "://" in text else f"//{text}").hostname or ""
    except (ValueError, UnicodeError):
        # 畸形 URL（如 `http://[abc`）不能让整轮跨源裁决崩掉，按「无域名」降级。
        # 与 quality.host_of / search.site_of 保持同一口径。
        return ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def numbers(text: Any) -> List[str]:
    """抽出文本里的数字并归一（去千分位、去小数尾零）：`12.00%` → `12`。"""
    out: List[str] = []
    for raw in _NUMBER_RE.findall(str(text or "")):
        token = raw.replace(",", "")
        if "." in token:
            token = token.rstrip("0").rstrip(".") or "0"
        elif token.strip("-") == "":
            continue
        if token not in out:
            out.append(token)
    return out


def signature(text: Any) -> str:
    """数值签名：排序后的数字串，用来比较「两个来源说的是不是同一个数」。"""
    return "|".join(sorted(numbers(text), key=lambda t: (len(t), t)))


# 结构化路径的答案模板是「<实体> 的<字段>为：<值>」（wiki_api.facts_from_pairs）。
# 判定「值型」时必须先把这个前缀剥掉，否则量到的是整句的残字：
# 三字名角色（「娜娜莉的攻击为」= 7 字）与生日类（「早雾的生日为」= 6 字 + 值的「月日」2 字）
# 会被 RESIDUE_MAX_CHARS 挡在投票之外。实测 593 条里只有 8 条能进投票，
# 原因是模板句式误伤，而不是数据本身没有可比的数值。
_TEMPLATE_PREFIX_RE = re.compile(r"^\s*.{1,14}?的.{1,12}?为[：:]\s*")
# 入库时 dedupe.merge_into 会把第二个来源的域名记进 tags（「并入:<域名>」）
_MERGED_DOMAIN_RE = re.compile(r"并入[:：]\s*([A-Za-z0-9._\-]+)")


def value_body(text: Any) -> str:
    """去掉「<实体> 的<字段>为：」模板前缀后的「值本体」。"""
    return _TEMPLATE_PREFIX_RE.sub("", str(text or "").strip(), count=1).strip()


def merged_domains(tags: Any) -> List[str]:
    """从 tags 里取「并入:<域名>」标记 → 同一件事的第二个独立来源。

    必须认这个标记：内容**完全相同**的二次确认会在入库时被合并成一行
    （`dedupe.merge_into` 只把域名追加进 tags），
    如果投票只按 `source_url` 取域名，越是「两源说法一致」越数不出 multi——
    恰好把跨源投票最该确认的那一类判例漏掉（实测跨源前 multi 恒为 0）。
    """
    found: List[str] = []
    for host in _MERGED_DOMAIN_RE.findall(str(tags or "")):
        clean = domain_of(host) or host.strip().lower()
        if clean and clean not in found:
            found.append(clean)
    return found


def is_value_like(text: Any) -> bool:
    """答案是不是「值型」（短 + 以数字为主 + 有区分度）。"""
    body = str(text or "").strip()
    if not body or len(body) > VALUE_MAX_CHARS:
        return False
    tokens = numbers(body)
    if not tokens:
        return False
    if not any(len(t.replace(".", "").replace("-", "")) >= MIN_DIGITS for t in tokens):
        return False
    residue = _NON_WORD_RE.sub("", _NUMBER_RE.sub("", value_body(body)))
    return len(residue) <= RESIDUE_MAX_CHARS


def slot_of(row: Mapping[str, Any]) -> str:
    """槽位键：优先用调用方算好的 `slot_key`，否则由标题现场推。"""
    slot = str(row.get("slot_key") or "").strip()
    if slot:
        return slot
    title = str(row.get("title") or "")
    if "·" not in title and not title:
        return ""
    from .store import KnowledgeBase  # 局部导入：store 反过来不依赖本模块

    return versioning.slot_key(KnowledgeBase.entity_of(title), title)


def _version_key(row: Mapping[str, Any]) -> Tuple[str, str]:
    return (str(row.get("effective_from") or "")[:10], str(row.get("version") or ""))


def _candidate(row: Mapping[str, Any]) -> Dict[str, Any]:
    domain = domain_of(row.get("source_url") or "")
    tags = str(row.get("tags") or "")
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "answer": row.get("answer"),
        "signature": signature(row.get("answer")),
        "numbers": numbers(row.get("answer")),
        "confidence": float(row.get("confidence") or 0.0),
        "effective_from": str(row.get("effective_from") or "")[:10],
        "version": str(row.get("version") or ""),
        "domain": domain,
        # 一行可能背着多个来源域名：「并入:<域名>」是入库时同一件事的二次确认
        "domains": ([domain] if domain else []) + merged_domains(tags),
        "source_url": row.get("source_url") or "",
        "tags": tags,
    }


def is_narrative_fact(row: Mapping[str, Any]) -> bool:
    """叙事/世界观来源的条目（只作答世界观/剧情，不参与字段投票）。"""
    return NARRATIVE_TAG in str(row.get("tags") or "")


def is_numeric_field(title: Any) -> bool:
    """标题是不是数值字段（生命/攻击/暴击……）。"""
    return bool(NUMERIC_FIELD_RE.search(str(title or "")))


def is_under_construction(url: Any) -> bool:
    """来源域名是否属于「施工中」站点（BWIKI）。"""
    domain = domain_of(url)
    return any(item and item in domain for item in CONSTRUCTION_DOMAINS)


def no_vote_reason(row: Mapping[str, Any]) -> str:
    """不参与投票的原因；空串表示可以投票。

    两类排除（2026-09-21 人工审核裁定）：
    - 叙事/世界观来源：任何字段都不投票；
    - 施工期站点的数值字段：不跨源比对（口径会变，比出来的是施工噪声）。
    """
    if is_narrative_fact(row):
        return "叙事/世界观来源"
    if is_numeric_field(row.get("title")) and is_under_construction(row.get("source_url")):
        return "施工期站点的数值字段（不跨源比对）"
    return ""


def vote(rows: Iterable[Mapping[str, Any]], min_domains: int = MIN_DOMAINS) -> List[Dict[str, Any]]:
    """按槽位投票，返回**只带有结论的**槽位（`single` 也返回，便于报告展示覆盖面）。

    返回项：`{slot, title, verdict, values, domains, fact_ids, version_keys}`。

    两道排除在这里落地：
    - `no_vote_reason()` 的叙事/施工期规则（见该函数）；
    - **已被取代的行不投票**。`supersede_fact` 把旧行标成 `status='superseded'`
      之后，它仍然是「这个槽位曾经的一种说法」，但已经不是当前结论。若让它继续投票，
      且它与有效行没有版本/日期元数据可区分，`vote()` 会判成 `conflict`，
      接着 `apply()` 会把**整个槽位（包括当前有效行）**标成 conflict——
      于是一个早已定案、只是换过版本的条目会在界面上显示成未解决的多源冲突。
    """
    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        slot = slot_of(row)
        if not slot or not is_value_like(row.get("answer")):
            continue
        if str(row.get("status") or "") == "superseded":
            continue
        if no_vote_reason(row):
            continue
        item = _candidate(row)
        if not item["domains"]:
            # 没有来源域名就无从判断「独立来源」，不参与投票
            continue
        bucket = groups.setdefault(slot, {"slot": slot, "by_domain": {}})
        bucket.setdefault("title", row.get("title"))
        for domain in item["domains"]:
            current = bucket["by_domain"].get(domain)
            if current is None or (
                item["confidence"],
                versioning.version_sort_key(item["version"]),
                item["effective_from"],
            ) > (
                current["confidence"],
                versioning.version_sort_key(current["version"]),
                current["effective_from"],
            ):
                # 同一域名在一个槽位里只投一票：留可信度最高、版本最新的那条。
                # 版本必须先过 version_sort_key：「1.10」按字符串比会输给「1.9」。
                bucket["by_domain"][domain] = item

    results: List[Dict[str, Any]] = []
    for bucket in groups.values():
        by_domain = bucket["by_domain"]
        picked = sorted(by_domain.values(), key=lambda c: str(c["domain"]))
        # 一行背着两个域名时会在 picked 里出现两次，报告只该看到一条事实：
        # 去重后 values/fact_ids 才是「几个来源说了几种说法」。
        unique: List[Dict[str, Any]] = []
        seen_ids: set = set()
        for cand in picked:
            key = cand["id"] if cand["id"] is not None else id(cand)
            if key in seen_ids:
                continue
            seen_ids.add(key)
            unique.append(cand)
        signatures = {c["signature"] for c in unique}
        version_keys = {_version_key(c) for c in unique}
        if len(by_domain) < min_domains:
            verdict = VERDICT_SINGLE
        elif len(version_keys) > 1:
            verdict = VERDICT_VERSIONED
        elif len(signatures) == 1:
            verdict = VERDICT_MULTI
        else:
            verdict = VERDICT_CONFLICT
        results.append(
            {
                "slot": bucket["slot"],
                "title": bucket.get("title") or "",
                "verdict": verdict,
                "values": [c["answer"] for c in unique],
                "numbers": [c["numbers"] for c in unique],
                "domains": sorted(by_domain),
                "fact_ids": [c["id"] for c in unique],
                "version_keys": sorted(f"{v or '-'}@{e or '-'}" for v, e in version_keys),
                "facts": unique,
            }
        )
    order = {VERDICT_CONFLICT: 0, VERDICT_MULTI: 1, VERDICT_VERSIONED: 2, VERDICT_SINGLE: 3}
    results.sort(key=lambda r: (order.get(r["verdict"], 9), r["slot"]))
    return results


def summarize(verdicts: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """按判定分类计数，供报告与统计口径使用。"""
    counts = {
        VERDICT_MULTI: 0,
        VERDICT_CONFLICT: 0,
        VERDICT_VERSIONED: 0,
        VERDICT_SINGLE: 0,
    }
    for item in verdicts:
        key = str(item.get("verdict") or "")
        counts[key] = counts.get(key, 0) + 1
    return {
        "slots": len(verdicts),
        "multi": counts[VERDICT_MULTI],
        "conflict": counts[VERDICT_CONFLICT],
        "versioned": counts[VERDICT_VERSIONED],
        "single": counts[VERDICT_SINGLE],
    }


def _ensure_tag(tags: str, tag: str) -> str:
    parts = [p.strip() for p in str(tags or "").split("|") if p.strip()]
    if any(p == tag or p.startswith(f"{tag}:") for p in parts):
        return str(tags or "")
    parts.append(tag)
    return " | ".join(parts)


def apply(
    kb: Any,
    verdicts: Sequence[Mapping[str, Any]],
    dry_run: bool = True,
) -> Dict[str, Any]:
    """把结论写回库：`multi` 加「多源确认」（+0.08，只加一次），`conflict` 标状态、不覆盖。

    `dry_run=True`（默认）只统计将要发生的改动，不写库——用户审核判例之前不落盘。
    """
    report = {
        "dry_run": bool(dry_run),
        "multi_slots": 0,
        "confirmed": 0,
        "conflict_slots": 0,
        "conflicts": 0,
        "skipped": 0,
    }
    for item in verdicts:
        verdict = item.get("verdict")
        if verdict == VERDICT_MULTI:
            report["multi_slots"] += 1
            for fact in item.get("facts") or []:
                fid = fact.get("id")
                if fid is None:
                    continue
                tags = str(fact.get("tags") or "")
                if MULTI_SOURCE_TAG in tags:
                    report["skipped"] += 1
                    continue
                confidence = round(
                    min(1.0, float(fact.get("confidence") or 0.0) + MULTI_SOURCE_BONUS), 4
                )
                report["confirmed"] += 1
                if dry_run:
                    continue
                kb.update_fact(
                    int(fid),
                    confidence=confidence,
                    tags=_ensure_tag(tags, MULTI_SOURCE_TAG),
                )
        elif verdict == VERDICT_CONFLICT:
            report["conflict_slots"] += 1
            values = " vs ".join(str(v) for v in item.get("values") or [])
            domains = " / ".join(str(d) for d in item.get("domains") or [])
            note = f"{CONFLICT_TAG_PREFIX}:{values}（{domains}）"
            # 施工期站点的数值字段出现冲突时，按 2026-09-21 人工审核裁定降级为「字段级存疑」：
            # 额外打 NUMERIC_FIELD_TAG，提醒这条口径本身待确认，不要当成定论。
            construction = is_numeric_field(item.get("title")) and any(
                any(item_domain and item_domain in str(domain) for item_domain in CONSTRUCTION_DOMAINS)
                for domain in (item.get("domains") or [])
            )
            if construction:
                report["numeric_field_flagged"] = int(report.get("numeric_field_flagged") or 0) + 1
            for fact in item.get("facts") or []:
                fid = fact.get("id")
                if fid is None:
                    continue
                tags = str(fact.get("tags") or "")
                if CONFLICT_TAG_PREFIX in tags:
                    report["skipped"] += 1
                    continue
                report["conflicts"] += 1
                if construction:
                    tags = _ensure_tag(tags, NUMERIC_FIELD_TAG)
                    tags = _ensure_tag(tags, CONSTRUCTION_NOTE)
                if dry_run:
                    continue
                kb.update_fact(int(fid), status="conflict", tags=_ensure_tag(tags, note))
    return report


def reconcile(kb: Any, dry_run: bool = True, limit: int = 0) -> Dict[str, Any]:
    """对整库跑一次投票（可只跑前 `limit` 条），返回汇总 + 每槽结论。"""
    rows = kb.list_facts(limit=int(limit) if limit else 100000, offset=0)
    verdicts = vote(rows)
    summary = summarize(verdicts)
    changes = apply(kb, verdicts, dry_run=dry_run)
    # 被排除的条目单独计数：否则「投票面缩小」在报告里看不出来
    excluded: Dict[str, int] = {}
    for row in rows:
        reason = no_vote_reason(row)
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
    return {
        "summary": summary,
        "changes": changes,
        "verdicts": verdicts,
        "excluded": excluded,
    }


def describe() -> Dict[str, Any]:
    """给诊断工具/断言看的规则说明（自解释，避免读代码猜口径）。"""
    return {
        "min_domains": MIN_DOMAINS,
        "value_max_chars": VALUE_MAX_CHARS,
        "residue_max_chars": RESIDUE_MAX_CHARS,
        "min_digits": MIN_DIGITS,
        "verdicts": [VERDICT_MULTI, VERDICT_CONFLICT, VERDICT_VERSIONED, VERDICT_SINGLE],
        "multi_source_tag": MULTI_SOURCE_TAG,
        "conflict_tag_prefix": CONFLICT_TAG_PREFIX,
        "multi_source_bonus": MULTI_SOURCE_BONUS,
        "narrative_tag": NARRATIVE_TAG,
        "numeric_field_tag": NUMERIC_FIELD_TAG,
        "construction_domains": list(CONSTRUCTION_DOMAINS),
        "rules": [
            "只比同一个槽位（实体 + 字段），不同实体/不同商品的数字永不互判冲突",
            "只比值型答案（≤40 字、以数字为主），散文里的数字不构成冲突",
            "『值型』判定会先剥掉「<实体> 的<字段>为：」模板前缀，否则三字名角色与生日类字段会被误杀",
            "至少一个数字有 2 位以上，孤立个位数不参与投票",
            "同一域名在一个槽位只投一票（取可信度最高、版本最新的一条）",
            "同一行 tags 里的「并入:<域名>」算第二个独立来源（内容完全相同的二次确认入库时会被并成一行）",
            "版本/生效时间不同的两条判 versioned（谁新谁在前），不判 conflict",
            "叙事/世界观来源（标签带「叙事/世界观（不参与字段投票）」）不参与任何字段投票",
            "施工期站点（BWIKI）的数值字段不跨源比对；一旦冲突只做字段级存疑标记",
        ],
    }
