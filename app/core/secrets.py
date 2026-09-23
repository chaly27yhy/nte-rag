"""密钥保护：Windows DPAPI 加解密 + 显示脱敏 + 日志清洗。

设计要点
- 用户密钥只以密文写入 config.json，密文由 DPAPI 绑定当前 Windows 用户，
  拷到别的机器/别的用户账户下无法解密（此时要求用户重新填写）。
- 任何日志、界面回显一律走 mask()，绝不输出完整密钥。
- 本模块不含、也不接受任何硬编码密钥。
"""

from __future__ import annotations

import base64
import ctypes
import os
import re
from ctypes import wintypes
from typing import Any

IS_WINDOWS = os.name == "nt"

_DPAPI_PREFIX = "dpapi:"
_PLAIN_PREFIX = "plain:"  # 仅非 Windows 开发环境降级使用

if IS_WINDOWS:  # pragma: no cover - 平台相关

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _to_blob(data: bytes):
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer  # 必须返回 buffer 保持引用，否则内存被回收


def _from_blob(blob: "_DataBlob") -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def protect(secret: str) -> str:
    """把明文密钥转成可安全落盘的密文串；空串返回空串。"""
    if not secret:
        return ""
    raw = secret.encode("utf-8")

    if not IS_WINDOWS:  # 开发兜底：不是加密，仅避免明文误提交
        return _PLAIN_PREFIX + base64.b64encode(raw).decode("ascii")

    blob_in, keepalive = _to_blob(raw)
    blob_out = _DataBlob()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        "NTE-RAG",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    del keepalive
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData 调用失败")
    try:
        encrypted = _from_blob(blob_out)
    finally:
        _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))
    return _DPAPI_PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect(cipher: str) -> str:
    """解密；解不开（换机器/换用户/文件损坏）时返回空串，由调用方提示重填。"""
    if not cipher:
        return ""
    if cipher.startswith(_PLAIN_PREFIX):
        try:
            return base64.b64decode(cipher[len(_PLAIN_PREFIX) :]).decode("utf-8")
        except Exception:
            return ""
    if not cipher.startswith(_DPAPI_PREFIX):
        return ""
    if not IS_WINDOWS:
        return ""

    try:
        raw = base64.b64decode(cipher[len(_DPAPI_PREFIX) :])
    except Exception:
        return ""

    blob_in, keepalive = _to_blob(raw)
    blob_out = _DataBlob()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    del keepalive
    if not ok:
        return ""
    try:
        decrypted = _from_blob(blob_out)
    finally:
        _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))
    try:
        return decrypted.decode("utf-8")
    except Exception:
        return ""


def mask(secret: str, head: int = 4, tail: int = 4) -> str:
    """显示用脱敏：只留前 4 后 4，其余用 * 代替。"""
    if not secret:
        return ""
    if len(secret) <= head + tail:
        return "*" * len(secret)
    return f"{secret[:head]}{'*' * 6}{secret[-tail:]}"


def is_mask_value(value: str, current: str) -> bool:
    """判断一个字段值是不是本程序生成的掩码串（语义为「保持原值不变」）。

    判定必须与 mask() 的形态严格对应：过去用 `len(set(value)) <= 6` 这种启发式，
    真实密钥（如 `sk-a******wxyz` 有 9 个不同字符）永远不满足，于是前端回传的
    掩码串被当成新密钥存了下来，静默毁掉用户原本可用的 Key。

    「算不算新密钥」有两处依赖这个判断：Config.set_secret 决定是否覆盖，
    api._probe_config 决定要不要采用请求方带来的服务地址。两处必须是同一套规则。
    """
    if not value or "*" not in value:
        return False
    return value == mask(current) or set(value) == {"*"}


# 常见密钥形态，用于日志清洗与打包前扫描
# 博查、Serper 的 Key 没有任何前缀，只能是纯 UUID 或 40 位十六进制串，
# 单独按形状匹配会把提交哈希、哈希值等正常文本一起吃掉；所以这两类改成
# 「带上下文」匹配：只有在 api_key / X-API-KEY / Authorization 等字段名附近
# 出现的才当密钥。前缀明确的（sk-、tvly-、AIza 等）仍然直接匹配。
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"tvly-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}"),
    re.compile(r"(?i)(api[_-]?key[\"'\s:=]+)([A-Za-z0-9_\-]{16,})"),
    # 上下文形态：X-API-KEY: <40 位十六进制>（Serper）
    re.compile(r"(?i)(x-api-key[\"'\s:=]+)([0-9a-f]{32,64})"),
    # 上下文形态：api_key/token 附近的 UUID（博查）
    re.compile(
        r"(?i)((?:api[_-]?key|token|secret)[\"'\s:=]{0,4})"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    ),
    # 上下文形态：api_key/token 附近的 32/40 位十六进制串
    re.compile(
        r"(?i)((?:api[_-]?key|token|secret)[\"'\s:=]{0,4})(?=[0-9a-f]{32,40}\b)"
        r"([0-9a-f]{32,40})"
    ),
]


def scrub(text: str) -> str:
    """把文本里疑似密钥的片段替换成 ***，用于日志与错误信息。"""
    if not text:
        return text
    result = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            result = pattern.sub(lambda m: m.group(1) + "***", result)
        else:
            result = pattern.sub("***", result)
    return result


def looks_like_secret(value: str) -> bool:
    """判断字符串是否像真实密钥（供打包前扫描使用）。"""
    if not value or len(value) < 16:
        return False
    return any(p.search(value) for p in _SECRET_PATTERNS)


def safe_repr(obj: Any) -> str:
    return scrub(repr(obj))
