"""开发机专用：PyInstaller 运行器。

做两件适配（都不影响产物本身）
------------------------------
1. **临时目录**：本机沙箱下 `tempfile.mkdtemp()` 创建的目录（内部以 0o700 创建）
   会拒绝后续写入，见 tools/_tmpfix.py；
2. **隔离子进程**：PyInstaller 内部用 `PyInstaller.isolated` 起子进程并通过管道通信
   （例如 discover_hook_directories、collect_submodules、collect_data_files），
   而本机沙箱禁止这类管道，会抛 `PermissionError [WinError 5]`。
   这里把 isolated 的 `call` / `Python` 替换为「在当前进程内直接执行」的等价实现。

   隔离机制原本是为了避免 hook 导入包时污染构建进程的 sys.path / 环境变量。
   本机开发环境下改为就地执行是安全的：重量级可视化依赖已排除，
   但在其它机器上**应当直接使用原生 PyInstaller**，不需要本运行器。

用法与 PyInstaller 完全一致：
    python tools\\pyinstaller_runner.py --clean --noconfirm NTE-RAG.spec
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tmpfix  # noqa: E402


class _LocalPython:
    """`isolated.Python()` 的就地替身：不启动子进程，直接执行。"""

    def __enter__(self) -> "_LocalPython":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def call(self, function, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return function(*args, **kwargs)


def _local_call(function, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    return function(*args, **kwargs)


def patch_isolated() -> bool:
    try:
        from PyInstaller import isolated
        from PyInstaller.isolated import _parent
    except Exception:
        return False

    _parent.call = _local_call
    _parent.Python = _LocalPython  # type: ignore[assignment]
    isolated.call = _local_call
    isolated.Python = _LocalPython  # type: ignore[assignment]
    return True


def main() -> int:
    _tmpfix.install()
    try:
        from PyInstaller.__main__ import run
    except ImportError:
        sys.stderr.write("未安装 PyInstaller。请先执行：pip install -r requirements-dev.txt\n")
        return 2

    if patch_isolated():
        print("[pyinstaller_runner] 已启用 isolated 就地执行适配（沙箱环境）")

    run(sys.argv[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
