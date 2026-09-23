"""发布门禁：扫描开发期密钥，防止它们进入源码或最终 exe。

三层检查
--------
1. 源码层：遍历将要打包的目录（app/、seed/、tools/、根目录配置），
   按密钥特征匹配，命中即失败；
2. 配置层：读取本机 .env，取出其中的真实密钥值，作为「精确指纹」用于后续两层比对；
3. 产物层：若已构建 exe，直接在二进制里搜索上述精确指纹与通用特征，
   这是最有力的一道检查——即使密钥被编码进资源、被压缩进 exe，也会被抓出来。

退出码：0 = 通过，1 = 发现问题（构建脚本据此中止）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

# Windows 控制台默认是 GBK：`✓`/`✗` 这类符号编码不了，会让 print 抛
# UnicodeEncodeError。之前它就发生在「成功」那一行上——脚本其实通过了，
# 却因为打印成功提示而崩溃退出，构建脚本据此误报成「发现密钥泄漏」。
# 这里只放宽错误处理（保留 GBK，不改变中文显示），编码不了的字符退化成 ?。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 允许出现在仓库里的通用特征（源码中的正则、示例、文档）
_PATTERN_RULES: List[Tuple[str, str]] = [
    ("OpenAI/DeepSeek 风格 sk- 密钥", r"sk-[A-Za-z0-9_\-]{20,}"),
    ("Anthropic 密钥", r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    ("Google API 密钥", r"AIza[0-9A-Za-z_\-]{33,}"),
    ("Tavily 密钥", r"tvly-[A-Za-z0-9_\-]{16,}"),
    ("博查/其它 32 位十六进制令牌", r"\b[0-9a-f]{32}\b"),
    ("形如 Bearer 的真实令牌", r"Bearer\s+[A-Za-z0-9_\-\.]{24,}"),
    # Serper 的 Key 是 40 位十六进制且没有前缀，只能结合头部名判断，
    # 单独按形状匹配会把正常的哈希值一起判成泄漏
    ("Serper X-API-KEY", r"(?i)x-api-key[\"'\s:=]+[0-9a-f]{32,64}"),
]

# 文本用 str 版、二进制用 bytes 版：两者规则必须一字不差，否则「源码干净、
# exe 里却带着密钥」这种最危险的情况会漏掉。之前文本分支误用了 bytes 版，
# 直接 TypeError 崩在扫描途中——门禁自己坏掉比误报更糟。
GENERIC_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (label, re.compile(rule)) for label, rule in _PATTERN_RULES
]
GENERIC_BYTES_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (label, re.compile(rule.encode("utf-8"))) for label, rule in _PATTERN_RULES
]

# 这些文件/目录天然包含示例或正则，跳过以减少误报
SCAN_SKIP_DIRS = {
    ".venv", "venv", "dist", "build", "__pycache__", ".git", "data",
    "piptmp", ".sdist_tmp", ".seed_build", ".seed_check", ".selftest_data",
    ".probe", "node_modules", ".verify", ".build_selftest", ".field_report",
}
SCAN_SKIP_FILES = {"secret_scan.py", "secrets.py", ".env.example"}

# 行内放行标记：自检脚本必须准备看起来像真密钥的合成串来验证脱敏逻辑，
# 于是这些合成样本会命中通用特征，把构建门禁卡死（2026-09-23 真的卡住过一次：
# quality_check.py 里的 sk- 样例让 tools/build_exe.ps1 在第 3 步中止）。
# 放宽特征会放过真密钥，所以合成样本必须在同一行注释里声明：
# 标记只对本行生效，且必须写在 # 或 // 注释里，改一行只放行一行。
ALLOW_MARKER = "secret-scan: allow"
_COMMENT_TOKENS = ("#", "//")


def _line_start(content: str, position: int) -> int:
    newline = content.rfind("\n", 0, position)
    return 0 if newline < 0 else newline + 1


def _line_allowed(content: str, position: int) -> bool:
    """命中位置所在行是否带有效放行标记（标记必须在注释里，且位于命中之前）。"""
    start = _line_start(content, position)
    end = content.find("\n", start)
    line = content[start:] if end < 0 else content[start:end]
    marker = line.find(ALLOW_MARKER)
    if marker < 0:
        return False
    return any(0 <= line.find(token) < marker for token in _COMMENT_TOKENS)

TEXT_SUFFIXES = {".py", ".js", ".html", ".css", ".json", ".md", ".txt", ".ps1", ".cfg", ".ini", ".toml", ".yaml", ".yml"}


def iter_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SCAN_SKIP_DIRS for part in path.parts):
            continue
        yield path


def load_real_secrets(env_path: Path) -> Dict[str, str]:
    """从 .env 提取真实的密钥值，作为精确指纹。"""
    secrets_found: Dict[str, str] = {}
    if not env_path.exists():
        return secrets_found
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if not value or len(value) < 12:
            continue
        if not any(token in name.upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            continue
        secrets_found[name] = value
    return secrets_found


def scan_text_file(path: Path, needles: Dict[str, str]) -> List[str]:
    findings: List[str] = []
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {".gitignore", ".env.example"}:
        return findings
    if path.name in SCAN_SKIP_FILES:
        return findings
    if path.name == ".env":
        # .env 本身允许存在（本地开发用），但绝不能被打包——由构建脚本另行校验
        return findings
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return findings

    for name, value in needles.items():
        if value and value in content:
            findings.append(f"{path}: 出现 .env 中 {name} 的真实值（精确匹配）")
    for label, pattern in GENERIC_PATTERNS:
        # 在字符串上匹配而不是编码成 bytes：`match.start()` 随后要当作字符下标
        # 去回查所在行（文件里有中文，字节序与字符序不一致会查到错误的行）。
        for match in pattern.finditer(content):
            # 合成样本必须自己声明：所在行有 `# secret-scan: allow` 注释才跳过
            if _line_allowed(content, match.start()):
                continue
            token = match.group(0)
            findings.append(f"{path}: 命中「{label}」疑似密钥：{token[:12]}…")
    return findings


def scan_binary(path: Path, needles: Dict[str, str]) -> List[str]:
    findings: List[str] = []
    try:
        blob = path.read_bytes()
    except Exception as error:
        return [f"{path}: 无法读取（{error}）"]

    for name, value in needles.items():
        if value and value.encode("utf-8") in blob:
            findings.append(f"{path}: 二进制中出现 .env 的 {name} 真实值（严重泄漏）")

    # 二进制里的通用特征：排除 PyInstaller 自身元数据造成的误报，仅保留高置信度形态
    for label, pattern in GENERIC_BYTES_PATTERNS[:3]:
        matches = pattern.findall(blob)
        if matches:
            sample = matches[0][:12].decode("utf-8", errors="ignore")
            findings.append(f"{path}: 二进制命中「{label}」× {len(matches)}，示例 {sample}…")
    return findings


def check_env_excluded(spec_path: Path) -> List[str]:
    """确保打包配置里没有把 .env 塞进要打包的资源列表。

    先剥掉三引号文档字符串，避免把「说明文字里提到 .env」误判成打包项。
    """
    findings: List[str] = []
    if not spec_path.exists():
        return findings
    text = spec_path.read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r'"""[\s\S]*?"""', "", text)
    text = re.sub(r"'''[\s\S]*?'''", "", text)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ".env" in stripped:
            findings.append(f"{spec_path}: 打包配置中出现了 .env 相关内容：{stripped}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="开发期密钥泄漏扫描（发布门禁）")
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="项目根目录")
    parser.add_argument("--dist", default="", help="额外的二进制产物路径（exe）或目录")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    env_path = root / ".env"
    needles = load_real_secrets(env_path)

    def say(message: str) -> None:
        if not args.quiet:
            print(message)

    say(f"扫描根目录：{root}")
    if needles:
        say(f"已从 .env 提取 {len(needles)} 个密钥指纹：{', '.join(needles)}")
    else:
        say("未发现 .env（或其中没有密钥），仅做通用特征扫描")

    findings: List[str] = []
    scanned = 0
    for path in iter_files(root):
        if path.is_dir():
            continue
        scanned += 1
        findings.extend(scan_text_file(path, needles))

    # 两份打包脚本都要查：单一文件版与便携目录版是分别构建的，漏掉哪一份
    # 都可能让「被塞进 .env 路径的 spec」蒙混过关（两份 spec 的 docstring 都写着不含开发配置，
    # 正需要机器校验）。
    for spec_name in ("NTE-RAG.spec", "NTE-RAG-onedir.spec"):
        findings.extend(check_env_excluded(root / spec_name))

    if args.dist:
        dist_path = Path(args.dist)
        targets: List[Path] = []
        if dist_path.is_dir():
            targets = [p for p in dist_path.rglob("*") if p.is_file()]
        elif dist_path.exists():
            targets = [dist_path]
        else:
            findings.append(f"指定的产物路径不存在：{dist_path}")
        say(f"检查产物 {len(targets)} 个文件")
        for target in targets:
            findings.extend(scan_binary(target, needles))

    say(f"共扫描 {scanned} 个源码文件")

    if findings:
        print("\n发现潜在密钥泄漏（构建已中止）：")
        for item in findings[:60]:
            print(f"  ✗ {item}")
        if len(findings) > 60:
            print(f"  … 另有 {len(findings) - 60} 条")
        print("\n如果命中的是自检用的合成样本（不是真密钥），在那一行行尾加注释放行：")
        print(f"    token = \"sk-…\"  # {ALLOW_MARKER}")
        print("只有同一行、且写在注释里的标记才生效；真密钥请从源码里删掉，不要放行。")
        return 1

    print("\n✓ 未发现密钥泄漏，可以打包。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
