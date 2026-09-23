"""开发机专用：直接解包「纯 Python 源码包」并放入 site-packages。

背景
----
本机沙箱禁止「捕获子进程输出」的管道，而 pip 处理只有 sdist 的旧式包时
（例如 pywebview 的依赖 proxy_tools）一定会起 `python setup.py egg_info`
子进程，于是必然失败。

这类包往往就是单个 .py 文件，没必要构建。本工具直接从 PyPI 取源码包、
解出顶层 Python 模块，复制进目标目录，绕开 pip 的构建链路。

仅开发机使用，不会被打包进 exe。
用法：
    python tools\\fetch_pure_sdist.py proxy_tools --target .venv\\Lib\\site-packages
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tarfile
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
SKIP_FILES = {"setup.py", "setup.cfg", "conftest.py"}
WORK_DIR_NAME = ".sdist_tmp"


def _http_get(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "NTE-RAG-dev-tool/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _resolve_sdist(name: str, version: Optional[str]) -> Tuple[str, str]:
    meta = json.loads(_http_get(PYPI_JSON.format(name=name)).decode("utf-8"))
    version = version or meta["info"]["version"]
    for entry in meta.get("releases", {}).get(version, []):
        if entry.get("packagetype") == "sdist":
            return entry["url"], version
    # 兜底：从 urls（最新版）里找
    for entry in meta.get("urls", []):
        if entry.get("packagetype") == "sdist":
            return entry["url"], version
    raise SystemExit(f"未找到 {name} 的源码包（sdist）")


def _safe_extract(archive: Path, destination: Path) -> None:
    """手工解包，刻意不用 tarfile.extractall。

    本机沙箱下 `os.mkdir(path, 0o700)` 创建的目录后续不可写，而
    tarfile 内部正是以 0o700 建目录，会导致解包中途 PermissionError。
    这里自己控制目录创建（默认权限）与文件写入，同时规避软链接与
    路径穿越成员。
    """
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    def _safe_target(name: str) -> Path:
        """把成员名解析成解包目录内的路径，越界即中止。

        不能只做 `str(target).startswith(str(destination))` 前缀判断：
        `../src_evil/x` 会解析成 `<父目录>\\src_evil\\x`，它仍然以
        `<父目录>\\src` 开头，检查会被绕过。因此按父目录链判断。
        """
        target = (destination / name).resolve()
        if target != destination and destination not in target.parents:
            raise SystemExit(f"压缩包路径异常，已中止：{name}")
        return target

    if archive.name.endswith((".tar.gz", ".tgz", ".tar")):
        mode = "r:gz" if archive.name.endswith((".tar.gz", ".tgz")) else "r:"
        with tarfile.open(archive, mode) as tar:
            for member in tar.getmembers():
                target = _safe_target(member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = tar.extractfile(member)
                    if source is None:
                        continue
                    with source, open(target, "wb") as handle:
                        shutil.copyfileobj(source, handle)
                # 软链接/设备文件等一律跳过
    elif archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                _safe_target(member)
            zf.extractall(destination)
    else:
        raise SystemExit(f"不支持的压缩格式：{archive.name}")


def _collect_modules(root: Path) -> List[Path]:
    """找出源码根里的顶层模块/包。"""
    items: List[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.name in SKIP_FILES or entry.name.startswith("."):
            continue
        if entry.is_file() and entry.suffix == ".py":
            items.append(entry)
        elif entry.is_dir() and (entry / "__init__.py").exists() and entry.name not in {"tests", "test", "docs"}:
            items.append(entry)
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="从 PyPI 解包纯 Python 源码包到 site-packages")
    parser.add_argument("package", help="包名，例如 proxy_tools")
    parser.add_argument("--version", default=None, help="指定版本，默认取最新")
    parser.add_argument("--target", required=True, help="目标 site-packages 目录")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时解包目录")
    args = parser.parse_args()

    target = Path(args.target).resolve()
    target.mkdir(parents=True, exist_ok=True)

    url, version = _resolve_sdist(args.package, args.version)
    print(f"源码包：{args.package} {version}\n  {url}")

    # 每次用唯一目录：万一上次运行的目录因权限问题删不掉，也不会互相污染
    work = (Path.cwd() / WORK_DIR_NAME / f"{args.package}-{version}-{uuid.uuid4().hex[:8]}").resolve()
    work.mkdir(parents=True, exist_ok=True)

    archive = work / url.rsplit("/", 1)[-1]
    archive.write_bytes(_http_get(url))
    print(f"已下载：{archive.name} ({archive.stat().st_size} 字节)")

    extract_root = work / "src"
    _safe_extract(archive, extract_root)

    # 源码包通常多一层以 包名-版本 命名的目录
    roots = [p for p in extract_root.iterdir() if p.is_dir()]
    source_root = roots[0] if len(roots) == 1 else extract_root

    modules = _collect_modules(source_root)
    if not modules:
        raise SystemExit("未在源码包中找到顶层 Python 模块，请改为手动安装")

    for module in modules:
        destination = target / module.name
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination, ignore_errors=True)
            else:
                destination.unlink()
        if module.is_dir():
            shutil.copytree(module, destination)
        else:
            shutil.copy2(module, destination)
        print(f"已安装：{module.name} -> {destination}")

    if not args.keep_temp:
        shutil.rmtree(work, ignore_errors=True)
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
