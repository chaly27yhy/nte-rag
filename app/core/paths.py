"""路径解析：区分「只读资源」与「可写数据」，并决定便携模式。

规则：
- 只读资源（web 静态页、种子知识库）来自 bundle_root()，
  打包后为 PyInstaller 解包目录，开发时为项目根目录。
- 可写数据（config.json / knowledge.db / logs）优先放在 exe 同级 data/，
  以便整目录拷到 U 盘即可带走；exe 所在目录不可写时回落到 %APPDATA%\\NTE-RAG。

环境变量开关（便于测试）：
- NTE_RAG_PORTABLE=1 强制便携模式，=0 强制 APPDATA 模式。
- NTE_RAG_DATA_DIR 直接指定数据目录（优先级最高）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

from . import env as env_mod

APP_NAME = "NTE-RAG"
PORTABLE_FLAG = "portable.flag"

_writable_cache: dict[str, bool] = {}
_data_dir_cache: Optional[Path] = None


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的 exe 中。"""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """开发态的项目根目录（app/ 的上一级）。"""
    return Path(__file__).resolve().parents[2]


def bundle_root() -> Path:
    """只读资源根目录。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return project_root()


def exe_dir() -> Path:
    """exe 所在目录（开发态为项目根目录）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return project_root()


def _is_writable(directory: Path) -> bool:
    key = str(directory)
    if key in _writable_cache:
        return _writable_cache[key]
    ok = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=".wtest", dir=str(directory))
        os.close(handle)
        os.unlink(name)
        ok = True
    except Exception:
        ok = False
    _writable_cache[key] = ok
    return ok


def is_portable() -> bool:
    """是否使用便携模式（数据跟随 exe）。"""
    flag = env_mod.get(env_mod.PORTABLE)
    if flag == "1":
        return True
    if flag == "0":
        return False
    home = exe_dir()
    if (home / PORTABLE_FLAG).exists():
        return True
    return _is_writable(home)


def data_dir() -> Path:
    """可写数据目录，保证存在。"""
    global _data_dir_cache
    if _data_dir_cache is not None:
        return _data_dir_cache

    override = env_mod.get(env_mod.DATA_DIR)
    if override:
        target = Path(override).expanduser()
    elif is_portable():
        target = exe_dir() / "data"
    else:
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
        target = Path(base) / APP_NAME

    target.mkdir(parents=True, exist_ok=True)
    _data_dir_cache = target
    return target


def config_path() -> Path:
    return data_dir() / "config.json"


def db_path() -> Path:
    return data_dir() / "knowledge.db"


def log_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def web_root() -> Path:
    return bundle_root() / "app" / "web"


def seed_kb_path() -> Path:
    return bundle_root() / "seed" / "seed_kb.json"


def describe_layout() -> dict:
    """给「关于/诊断」页展示的路径信息。"""
    return {
        "frozen": is_frozen(),
        "portable": is_portable(),
        "exe_dir": str(exe_dir()),
        "bundle_root": str(bundle_root()),
        "data_dir": str(data_dir()),
        "config": str(config_path()),
        "database": str(db_path()),
        "logs": str(log_dir()),
    }
