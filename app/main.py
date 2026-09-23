"""程序入口：起本地服务 + 打开桌面窗口（缺失 WebView2 时自动降级到浏览器）。

用法
----
    NTE-RAG.exe                 正常使用：桌面窗口
    NTE-RAG.exe --no-window     只起服务并打开浏览器（调试用）
    NTE-RAG.exe --headless      只起服务，不打开任何界面（自动化测试用）
    NTE-RAG.exe --selftest      自检：启动服务→请求 /api/health 与 /api/state→退出
    NTE-RAG.exe --port 8765     指定端口（默认自动挑空闲端口）

日志写在数据目录的 logs/app.log，所有日志都会做密钥脱敏。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from . import APP_TITLE, __version__
from .core import env as env_mod
from .core import paths, secrets

LOGGER = logging.getLogger(env_mod.LOGGER_NAME)

# 「完整密钥」的形态：前缀 + 足够长的连续密钥字符。
# 用于自检判断配置回显里有没有真的把密钥漏出去（掩码串中的 * 不会命中）。
SECRET_SHAPE = re.compile(
    r"(?:sk-ant-[A-Za-z0-9_\-]{16,}|sk-[A-Za-z0-9_\-]{24,}|AIza[A-Za-z0-9_\-]{33,}|tvly-[A-Za-z0-9_\-]{16,})"
)


# ----------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------


class _ScrubFormatter(logging.Formatter):
    """在「格式化完成之后」对整行日志做密钥脱敏。

    不能在 Filter 里改写 record.msg / record.args：
    那会把 %d 之类的数值参数变成字符串，直接导致 logging 报
    `TypeError: %d format: a real number is required, not str`。
    """

    def format(self, record: logging.LogRecord) -> str:
        return secrets.scrub(super().format(record))


# 这些库默认会打大量 INFO（httpx 每发一个请求一条），
# 既淹没真正有用的信息，也会让日志文件迅速膨胀
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "charset_normalizer",
    "trafilatura",
    "courlan",
    "htmldate",
    "dateparser",
    "primp",
    "ddgs",
    "asyncio",
    "multipart",
)


def setup_logging(verbose: bool = False) -> None:
    """配置文件日志。

    处理器统一挂在 root 上：uvicorn 等第三方库用自己的 logger 名，
    只有挂到 root 才能把它们的输出（尤其是错误）收进同一个日志文件，
    否则窗口模式下出问题就是「什么都看不到」。
    """
    level = logging.DEBUG if verbose else logging.INFO
    formatter = _ScrubFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handlers: list = []

    try:
        file_handler = RotatingFileHandler(
            paths.log_dir() / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except Exception:
        pass

    # 窗口程序没有 stderr（sys.stderr 为 None），此时不要建流处理器
    if sys.stderr is not None:
        try:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(formatter)
            handlers.append(stream)
        except Exception:
            pass

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    for handler in handlers:
        root.addHandler(handler)

    # 只挂 root，让本项目的日志向上传播一次即可，避免重复输出
    LOGGER.handlers.clear()
    LOGGER.setLevel(level)
    LOGGER.propagate = True

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def fatal(message: str, error: Optional[BaseException] = None) -> None:
    """致命错误：写完整堆栈到日志 + 弹系统对话框（有界面时）并告知日志位置。"""
    LOGGER.error(message, exc_info=error)
    log_path = paths.log_dir() / "app.log"
    try:
        import ctypes

        body = f"{message}\n\n详细堆栈已写入日志：\n{log_path}"
        ctypes.windll.user32.MessageBoxW(None, body[:1800], f"{APP_TITLE} 启动失败", 0x10)
    except Exception:
        pass


def _say(message: str) -> None:
    """安全输出。

    窗口程序（console=False）由双击启动时，`sys.stdout` 是 None，
    此时直接 print 会抛 AttributeError 并把进程带崩。
    这类问题只在「双击」时出现，用命令行参数启动（会 attach 控制台）永远复现不了，
    因此统一走这个函数，并且绝不能让它抛异常。
    """
    stream = sys.stdout
    if stream is None:
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            LOGGER.info(message)
            return
    try:
        # 这里是唯一允许调用内建 print 的地方
        print(message, file=stream, flush=True)
    except Exception:
        try:
            LOGGER.info(message)
        except Exception:
            pass


def attach_console() -> None:
    """打包为窗口程序后，CLI 子命令（--selftest/--headless）仍需要能看到输出。

    Windows 下窗口程序没有控制台，这里尝试附着到调用方的控制台并重接标准流；
    失败也不影响功能，因为自检报告同时会写入数据目录的 JSON 文件。
    """
    if not paths.is_frozen():
        return
    try:
        import ctypes

        if not ctypes.windll.kernel32.AttachConsole(-1):
            ctypes.windll.kernel32.AllocConsole()
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1, errors="replace")
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1, errors="replace")
    except Exception:
        pass


# ----------------------------------------------------------------------
# 端口
# ----------------------------------------------------------------------


def find_free_port(preferred: int = 0) -> int:
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ----------------------------------------------------------------------
# 服务
# ----------------------------------------------------------------------


def start_server(port: int):
    """在后台线程里启动 uvicorn，返回 (server, thread, app_context)。

    关于 log_config=None
    -------------------
    uvicorn 默认会用 `logging.config.dictConfig` 安装一套彩色控制台日志，
    其中引用了 `uvicorn.logging.DefaultFormatter`。在冻结（打包）环境里这一步
    曾导致启动直接失败：

        Unable to configure formatter 'default'

    对窗口程序来说那套控制台格式毫无意义，因此这里显式关闭它，
    统一用 setup_logging() 配置的日志（已挂到 root，uvicorn 的输出也会进日志文件）。
    """
    import uvicorn

    from .server.api import AppContext, create_app

    ctx = AppContext()
    app = create_app(ctx)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        log_config=None,  # 不用 uvicorn 自带的 dictConfig，避免冻结环境下的解析问题
        access_log=False,
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="nte-rag-uvicorn", daemon=True)
    thread.start()
    return server, thread, ctx


def wait_ready(port: int, timeout: float = 20.0) -> bool:
    """轮询健康检查，确认服务真的起来了（而不是端口占用/启动异常）。

    每条失败路径都要 sleep：改前只有「抛异常」才 sleep，服务起来了却一直回非 200
    时会全程忙等，20 秒里把 CPU 打满还不知道卡在哪 —— 所以末次状态码要记下来进日志。
    """
    import httpx

    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    last_status: Any = "（尚未收到响应）"
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=1.5)
            last_status = response.status_code
            if response.status_code == 200:
                return True
        except Exception as error:  # noqa: BLE001
            last_status = f"请求异常（{error}）"
        time.sleep(0.25)
    LOGGER.warning("等待服务就绪超时（%.0f 秒）：%s 最后一次结果 = %s", timeout, url, last_status)
    return False


# ----------------------------------------------------------------------
# 界面
# ----------------------------------------------------------------------


def open_window(url: str, config=None) -> bool:
    """尝试打开原生窗口；失败返回 False 由调用方降级到浏览器。

    先看探测缓存：探测失败时**绝不**导入 webview，避免整个进程被 CLR 异常带走。
    config 透传给探测函数，保证探测结果写回的是同一个 Config 实例。
    """
    if not probe_webview_supported(config):
        LOGGER.info("WebView2 不可用，直接使用浏览器打开")
        return False
    try:
        import webview  # type: ignore
    except Exception as error:  # pragma: no cover
        LOGGER.warning("pywebview 不可用，将改用浏览器：%s", error)
        return False
    try:
        webview.create_window(
            f"{APP_TITLE} v{__version__}",
            url,
            width=1280,
            height=880,
            min_size=(980, 640),
            text_select=True,
        )
        webview.start()
        return True
    except Exception as error:  # pragma: no cover
        LOGGER.warning("创建窗口失败，将改用浏览器：%s", error)
        return False


BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_browser_exe() -> str:
    """找 Edge / Chrome 的可执行文件（注册表 App Paths 优先，其次固定路径）。

    仅用于「日志里告诉用户该点哪个浏览器」以及诊断展示，不用于自动拉起，
    因为 Chromium 在已有实例运行时会把命令委派给既有进程并立即退出，
    行为不可控（实测会出现「进程已退出但窗口归属不明」的情况）。
    """
    try:
        import winreg  # type: ignore

        for name in ("msedge.exe", "chrome.exe"):
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(
                        root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}"
                    ) as key:
                        path = winreg.QueryValueEx(key, "")[0]
                        if path and Path(path).exists():
                            return path
                except OSError:
                    continue
    except Exception:
        pass
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return ""


def probe_webview_supported(config=None) -> bool:
    """探测 WebView2（pythonnet）是否可用，结果缓存到配置里。

    为什么必须用子进程
    ------------------
    pythonnet 初始化失败时（例如被安全软件/受限令牌拦住 OpenProcess），
    整个进程会以 CLR 异常码直接退出，无法用 try/except 捕获。
    因此只能起一个子进程去试，主进程读结果；探测结果缓存，只付一次代价。

    config 要由调用方传入**当前那个** Config 实例：改前这里自己另建一个
    `Config()`，它和 AppContext 里的实例各有各的锁和内存副本，两边先后
    save() 会互相把对方的字段写回旧值（last-saver-wins），用户改的设置会莫名回退。
    """
    from .config import Config

    if config is None:
        config = Config()
    cached = config.get("ui", "webview_probe", None)
    cached_version = config.get("ui", "webview_probe_version", "")
    if cached is not None and cached_version == __version__:
        return bool(cached)

    import subprocess

    if paths.is_frozen():
        command = [sys.executable, "--probe-webview"]
        cwd = None
    else:
        command = [sys.executable, "-m", "app.main", "--probe-webview"]
        cwd = str(paths.project_root())

    supported = False
    try:
        # DEVNULL 是设备文件而非管道：受限环境禁止管道通信
        completed = subprocess.run(
            command,
            cwd=cwd,
            timeout=45,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        supported = completed.returncode == 0
        LOGGER.info("WebView2 探测结果：%s（返回码 %s）", supported, completed.returncode)
    except Exception as error:  # noqa: BLE001
        LOGGER.warning("WebView2 探测失败，将直接使用浏览器：%s", error)

    try:
        config.set("ui", "webview_probe", supported, autosave=False)
        config.set("ui", "webview_probe_version", __version__)
    except Exception:
        pass
    return supported


def check_webview_backend() -> dict:
    """导入 pywebview 与其 WebView2 后端，报告可用性。

    本函数只应在「专门的探测子进程」或自检里调用；
    主程序路径必须先经过 probe_webview_supported()，
    否则 pythonnet 初始化失败会直接带走整个进程。
    """
    info: dict = {"available": False, "version": "", "backend_imported": False, "error": ""}
    try:
        import webview  # type: ignore

        info["version"] = getattr(webview, "__version__", "")
        info["available"] = True
    except Exception as error:  # pragma: no cover
        info["error"] = secrets.scrub(f"导入 pywebview 失败：{error}")
        return info

    if os.name == "nt":
        try:
            from webview.platforms import edgechromium  # type: ignore  # noqa: F401

            info["backend_imported"] = True
        except Exception as error:  # pragma: no cover
            info["error"] = secrets.scrub(f"导入 WebView2 后端失败：{error}")
    else:
        info["backend_imported"] = True
    return info


def window_smoke_test(port: int, seconds: float = 4.0) -> int:
    """窗口冒烟测试：真正创建原生窗口并自动关闭，验证打包后的开窗链路。

    退出码：0 = 窗口创建成功；2 = 本机 WebView2/pythonnet 不可用（程序会按设计
    降级为浏览器，不算缺陷）；1 = 其它错误。
    """
    server, _thread, _ctx = start_server(port)
    report: dict = {
        "port": port,
        "version": __version__,
        "browser_hint": find_browser_exe(),
    }
    try:
        if not wait_ready(port, timeout=25):
            report["error"] = "服务未就绪"
            _say(json.dumps(report, ensure_ascii=False, indent=2))
            return 1

        supported = probe_webview_supported(_ctx.config)
        report["webview_supported"] = supported
        if not supported:
            report["strategy"] = "browser-fallback"
            report["note"] = "本机 WebView2/pythonnet 不可用，程序会打开默认浏览器（设计内降级）"
            _say(json.dumps(report, ensure_ascii=False, indent=2))
            return 2

        url = f"http://127.0.0.1:{port}/"
        try:
            import webview  # type: ignore

            window = webview.create_window(
                f"{APP_TITLE} v{__version__}", url, width=1100, height=760, text_select=True
            )

            def closer() -> None:
                time.sleep(seconds)
                try:
                    window.destroy()
                except Exception:
                    pass

            webview.start(closer)
            report["strategy"] = "pywebview-WebView2"
            report["window_created"] = True
            report["note"] = f"窗口已创建并在 {seconds} 秒后自动关闭"
            _say(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        except Exception as error:  # pragma: no cover
            report["strategy"] = "pywebview-WebView2"
            report["window_created"] = False
            report["error"] = secrets.scrub(f"创建窗口失败：{error}")
            _say(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
    finally:
        try:
            server.should_exit = True
            time.sleep(0.5)
        except Exception:
            pass


# ----------------------------------------------------------------------
# 自检
# ----------------------------------------------------------------------


def run_selftest(port: int) -> int:
    """不依赖界面地验证：服务能起、鉴权生效、接口能通、知识库可写。"""
    import httpx

    from .server.api import TOKEN_COOKIE, TOKEN_HEADER, AUTH_DISABLED

    server, _thread, ctx = start_server(port)
    report: dict = {"port": port, "version": __version__, "auth_disabled": AUTH_DISABLED}
    checks: list = []
    try:
        if not wait_ready(port, timeout=25):
            report["error"] = "服务未在超时时间内就绪"
        else:
            base = f"http://127.0.0.1:{port}"
            anonymous = httpx.Client(timeout=10)
            authed = httpx.Client(timeout=60, headers={TOKEN_HEADER: ctx.token})

            health = httpx.get(f"{base}/api/health", timeout=5).json()
            anon_status = anonymous.get(f"{base}/api/state", timeout=10).status_code
            state = authed.get(f"{base}/api/state", timeout=30).json()
            index = httpx.get(f"{base}/", timeout=5)
            js = httpx.get(f"{base}/app.js", timeout=5)
            css = httpx.get(f"{base}/style.css", timeout=5)
            config = authed.get(f"{base}/api/config", timeout=10).json()

            cookie_client = httpx.Client(timeout=10)
            cookie_client.get(f"{base}/", timeout=5)
            cookie_status = cookie_client.get(f"{base}/api/state", timeout=10).status_code

            report.update(
                health=health,
                stats=state.get("stats"),
                paths=state.get("paths"),
                index_status=index.status_code,
                app_js_status=js.status_code,
                style_css_status=css.status_code,
                index_sets_cookie=(TOKEN_COOKIE in index.cookies),
                unauth_state_status=anon_status,
                cookie_auth_status=cookie_status,
                # 只匹配「完整密钥」的形态：掩码回显（如 sk-d******6f7d）不算泄漏，
                # 否则用户一旦配置了密钥，自检就会误报。
                secret_leak_in_config=SECRET_SHAPE.search(json.dumps(config, ensure_ascii=False))
                is not None,
            )

            checks.append(("health", bool(health.get("ok"))))
            checks.append(("static_assets", index.status_code == 200 and js.status_code == 200 and css.status_code == 200))
            checks.append(("cookie_issued", TOKEN_COOKIE in index.cookies))
            if AUTH_DISABLED:
                checks.append(("auth_enforced", True))
            else:
                checks.append(("auth_enforced", anon_status == 403 and cookie_status == 200))
            checks.append(("no_secret_in_config", not report["secret_leak_in_config"]))

            # 界面与外观：新版界面的关键结构必须在，壁纸接口要能读写并恢复默认
            checks.append(("ui_markup", all(marker in index.text for marker in (
                'id="wizard"', 'id="wizard-back"', 'class="sprite"', 'id="theme-seg"',
                'id="wallpaper-layer"', 'class="card disclaimer"', 'id="starter-asks"',
                'id="wp-fit"',
                # 关于页不出现面向开发者的内容（打包门禁之类的说明只留在 README 里）
                '只会发给你自己选的服务商', '没有统计与上报',
            ))))
            # 壁纸往返会覆盖并删除用户当前壁纸，所以先原样记下来，末尾必须还回去。
            # 旧实现直接假定「一定没有壁纸」：既有壁纸会被永久删除，且 404 断言必然失败。
            wp_unset = authed.get(f"{base}/api/ui/wallpaper", timeout=15)
            had_wallpaper = wp_unset.status_code == 200
            pre_bytes = wp_unset.content if had_wallpaper else b""
            pre_type = ""
            if had_wallpaper:
                pre_type = (wp_unset.headers.get("content-type") or "image/png").split(";")[0].strip()
            wp_bad = authed.post(
                f"{base}/api/ui/wallpaper",
                content=b"plain text", headers={"Content-Type": "text/plain"}, timeout=15,
            )
            wp_put = authed.post(
                f"{base}/api/ui/wallpaper",
                content=b"\x89PNG\r\n\x1a\n" + b"0" * 64,
                headers={"Content-Type": "image/png"}, timeout=20,
            )
            wp_got = authed.get(f"{base}/api/ui/wallpaper", timeout=15)
            wp_del = authed.delete(f"{base}/api/ui/wallpaper", timeout=15)
            # 还原用户壁纸：媒体类型取自 GET 响应，与 WALLPAPER_SUFFIX 一一对应，后缀不会漂移
            restore_ok = True
            if had_wallpaper and pre_bytes:
                wp_fix = authed.post(
                    f"{base}/api/ui/wallpaper",
                    content=pre_bytes,
                    headers={"Content-Type": pre_type or "image/png"},
                    timeout=20,
                )
                wp_back = authed.get(f"{base}/api/ui/wallpaper", timeout=15)
                restore_ok = (
                    wp_fix.status_code == 200
                    and wp_back.status_code == 200
                    and wp_back.content == pre_bytes
                )
            report["wallpaper"] = {
                "had_wallpaper": had_wallpaper,
                "unset_status": wp_unset.status_code,
                "bad_type_status": wp_bad.status_code,
                "put_status": wp_put.status_code,
                "get_status": wp_got.status_code,
                "get_bytes": len(wp_got.content),
                "delete_status": wp_del.status_code,
                "restored": restore_ok,
            }
            checks.append((
                "wallpaper_roundtrip",
                # 本来就该是 404 时才要求 404；已有壁纸时要求的是「读得到 + 原样还原」
                (wp_unset.status_code == 404 if not had_wallpaper else restore_ok)
                and wp_bad.status_code == 400
                and wp_put.status_code == 200
                and wp_got.status_code == 200
                and wp_got.content.startswith(b"\x89PNG")
                and wp_del.status_code == 200
                and ((wp_del.json().get("config") or {}).get("ui") or {}).get("wallpaper_enabled") is False,
            ))

            # 推荐问题：接口要能随机给出 3 条（写死或报错都会在这里暴露）
            asks = authed.get(f"{base}/api/kb/starter-asks?count=3", timeout=15)
            ask_items = (asks.json().get("items") or []) if asks.status_code == 200 else []
            report["starter_asks"] = {
                "status": asks.status_code,
                "count": len(ask_items),
                "sample": [item.get("label", "") for item in ask_items],
            }
            checks.append(("starter_asks", asks.status_code == 200 and len(ask_items) == 3
                           and all(item.get("ask") for item in ask_items)))

            # 知识库写入 + 检索。先清一遍上一次崩溃可能留下的自检条目：
            # 正常路径末尾会按 id 删掉，但中途崩溃（正是自检要覆盖的场景）就会在
            # 用户知识库里留下一条永不消失的垃圾，且每跑一次多一条。
            try:
                purged = ctx.kb.delete_facts_by_topic("__selftest__")
                if purged:
                    report["selftest_purged"] = purged
            except Exception as error:  # noqa: BLE001
                LOGGER.warning("清理历史自检条目失败（不影响自检本身）：%s", error)
            selftest_fact_id = ctx.kb.add_fact(
                title="自检条目",
                answer="这是一条写入自检数据，用于确认 SQLite 与 FTS5 正常工作。",
                topic="__selftest__",
                tags="自检",
                confidence=0.1,
            )
            hits = ctx.kb.search_facts("自检条目")
            report["kb_write_read_ok"] = any(item.get("topic") == "__selftest__" for item in hits)
            checks.append(("kb_write_read", report["kb_write_read_ok"]))
            report["retrieval_selfcheck"] = ctx.rag.retrieve("自检")
            # 清掉自检数据，避免反复运行污染知识库
            try:
                ctx.kb.delete_fact(selftest_fact_id)
            except Exception:
                pass

            # 未配置模型时，问答应优雅降级而不是报错
            chat = authed.post(f"{base}/api/chat", json={"question": "薄荷是谁", "allow_web": False}, timeout=60)
            chat_body = chat.json() if chat.status_code == 200 else {}
            report["chat_status"] = chat.status_code
            report["chat_degraded"] = bool(chat_body.get("degraded"))
            report["chat_text_preview"] = (chat_body.get("text") or "")[:120]
            report["chat_citations"] = len(chat_body.get("citations") or [])
            checks.append(("chat_endpoint", chat.status_code == 200))

            # 流式接口至少要能建立并正常收尾
            with authed.stream(
                "POST", f"{base}/api/chat/stream",
                json={"question": "薄荷是谁", "allow_web": False}, timeout=60,
            ) as stream:
                events = []
                for line in stream.iter_lines():
                    if line.startswith("data:"):
                        try:
                            events.append(json.loads(line[5:].strip()))
                        except json.JSONDecodeError:
                            pass
                    if len(events) > 300:
                        break
            report["stream_event_types"] = sorted({e.get("type") for e in events})
            checks.append(("chat_stream", any(e.get("type") in ("done", "sources") for e in events)))

            # WebView2 可用性（必须走子进程探测：在主进程导入会让 pythonnet
            # 初始化失败时直接终止整个进程；这里只报告结论）
            report["webview_supported"] = probe_webview_supported(ctx.config)
            report["browser_hint"] = find_browser_exe()
    except Exception as error:  # noqa: BLE001
        report["error"] = secrets.scrub(f"{type(error).__name__}: {error}")
    finally:
        try:
            server.should_exit = True
            time.sleep(0.8)
        except Exception:
            pass

    report["checks"] = {name: passed for name, passed in checks}
    report["failed_checks"] = [name for name, passed in checks if not passed]
    text = json.dumps(report, ensure_ascii=False, indent=2)

    # 窗口模式下看不到控制台，因此同时把报告写到数据目录，便于自动化验证
    try:
        report_path = paths.data_dir() / "selftest_report.json"
        report_path.write_text(text, encoding="utf-8")
        LOGGER.info("自检报告已写入 %s", report_path)
    except Exception:
        pass

    _say(text)
    return 0 if checks and all(passed for _name, passed in checks) else 1


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="NTE-RAG", description=APP_TITLE)
    parser.add_argument("--port", type=int, default=0, help="监听端口，默认自动选择空闲端口")
    parser.add_argument("--headless", action="store_true", help="只起服务，不打开界面")
    parser.add_argument("--no-window", action="store_true", help="用默认浏览器打开而非原生窗口")
    parser.add_argument("--selftest", action="store_true", help="执行启动自检后退出")
    parser.add_argument("--window-test", action="store_true", help="窗口冒烟测试：开窗数秒后自动关闭")
    parser.add_argument("--probe-webview", action="store_true", help="仅在子进程中探测 WebView2 是否可用")
    parser.add_argument(
        "--no-console",
        action="store_true",
        help="诊断用：模拟双击启动的窗口程序（sys.stdout/stderr 置空），用于复现「无控制台」下的问题",
    )
    parser.add_argument("--window-seconds", type=float, default=4.0, help="窗口冒烟测试的停留秒数")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="store_true", help="打印版本号")
    args = parser.parse_args(argv)

    if args.no_console:
        # 关键：必须在 attach_console 之前，精确模拟「双击窗口程序」的标准流状态。
        # PyInstaller 的 windowed 程序里 sys.stdout / sys.stderr 都是 None，
        # uvicorn 的默认日志格式器会对 sys.stdout.isatty() 求值并因此崩溃。
        sys.stdout = None
        sys.stderr = None
    elif args.selftest or args.headless or args.version or args.window_test or args.probe_webview:
        attach_console()

    if args.probe_webview:
        # 该分支可能因 pythonnet 初始化失败而让进程异常退出，这正是探测的意义：
        # 退出码非 0 即代表本机不可用，父进程据此降级。
        info = check_webview_backend()
        _say(json.dumps(info, ensure_ascii=False))
        return 0 if info.get("backend_imported") else 1

    if args.version:
        _say(f"{APP_TITLE} v{__version__}")
        return 0

    setup_logging(args.verbose)
    LOGGER.info("启动 %s v%s（frozen=%s）", APP_TITLE, __version__, paths.is_frozen())
    layout = paths.describe_layout()
    LOGGER.info("数据目录：%s（便携模式=%s）", layout["data_dir"], layout["portable"])

    try:
        port = find_free_port(args.port or 0)
    except Exception as error:  # noqa: BLE001
        fatal(f"无法分配本地端口：{secrets.scrub(str(error))}", error)
        return 2

    if args.selftest:
        return run_selftest(port)

    if args.window_test:
        return window_smoke_test(port, seconds=args.window_seconds)

    try:
        server, _thread, _ctx = start_server(port)
    except Exception as error:  # noqa: BLE001
        fatal(f"本地服务启动失败：{secrets.scrub(str(error))}", error)
        return 3

    if not wait_ready(port):
        fatal("本地服务未能在 20 秒内就绪。")
        return 4

    url = f"http://127.0.0.1:{port}/"
    LOGGER.info("服务就绪：%s", url)

    if args.headless:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        server.should_exit = True
        return 0

    if not args.no_window:
        if open_window(url, _ctx.config):
            LOGGER.info("原生窗口已关闭，正在退出")
            server.should_exit = True
            time.sleep(0.5)
            return 0
        LOGGER.info("未使用原生窗口，改用默认浏览器")

    try:
        webbrowser.open(url)
    except Exception as error:  # noqa: BLE001
        LOGGER.error("无法打开浏览器：%s", error)

    _say(f"服务已启动：{url}\n关闭此窗口即可退出。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
