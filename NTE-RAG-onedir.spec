# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir 便携目录版）。

与 NTE-RAG.spec（单文件版）内容一致，区别只在最后用 COLLECT 生成文件夹：
- 单文件版启动时要把自身解包到临时目录，部分受限环境（安全软件、受控令牌、
  沙箱策略）会拒绝解包；
- onedir 版不需要解包，启动更快也更稳，代价是交付物为一个文件夹。

两个版本都不需要用户安装依赖，双击 exe 即可。

安全约定与单文件版相同：datas 只含前端与种子知识库，不含任何开发配置。
"""

import importlib
import pkgutil
from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821 - SPECPATH 由 PyInstaller 注入


def submodules(package_name):
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
            collected.append((str(path), str(Path(package_name) / path.relative_to(root_path).parent)))
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
for _package in ("trafilatura", "justext", "dateparser", "htmldate", "courlan"):
    datas += data_files(_package)

hiddenimports = [
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
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader",
    "proxy_tools",
    "bottle",
]
hiddenimports += submodules("trafilatura")

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

VERSION_FILE = ROOT / "assets" / "version_info.txt"
# 版本资源由 tools/make_version_info.py 在构建时生成（版本号取自 app/__init__.py）。
# 单独跑裸 PyInstaller 时文件可能不存在，此时退回 None，不让打包因缺文件而失败。
VERSION_RESOURCE = str(VERSION_FILE) if VERSION_FILE.is_file() else None

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NTE-RAG",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "icon.ico"),
    # 版本资源必须挂在 EXE 上，不能挂在 COLLECT：PyInstaller 在构建 EXE 时把版本
    # 资源写进 PE（构建日志里的 "Copying version information to EXE"）。只写在
    # COLLECT 上时，onefile 版有版本信息、onedir 版没有（实测踩过）。
    version=VERSION_RESOURCE,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    # 目录名就叫产品名：这个文件夹会被打成 zip 直接发到 Releases，
    # 用户解压后应该看到一个 NTE-RAG\ 而不是 NTE-RAG-onedir\。
    name="NTE-RAG",
)
