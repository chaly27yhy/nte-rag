# -*- coding: utf-8 -*-
"""生成 Windows 版本资源文件（PyInstaller 的 ``version=`` 参数要用）。

背景
----
不写版本资源时，Windows 属性面板里「产品名称 / 文件说明 / 版本」全是空的：
用户看不出这个 exe 是什么、属于哪个版本，出了问题也说不清装的是哪一版。
两份 .spec 里的 ``version=`` 指向本脚本生成的 ``assets/version_info.txt``。

构建时生成
----------
版本号的唯一事实来源是 ``app/__init__.py`` 的 ``__version__``。写死一份 txt
迟早会和代码里的版本号对不上，所以每次构建时用 App 自己的版本重新生成。

用法（通常由 tools\\build_exe.ps1 调用）::

    python tools\\make_version_info.py                 # 版本取自 app.__version__
    python tools\\make_version_info.py --version 1.2.0 # 手动指定（不推荐）
    python tools\\make_version_info.py --check         # 只校验现有文件是否与版本一致
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  （GBK 控制台下安全打印，见 tools/_console.py）

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "assets" / "version_info.txt"

COMPANY_NAME = "NTE-RAG contributors"
LEGAL_COPYRIGHT = "MIT License. 游戏素材与知识摘录版权归原站所有，详见 THIRD_PARTY_NOTICES.md"

TEMPLATE = """# UTF-8
#
# 由 tools/make_version_info.py 生成，请勿手工编辑（改版本号请改 app/__init__.py）。
# 版本号的唯一事实来源：app/__init__.py 的 __version__。
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v0}, {v1}, {v2}, {v3}),
    prodvers=({v0}, {v1}, {v2}, {v3}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '080404B0',
          [StringStruct('CompanyName', '{company}'),
           StringStruct('FileDescription', '{description}'),
           StringStruct('FileVersion', '{version}'),
           StringStruct('InternalName', '{product}'),
           StringStruct('LegalCopyright', '{copyright}'),
           StringStruct('OriginalFilename', '{product}.exe'),
           StringStruct('ProductName', '{product}'),
           StringStruct('ProductVersion', '{version}')])
      ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """把 '1.2.3' / '1.2.3.4' 变成四元组；非数字段落一律当 0。"""
    parts = [p for p in str(version).strip().split(".") if p != ""]
    numbers: list[int] = []
    for part in parts[:4]:
        digits = "".join(ch for ch in part if ch.isdigit())
        numbers.append(int(digits) if digits else 0)
    while len(numbers) < 4:
        numbers.append(0)
    return tuple(numbers[:4])  # type: ignore[return-value]


def render(version: str, product: str, description: str) -> str:
    v0, v1, v2, v3 = version_tuple(version)
    return TEMPLATE.format(
        v0=v0,
        v1=v1,
        v2=v2,
        v3=v3,
        version=version,
        product=product,
        description=description,
        company=COMPANY_NAME,
        copyright=LEGAL_COPYRIGHT,
    )


def build_text(version: str = "") -> str:
    """按当前 App 的真实名称与版本生成版本资源文本（唯一入口，测试也走这里）。"""
    from app import APP_NAME, APP_TITLE, __version__  # noqa: PLC0415

    resolved = version or __version__
    return render(resolved, APP_NAME, f"{APP_NAME} —— {APP_TITLE}".strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Windows 版本资源文件")
    parser.add_argument("--version", default="", help="手动指定版本号（默认取 app.__version__）")
    parser.add_argument("--out", default=str(TARGET), help="输出路径")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args()

    from app import __version__  # noqa: PLC0415

    version = args.version or __version__
    text = build_text(version)

    out = Path(args.out)
    if args.check:
        if not out.is_file():
            print(f"缺少版本资源文件：{out}")
            return 1
        if out.read_text(encoding="utf-8-sig") != text:
            print(f"版本资源文件与当前版本（{version}）不一致：{out}")
            return 1
        print(f"版本资源文件与 {version} 一致：{out}")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    # 二进制写入，刻意绕开文本模式的换行翻译：
    # Windows 上 write_text() 会把 \n 变成 \r\n，而 --check 与自测比对的是
    # 带 \n 的生成文本，两边不一致就会误报「版本资源被手工改过」。
    # 必须带 BOM：PyInstaller 读版本资源时按 UTF-8 解析，带 BOM 能让它和
    # PowerShell 5.1 都正确识别编码（文件内有中文，缺 BOM 时容易出乱码）。
    out.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    print(f"已生成版本资源：{out}（版本 {version}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
