"""程序入口（本地运行与 PyInstaller 打包共用）。

为什么不直接拿 app/main.py 当入口
----------------------------------
PyInstaller 会把入口脚本当作 `__main__` 直接执行，此时脚本没有包上下文，
`app/main.py` 里的相对导入（`from . import APP_TITLE`）会抛：

    ImportError: attempted relative import with no known parent package

用这个顶层启动器先建立包上下文、再调用 `app.main.main()`，两种运行方式都正常。

用法：
    python run.py                        # 启动桌面窗口（WebView2 不可用时自动降级）
    python run.py --headless             # 只起本地服务，不开窗口
    python run.py --selftest             # 启动自检（11 项，报告写到数据目录）
    python run.py --window-test          # 只测一次窗口能否创建，然后退出
    python run.py --no-window            # 等同 --headless，显式表达"不要窗口"
    python run.py --no-console           # 不附加控制台（双击运行时更干净）
    python run.py --probe-webview        # 子进程探测 WebView2 是否可用（内部用）
    python run.py --port 8765            # 指定端口（默认自动挑一个空闲端口）
    python run.py --verbose              # 打开调试日志

全部开关见 `app/main.py` 的 `main(argv)`。
"""

from __future__ import annotations

import os
import sys

# 开发态直接 `python run.py` 时保证能找到 app 包；打包后该路径无副作用
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
