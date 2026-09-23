"""开发机专用：在受限沙箱环境下运行 pip 的包装器。

背景
----
本机开发环境对 `tempfile.mkdtemp()` 创建的目录（其内部用 `os.mkdir(path, 0o700)`）
会拒绝后续写入，连 `chmod` 都会被拒绝，表现为：

    ERROR: Could not install packages due to an OSError:
    [Errno 13] Permission denied: '...\\pip-unpack-xxxx\\xxx.whl.metadata'

pip 解包 wheel 时恰好依赖 mkdtemp，于是任何安装都会失败。
本文件复用 tools/_tmpfix.py 的修正，属于开发机环境适配，
与最终 exe 产物无关，也不会被打包。

用法：
    .venv\\Scripts\\python.exe tools\\pip_runner.py install -r requirements-dev.txt
或：
    python tools\\pip_runner.py install --target .venv\\Lib\\site-packages -r requirements.txt
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tmpfix  # noqa: E402


def main() -> int:
    _tmpfix.install()
    try:
        from pip._internal.cli.main import main as pip_main
    except ImportError:
        sys.stderr.write("未找到 pip，请先用带 pip 的 Python 运行。\n")
        return 2
    return int(pip_main(sys.argv[1:]) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
