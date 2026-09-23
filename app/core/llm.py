"""模型客户端：一套接口同时支持 OpenAI 兼容 / Anthropic / Gemini 三种协议。

设计取舍
--------
- 直接用 httpx 手写协议，不引入各家官方 SDK：依赖更少、exe 更小、
  也可让用户自由填 base_url（DeepSeek、通义、Kimi、硅基流动、Ollama、
  vLLM、OneAPI 等一切兼容网关）。
- 全部同步接口：FastAPI 侧用同步路由（自动跑在线程池），
  流式回答用同步生成器 + StreamingResponse，避免 async/sync 混用的坑。
- 所有异常信息都会经过 secrets.scrub()，绝不把密钥带进错误栈。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

import httpx

from . import secrets

PROVIDERS = ("openai", "anthropic", "gemini")

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
}

PROVIDER_LABELS = {
    "openai": "OpenAI 兼容（DeepSeek / 通义 / Kimi / Ollama / vLLM 等）",
    "anthropic": "Anthropic Claude 原生协议",
    "gemini": "Google Gemini 原生协议",
}

ANTHROPIC_VERSION = "2023-06-01"

# Gemini 原生协议的动作名。判断「用户是否直接填了完整方法路径」时必须命中其中之一：
# `http://127.0.0.1:8000` 这种带端口的裸地址最后一段也含冒号，只看冒号会把请求
# 发到裸主机上（见 `_endpoint` 的注释）。
GEMINI_ACTIONS = ("generateContent", "streamGenerateContent", "embedContent", "countTokens")


class LLMError(RuntimeError):
    """模型调用失败，message 已脱敏，可直接展示给用户。"""

    def __init__(self, message: str, retry_after: float = 0.0, status: int = 0) -> None:
        super().__init__(message)
        # 服务端用 Retry-After 给出需要等待的时长（429/503 常见）：
        # 过去直接忽略，按 1.5s 线性退避硬撞，等于自己把限流撞得更死。
        self.retry_after = float(retry_after or 0.0)
        self.status = int(status or 0)


@dataclass
class ChatResult:
    text: str
    model: str = ""
    provider: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def normalize_base_url(provider: str, base_url: str) -> str:
    """把用户填的 base_url 规整成可直接拼接接口路径的形式。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return DEFAULT_BASE_URLS.get(provider, "")
    return base


def _retry_after_seconds(value: str) -> float:
    """解析 Retry-After 头（秒数或 HTTP 日期两种格式），拿不准就返回 0。"""
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(text)
        if target is None:
            return 0.0
        return max(0.0, target.timestamp() - time.time())
    except Exception:  # noqa: BLE001 - 头部格式千奇百怪，解析不了就当没有
        return 0.0


# 只重试「大概是瞬时抖动」的网络错误。过去是 isinstance(error, httpx.HTTPError)，
# 于是 DNS 打错、TLS 校验失败、base_url 填错这类永远不可能成功的错误也会重试 3 次，
# 每次还睡 1.5s/3s/4.5s，用户看到的是「卡了很久然后报一个配置错误」。
RETRYABLE_TRANSPORT_ERRORS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    httpx.ConnectError,
)

# 可以退避重试的 HTTP 状态：限流与网关/服务端临时故障
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524})

# 模型输出的解析上限与围栏剥离正则
MAX_EXTRACT_CHARS = 200_000
_FENCE_OPEN = re.compile(r"^```[a-zA-Z0-9_-]*[ \t]*\r?\n?")
_FENCE_CLOSE = re.compile(r"\r?\n?[ \t]*```[ \t]*$")


def _retry_wait(attempt: int, error: Optional[Exception]) -> float:
    """第 attempt+1 次重试前该睡多久：优先听 Retry-After，否则指数退避 + 抖动。"""
    server_hint = float(getattr(error, "retry_after", 0.0) or 0.0)
    base = 1.5 * (2 ** attempt)
    delay = max(base, server_hint)
    # 抖动：多个来源同时撞限流时，避免所有请求在同一毫秒一起回来
    return min(delay + random.uniform(0.0, 0.5), 60.0)


def _raise_for_status(response: httpx.Response, provider: str) -> None:
    if response.status_code < 400:
        return
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error)
            elif error:
                detail = str(error)
            else:
                detail = str(payload.get("message") or payload)[:500]
    except Exception:
        detail = (response.text or "")[:500]
    hints = {
        401: "密钥无效或未授权，请检查 API Key",
        403: "密钥无权访问该模型，或该模型未开通",
        404: "接口路径或模型名不存在，请检查 base_url 与模型名",
        429: "触发限流或额度不足，请稍后重试",
    }
    hint = hints.get(response.status_code, "")
    raise LLMError(
        secrets.scrub(
            f"[{provider}] HTTP {response.status_code} {hint} {detail}".strip()
        ),
        retry_after=_retry_after_seconds(response.headers.get("retry-after", "")),
        status=response.status_code,
    )


class LLMClient:
    """按配置发起的对话补全客户端。"""

    def __init__(
        self,
        provider: str = "openai",
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        temperature: float = 0.3,
        max_tokens: int = 3000,
        timeout: float = 120,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.provider = (provider or "openai").lower()
        if self.provider not in PROVIDERS:
            self.provider = "openai"
        self.base_url = normalize_base_url(self.provider, base_url)
        self.api_key = api_key or ""
        self.model = model or ""
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout)
        self.extra_headers = dict(extra_headers or {})
        # 连接池复用：一次抓取里每页两次模型调用，每次调用再重试两次，
        # 若每次尝试都新建 client，就是成百上千次 TLS 握手。这里复用一个。
        self._client: Optional[httpx.Client] = None

    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        return self._client

    def reset_client(self) -> None:
        """丢弃连接池；流式被中途放弃、以及每次重试前都会用到。"""
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self.reset_client()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 内部构造
    # ------------------------------------------------------------------

    def _endpoint(self) -> str:
        if self.provider == "openai":
            base = self.base_url or DEFAULT_BASE_URLS["openai"]
            if base.endswith("/chat/completions"):
                return base
            if base.endswith("/v1") or "/v1" in base or base.endswith("/v1beta"):
                return _join_url(base, "chat/completions")
            return _join_url(base, "v1/chat/completions")
        if self.provider == "anthropic":
            base = self.base_url or DEFAULT_BASE_URLS["anthropic"]
            if base.endswith("/messages"):
                return base
            if base.endswith("/v1"):
                return _join_url(base, "messages")
            return _join_url(base, "v1/messages")
        base = self.base_url or DEFAULT_BASE_URLS["gemini"]
        # 用户直接填了完整方法路径（…/models/gemini-x:generateContent）时原样使用。
        # 必须同时命中已知动作名：`http://127.0.0.1:8000` 这种带端口的裸地址最后一段
        # 也含冒号，只看冒号会把请求 POST 到裸主机上，报一个看不懂的 404。
        if ":" in base.split("/")[-1] and any(action in base for action in GEMINI_ACTIONS):
            return base
        if "/models/" in base:
            return f"{base}:{{action}}"
        if not base.endswith("/v1beta") and "/v1beta" not in base:
            base = _join_url(base, "v1beta")
        return f"{base}/models/{{model}}:{{action}}"

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.provider == "openai":
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.provider == "anthropic":
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = ANTHROPIC_VERSION
        else:
            headers["x-goog-api-key"] = self.api_key
        headers.update({k: v for k, v in self.extra_headers.items() if v})
        return headers

    def _payload(
        self,
        messages: Sequence[Dict[str, str]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        stream: bool,
    ) -> Dict[str, Any]:
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        if self.provider == "openai":
            return {
                "model": self.model,
                "messages": list(messages),
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": stream,
            }

        if self.provider == "anthropic":
            system_parts = [m["content"] for m in messages if m.get("role") == "system"]
            conversation = [m for m in messages if m.get("role") != "system"]
            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": conversation,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": stream,
            }
            if system_parts:
                payload["system"] = "\n\n".join(system_parts)
            return payload

        # gemini
        contents: List[Dict[str, Any]] = []
        system_parts: List[str] = []
        for message in messages:
            role = message.get("role", "user")
            if role == "system":
                system_parts.append(message.get("content", ""))
                continue
            contents.append(
                {"role": "model" if role == "assistant" else "user", "parts": [{"text": message.get("content", "")}]}
            )
        payload = {
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        return payload

    def _resolve_url(self, action: str) -> str:
        endpoint = self._endpoint()
        if "{model}" in endpoint or "{action}" in endpoint:
            endpoint = endpoint.replace("{model}", self.model).replace("{action}", action)
        if self.provider == "gemini" and not endpoint.endswith("alt=sse") and action.endswith("streamGenerateContent"):
            endpoint = f"{endpoint}?alt=sse"
        return endpoint

    # ------------------------------------------------------------------
    # 对话
    # ------------------------------------------------------------------

    def _ensure_ready(self) -> None:
        if not self.api_key:
            raise LLMError("尚未配置模型 API Key，请到「设置」页填写并保存。")
        if not self.model:
            raise LLMError("尚未配置模型名称，请到「设置」页填写（例如 deepseek-chat）。")

    def chat(
        self,
        messages: Sequence[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        retries: int = 2,
    ) -> ChatResult:
        """一次性返回完整回答。"""
        self._ensure_ready()
        url = self._resolve_url("generateContent" if self.provider == "gemini" else "chat")
        payload = self._payload(messages, temperature, max_tokens, stream=False)
        started = time.perf_counter()
        last_error: Optional[Exception] = None

        for attempt in range(retries + 1):
            try:
                response = self.client().post(url, headers=self._headers(), json=payload)
                _raise_for_status(response, self.provider)
                data = response.json()
                text, usage = self._parse_response(data)
                return ChatResult(
                    text=text,
                    model=self.model,
                    provider=self.provider,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    prompt_tokens=usage.get("prompt", 0),
                    completion_tokens=usage.get("completion", 0),
                    raw=data,
                )
            except (httpx.HTTPError, LLMError) as error:
                last_error = error
                # 只重试瞬时抖动：见 RETRYABLE_TRANSPORT_ERRORS 的说明
                retryable = isinstance(error, RETRYABLE_TRANSPORT_ERRORS) or (
                    isinstance(error, LLMError) and error.status in RETRYABLE_STATUSES
                )
                if attempt >= retries or not retryable:
                    break
                # 连接池里可能留着一条已经死掉的 keep-alive，重试前先丢掉
                self.reset_client()
                time.sleep(_retry_wait(attempt, error))
        # 状态码与 Retry-After 必须透传：否则 401（密钥错）、429（限流）、
        # 500（服务端故障）在上层与 UI 眼里完全一样，用户无法判断该改什么。
        raise LLMError(
            secrets.scrub(str(last_error or "模型调用失败")),
            retry_after=getattr(last_error, "retry_after", 0.0),
            status=getattr(last_error, "status", 0),
        )

    def stream(
        self,
        messages: Sequence[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        retries: int = 1,
    ) -> Iterator[str]:
        """流式返回增量文本。

        过去这里没有任何重试，也没有整体时限：标量 timeout 是「每个阶段」的超时，
        提供商每 60 秒吐一个 token 就能把流式回答永远吊住。
        现在加了三层保护：整体墙钟预算、首个块的单独期限、以及**只在还没有吐过
        任何文字之前**允许重试（已经吐出去的内容没法撤回，重试会重复输出）。
        """
        self._ensure_ready()
        action = "streamGenerateContent" if self.provider == "gemini" else "chat"
        url = self._resolve_url(action)
        payload = self._payload(messages, temperature, max_tokens, stream=True)
        last_error: Optional[Exception] = None
        malformed = 0          # SSE 行解析失败计数：不再静默丢弃，结束时记一条日志
        emitted = False
        for attempt in range(retries + 1):
            if attempt:
                self.reset_client()
                time.sleep(_retry_wait(attempt - 1, last_error))
            chunk_seen = False
            deadline = time.monotonic() + max(self.timeout, 60.0)
            try:
                with self.client().stream("POST", url, headers=self._headers(), json=payload) as response:
                    if response.status_code >= 400:
                        response.read()
                        _raise_for_status(response, self.provider)
                    for line in response.iter_lines():
                        if time.monotonic() > deadline:
                            raise httpx.ReadTimeout("流式响应超出整体时限")
                        chunk = self._parse_stream_line(line)
                        if chunk:
                            chunk_seen = True
                            emitted = True
                            yield chunk
                        elif line and line.strip() not in ("", "data: [DONE]"):
                            malformed += 1
                if malformed:
                    logging.warning(
                        "[%s] 流式响应里有 %d 行无法解析（协议不匹配或服务端异常），已跳过",
                        self.provider, malformed,
                    )
                return
            except (httpx.HTTPError, LLMError) as error:
                last_error = error
                if chunk_seen or emitted:
                    # 已经吐过字了：重试会让用户看到重复的文本，直接如实报错
                    raise LLMError(
                        secrets.scrub(f"[{self.provider}] 流式响应中断：{error}"),
                        retry_after=getattr(error, "retry_after", 0.0),
                        status=getattr(error, "status", 0),
                    ) from error
                # LLMError 也要参与重试：以前只捕 httpx.HTTPError，于是 _raise_for_status
                # 抛出的 429/5xx 直接穿出循环，retries 参数对流式路径形同虚设。
                retryable = isinstance(error, RETRYABLE_TRANSPORT_ERRORS) or (
                    isinstance(error, LLMError) and error.status in RETRYABLE_STATUSES
                )
                if attempt >= retries or not retryable:
                    raise LLMError(
                        secrets.scrub(f"[{self.provider}] 网络错误：{error}"),
                        retry_after=getattr(error, "retry_after", 0.0),
                        status=getattr(error, "status", 0),
                    ) from error
                logging.warning("[%s] 流式请求失败，重试第 %d 次：%s", self.provider, attempt + 1, error)

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def _parse_response(self, data: Dict[str, Any]) -> Any:
        if self.provider == "openai":
            choices = data.get("choices") or []
            text = ""
            if choices:
                message = choices[0].get("message") or {}
                text = message.get("content") or ""
                if not text and choices[0].get("text"):
                    text = choices[0]["text"]
            usage = data.get("usage") or {}
            return text, {
                "prompt": usage.get("prompt_tokens", 0),
                "completion": usage.get("completion_tokens", 0),
            }

        if self.provider == "anthropic":
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
            usage = data.get("usage") or {}
            return text, {
                "prompt": usage.get("input_tokens", 0),
                "completion": usage.get("output_tokens", 0),
            }

        candidates = data.get("candidates") or []
        text = ""
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        usage = data.get("usageMetadata") or {}
        return text, {
            "prompt": usage.get("promptTokenCount", 0),
            "completion": usage.get("candidatesTokenCount", 0),
        }

    def _parse_stream_line(self, line: str) -> str:
        line = (line or "").strip()
        if not line:
            return ""
        if self.provider == "openai":
            if not line.startswith("data:"):
                return ""
            body = line[5:].strip()
            if body == "[DONE]":
                return ""
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return ""
            choices = data.get("choices") or []
            if not choices:
                return ""
            delta = choices[0].get("delta") or {}
            return delta.get("content") or ""

        if self.provider == "anthropic":
            if not line.startswith("data:"):
                return ""
            try:
                data = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                return ""
            if data.get("type") == "content_block_delta":
                return (data.get("delta") or {}).get("text") or ""
            return ""

        # gemini SSE
        if line.startswith("data:"):
            line = line[5:].strip()
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return ""
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict))

    # ------------------------------------------------------------------
    # 可用模型列表（解决「模型名会过时」的问题：直接问服务商要）
    # ------------------------------------------------------------------

    def _models_url(self) -> str:
        base = self.base_url or DEFAULT_BASE_URLS.get(self.provider, "")
        if self.provider == "openai":
            if base.endswith("/models"):
                return base
            if "/v1" in base or "/v1beta" in base:
                return _join_url(base, "models")
            return _join_url(base, "v1/models")
        if self.provider == "anthropic":
            if base.endswith("/models"):
                return base
            if base.endswith("/v1"):
                return _join_url(base, "models")
            return _join_url(base, "v1/models")
        if not base.endswith("/v1beta") and "/v1beta" not in base:
            base = _join_url(base, "v1beta")
        return _join_url(base, "models")

    def list_models(self, timeout: Optional[float] = None) -> List[str]:
        """拉取该服务商当前可用的模型 ID 列表。

        比在界面里写死模型名可靠得多——各家的模型迭代很快。
        取不到时抛出 LLMError，由界面提示用户手动填写。
        timeout 用于「拉取模型列表」按钮：默认沿用客户端超时（可能 120 秒），
        界面长时间没有反应会被当成程序卡死。
        """
        url = self._models_url()
        headers = self._headers()
        headers["Accept"] = "application/json"
        limit = min(self.timeout, 30) if timeout is None else max(1.0, float(timeout))
        try:
            with httpx.Client(timeout=limit, follow_redirects=True) as client:
                response = client.get(url, headers=headers)
            _raise_for_status(response, f"{self.provider} 模型列表")
            payload = response.json()
        except LLMError:
            raise
        except Exception as error:  # noqa: BLE001
            raise LLMError(secrets.scrub(f"获取模型列表失败：{error}")) from error

        names: List[str] = []
        if self.provider == "gemini":
            for item in payload.get("models") or []:
                name = str(item.get("name") or "")
                methods = item.get("supportedGenerationMethods") or []
                if methods and "generateContent" not in methods:
                    continue
                names.append(name.split("/", 1)[-1] if name.startswith("models/") else name)
        else:
            for item in payload.get("data") or []:
                model_id = item.get("id") or item.get("name")
                if model_id:
                    names.append(str(model_id))
        return sorted({name for name in names if name})

    # ------------------------------------------------------------------
    # 连通性测试
    # ------------------------------------------------------------------

    def test_connection(self) -> Dict[str, Any]:
        """给「设置」页用的连通性自检，返回结构化结果（不含密钥）。"""
        result: Dict[str, Any] = {
            "ok": False,
            "provider": self.provider,
            "provider_label": PROVIDER_LABELS.get(self.provider, self.provider),
            "model": self.model,
            "endpoint": self._resolve_url("generateContent" if self.provider == "gemini" else "chat"),
            "latency_ms": 0,
            "sample": "",
            "error": "",
            "key_preview": secrets.mask(self.api_key),
        }
        try:
            reply = self.chat(
                [
                    {"role": "system", "content": "你是连通性测试助手，只回复要求的内容。"},
                    {"role": "user", "content": "请只回复两个字：正常"},
                ],
                temperature=0,
                max_tokens=32,
                retries=0,
            )
            result.update(
                ok=True,
                latency_ms=reply.latency_ms,
                sample=(reply.text or "").strip()[:80],
            )
        except Exception as error:  # noqa: BLE001 - 需要把任何异常都转成可读结果
            result["error"] = secrets.scrub(str(error))[:600]
        return result

    # ------------------------------------------------------------------
    # 结构化输出
    # ------------------------------------------------------------------

    def chat_json(
        self,
        messages: Sequence[Dict[str, str]],
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        retries: int = 1,
    ) -> Any:
        """要求模型输出 JSON，并做容错解析。"""
        result = self.chat(messages, temperature=temperature, max_tokens=max_tokens, retries=retries)
        return extract_json(result.text)


def extract_json(text: str) -> Any:
    """从模型输出里稳健地取出 JSON（容忍 ```json 围栏与前后废话）。"""
    if not text:
        raise LLMError("模型未返回内容")
    # 上限保护：json.loads 对深嵌套会抛 RecursionError，超长文本还会吃掉大量内存，
    # 而这两种异常过去都不在 except 名单里，于是从「模型返回了坏 JSON」升级成
    # 「整个抽取流程崩掉、这一页静默不入库」。
    if len(text) > MAX_EXTRACT_CHARS:
        raise LLMError(f"模型返回内容过长（{len(text)} 字符，上限 {MAX_EXTRACT_CHARS}），已放弃解析")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # 用正则剥围栏：原实现是「按第一个换行切一刀再把末尾三引号切掉」，
        # 单行 ```json[{...}]``` 这种形态第一个换行不存在，围栏原样留下 → 解析必失败。
        cleaned = _FENCE_OPEN.sub("", cleaned, count=1)
        cleaned = _FENCE_CLOSE.sub("", cleaned, count=1)
        cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, RecursionError, MemoryError):
        pass

    # 扫描第一个平衡的 {...} 或 [...]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        if start < 0:
            continue
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    snippet = cleaned[start : index + 1]
                    try:
                        return json.loads(snippet)
                    except (json.JSONDecodeError, RecursionError, MemoryError):
                        break
    raise LLMError(f"模型返回的内容不是合法 JSON：{secrets.scrub(text[:200])}")


def build_llm(config: Any) -> LLMClient:
    """从 Config 构造对话客户端。

    base_url 的解析优先级：用户填写的值 > 服务商预设的官方地址。
    这一步很关键：过去留空会回落到 OpenAI 官方地址，
    导致「填了 DeepSeek 的 Key 却去打 api.openai.com」→ HTTP 401。

    模型名**不做任何猜测**：没填就直接报错让用户去「拉取可用模型」选，
    否则预填的名字一旦过时，界面会显示一个其实不存在的模型。
    """
    from . import providers

    section = config.section("llm")
    preset = str(section.get("preset") or "")
    base_url = str(section.get("base_url") or "").strip() or providers.default_base_url(preset)
    provider = str(section.get("provider") or "").strip() or providers.protocol_of(preset)
    return LLMClient(
        provider=provider,
        base_url=base_url,
        api_key=config.get_secret("key_enc", "llm"),
        model=str(section.get("model") or "").strip(),
        temperature=section.get("temperature", 0.3),
        max_tokens=section.get("max_tokens", 3000),
        timeout=section.get("timeout", 120),
        extra_headers=section.get("extra_headers") or {},
    )


class EmbeddingClient:
    """可选的向量化客户端（仅 OpenAI 兼容的 /embeddings 接口）。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60, batch_size: int = 16) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URLS["openai"]).rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.batch_size = max(1, int(batch_size))

    def _endpoint(self) -> str:
        if self.base_url.endswith("/embeddings"):
            return self.base_url
        if self.base_url.endswith("/v1") or "/v1" in self.base_url:
            return _join_url(self.base_url, "embeddings")
        return _join_url(self.base_url, "v1/embeddings")

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        if not self.api_key or not self.model:
            raise LLMError("向量模型未配置完整（需要 base_url / api_key / model）。")
        vectors: List[List[float]] = []
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for start in range(0, len(texts), self.batch_size):
                batch = list(texts[start : start + self.batch_size])
                response = client.post(
                    self._endpoint(),
                    headers=headers,
                    json={"model": self.model, "input": batch},
                )
                _raise_for_status(response, "embeddings")
                data = response.json()
                items = sorted(data.get("data", []), key=lambda item: item.get("index", 0))
                # 条数必须与请求一致：少返一条会让此后每条向量与文本整体错位，
                # 而且既不报错也不重试，错误会一路静默传到检索结果里。
                if len(items) != len(batch):
                    raise LLMError(
                        f"向量接口返回 {len(items)} 条，与请求的 {len(batch)} 条不一致，已放弃这一批。"
                    )
                batch_vectors = [item.get("embedding") or [] for item in items]
                if any(not vector for vector in batch_vectors):
                    raise LLMError("向量接口返回了空向量，已放弃这一批。")
                vectors.extend(batch_vectors)
        return vectors

    def test_connection(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "ok": False,
            "model": self.model,
            "dim": 0,
            "error": "",
            # 端点会回显给界面，用户可能在 base_url 里带查询串（例如 ?key=…），
            # 所以要过一遍清洗再返回。
            "endpoint": secrets.scrub(self._endpoint()),
        }
        try:
            vectors = self.embed(["连通性测试"])
            result["ok"] = bool(vectors and vectors[0])
            result["dim"] = len(vectors[0]) if vectors and vectors[0] else 0
        except Exception as error:  # noqa: BLE001
            result["error"] = secrets.scrub(str(error))[:600]
        return result


def build_embedder(config: Any) -> Optional[EmbeddingClient]:
    section = config.section("embedding")
    if not section.get("enabled"):
        return None
    return EmbeddingClient(
        base_url=section.get("base_url", ""),
        api_key=config.get_secret("key_enc", "embedding"),
        model=section.get("model", ""),
        batch_size=section.get("batch_size", 16),
        timeout=config.get("llm", "timeout", 60),
    )
