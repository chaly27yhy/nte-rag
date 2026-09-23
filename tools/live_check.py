"""开发期联调脚本：用 .env 中的真实模型配置验证全链路。

覆盖：连通性 → 非流式问答 → 流式问答 → 带引用回答质量 → 联网补齐 → 知识条目抽取。

用法：
    python tools\\live_check.py                 # 全部检查
    python tools\\live_check.py --only llm      # 只测模型连通性
    python tools\\live_check.py --only chat     # 只测问答链路
    python tools\\live_check.py --only web      # 只测联网搜索与抓取
    python tools\\live_check.py --only extract  # 只测知识条目抽取
"""

from __future__ import annotations

import _console  # noqa: F401  （GBK 控制台下安全打印，见 tools/_console.py）

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config  # noqa: E402
from app.core.fetch import Fetcher  # noqa: E402
from app.core.ingest import extract_facts, load_seed  # noqa: E402
from app.core.llm import build_llm  # noqa: E402
from app.core.rag import RagEngine  # noqa: E402
from app.core.search import build_search  # noqa: E402
from app.core.store import KnowledgeBase  # noqa: E402

WORK = Path(".live_check")


def _setup():
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    config = Config(path=WORK / "config.json")
    config.apply_dev_env(explicit=True)
    kb = KnowledgeBase(db_file=WORK / "knowledge.db")
    # 导入随程序分发的种子知识库，否则检索层是空的、测不出真实效果
    seed = Path("seed/seed_kb.json").resolve()
    if seed.exists():
        report = load_seed(kb, config, seed)
        print(f"[准备] 已导入种子知识库：{report['documents']} 篇文档 / {report['facts']} 条条目")
    else:
        print("[准备] 警告：未找到 seed/seed_kb.json")
    return config, kb


def check_llm(config) -> bool:
    print("=" * 70)
    print("1) 模型连通性")
    section = config.section("llm")
    print(f"   协议   : {section.get('provider')}")
    print(f"   Base   : {section.get('base_url')}")
    print(f"   模型   : {section.get('model')}")
    client = build_llm(config)
    result = client.test_connection()
    print(f"   端点   : {result.get('endpoint')}")
    print(f"   密钥   : {result.get('key_preview')}")
    print(f"   结果   : {'✅ 成功' if result['ok'] else '❌ 失败'}")
    if result["ok"]:
        print(f"   延迟   : {result['latency_ms']} ms")
        print(f"   回复   : {result['sample']!r}")
    else:
        print(f"   错误   : {result['error']}")
    return bool(result["ok"])


def check_chat(config, kb) -> bool:
    print("=" * 70)
    print("2) 问答链路（本地知识库 → 带引用作答）")
    engine = RagEngine(config, kb)
    question = "异环里的薄荷是谁？"
    started = time.time()
    print("   流式输出：", end="", flush=True)
    text = ""
    citations = []
    done_event = {}
    for event in engine.stream_answer(question, allow_web=False):
        if event["type"] == "token":
            text += event["text"]
            print(event["text"], end="", flush=True)
        elif event["type"] == "sources":
            citations = event.get("citations") or []
        elif event["type"] == "done":
            done_event = event
        elif event["type"] == "error":
            print(f"\n   ❌ 错误：{event['message']}")
            return False
    print()
    print(f"   耗时   : {done_event.get('latency_ms', 0)} ms（总 {time.time() - started:.1f}s）")
    print(f"   引用数 : {len(citations)}")
    for item in citations[:4]:
        print(f"     [{item['index']}] {item['title'][:40]} | {item['source_type']} | {item['url'][:60]}")
    print(f"   检索   : {done_event.get('retrieval')}")
    print(f"   答案长度: {len(text)} 字")
    if not text.strip():
        print("   ❌ 模型没有返回内容")
        return False
    if "[" not in text:
        print("   ⚠️ 答案里没有出现引用编号，检查提示词效果")
    print(f"   ✅ 问答链路可用")
    return True


def check_web(config, kb) -> bool:
    print("=" * 70)
    print("3) 联网搜索与抓取（免 Key 兜底源）")
    client = build_search(config)
    print(f"   主源   : {config.get('search', 'provider')}")
    try:
        results = client.search("异环 1.4版本 前瞻", max_results=5)
    except Exception as error:
        print(f"   ❌ 搜索失败：{error}")
        print(f"   尝试记录：{client.last_errors}")
        return False
    print(f"   结果   : {len(results)} 条")
    for item in results[:5]:
        print(f"     · [{item.provider}/{item.source_type}] {item.title[:40]}")
        print(f"       {item.url[:90]}")
    if client.last_errors:
        print(f"   降级记录：{client.last_errors}")

    fetcher = Fetcher(timeout=25)
    fetched_ok = 0
    for item in results[:2]:
        page = fetcher.fetch(item.url)
        status = "✅" if page.ok else "❌"
        print(f"   {status} 抓取 {item.url[:70]} → {len(page.text)} 字 {page.error}")
        if page.ok and page.text:
            fetched_ok += 1
            print(f"      预览：{page.text[:100].replace(chr(10), ' ')}")
    return bool(results) and fetched_ok > 0


def check_extract(config, kb) -> bool:
    print("=" * 70)
    print("4) 知识条目抽取（把网页正文变成结构化条目）")
    llm = build_llm(config)
    sample_path = Path("seed/seed_kb.json")
    if not sample_path.exists():
        print("   ❌ 缺少 seed/seed_kb.json，请先运行 tools/seed_builder.py")
        return False
    import json

    payload = json.loads(sample_path.read_text(encoding="utf-8"))
    documents = payload.get("documents") or []
    if not documents:
        # 「只有结构化 facts、0 篇文档」是 seed_builder 的正常产物（见其注释），
        # 不是错误，所以这里跳过网页链路检查而不是崩在 documents[0] 上。
        print("   ⚠️ 种子库里没有文档（只有结构化事实），跳过网页链路检查")
        return True
    target = next(
        (d for d in documents if d.get("source_type") == "official" and len(d.get("chunks") or []) > 1),
        None,
    )
    if target is None:
        target = documents[0]
    text = "\n\n".join(target.get("chunks") or [])[:6000]
    print(f"   样本   : {str(target.get('title', ''))[:50]}")
    print(f"   来源   : {str(target.get('url', ''))[:80]}（{target.get('source_type', '未知')}）")
    print(f"   正文   : {len(text)} 字")
    started = time.time()
    stats = extract_facts(
        llm,
        kb,
        title=target["title"],
        text=text,
        url=target["url"],
        source_type=target["source_type"],
        topic="联调测试",
        quality_label="normal",
        max_facts=5,
    )
    print(f"   耗时   : {time.time() - started:.1f}s")
    print(f"   统计   : 新增 {stats['added']}｜更新 {stats['updated']}｜重复 {stats['duplicate']}｜冲突 {stats['conflict']}")
    if stats["error"]:
        print(f"   ❌ 错误：{stats['error']}")
        return False
    facts = kb.list_facts(limit=10)
    print(f"   入库条目 {len(facts)} 条：")
    for fact in facts[:5]:
        print(f"     · [{fact['confidence']:.2f}] {fact['title']}｜{fact['tags']}")
        print(f"       {fact['answer'][:110]}")
    return bool(facts)


def check_providers(config) -> bool:
    print("=" * 70)
    print("0) 服务商预设与模型列表")
    from app.core import providers
    from app.core.llm import build_llm

    print(f"   共 {len(providers.PROVIDERS)} 个预设：")
    for item in providers.PROVIDERS:
        base = item["base_url"] or "（需自行填写）"
        print(f"     {item['id']:<12} {item['protocol']:<9} {item['label']:<22} {base}")
    print()
    client = build_llm(config)
    print(f"   当前配置的对话端点 : {client._resolve_url('chat')}")
    print(f"   模型列表端点       : {client._models_url()}")
    try:
        names = client.list_models()
    except Exception as error:  # noqa: BLE001
        print(f"   ❌ 拉取模型列表失败：{error}")
        return False
    print(f"   ✅ 拉取到 {len(names)} 个可用模型：{names[:15]}")
    print("   （界面上的「拉取可用模型」按钮就是调用这个接口）")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="开发期真实链路联调")
    parser.add_argument(
        "--only", choices=["providers", "llm", "chat", "web", "extract"], default=""
    )
    args = parser.parse_args()

    config, kb = _setup()
    if not config.get_secret("key_enc", "llm"):
        print("❌ .env 中没有读到模型 API Key，无法联调")
        return 2

    results = {}
    if args.only in ("", "providers"):
        results["providers"] = check_providers(config)
    if args.only in ("", "llm"):
        results["llm"] = check_llm(config)
    if args.only in ("", "chat"):
        results["chat"] = check_chat(config, kb)
    if args.only in ("", "web"):
        results["web"] = check_web(config, kb)
    if args.only in ("", "extract"):
        results["extract"] = check_extract(config, kb)

    print("=" * 70)
    print("汇总：", "　".join(f"{k}={'✅' if v else '❌'}" for k, v in results.items()))
    kb.close()
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
