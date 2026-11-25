"""从仓库审计种子生成 Windows 发行用的种子文件。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.curation import paused_reason, revoked_fact, source_paused, strip_gamersky_footer


def write_release_seed(archive: Path, destination: Path) -> Path:
    """保留可分发资料，并省略暂停来源的原文、事实与混合记录。"""
    payload = json.loads(archive.read_text(encoding="utf-8"))
    payload["documents"] = [doc for doc in payload["documents"] if not paused_reason(doc["url"])]
    for doc in payload["documents"]:
        doc["chunks"] = [
            strip_gamersky_footer(doc["url"], chunk) for chunk in doc.get("chunks", [])
        ]
    facts = []
    for fact in payload["facts"]:
        tags = fact.get("tags", "")
        if source_paused(fact["source_url"], tags) or revoked_fact(fact["source_url"], fact["title"]):
            continue
        # 独立来源的薄荷生日已复核，保留采纳标注和正确日期。
        if isinstance(tags, str) and "，BWIKI 8月20日存疑已撤下" in tags:
            fact["tags"] = tags.replace("，BWIKI 8月20日存疑已撤下", "")
        facts.append(fact)
    payload["facts"] = facts

    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as folder:
        output = Path(folder) / destination.name
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        output.replace(destination)
    return destination
