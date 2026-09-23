"""开发机沙箱适配：修正 tempfile.mkdtemp 的行为。

背景
----
本机开发环境对 `tempfile.mkdtemp()` 创建的目录（内部用 `os.mkdir(path, 0o700)`）
会拒绝后续写入，连 chmod 都会被拒绝，表现为 PermissionError / [Errno 13]。

pip、PyInstaller 等工具内部都依赖 mkdtemp，于是会直接失败。
本模块把 mkdtemp 换成「以默认权限创建目录」的实现。

仅在开发机上由 tools/ 下的运行器导入，不会进入 exe。
"""

from __future__ import annotations

import os
import tempfile


def _mkdtemp_loose(suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> str:
    suffix = suffix or ""
    prefix = prefix or "tmp"
    directory = dir or tempfile.gettempdir()
    names = tempfile._RandomNameSequence()
    for _ in range(10000):
        path = os.path.join(directory, prefix + next(names) + suffix)
        try:
            os.mkdir(path, 0o777)
        except FileExistsError:
            continue
        return path
    raise FileExistsError("无法创建临时目录：候选名已用尽")


def install() -> None:
    """安装修正版 mkdtemp（幂等）。"""
    tempfile.mkdtemp = _mkdtemp_loose  # type: ignore[assignment]
