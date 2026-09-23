"""服务商预设目录。

背景
----
让用户手填 Base URL 是错的：留空时只能回落到 OpenAI 官方地址，
于是「填了 DeepSeek 的 Key 却去打 api.openai.com」→ HTTP 401（实际踩到过）。

这里为每个主流服务商预置**正确的 Base URL**，用户只需要：
选服务商 → 粘贴 API Key → 点「拉取可用模型」选模型。

模型名刻意只给「建议值」而不是写死：各家模型迭代很快
（例如 DeepSeek 的 /models 现在返回 deepseek-flash、deepseek-v4-pro），
所以界面上提供了「拉取可用模型」按钮，直接问服务商要当前可用列表。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# protocol: openai = OpenAI 兼容协议；anthropic / gemini = 原生协议
PROVIDERS: List[Dict[str, Any]] = [
    {
        "id": "deepseek",
        "label": "DeepSeek 深度求索",
        "protocol": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "note": "国内可直连，性价比高，本项目的默认推荐",
        "docs": "https://platform.deepseek.com/api_keys",
    },
    {
        "id": "dashscope",
        "label": "通义千问（阿里云百炼）",
        "protocol": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "note": "国内可直连，OpenAI 兼容模式",
        "docs": "https://bailian.console.aliyun.com/",
    },
    {
        "id": "moonshot",
        "label": "Kimi（月之暗面）",
        "protocol": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "note": "国内可直连",
        "docs": "https://platform.moonshot.cn/console/api-keys",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "protocol": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "note": "国内可直连，另有免费档模型",
        "docs": "https://open.bigmodel.cn/usercenter/apikeys",
    },
    {
        "id": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "protocol": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "note": "聚合多家开源模型，国内可直连",
        "docs": "https://cloud.siliconflow.cn/account/ak",
    },
    {
        "id": "volcengine",
        "label": "火山方舟（豆包）",
        "protocol": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "note": "模型名通常要填「接入点 ID」(ep-xxxx)，请用拉取或按控制台填写",
        "docs": "https://console.volcengine.com/ark",
    },
    {
        "id": "hunyuan",
        "label": "腾讯混元",
        "protocol": "openai",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "note": "国内可直连",
        "docs": "https://console.cloud.tencent.com/hunyuan/api-key",
    },
    {
        "id": "stepfun",
        "label": "阶跃星辰 StepFun",
        "protocol": "openai",
        "base_url": "https://api.stepfun.com/v1",
        "note": "国内可直连",
        "docs": "https://platform.stepfun.com/",
    },
    {
        "id": "minimax",
        "label": "MiniMax",
        "protocol": "openai",
        "base_url": "https://api.minimax.chat/v1",
        "note": "国内可直连",
        "docs": "https://platform.minimaxi.com/",
    },
    {
        "id": "baichuan",
        "label": "百川智能",
        "protocol": "openai",
        "base_url": "https://api.baichuan-ai.com/v1",
        "note": "国内可直连",
        "docs": "https://platform.baichuan-ai.com/console/apikey",
    },
    {
        "id": "openai",
        "label": "OpenAI 官方",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "note": "需要能直连海外网络",
        "docs": "https://platform.openai.com/api-keys",
    },
    {
        "id": "anthropic",
        "label": "Anthropic Claude",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com",
        "note": "原生协议；需要能直连海外网络",
        "docs": "https://console.anthropic.com/settings/keys",
    },
    {
        "id": "gemini",
        "label": "Google Gemini",
        "protocol": "gemini",
        "base_url": "https://generativelanguage.googleapis.com",
        "note": "原生协议；需要能直连海外网络",
        "docs": "https://aistudio.google.com/app/apikey",
    },
    {
        "id": "ollama",
        "label": "Ollama（本机运行）",
        "protocol": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "note": "本地推理，不需要联网；Key 随便填（如 ollama）",
        "docs": "https://ollama.com/",
    },
    {
        "id": "lmstudio",
        "label": "LM Studio（本机运行）",
        "protocol": "openai",
        "base_url": "http://127.0.0.1:1234/v1",
        "note": "本地推理；Key 随便填",
        "docs": "https://lmstudio.ai/",
    },
    {
        "id": "vllm",
        "label": "vLLM / 自建网关（本机或内网）",
        "protocol": "openai",
        "base_url": "http://127.0.0.1:8000/v1",
        "note": "任何 OpenAI 兼容网关都可以，如 OneAPI / NewAPI",
        "docs": "",
    },
    {
        "id": "custom",
        "label": "自定义（自己填 Base URL）",
        "protocol": "openai",
        "base_url": "",
        "note": "用于上面没有列出的服务商；Base URL 必须自己填对",
        "docs": "",
    },
]

PROVIDER_BY_ID: Dict[str, Dict[str, Any]] = {item["id"]: item for item in PROVIDERS}


def get_provider(preset_id: str) -> Optional[Dict[str, Any]]:
    return PROVIDER_BY_ID.get((preset_id or "").strip().lower())


def default_base_url(preset_id: str) -> str:
    item = get_provider(preset_id)
    return item["base_url"] if item else ""


def protocol_of(preset_id: str) -> str:
    item = get_provider(preset_id)
    return item["protocol"] if item else "openai"
