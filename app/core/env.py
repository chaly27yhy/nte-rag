"""进程级环境变量的名字与读取（统一前缀 ``NTE_RAG_``）。

集中放这里的理由有两条：

1. 这些名字不止代码在用——`tools/build_exe.ps1`、`tools/run_eval.py`、
   `tools/kb_probe.py`、README 与 `.env.example` 都要写同一批字符串，
   散落各处时改一次名字必然漏掉一两个；
2. 取值规则（空串不算设置、真假的写法）只该有一份实现。

**没有旧名兼容层**：项目从未对外发布过，不存在带着旧名字的既有 `.env`，
留一层回退只会让后来的人以为还有别的地方在用旧名。
"""

from __future__ import annotations

import os
from typing import Optional

# 项目统一的 logger 名（app/main.py 的 setup_logging 也用它）
LOGGER_NAME = "nte-rag"

# 语义化常量：调用方不必硬编码字符串
DATA_DIR = "NTE_RAG_DATA_DIR"            # 数据目录（优先级最高）
PORTABLE = "NTE_RAG_PORTABLE"            # 1 强制便携模式；0 强制 %APPDATA%
DISABLE_AUTH = "NTE_RAG_DISABLE_AUTH"    # 1 关闭接口鉴权（仅自动化测试）

# 开发期覆盖（只被 app/config.py 的 apply_dev_env() 读取，且仅在未打包时生效）
# 总开关：不设它时，源码检出/便携分发都不会被 .env 覆盖（默认关）。
DEV_ENV = "NTE_RAG_DEV_ENV"              # 1 显式允许 .env 灌入配置
DEV_LLM_PROVIDER = "NTE_RAG_DEV_LLM_PROVIDER"
DEV_LLM_BASE_URL = "NTE_RAG_DEV_LLM_BASE_URL"
DEV_LLM_MODEL = "NTE_RAG_DEV_LLM_MODEL"
DEV_LLM_API_KEY = "NTE_RAG_DEV_LLM_API_KEY"
DEV_SEARCH_PROVIDER = "NTE_RAG_DEV_SEARCH_PROVIDER"
DEV_SEARCH_API_KEY = "NTE_RAG_DEV_SEARCH_API_KEY"

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def get(name: str, default: Optional[str] = None) -> Optional[str]:
    """读取环境变量；空串按「没设置」处理，返回 ``default``。"""
    value = os.environ.get(name)
    if value:
        return value
    return default


def get_bool(name: str, default: bool = False) -> bool:
    """把 ``1/true/yes/on`` 视为真、``0/false/no/off`` 视为假，无法识别时用 default。"""
    raw = get(name)
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default
