"""中文文本处理：归一化、分词、切块、SimHash 指纹。

为什么自研分词
--------------
SQLite FTS5 的 unicode61 分词器对中文几乎无效（会把整句当成一个 token），
而 jieba 只有源码包、体积大、且引入构建依赖。这里采用中文检索的经典做法：
**汉字 bigram**（相邻两字组合）作为索引与查询单元。

- 索引侧：CJK 片段切成 bigram；单字片段保留单字；英文/数字保留原词。
- 查询侧：同样切 bigram，并过滤掉由虚词组成的噪声 bigram（如「的角」）。
- 排序侧：FTS5 的 bm25 负责词频，另有覆盖率与来源权重在 store/rag 层叠加。

若运行环境恰好装了 jieba，会自动追加「搜索模式」词，作为精度增强而非依赖。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Sequence

try:  # 可选增强，不作为硬依赖
    import jieba as _jieba  # type: ignore

    _JIEBA_READY = True
except Exception:  # pragma: no cover - 环境相关
    _jieba = None
    _JIEBA_READY = False


_CJK_RANGE = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff"
_CJK_RUN = re.compile(f"[{_CJK_RANGE}]+")
_TOKEN_RE = re.compile(
    f"[{_CJK_RANGE}]+"
    r"|[A-Za-z][A-Za-z0-9_+#.\-]*"
    r"|\d+(?:\.\d+)?%?"
)

# 虚词：用于剔除查询侧的噪声 bigram，避免「异环的角色」被「环的」「的角」拖低召回
_STOP_CHARS = set("的了是在和与有也都很就这那你我他她它们个之而及或者对于把被从到用为以等且但如若因所")
_SENTENCE_END = "。！？!?；;\n\r"
_SOFT_BREAK = "，,、：:）)】」》…— "


def normalize(text: str) -> str:
    """NFKC 归一 + 统一小写 + 压缩空白，保证全角/半角、大小写不影响检索。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    text = text.lower()
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _cjk_units(run: str) -> List[str]:
    """CJK 片段 -> bigram 列表；长度为 1 时保留该单字。"""
    chars = list(run)
    if len(chars) == 1:
        return chars
    return [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]


def _is_noise_bigram(token: str) -> bool:
    return len(token) == 2 and token[0] in _STOP_CHARS and token[1] in _STOP_CHARS


# 引号（中文书名号/方头括号也常用来包专名）：包在里面的内容一律当专名，不做虚词过滤。
_QUOTED_RE = re.compile(r"[「『“\"'《【]([^」』”\"'》】]{1,40})[」』”\"'》】]")


def quoted_terms(text: str) -> set:
    """取出被引号包住的内容里出现的 bigram（专名保护）。

    `我们。` 这种**由虚词组成的专名**（弧盘「我们。」）会被查询侧的虚词过滤
    整条丢掉，问题于是退化成只按字段名检索（`效果`/`描述` 能匹配几十个其它弧盘），
    真正那条证据根本进不了候选。
    只有**被引号明确标出来**的才保护：普通句子里的「我/们」仍然照旧过滤。
    """
    protected: set = set()
    for match in _QUOTED_RE.finditer(normalize(text or "")):
        protected.update(tokenize(match.group(1), with_jieba=False))
    return protected


def tokenize_query(text: str) -> List[str]:
    """查询用分词：去掉虚词 bigram，并去重保序（引号里的专名除外）。"""
    protected = quoted_terms(text)
    seen: set[str] = set()
    result: List[str] = []
    for token in tokenize(text):
        if _is_noise_bigram(token) and token not in protected:
            continue
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def tokenize(text: str, with_jieba: bool = True) -> List[str]:
    """索引与查询共用的分词入口（不做虚词过滤）。"""
    normalized = normalize(text)
    if not normalized:
        return []
    tokens: List[str] = []
    for match in _TOKEN_RE.finditer(normalized):
        piece = match.group(0)
        if _CJK_RUN.fullmatch(piece):
            tokens.extend(_cjk_units(piece))
        else:
            tokens.append(piece)
    if with_jieba and _JIEBA_READY and _CJK_RUN.search(normalized):
        try:
            for word in _jieba.cut_for_search(normalized):  # type: ignore[union-attr]
                word = word.strip()
                if not word:
                    continue
                if _CJK_RUN.fullmatch(word) and len(word) >= 2:
                    tokens.append(word)
                elif not _CJK_RUN.fullmatch(word):
                    tokens.append(word)
        except Exception:
            pass
    return tokens


def tokens_to_index(tokens: Iterable[str]) -> str:
    """FTS5 存储形式：空格分隔（unicode61 分词器按空白切分）。"""
    return " ".join(tokens)


def build_fts_query(tokens: Sequence[str]) -> str:
    """把 token 列表拼成 FTS5 MATCH 表达式：OR 连接，交由排序层决定相关性。"""
    # 先去引号再丢空：反过来时，一个只由引号组成的 token 会活到 f'"{t}"' 里，
    # 拼出 MATCH '' 让 FTS5 抛 "fts5: syntax error near"。
    safe = [item for item in (t.replace('"', "") for t in tokens) if item]
    if not safe:
        return ""
    # 用引号包裹，避免 token 里的 - / 等字符被当作 FTS5 语法
    return " OR ".join(f'"{t}"' for t in safe)


# --------------------------------------------------------------------------
# 切块
# --------------------------------------------------------------------------


def _split_sentences(paragraph: str) -> List[str]:
    """按句末标点切句，**保留分隔符本身**。

    这里刻意不做 strip，也不在拼接时补空格——
    因为 `_SENTENCE_END` 里包含换行符，一旦用空格重新拼接，
    整张 markdown 表格就会被压成一行（表格行没有句末标点，
    会被当成「一个超长句子」），数值信息随之丢失。
    """
    sentences: List[str] = []
    buffer = ""
    for ch in paragraph:
        buffer += ch
        if ch in _SENTENCE_END:
            if buffer.strip():
                sentences.append(buffer)
            buffer = ""
    if buffer.strip():
        sentences.append(buffer)
    return sentences


def _hard_split(text: str, size: int) -> List[str]:
    """超长无标点串（如长 URL 串）按长度硬切。"""
    return [text[i : i + size] for i in range(0, len(text), size)]


# --- 表格识别与按行拆分 -------------------------------------------------
# 表格原语统一放在 app/core/tables.py，避免两处各写一套规则导致不一致
# （历史上就是因为 chunk 与 tables 各自假设「分隔行在第 2 行」而出过错）。


def looks_like_table(block: str) -> bool:
    from . import tables as table_mod

    return table_mod.looks_like_table(block)


def split_table(block: str, size: int) -> List[str]:
    from . import tables as table_mod

    return table_mod.split_table(block, size)


def chunk_text(text: str, size: int = 700, overlap: int = 100) -> List[str]:
    """把长正文切成适合检索与引用的片段。

    - 先按空行分段，段落短于 size 时向后合并；
    - 单段过长时按句子累积切分，并保留 overlap 个字符的重叠；
    - 仍过长（无标点）时按长度硬切。
    """
    size = max(120, int(size))
    overlap = max(0, min(int(overlap), size // 2))
    normalized = normalize(text)
    if not normalized:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", normalized) if p.strip()]
    chunks: List[str] = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            chunks.append(buffer.strip())
        buffer = ""

    for para in paragraphs:
        if len(para) > size:
            flush()
            # 表格按「行」拆，并在每片重复表头；绝不当成句子拼接（那会把整张表压成一行）
            if looks_like_table(para):
                chunks.extend(split_table(para, size))
                continue
            current = ""
            for sentence in _split_sentences(para):
                pieces = _hard_split(sentence, size) if len(sentence) > size else [sentence]
                for piece in pieces:
                    if len(current) + len(piece) <= size:
                        # 直接拼接：piece 自带句末标点/换行，补空格反而会破坏表格与换行结构
                        current = f"{current}{piece}"
                    else:
                        if current:
                            chunks.append(current.strip())
                            tail = current[-overlap:] if overlap else ""
                            current = f"{tail}{piece}" if tail else piece
                        else:
                            chunks.append(piece.strip())
                            current = ""
            if current.strip():
                chunks.append(current.strip())
            continue

        candidate = f"{buffer}\n{para}".strip() if buffer else para
        if len(candidate) <= size:
            buffer = candidate
        else:
            flush()
            buffer = para

    flush()
    return [c for c in chunks if len(c) >= 10]


# --------------------------------------------------------------------------
# SimHash
# --------------------------------------------------------------------------


def simhash64(text: str, bits: int = 64) -> int:
    """64 位 SimHash，用于近似去重。"""
    tokens = tokenize(text)
    if not tokens:
        return 0
    weights: dict[str, int] = {}
    for token in tokens:
        weights[token] = weights.get(token, 0) + 1
    vector = [0] * bits
    for token, weight in weights.items():
        digest = _stable_hash(token)
        for i in range(bits):
            if digest >> i & 1:
                vector[i] += weight
            else:
                vector[i] -= weight
    fingerprint = 0
    for i in range(bits):
        if vector[i] > 0:
            fingerprint |= 1 << i
    return fingerprint


_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_FNV_MASK = 0xFFFFFFFFFFFFFFFF


def _stable_hash(token: str) -> int:
    """FNV-1a 64 位哈希：跨进程/跨版本稳定，保证指纹可比较。"""
    value = _FNV_OFFSET
    for byte in token.encode("utf-8"):
        value ^= byte
        value = (value * _FNV_PRIME) & _FNV_MASK
    return value


def summarize(text: str, limit: int = 160) -> str:
    """生成用于列表展示的短摘要。"""
    clean = re.sub(r"\s+", " ", text or "").strip()
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"
