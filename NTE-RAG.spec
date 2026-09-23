# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、窗口模式、含前端资源与种子知识库。

安全约定
--------
- datas 里只有 app/web（前端）与 seed（种子知识库），不含开发配置文件、
  data/ 目录或任何开发期设置；
- tools/secret_scan.py 在构建前后各扫一遍：先在源码里找密钥，再到生成的 exe
  二进制里找开发配置中真实密钥的精确指纹，命中即判定构建失败。

实现说明
--------
不使用 PyInstaller.utils.hooks 的 collect_submodules / collect_data_files：
它们会启动隔离子进程并通过管道通信，在受限沙箱环境下会因 WinError 5 失败。
这里用 pkgutil / pathlib 就地实现等价功能。
"""

import importlib
import pkgutil
from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821 - SPECPATH 由 PyInstaller 注入


def submodules(package_name):
    """就地枚举某个包的全部子模块（等价于 collect_submodules，但不启子进程）。"""
    try:
        package = importlib.import_module(package_name)
    except Exception:
        return []
    names = [package_name]
    paths = list(getattr(package, "__path__", []) or [])
    if paths:
        for info in pkgutil.walk_packages(paths, package_name + "."):
            names.append(info.name)
    return names


def data_files(package_name):
    """就地收集某个包内的非 Python 数据文件。"""
    try:
        package = importlib.import_module(package_name)
    except Exception:
        return []
    collected = []
    for root in list(getattr(package, "__path__", []) or []):
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for path in root_path.rglob("*"):
            if not path.is_file() or path.suffix in (".py", ".pyc", ".pyo"):
                continue
            if "__pycache__" in path.parts:
                continue
            destination = Path(package_name) / path.relative_to(root_path).parent
            collected.append((str(path), str(destination)))
    return collected


datas = [
    (str(ROOT / "app" / "web"), "app/web"),
]
# 种子库只带发布用的 seed_kb.json；不把 seed_kb.before_*.json / seed_kb.prev.json 这些
# 对比基准打进发行包（它们只是本仓库的 A/B 基准，装进 exe 既占体积又容易被误认为有多套库）。
for _seed in sorted((ROOT / "seed").glob("*.json")):
    if _seed.name != "seed_kb.json":
        continue
    datas.append((str(_seed), "seed"))

# 这些包需要随包的数据文件（trafilatura 的 settings.cfg、justext 的停用词表等）
for _package in ("trafilatura", "justext", "dateparser", "htmldate", "courlan"):
    datas += data_files(_package)

hiddenimports = [
    # uvicorn 的运行时子模块是动态导入的，必须显式声明
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # pywebview 的 Windows 后端
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader",
    "proxy_tools",
    "bottle",
]

# trafilatura 内部有较多条件导入，整包纳入更稳
hiddenimports += submodules("trafilatura")

# 明确排除本项目用不到的重量级依赖，控制体积
excludes = [
    "tkinter",
    "PIL",
    "numpy",
    "pandas",
    "scipy",
    "matplotlib",
    "IPython",
    "pytest",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "gi",
    "cefpython3",
    "notebook",
    "PyInstaller",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="NTE-RAG",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 窗口程序；--selftest/--headless 时会自动附着控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "icon.ico"),
    # 版本资源由 tools/make_version_info.py 在构建时生成（版本号取自 app/__init__.py）。
    # 单独跑裸 PyInstaller 时文件可能不存在，此时传 None，不让打包因缺文件而失败。
    version=(str(ROOT / "assets" / "version_info.txt")
             if (ROOT / "assets" / "version_info.txt").is_file() else None),
)
