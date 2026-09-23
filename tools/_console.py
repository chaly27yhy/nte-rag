"""让开发工具在 Windows 的 GBK 控制台下也能安全打印。

背景
----
Windows 控制台默认编码是 GBK，`✅` `❌` `⚠️` `✓` `✗` 这些符号**不在 GBK 里**，
`print` 会直接抛 `UnicodeEncodeError`。

这个坑真实发生过两次：
- `secret_scan.py` 在**成功**那一行（`✓ 未发现密钥泄漏`）崩溃 → 退出码非 0 →
  `build_exe.ps1` 误报「源码中发现疑似密钥，已中止构建」，其实一个密钥都没有；
- `seed_builder.py` 在抓完全部页面、**准备输出汇总**时崩溃 → 几十分钟的
  抓取成果当场丢失。

做法：只放宽错误处理（`errors="replace"`），保留 GBK 编码，中文照常显示，
编码不了的符号退化成 `?`，不再崩溃。

用法：`import _console  # noqa: F401`（导入即生效）。
"""

from __future__ import annotations

import sys


def safe_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


safe_console()
