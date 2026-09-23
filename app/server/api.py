"""本地 HTTP 接口层。

安全设计
--------
- 只监听 127.0.0.1，随机端口，不对外网暴露；
- 校验 Host 头：本机以外的域名一律 403。仅靠「只监听 127.0.0.1」挡不住 DNS
  rebinding——攻击页把自己的域名解析到 127.0.0.1，浏览器就会带着
  `Host: evil.tld` 访问本服务，而请求在服务端看来完全同源；
- 首屏访问时下发一枚随机 token 到 SameSite=Strict + HttpOnly 的 Cookie，之后所有
  /api/* 都要带上它，防止本机其它程序/网页读取知识库；
- 任何返回给前端的配置都走 config.masked_summary()，绝不回传明文密钥；
- 错误信息统一经 secrets.scrub() 清洗，避免密钥出现在报错里。
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets as pysecrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from .. import APP_TITLE, __version__
from ..config import DEFAULT_CONFIG, Config
from ..core import env as env_mod
from ..core import curation
from ..core import paths, providers, secrets
from ..core.autoupdate import UpdateManager
from ..core.fetch import Fetcher, build_fetcher
from ..core.ingest import ingest_sources, load_seed, seed_fingerprint
from ..core.llm import LLMError, build_embedder, build_llm
from ..core.rag import RagEngine
from ..core.search import build_search
from ..core.sources import get_sources
from ..core.starter import build_starter_asks
from ..core.store import KnowledgeBase
from ..core import trust as trust_mod

TOKEN_COOKIE = "nte-rag_token"
TOKEN_HEADER = "x-nte-rag-token"
AUTH_DISABLED = env_mod.get_bool(env_mod.DISABLE_AUTH)

_LOGGER = logging.getLogger(env_mod.LOGGER_NAME)

# 壁纸：只接受常见图片格式，只从数据目录读写（不提供任意路径读取接口）。
WALLPAPER_TYPES: Dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}
WALLPAPER_SUFFIX: Dict[str, str] = {v: k for k, v in WALLPAPER_TYPES.items()}
WALLPAPER_MAX_BYTES = 12 * 1024 * 1024
# 手动添加知识条目的正文上限。界面上**故意没有**加 maxlength：手写条目允许粘贴
# 整段攻略，限制在 4000 字那种级别会挡掉正常用法；但完全没有上限时，
# 一份几 MB 的文档会被收下、入库，并在每次打开知识库时重新渲染进列表。
KB_ANSWER_MAX_CHARS = 200_000


class AppContext:
    """进程内共享的运行时上下文。"""

    def __init__(self) -> None:
        self.config = Config()
        self.config.apply_dev_env()
        self.kb = KnowledgeBase()
        self.updater = UpdateManager(self.config, self.kb)
        self.rag = RagEngine(self.config, self.kb)
        self.token = pysecrets.token_urlsafe(24)
        self.started_at = time.time()
        self._seed_checked = False

    def ensure_curation(self) -> None:
        """把「已证伪来源的撤回」补做到已有库上（幂等，名单见 app/core/curation.py）。

        撤回名单是代码的一部分，但被证伪的内容可能早就躺在库里（种子导入带来的，
        或旧版本抓回来的）。种子指纹没变时 `ensure_seed()` 会直接早退，所以这件事
        必须独立跑，否则老用户升到新版照样会看到「两个说法互相矛盾」。
        """
        try:
            report = curation.apply_revocations(self.kb)
        except Exception:
            return
        if report.get("applied"):
            self.kb.set_meta("curation_report", json.dumps(report, ensure_ascii=False))

    def ensure_seed(self) -> None:
        """导入随程序分发的种子知识库。

        不只是「首次运行」：每次启动都比对种子文件指纹，换了新种子就重新导入。
        否则用户升级 exe 后，新版种子里的资料（例如后来补进来的表格数值条目）
        永远进不了他的知识库——旧实现只看 `seed_loaded=1`，升级等于白升。
        """
        if self._seed_checked:
            return
        self._seed_checked = True
        # 先撤回再导入：反过来的话，种子里的已证伪页面会先被写进去再撤回，
        # 白白多一次写入。
        self.ensure_curation()
        try:
            seed_path = paths.seed_kb_path()
            fingerprint = seed_fingerprint(seed_path)
            if fingerprint and self.kb.get_meta("seed_fingerprint", "") == fingerprint:
                return
            report = load_seed(self.kb, self.config, seed_path)
            if not report.get("skipped"):
                self.kb.set_meta(
                    "seed_report",
                    json.dumps(report, ensure_ascii=False),
                )
        except Exception:
            pass

    def state(self) -> Dict[str, Any]:
        stats = self.kb.stats()
        return {
            "version": __version__,
            "title": APP_TITLE,
            "uptime_s": round(time.time() - self.started_at, 1),
            "llm_ready": self.rag.llm_ready(),
            "update": self.updater.status(),
            "stats": stats,
            "paths": paths.describe_layout(),
        }


# 对话请求的硬上限（见 ChatRequest）：界面只带最近 6 轮、每轮几百字，
# 这里给足两倍余量，只为挡住「一条 curl 把几兆文本发给付费模型」。
MAX_HISTORY_TURNS = 12
MAX_HISTORY_CHARS = 2000
MAX_TOP_K = 50


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    # history 是直接转发给付费模型的消息列表：改前只限了 question，history 与
    # top_k 完全无界，一条 POST 就能把几兆文本原样送到服务商（花用户的钱）。
    history: List[Dict[str, str]] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)
    allow_web: Optional[bool] = None
    top_k: Optional[int] = Field(None, ge=1, le=MAX_TOP_K)

    @field_validator("history")
    @classmethod
    def _trim_history(cls, value: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """单条消息也限长：`max_length` 只管列表条数，不管里面的字符串。"""
        trimmed: List[Dict[str, str]] = []
        for item in value or []:
            if not isinstance(item, dict):
                continue
            trimmed.append(
                {
                    str(key)[:32]: str(text)[:MAX_HISTORY_CHARS]
                    for key, text in list(item.items())[:4]
                }
            )
        return trimmed[-MAX_HISTORY_TURNS:]


def _host_name(value: str) -> str:
    """从 Host 头取出主机名：去掉端口、去掉 IPv6 方括号、小写、去掉末尾的点。

    `Host: 127.0.0.1:51234` 与 `Host: [::1]:51234` 都要能正确归一，
    否则本机访问会被自己的校验挡住。
    """
    text = (value or "").strip()
    if not text:
        return ""
    if text.startswith("["):
        return text[1 : text.find("]")].lower() if "]" in text else text[1:].lower()
    if ":" in text:
        return text.rsplit(":", 1)[0].lower()
    return text.lower().rstrip(".")


ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


class _LocalHostGuard(BaseHTTPMiddleware):
    """拒绝 Host 头不是本机回环地址的请求（防 DNS rebinding）。

    本服务只可能在 127.0.0.1 上被访问，所以 Host 必然是本机回环名；
    出现别的域名就说明有人把外部域名解析到了 127.0.0.1 来借道浏览器。
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        if _host_name(request.headers.get("host", "")) not in ALLOWED_HOSTS:
            return PlainTextResponse(
                "拒绝访问：本服务只接受来自本机回环地址的请求。",
                status_code=403,
            )
        return await call_next(request)


# 内容安全策略：这个应用不需要从任何外部站点加载东西——界面脚本/样式的来源只有
# 自己，模型与搜索都是服务端出网。所以除了「本机」一律关掉。
# script-src 里没有 'unsafe-inline'：index.html 只有一行外链 <script src="/app.js">，
# 页面里没有内联脚本，代码也都是 addEventListener 而不是 onclick= 属性。
# style-src 的 data: 给壁纸预览用；img-src 的 blob: 给导出的本地对象 URL。
# CSP 是纵深防御（万一某个渲染汇点漏了转义），不是替代 esc()/renderMarkdown。
CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline' data:; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class _SecurityHeaders(BaseHTTPMiddleware):
    """给每个响应补上 CSP 等安全头（含被 _LocalHostGuard 拒绝的响应）。"""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response


def create_app(context: Optional[AppContext] = None) -> FastAPI:
    ctx = context or AppContext()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ctx.ensure_seed()
        ctx.updater.start()
        try:
            yield
        finally:
            ctx.updater.stop()
            ctx.kb.close()

    app = FastAPI(
        title=APP_TITLE,
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # 与 docs/redoc 一起关掉：否则 /openapi.json 是无鉴权接口清单
        lifespan=lifespan,
    )
    # add_middleware 是「后加的在外层」：这样 _SecurityHeaders 包在 _LocalHostGuard
    # 外面，连被 Host 守卫拒绝的 403 响应也带上安全头。
    app.add_middleware(_LocalHostGuard)
    app.add_middleware(_SecurityHeaders)

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    def _check(request: Request) -> None:
        if AUTH_DISABLED:
            return
        supplied = request.headers.get(TOKEN_HEADER) or request.cookies.get(TOKEN_COOKIE)
        if not supplied:
            raise HTTPException(status_code=403, detail="本地会话令牌无效，请刷新页面重试")
        # compare_digest 只接受 ASCII：非 ASCII 入参会抛 TypeError → 变成 500。
        # 这里统一转成 bytes 并在异常时按「不匹配」处理，保证永远返回 403。
        try:
            matched = pysecrets.compare_digest(
                supplied.encode("utf-8", "ignore"), ctx.token.encode("utf-8", "ignore")
            )
        except TypeError:
            matched = False
        if not matched:
            raise HTTPException(status_code=403, detail="本地会话令牌无效，请刷新页面重试")

    # ------------------------------------------------------------------
    # 静态页面
    # ------------------------------------------------------------------

    @app.get("/")
    def index() -> Response:
        page = paths.web_root() / "index.html"
        if not page.exists():
            # 不回传磁盘绝对路径：这是无鉴权接口，路径属于不该外泄的本机信息
            return JSONResponse({"error": "前端资源缺失"}, status_code=500)
        response = FileResponse(page, media_type="text/html; charset=utf-8")
        response.set_cookie(
            TOKEN_COOKIE,
            ctx.token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/app.js")
    def app_js() -> Response:
        return _static("app.js", "application/javascript; charset=utf-8")

    @app.get("/style.css")
    def style_css() -> Response:
        return _static("style.css", "text/css; charset=utf-8")

    def _static(name: str, media_type: str) -> Response:
        target = paths.web_root() / name
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"缺少静态资源 {name}")
        return FileResponse(target, media_type=media_type)

    # ------------------------------------------------------------------
    # 界面壁纸（用户自备图片，仅在本机渲染）
    # ------------------------------------------------------------------
    # 分发包内不包含任何美术素材；这里只负责把用户自己导入的图片
    # 存在数据目录并回传。接口不接受任意路径，只认数据目录下 wallpaper.<ext>。

    def _wallpaper_target() -> Optional[Path]:
        name = str(ctx.config.get("ui", "wallpaper_file", "") or "").strip()
        if not name:
            return None
        root = paths.data_dir()
        target = root / Path(name).name
        if not target.exists() or target.suffix.lower() not in WALLPAPER_SUFFIX:
            return None
        return target

    def _drop_wallpapers(keep: str = "") -> None:
        for suffix in WALLPAPER_SUFFIX:
            if suffix == keep:
                continue
            try:
                (paths.data_dir() / f"wallpaper{suffix}").unlink()
            except Exception:
                pass

    @app.get("/api/ui/wallpaper")
    def get_wallpaper(request: Request) -> Response:
        """回传用户自备壁纸；未设置时 404，前端据此保持纯 CSS 背景。"""
        _check(request)
        target = _wallpaper_target()
        if target is None:
            raise HTTPException(status_code=404, detail="未设置壁纸")
        response = FileResponse(target, media_type=WALLPAPER_SUFFIX[target.suffix.lower()])
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/ui/wallpaper")
    async def upload_wallpaper(request: Request) -> Dict[str, Any]:
        """接收原始图片字节（不引入 multipart 依赖），写入数据目录。"""
        _check(request)
        media = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        suffix = WALLPAPER_TYPES.get(media)
        if not suffix:
            raise HTTPException(status_code=400, detail="只支持 JPG / PNG / WebP / GIF / BMP 格式的图片")
        # 边收边数：过去是先把整个 body 一次性读进内存，再判断大小——
        # 一个 2 GB 的上传会把进程内存先吃掉，限制形同虚设。
        buffer = bytearray()
        async for chunk in request.stream():
            buffer.extend(chunk)
            if len(buffer) > WALLPAPER_MAX_BYTES:
                raise HTTPException(status_code=400, detail="图片太大（上限 12 MB），请先压缩后再试")
        data = bytes(buffer)
        if not data:
            raise HTTPException(status_code=400, detail="没有收到图片数据")
        _drop_wallpapers(keep=suffix)
        name = f"wallpaper{suffix}"
        target = paths.data_dir() / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        ctx.config.update_section("ui", {"wallpaper_file": name, "wallpaper_enabled": True})
        return {"ok": True, "file": name, "bytes": len(data), "config": ctx.config.masked_summary()}

    @app.delete("/api/ui/wallpaper")
    def clear_wallpaper(request: Request) -> Dict[str, Any]:
        _check(request)
        _drop_wallpapers()
        ctx.config.update_section("ui", {"wallpaper_file": "", "wallpaper_enabled": False})
        return {"ok": True, "config": ctx.config.masked_summary()}

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        """只回答「活着吗」。

        这个口子**不鉴权**（启动等待与自检都要用它，那时还没有 cookie），
        本机任何进程、任何网页都能通过 127.0.0.1 读到它，所以不该顺带泄露
        程序版本与知识库后端形态。版本与 FTS 状态由已鉴权的 /api/state 提供
        （前端读的也正是那里）。
        """
        return {"ok": True}

    # ------------------------------------------------------------------
    # 总览
    # ------------------------------------------------------------------

    @app.get("/api/state")
    def state(request: Request) -> Dict[str, Any]:
        _check(request)
        ctx.ensure_seed()
        return ctx.state()

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    @app.get("/api/config")
    def get_config(request: Request) -> Dict[str, Any]:
        _check(request)
        return ctx.config.masked_summary()

    @app.post("/api/config")
    def save_config(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        _check(request)
        _apply_config(ctx.config, payload)
        ctx.config.save()
        return {"ok": True, "config": ctx.config.masked_summary()}

    # 「测试连接」「拉取可用模型」共用 _probe_config()：一次性副本 + 地址采用规则
    # 都写在那里；自检要直接调用它，所以放在模块级，不关在这个函数里。

    @app.post("/api/config/test-llm")
    def test_llm(request: Request, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        _check(request)
        probe = _probe_config(ctx.config, "llm", payload) if payload else ctx.config
        return _scrub_any(build_llm(probe).test_connection())

    @app.get("/api/providers")
    def list_providers(request: Request) -> Dict[str, Any]:
        """服务商预设：界面用它填充「服务商」下拉框。

        有了它，用户只需要「选服务商 + 粘贴 Key」，
        Base URL 不会再因为留空而错误地回落到 OpenAI 官方地址。
        """
        _check(request)
        return {"providers": providers.PROVIDERS}

    @app.post("/api/config/models")
    def list_models(request: Request, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        """向服务商拉取当前可用模型列表，避免界面里写死的模型名过时。"""
        _check(request)
        client = build_llm(_probe_config(ctx.config, "llm", payload) if payload else ctx.config)
        try:
            # 列表查询用短超时：默认 120 秒会让界面在服务商无响应时长时间卡住
            names = client.list_models(timeout=20)
        except LLMError as error:
            return {
                "ok": False,
                "endpoint": client._models_url(),
                "error": secrets.scrub(str(error)),
                "models": [],
            }
        return _scrub_any({"ok": bool(names), "endpoint": client._models_url(), "models": names})

    @app.post("/api/config/test-embedding")
    def test_embedding(request: Request, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        _check(request)
        embedder = build_embedder(_probe_config(ctx.config, "embedding", payload) if payload else ctx.config)
        if embedder is None:
            return {"ok": False, "error": "向量模型未启用"}
        return _scrub_any(embedder.test_connection())

    @app.post("/api/config/test-search")
    def test_search(request: Request, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        _check(request)
        return _scrub_any(build_search(_probe_config(ctx.config, "search", payload) if payload else ctx.config).test())

    # ------------------------------------------------------------------
    # 问答
    # ------------------------------------------------------------------

    @app.post("/api/chat")
    def chat(request: Request, body: ChatRequest) -> Dict[str, Any]:
        _check(request)
        try:
            result = ctx.rag.answer(
                body.question,
                history=body.history,
                allow_web=body.allow_web,
                top_k=body.top_k,
            )
        except LLMError as error:
            raise HTTPException(status_code=400, detail=secrets.scrub(str(error))) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=secrets.scrub(str(error))) from error
        _remember_gap(ctx, body.question, result.retrieval)
        return result.to_dict()

    @app.post("/api/chat/stream")
    def chat_stream(request: Request, body: ChatRequest) -> StreamingResponse:
        _check(request)

        def generate():
            try:
                for event in ctx.rag.stream_answer(
                    body.question,
                    history=body.history,
                    allow_web=body.allow_web,
                    top_k=body.top_k,
                ):
                    if event.get("type") == "done":
                        _remember_gap(ctx, body.question, event.get("retrieval") or {})
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as error:  # noqa: BLE001
                payload = {"type": "error", "message": secrets.scrub(str(error))}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield "data: {\"type\": \"end\"}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------
    # 知识库
    # ------------------------------------------------------------------

    @app.get("/api/kb/stats")
    def kb_stats(request: Request) -> Dict[str, Any]:
        _check(request)
        return ctx.kb.stats()

    @app.get("/api/kb/facts")
    def kb_facts(
        request: Request,
        keyword: str = "",
        status: str = "",
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> Dict[str, Any]:
        _check(request)
        return {"items": ctx.kb.list_facts(limit=limit, offset=offset, keyword=keyword, status=status)}

    @app.post("/api/kb/facts")
    def kb_add_fact(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        _check(request)
        title = str(payload.get("title") or "").strip()
        answer = str(payload.get("answer") or "").strip()
        if not title or not answer:
            raise HTTPException(status_code=400, detail="标题与内容都不能为空")
        if len(answer) > KB_ANSWER_MAX_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"内容过长（上限 {KB_ANSWER_MAX_CHARS} 字），请拆分后再添加",
            )
        fact_id = ctx.kb.add_fact(
            title=title,
            answer=answer,
            topic=str(payload.get("topic") or "手动添加"),
            tags=str(payload.get("tags") or ""),
            source_url=str(payload.get("source_url") or ""),
            source_type="manual",
            # 用户手写的条目按「manual 提取方式」给最高档可信度（0.95），
            # 不再沿用界面里的 confidence 字段（该字段已从表单移除）。
            confidence=trust_mod.compute_trust(
                source_type="manual",
                extraction="manual",
                published_at="",
                title=title,
                answer=answer,
            ),
            extraction="manual",
        )
        return {"ok": True, "id": fact_id}

    @app.put("/api/kb/facts/{fact_id}")
    def kb_update_fact(request: Request, fact_id: int, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        _check(request)
        ctx.kb.update_fact(fact_id, **payload)
        return {"ok": True}

    @app.delete("/api/kb/facts/{fact_id}")
    def kb_delete_fact(request: Request, fact_id: int) -> Dict[str, Any]:
        _check(request)
        ctx.kb.delete_fact(fact_id)
        return {"ok": True}

    @app.get("/api/kb/documents")
    def kb_documents(
        request: Request,
        keyword: str = "",
        source_type: str = "",
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> Dict[str, Any]:
        _check(request)
        return {
            "items": ctx.kb.list_documents(
                limit=limit, offset=offset, keyword=keyword, source_type=source_type
            )
        }

    @app.delete("/api/kb/documents/{doc_id}")
    def kb_delete_document(request: Request, doc_id: int) -> Dict[str, Any]:
        _check(request)
        ctx.kb.delete_document(doc_id)
        return {"ok": True}

    @app.get("/api/kb/search")
    def kb_search(request: Request, q: str = Query(..., min_length=1), limit: int = 10) -> Dict[str, Any]:
        _check(request)
        return {
            "facts": ctx.kb.search_facts(q, limit=limit),
            "chunks": ctx.kb.search_chunks(q, limit=limit),
        }

    @app.get("/api/kb/starter-asks")
    def kb_starter_asks(request: Request, count: int = Query(3, ge=1, le=8)) -> Dict[str, Any]:
        """开场推荐问题：每次调用都从知识库里随机抽，避免写死后过期。"""
        _check(request)
        return {"items": build_starter_asks(ctx.kb, count=count)}

    @app.get("/api/kb/export")
    def kb_export(request: Request) -> Response:
        _check(request)
        payload = _export_payload(ctx)
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="nte-rag_kb_export.json"'},
        )

    # ------------------------------------------------------------------
    # 数据源与更新
    # ------------------------------------------------------------------

    @app.get("/api/sources")
    def list_sources(request: Request) -> Dict[str, Any]:
        """只返回内置数据源。

        这里曾经有一个 `custom` 字段，把用户自己填的抓取地址拼成数据源返回——
        但抓取链路从头到尾没有消费过它（`ingest_sources` 只认内置 source_id），
        等于「能添加、能看到、永远不生效」。2026-09-23 移除：
        内置源目录是经过可爬性验证与来源分级的白名单，不受该流程约束的抓取入口
        会破坏 trust/consistency 的既有结论。用户补资料走两条既有路径：
        主题队列（联网搜索）与「手动添加」条目。
        """
        _check(request)
        catalog = get_sources(include_disabled=True)
        return {"builtin": catalog}

    @app.post("/api/update/run")
    def run_update(request: Request, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        _check(request)
        topics = payload.get("topics")
        if isinstance(topics, str):
            topics = [topics]
        result = ctx.updater.trigger(
            topics=topics,
            trigger=str(payload.get("trigger") or "manual"),
            max_pages=payload.get("max_pages"),
            max_topics=payload.get("max_topics"),
        )
        return result

    @app.get("/api/update/status")
    def update_status(request: Request) -> Dict[str, Any]:
        _check(request)
        return ctx.updater.status()

    @app.get("/api/update/logs")
    def update_logs(request: Request, limit: int = 30) -> Dict[str, Any]:
        _check(request)
        return {"items": ctx.kb.list_update_logs(limit=limit)}

    @app.post("/api/update/sources")
    def run_source_ingest(request: Request, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        """抓取内置数据源（种子/全量刷新），同步执行并返回报告。"""
        _check(request)
        source_ids = payload.get("source_ids") or None
        per_source = payload.get("per_source_limit")
        fetcher = build_fetcher(ctx.config)
        llm = build_llm(ctx.config) if ctx.rag.llm_ready() else None
        report = ingest_sources(
            ctx.kb,
            ctx.config,
            fetcher,
            llm=llm,
            source_ids=source_ids,
            per_source_limit=per_source,
        )
        return report

    # ------------------------------------------------------------------
    # 主题队列
    # ------------------------------------------------------------------

    @app.get("/api/topics")
    def list_topics(request: Request) -> Dict[str, Any]:
        _check(request)
        return {
            "configured": ctx.config.get("auto_update", "topics") or [],
            "queue": ctx.kb.list_topics(),
        }

    @app.post("/api/topics")
    def add_topic(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        _check(request)
        topic = str(payload.get("topic") or "").strip()
        if not topic:
            raise HTTPException(status_code=400, detail="主题不能为空")
        current = list(ctx.config.get("auto_update", "topics") or [])
        if topic not in current:
            current.append(topic)
        ctx.config.set("auto_update", "topics", current)
        return {"ok": True, "configured": current}

    @app.delete("/api/topics")
    def remove_topic(request: Request, topic: str = Query(...)) -> Dict[str, Any]:
        _check(request)
        current = [t for t in (ctx.config.get("auto_update", "topics") or []) if t != topic]
        ctx.config.set("auto_update", "topics", current)
        ctx.kb.remove_topic(topic)
        return {"ok": True, "configured": current}

    # ------------------------------------------------------------------

    return app


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------


_SECRET_FIELDS = {
    "llm": {"api_key": "key_enc"},
    "embedding": {"api_key": "key_enc"},
    "search": {
        "bocha_key": "bocha_key_enc",
        "tavily_key": "tavily_key_enc",
        "serper_key": "serper_key_enc",
    },
}

# POST /api/config 是设置的唯一写入口，但改前它把请求体原样交给 update_section：
# 界面上那些 min/max 只是 HTML 属性，一条 curl 就能写进 top_k=999999 或
# temperature="abc"，而下游（store/rag/ingest）全都直接信这些数字——top_k 会变成
# 一次召回十万条，chunk_size=0 会让切片循环退化成死循环。这里按默认值的类型
# 收敛，并对已知数值键夹紧到界面声明的区间。
_NUMERIC_LIMITS: Dict[str, Dict[str, Any]] = {
    "llm": {"temperature": (0.0, 2.0), "max_tokens": (256, 32000), "timeout": (10, 600)},
    "embedding": {"batch_size": (1, 128)},
    "search": {"max_results": (3, 20), "timeout": (5, 120)},
    "answer": {
        "web_trigger_score": (0.0, 1.0),
        "max_web_pages": (1, 10),
        "min_web_pages": (1, 10),
    },
    "kb": {
        "top_k": (3, 30),
        "chunk_size": (200, 2000),
        "chunk_overlap": (0, 500),
        "min_relevance": (0.0, 1.0),
    },
    "auto_update": {
        "interval_hours": (1, 720),
        "max_pages_per_run": (1, 200),
        "max_topics_per_run": (1, 30),
    },
    "quality": {"min_page_chars": (50, 2000), "min_info_density": (0.0, 1.0)},
    "fetch": {"cooldown_seconds": (30, 86400), "wiki_min_interval": (1, 600)},
    "ui": {"port": (0, 65535), "wallpaper_dim": (0.0, 0.9)},
}
_MAX_STR = 4000
_MAX_LIST = 200


def _as_number(value: Any) -> Optional[float]:
    """把数字或纯数字字符串转成 float；布尔、NaN、inf 一律不算数字。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _coerce(value: Any, default: Any) -> Any:
    """把界面传来的值收敛成与默认值同类型；类型不合返回 None（调用方丢弃）。"""
    if isinstance(default, bool):
        return bool(value) if isinstance(value, (bool, int)) else None
    if isinstance(default, int):
        number = _as_number(value)
        return None if number is None else int(number)
    if isinstance(default, float):
        number = _as_number(value)
        return None if number is None else float(number)
    if isinstance(default, str):
        return value if isinstance(value, str) and len(value) <= _MAX_STR else None
    if isinstance(default, list):
        if not isinstance(value, list):
            return None
        return [str(item)[:200] for item in value[:_MAX_LIST] if isinstance(item, (str, int, float))]
    if isinstance(default, dict):
        return value if isinstance(value, dict) and len(value) <= 64 else None
    return None


def _sanitize_section(section: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """只放进已知字段、类型正确的值；未知字段与坏值记一条日志后丢弃。"""
    defaults = DEFAULT_CONFIG.get(section) or {}
    limits = _NUMERIC_LIMITS.get(section) or {}
    clean: Dict[str, Any] = {}
    for key, value in values.items():
        if key in (_SECRET_FIELDS.get(section) or {}):
            # 密钥字段不在 DEFAULT_CONFIG 里，由调用方单独走 set_secret；这里只透传
            clean[key] = value
            continue
        if key not in defaults:
            _LOGGER.warning("[api] 配置 %s.%s 不是已知字段，已忽略", section, key)
            continue
        coerced = _coerce(value, defaults[key])
        if coerced is None:
            _LOGGER.warning("[api] 配置 %s.%s 的值类型不合法，已忽略", section, key)
            continue
        if key in limits:
            low, high = limits[key]
            coerced = min(max(coerced, low), high)
        clean[key] = coerced
    return clean


def _host_of(url: str) -> str:
    """取出 base_url 的主机名（含端口），用于「换主机就别复用旧密钥」的判断。"""
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return (urlsplit(text).netloc or "").lower()


def _effective_host(config: Config, payload: Dict[str, Any]) -> str:
    """算出本次请求实际会访问的主机：优先用本次提交的值，否则用已存配置。

    服务商预设也参与解析——只改 provider 不改 base_url，同样会把请求送到别的主机。
    """
    llm = payload.get("llm") if isinstance(payload.get("llm"), dict) else {}
    section = config.section("llm")
    provider = str(llm.get("provider") or section.get("provider") or "")
    explicit = str(llm.get("base_url") if "base_url" in llm else section.get("base_url") or "").strip()
    if explicit:
        return _host_of(explicit)
    return _host_of(providers.default_base_url(provider))


def _apply_config(
    config: Config,
    payload: Dict[str, Any],
    persist: bool = True,
    allow_reuse_key: bool = False,
) -> None:
    """把前端提交的设置写进配置；密钥字段单独处理（掩码值视为不变）。

    allow_reuse_key=False 时，如果本次请求把 LLM 请求地址换到了别的主机，又没
    同时提交新的密钥，就清掉已存的密钥。否则「测试连接」这类接口会拿着用户的
    真实密钥去请求一个只由请求方指定的地址，等于一个密钥外泄原语
    （persist=False 让配置文件都不留痕，事后无从追查）。
    """
    if not allow_reuse_key:
        submitted = payload.get("llm") if isinstance(payload.get("llm"), dict) else {}
        has_new_key = bool(str(submitted.get("api_key") or "").strip())
        if not has_new_key and config.has_secret("key_enc") and _effective_host(config, payload) != _host_of(
            str(config.section("llm").get("base_url") or "")
        ):
            raise HTTPException(
                status_code=400,
                detail="更换模型服务地址后需要重新填写 API Key（避免把已保存的密钥发送到新的地址）",
            )

    for section, values in (payload or {}).items():
        if not isinstance(values, dict):
            continue
        if section not in _SECRET_FIELDS:
            if section in (
                "kb",
                "auto_update",
                "answer",
                "quality",
                "fetch",
                "ui",
                "privacy",
                "llm",
                "search",
                "embedding",
            ):
                config.update_section(section, _sanitize_section(section, values), autosave=False)
            continue
        clean = {
            k: v
            for k, v in _sanitize_section(section, values).items()
            if k not in _SECRET_FIELDS[section]
        }
        config.update_section(section, clean, autosave=False)
        for field, enc_field in _SECRET_FIELDS[section].items():
            if field in values:
                # autosave=False：persist 只由本函数的最后一步决定，
                # 否则 persist=False 会因为 set_secret 内部无条件 save() 而失效
                config.set_secret(
                    enc_field, str(values[field] or ""), section=section, autosave=False
                )
    if persist:
        config.save()


def _section_key(config: Config, section: str) -> str:
    """取某个 section 当前生效的密钥明文（只在本次请求内用于比对，不落盘、不外传）。"""
    for enc_field in (_SECRET_FIELDS.get(section) or {}).values():
        value = config.get_secret(enc_field, section)
        if value:
            return value
    return ""


def _probe_config(config: Config, section: str, payload: Dict[str, Any]) -> Config:
    """给「测试连接 / 拉取可用模型」用的**一次性配置副本**，活配置一个字节都不改。

    改前是直接在活配置上 `_apply_config(ctx.config, ..., persist=False)`：base_url
    被剥掉、其余字段却真的写进了活配置内存，于是探针拿新协议打旧地址（结果误导），
    而且内存里的 provider/model 已经被改过、磁盘却没动，用户不点「保存」就一直是
    错位状态。副本的路径指向一个不存在的文件，调用方一律 persist=False。

    地址（base_url）的采用规则，改前是「一律丢弃请求体里的地址」：于是设置页填好
    地址与 Key、没点保存就点「拉取可用模型」，请求实际打的是上一次保存的地址，
    用户看到的是上游莫名其妙的报错；而首启向导每一步都先保存，所以同一件事在向导
    里能用、在设置页不能用。现在的规则：
    - 请求**带了新密钥**（不是本程序生成的掩码串）时，采用它带来的地址。密钥是请求
      方自己给的，不存在「拿用户已存的真实密钥去请求外部地址」这条外泄路径。
    - 没有新密钥、却要换到别的主机时（含「只换服务商」——预设表的地址一样是别的主机）
      直接拒绝并说明原因，而不是悄悄用旧地址或把旧密钥送到新主机。这与保存路径
      （_apply_config）的规则一致。
    - 其余情况（没有已存密钥可泄、或主机没变）用本次请求的地址，没有就用服务端的
      预设表（这部分数据不来自请求方），最后才是已存地址。

    请求体两种形态都认：四个探针接口各自只服务一个 section，界面发的就是该 section
    自己的字段（`llmPayload()` 直接是 `{preset, base_url, api_key…}`），而保存接口
    发的是套了一层的 `{llm: {…}}`。这里两种都接受，免得探针整份请求体都读不到、
    变成「拿旧配置探测」还报出旧地址的错。
    """
    values = payload.get(section) if isinstance(payload, dict) else None
    if not isinstance(values, dict):
        values = payload if isinstance(payload, dict) else None
    submitted = dict(values) if isinstance(values, dict) else {}
    submitted_url = str(submitted.get("base_url") or "").strip()
    submitted.pop("base_url", None)      # 地址不跟着其余字段一起进第一个副本
    preset = str(submitted.get("preset") or "")
    preset_url = providers.default_base_url(preset) if preset else ""

    probe = config.detached_copy()
    if submitted:
        _apply_config(probe, {section: submitted}, persist=False)

    stored_key = _section_key(config, section)
    saved_url = str(config.section(section).get("base_url") or "").strip()
    submitted_key = ""
    for field in (_SECRET_FIELDS.get(section) or {}):
        if str(submitted.get(field) or "").strip():
            submitted_key = str(submitted[field]).strip()
            break
    brings_key = bool(submitted_key) and not secrets.is_mask_value(submitted_key, stored_key)

    wanted_url = submitted_url or preset_url
    if wanted_url and stored_key and not brings_key and _host_of(wanted_url) != _host_of(saved_url):
        raise HTTPException(
            status_code=400,
            detail="更换模型服务地址后需要重新填写 API Key（避免把已保存的密钥发送到新的地址）",
        )
    if wanted_url:
        probe.update_section(section, {"base_url": wanted_url}, autosave=False)
    return probe


def _scrub_any(value: Any) -> Any:
    """递归清洗任意结构里的字符串。

    「测试连接」类接口会把上游服务商返回的内容原样回传：有的服务商在 401 里
    会回显收到的 Authorization 头，搜索结果里也可能带上带 Key 的 URL。
    这里做最后一层兜底，确保响应体里不会出现密钥。
    """
    if isinstance(value, str):
        return secrets.scrub(value)
    if isinstance(value, dict):
        return {k: _scrub_any(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_any(v) for v in value]
    return value


def _remember_gap(ctx: AppContext, question: str, retrieval: Dict[str, Any]) -> None:
    """把「本地答不上来」的问题自动排进主题队列，供后续自动更新补齐。"""
    try:
        if not retrieval:
            return
        best = float(retrieval.get("best_score") or 0)
        if best < 0.2 and len(question.strip()) >= 4:
            ctx.kb.enqueue_topic(question.strip()[:60], origin="chat", priority=3)
    except Exception:
        pass


def _export_payload(ctx: AppContext) -> Dict[str, Any]:
    facts = ctx.kb.list_facts(limit=5000)
    documents = ctx.kb.list_documents(limit=5000)
    return {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "version": __version__,
        "stats": ctx.kb.stats(),
        "facts": [
            {
                "title": f.get("title"),
                "answer": f.get("answer"),
                "topic": f.get("topic"),
                "tags": f.get("tags"),
                "source_url": f.get("source_url"),
                "source_type": f.get("source_type"),
                "confidence": f.get("confidence"),
                "status": f.get("status"),
            }
            for f in facts
        ],
        "documents": [
            {
                "url": d.get("url"),
                "title": d.get("title"),
                "source_type": d.get("source_type"),
                "site": d.get("site"),
                "fetched_at": d.get("fetched_at"),
                "updated_at": d.get("updated_at"),
            }
            for d in documents
        ],
    }
