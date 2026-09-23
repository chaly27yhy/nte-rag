"""给 README 生成界面截图（纯本地，不需要模型、不需要联网）。

用法
----
1) 先起一个本地服务（截图用的数据目录建议单独一个，别污染你自己的）：

   $env:NTE_RAG_DATA_DIR = "<仓库>\\.shots\\demo"
   $env:NTE_RAG_DISABLE_AUTH = "1"
   .venv\\Scripts\\python.exe -m app.main --headless      # 日志里会打印 http://127.0.0.1:<随机端口>/

2) 再跑本脚本：

   .venv\\Scripts\\python.exe tools\\make_readme_shots.py --port 60986 --out docs\\screenshots

原理：用 Edge 的 DevTools Protocol 驱动无头浏览器（`--headless=new`），
逐张切页/切主题后截图。不需要 Playwright / Selenium / websocket-client，
只用标准库手写最小 WebSocket 帧收发（见下面 _WebSocket）。

截图里的知识库内容来自**你指定的数据目录**，与程序发行时的种子库无关。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: F401  （Windows 控制台 GBK，统一走这里）


class _WebSocket:
    """只够用的一小段 WebSocket 客户端（DevTools Protocol 需要）。

    不装 websocket-client：这是个给 README 产截图的一次性开发工具，
    不应该给仓库增加运行/开发依赖；RFC 6455 里只用到「文本帧 + 长度扩展 +
    客户端掩码」，几十行标准库代码就够，也不会被沙箱依赖安装问题卡住。
    """

    def __init__(self, url: str, timeout: float = 60.0) -> None:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buffer = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(handshake.encode("ascii"))
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("DevTools 握手失败：连接被关闭")
            header += chunk
        head, _, rest = header.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise RuntimeError(f"DevTools 握手失败：{status}")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if expected.lower() not in head.decode("latin-1").lower():
            raise RuntimeError("DevTools 握手失败：Sec-WebSocket-Accept 不匹配")
        self._buffer = rest

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        head = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            head.append(0x80 | length)
        elif length < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", length)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", length)
        mask = os.urandom(4)
        head += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def _recv_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(max(65536, count - len(self._buffer)))
            if not chunk:
                raise RuntimeError("DevTools 连接被关闭")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _recv_available(self, timeout: float) -> bytes:
        """读一小段：先看握手/上一帧剩下的字节，否则等一个分段。

        DevTools 是长连接，`recv()` 不会返回空串来表示「这一帧完了」，
        所以这里用 timeout 切段，靠帧头的 FIN 位判断消息是否收完。
        """
        if self._buffer:
            data, self._buffer = self._buffer, b""
            return data
        self.sock.settimeout(timeout)
        try:
            return self.sock.recv(65536)
        except socket.timeout:
            return b""

    def _recv_message(self) -> Tuple[bool, int, bytes]:
        """读一个完整的 WebSocket 帧，返回 (FIN, opcode, payload)。

        FIN 位必须和 opcode 分开返回。早先写成 `opcode = first & 0x0F`
        再用 `opcode & 0x80` 判消息结束，等于永远为假 —— 单帧响应会读不出来
        （本地 echo 测试只读一帧所以照不出来，连真实 DevTools 立刻卡死）。
        """
        while True:
            header = b""
            while len(header) < 2:
                piece = self._recv_available(0.2 if header else 60.0)
                if not piece:
                    if header:
                        raise RuntimeError("DevTools 帧头读到一半就断了")
                    continue
                header += piece
            first, second = header[0], header[1]
            fin = bool(first & 0x80)
            rest = bytearray(header[2:])
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                need = 2
            elif length == 127:
                need = 8
            else:
                need = 0
            if second & 0x80:
                need += 4
            while len(rest) < need:
                piece = self._recv_available(5.0)
                if not piece:
                    raise RuntimeError("DevTools 帧头不完整")
                rest += piece
            offset = 0
            if length == 126:
                length = struct.unpack(">H", bytes(rest[offset:offset + 2]))[0]
                offset += 2
            elif length == 127:
                length = struct.unpack(">Q", bytes(rest[offset:offset + 8]))[0]
                offset += 8
            mask = b""
            if second & 0x80:
                mask = bytes(rest[offset:offset + 4])
                offset += 4
            payload = bytearray(rest[offset:])
            while len(payload) < length:
                piece = self._recv_available(60.0)
                if not piece:
                    raise RuntimeError("DevTools 帧体不完整")
                payload += piece
            payload = bytes(payload[:length])
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x9:            # ping → 回 pong，继续等
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:            # pong → 忽略
                continue
            if opcode == 0x8:            # close
                raise RuntimeError("DevTools 主动关闭了连接")
            return fin, opcode, payload

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        parts: List[bytes] = []
        while True:
            fin, _opcode, payload = self._recv_message()
            parts.append(payload)
            if fin:                      # 分片消息的最后一帧
                return b"".join(parts).decode("utf-8")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


DEFAULT_EDGE = [
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
]


def find_edge(explicit: str = "") -> Path:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path
        raise SystemExit(f"指定的浏览器不存在：{path}")
    for path in DEFAULT_EDGE:
        if path.is_file():
            return path
    raise SystemExit("没找到 Edge。用 --edge 指定 msedge.exe 的完整路径。")


def log(message: str) -> None:
    """带时间戳的进度输出；用 stderr 且强制 flush。

    `--headless` 那种被重定向的管道里 stdout 是块缓冲的，卡住时什么都看不到，
    所以诊断信息一律走 stderr 并立刻 flush。
    """
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _devtools_json(port: int, path: str, retries: int = 60, delay: float = 0.5) -> Any:
    """向 DevTools 的 HTTP 接口要一小段 JSON。

    刻意不用 urllib：它会把 `Connection: close` 之外的长连接语义搞混。
    这里按 `Content-Length` 读够就返回：Chromium 的 /json 接口返回 Content-Length
    之后并不马上关连接，读到 EOF 会一直卡到超时。
    """
    last: Optional[Exception] = None
    for _ in range(retries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(
                    f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode("ascii")
                )
                raw = b""
                while b"\r\n\r\n" not in raw:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
                head, _, body = raw.partition(b"\r\n\r\n")
                if b" 200 " not in head.split(b"\r\n", 1)[0]:
                    raise RuntimeError(head.split(b"\r\n", 1)[0].decode("latin-1"))
                length = 0
                for line in head.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value.strip() or 0)
                while len(body) < length:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    body += chunk
                return json.loads(body.decode("utf-8"))
        except Exception as exc:  # 浏览器还没起来 / 端口还没监听
            last = exc
            time.sleep(delay)
    raise SystemExit(
        f"DevTools 接口不可用：http://127.0.0.1:{port}{path}（{type(last).__name__}: {last}）\n"
        "可以试：删掉 .shots 下的浏览器临时配置目录后重跑。"
    )


def _kill_leftover_browsers(profile: Path) -> int:
    """清掉上次跑截图残留的无头 Edge。

    只 terminate 自己不保证干净：Edge 是「启动器 + 一堆子进程」，孤儿进程会一直占着
    profile 目录，下一次启动的浏览器就会立刻退出，DevTools 端口永远等不到（连接被拒）。
    这里只在「连不上、准备重启」时才动手，并且只杀命令行里带本工具 profile 路径的进程。
    """
    killed = 0
    try:
        listing = subprocess.run(
            ["wmic", "process", "where", "name='msedge.exe'", "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return 0
    marker = str(profile).lower()
    for line in listing.splitlines():
        if marker not in line.lower():
            continue
        cells = [cell.strip() for cell in line.split(",") if cell.strip()]
        if not cells or not cells[-1].isdigit():
            continue
        try:
            subprocess.run(["taskkill", "/PID", cells[-1], "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            killed += 1
        except Exception:
            pass
    return killed


def _connect_targets(port: int, profile: Path) -> Any:
    """连 DevTools；连不上就清残留再给一次机会。"""
    try:
        return _devtools_json(port, "/json/list")
    except SystemExit:
        log("DevTools 连不上，清理残留的无头浏览器后重试一次")
        removed = _kill_leftover_browsers(profile)
        shutil.rmtree(profile, ignore_errors=True)
        log(f"已清理残留进程 {removed} 个，profile 已重置，等待 3 秒")
        time.sleep(3.0)
        return _devtools_json(port, "/json/list", retries=40)


class Browser:
    """一个最小的 DevTools Protocol 客户端：够用来切页、切主题、截图。"""

    def __init__(self, edge: Path, profile: Path, port: int, width: int, height: int) -> None:
        self.edge = edge
        self.profile = profile
        self.port = port
        self.width = width
        self.height = height
        self.proc: Optional[subprocess.Popen] = None
        self.ws = None
        self.msg_id = 0

    def __enter__(self) -> "Browser":
        if self.profile.exists():
            shutil.rmtree(self.profile, ignore_errors=True)
        log(f"启动无头浏览器（port={self.port}, profile={self.profile}）")
        self.proc = subprocess.Popen(
            [
                str(self.edge),
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-sync",
                "--no-service-autorun",
                f"--user-data-dir={self.profile}",
                f"--window-size={self.width},{self.height}",
                f"--remote-debugging-port={self.port}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        targets = _connect_targets(self.port, self.profile)
        page = next((item for item in targets if item.get("type") == "page"), None)
        if not page:
            raise SystemExit("没有可用的页面目标")
        log(f"DevTools 就绪，连接页面目标 {page.get('url')}")
        self.ws = _WebSocket(page["webSocketDebuggerUrl"])
        self.call("Page.enable")
        self.call("Runtime.enable")
        log("CDP 通道就绪（Page/Runtime 已 enable）")
        return self

    def restart(self) -> None:
        """进程还在但 DevTools 不通时，换一个端口重新拉起。"""
        self.__exit__()
        self.port = free_port()
        self.proc = None
        self.ws = None
        self.__enter__()

    def __exit__(self, *_exc: object) -> None:
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass
        if self.proc is not None:
            # Edge 是「启动器 + 一堆子进程」：只 terminate 主进程会留下孤儿进程，
            # 它们继续占用 profile 目录，下一次启动就起不来（表现为 DevTools 连接被拒）。
            # 所以用 taskkill /T 收整棵进程树，再兜底 terminate/kill。
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                )
            except Exception:
                pass
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except Exception:
                    self.proc.kill()

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        assert self.ws is not None
        self.msg_id += 1
        self.ws.send_text(json.dumps({"id": self.msg_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv_text())
            if message.get("id") == self.msg_id:
                if "error" in message:
                    raise RuntimeError(f"{method} 失败：{message['error']}")
                return message.get("result") or {}

    def js(self, expression: str, wait: float = 0.0) -> Any:
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if wait:
            time.sleep(wait)
        return (result.get("result") or {}).get("value")

    def open(self, url: str, settle: float = 3.0) -> None:
        self.call("Page.navigate", {"url": url})
        time.sleep(settle)

    def set_viewport(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.call("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
        })

    def shoot(self, path: Path) -> int:
        data = self.call("Page.captureScreenshot", {"format": "png"})["data"]
        raw = base64.b64decode(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return len(raw)


def shot(browser: Browser, name: str, out: Path, note: str) -> Tuple[str, int]:
    size = browser.shoot(out / name)
    log(f"已截图 {name}（{size / 1024:.1f} KB）— {note}")
    return name, size


def wait_and_report(browser: Browser, seconds: float, note: str) -> None:
    """等页面渲染完，再回读一次 DOM 状态打日志（截图是黑盒，只能靠回读 DOM 判断渲染是否完成）。"""
    time.sleep(seconds)
    state = browser.js(
        "(() => { const box = document.querySelector('#chat-log, #messages, .messages');"
        " const active = Array.from(document.querySelectorAll('.panel'))"
        "   .filter(el => el.offsetParent !== null).map(el => el.id || el.className);"
        " return {title: document.title,"
        " activePanels: active,"
        " messages: box ? box.children.length : -1,"
        " text: (document.body.innerText || '').slice(0, 150).replace(/\\n+/g, ' / ')}; })()"
    )
    log(f"{note} → {state}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="给 README 生成界面截图")
    parser.add_argument("--port", type=int, required=True, help="本地服务端口（app.main 日志里那个）")
    parser.add_argument("--out", default="docs/screenshots", help="输出目录")
    parser.add_argument("--edge", default="", help="msedge.exe 路径（默认自动查找）")
    parser.add_argument("--width", type=int, default=1360)
    parser.add_argument("--height", type=int, default=880)
    parser.add_argument("--keep-profile", action="store_true", help="保留浏览器临时配置目录（排查用）")
    parser.add_argument("--fresh", action="store_true", help="数据目录是空库：多截一张首次使用向导")
    args = parser.parse_args(argv)

    base = f"http://127.0.0.1:{args.port}/"
    out = Path(args.out).resolve()
    edge = find_edge(args.edge)

    # 先确认服务在跑，避免产出一堆错误页截图
    try:
        with urllib.request.urlopen(base + "api/health", timeout=5) as response:
            health = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise SystemExit(f"本地服务没起来（{base}）：{exc}\n先按脚本头部说明起 --headless。")
    log(f"服务正常：{health}")

    results: List[Tuple[str, int]] = []
    # 每次用一个独立的 profile 目录：Edge 对同一个 profile 是排他的，
    # 上一次的孤儿进程会让这一次的浏览器直接退出（DevTools 连接被拒）。
    profile = (Path(".shots") / f"edge-profile-{args.port}").resolve()
    with Browser(edge, profile, free_port(), args.width, args.height) as browser:
        browser.set_viewport(args.width, args.height)
        browser.open(base, settle=3.5)
        wait_and_report(browser, 0.2, "首页加载完成")
        if args.fresh:
            results.append(shot(browser, "01-first-run.png", out, "首次使用向导（空知识库）"))
            browser.js("document.querySelector('#wizard-skip')?.click() || true", wait=1.0)

        # 问答页：先真实问一次本地问题，让截图里有答案与引用（不需要模型）
        browser.js("switchTab('chat')", wait=0.8)
        asked = browser.js(
            "(() => { const q = document.getElementById('question');"
            " if (!q) return 'NO-QUESTION-BOX'; q.value = '薄荷的生日是哪天？';"
            " const send = document.getElementById('send-btn');"
            " if (!send) return 'NO-SEND-BUTTON'; send.click(); return 'asked'; })()"
        )
        log(f"发起提问：{asked}")
        wait_and_report(browser, 8.0, "问答渲染完成")
        results.append(shot(browser, "02-chat-dark.png", out, "问答页 · 深色（含引用来源）"))

        browser.js("applyTheme('light', false)", wait=1.2)
        results.append(shot(browser, "03-chat-light.png", out, "问答页 · 浅色"))

        browser.js("applyTheme('dark', false)", wait=0.8)
        browser.js("switchTab('kb')", wait=3.0)
        wait_and_report(browser, 0.2, "知识库页")
        results.append(shot(browser, "04-knowledge-base.png", out, "知识库页（条目 / 资料 / 统计）"))

        browser.js("switchTab('update')", wait=3.0)
        wait_and_report(browser, 0.2, "自动更新页")
        results.append(shot(browser, "05-auto-update.png", out, "自动更新页（主题队列 / 数据源）"))

        browser.js("switchTab('settings')", wait=3.0)
        wait_and_report(browser, 0.2, "设置页")
        results.append(shot(browser, "06-settings.png", out, "设置页（模型 / 搜索 / 界面）"))

        browser.js("switchTab('about')", wait=2.0)
        wait_and_report(browser, 0.2, "关于页")
        results.append(shot(browser, "07-about.png", out, "关于页（版本与来源可靠性说明）"))

    if not args.keep_profile:
        shutil.rmtree(profile, ignore_errors=True)

    total = sum(size for _, size in results)
    print(f"\n共 {len(results)} 张，合计 {total / 1024:.1f} KB，输出目录：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
