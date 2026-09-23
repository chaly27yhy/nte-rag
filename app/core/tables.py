"""表格 → 结构化知识条目（不依赖模型）。

背景
----
实测发现知识库里只有「噬心诡刃怎么获得」，没有「满级攻击力多少」——
因为数值都藏在**表格**里，而模型抽取时只会挑「信息量最大的几条」，
密集的数值表往往被整表忽略。

这里用**确定性规则**把表格逐行转成条目：
- 零模型成本、不丢数值、可重复；
- 与模型抽取互为补充：模型负责叙述性知识，这里负责数值/清单类知识。

表格形态来自 trafilatura（把 HTML 表格转成 markdown 风格）：
    | 角色 | 名称 | 稀有度 | 属性 | 类型 | 战斗类型 |
    |---|---|---|---|---|---|
    |  | 九原 |  |  | 输出 | 爆发输出，控制 |
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")

# 只由数字/日期/标点组成的单元格——这种是**数据**而不是列名
_NUMERIC_CELL = re.compile(r"^[\d\s.,%+/\-年月日时分秒:：()（）]+$")

# 列名里不会出现句读和书名号；出现就说明这一行是正文/数据
_HEADER_PUNCT = re.compile(r"[。！？，、；「」【】《》…]")

# 表格单元格里经常混进图片/图标的文件名——那是装饰，不是知识
# （实测道具图鉴的「稀有度」列抽出来是「文件:B标识.png」）
_NOISE_VALUE = re.compile(r"^(文件|File|Image|图片)\s*[:：]|\.(png|jpe?g|gif|svg|webp|ico)\b", re.I)

# 列名的长度上限（`类型 [联动注 1]` 这种约 10 字，20 已很宽松）
_MAX_HEADER_CELL = 20

# 单条条目的最大长度，超过就截断（避免把整行塞成一条）
_MAX_ANSWER = 300


def is_table_row(line: str) -> bool:
    return bool(_TABLE_ROW.match(line or ""))


def is_separator_row(line: str) -> bool:
    """判断是不是 markdown 表格的分隔行（`|---|---|` 或 `| --- | --- |`）。

    不能只用正则匹配「一堆 - 和 |」——那样空行 `| | |` 也会被误判。
    这里按单元格判断：每个单元格要么为空，要么只由 `-` / `:` 组成，且至少有一个含 `-`。
    """
    if not is_table_row(line):
        return False
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    if not any(cells):
        return False
    if not any("-" in cell for cell in cells):
        return False
    return all(cell == "" or set(cell) <= set("-:") for cell in cells)


def split_cells(line: str) -> List[str]:
    return [cell.strip() for cell in (line or "").strip().strip("|").split("|")]


def _looks_like_header(cells: Sequence[str]) -> bool:
    """判断一行像不像「列名」。

    分块会把表格从中间切开，切片开头的行其实是**数据行**
    （实测 `| 斯特利速递 | |`、`| 2024年 | | |` 都被当成了表头），
    于是「列名」变成日期/数值，抽出来的条目全是错位垃圾。
    这里要求至少有一个非空单元格，且不能全是数字/日期；
    同时列名必须**短且没有句读**——实测被切碎的表格残片会把
    `| 12月23日 | 「共存测试」…招募pv公开。 |` 这种正文行当表头。
    """
    filled = [cell.strip() for cell in cells if cell.strip()]
    if not filled:
        return False
    if all(_NUMERIC_CELL.match(cell) for cell in filled):
        return False
    if any(len(cell) > _MAX_HEADER_CELL for cell in filled):
        return False
    if any(_HEADER_PUNCT.search(cell) for cell in filled):
        return False
    return True


def find_table_at(lines: Sequence[str], start: int = 0) -> Optional[Tuple[int, int, int]]:
    """在 `lines[start:]` 里定位第一张表，返回 (表头行, 分隔行, 数据结束行) 三个下标。

    为什么不能用「第一个分隔行」：BWIKI 的真实结构是

        | 添加弧盘 | |                  ← 按钮行（2 列）
        |---|---|                       ← 按钮行自己的分隔行（诱饵）
        | 弧盘名 | 稀有度 | 效果 | 描述 | ← 真表头（4 列）
        |---|---|---|---|                ← 真分隔行
        | 该死的邂逅 | A | … | … |

    按「第一个分隔行」解析，表头会变成 `['添加弧盘','']`，于是每一行的
    列名都对不上、全部被丢弃——这正是「弧盘图鉴抓到了却 0 条数值条目」的原因。

    这里的做法：把每个分隔行都当成候选，选**列数最吻合数据行**的那个。
    """
    separators = [i for i, line in enumerate(lines) if i >= start and is_separator_row(line)]

    best: Optional[Tuple[int, int, int]] = None
    best_score = 0
    for index in separators:
        if index == 0:
            continue
        header = split_cells(lines[index - 1])
        if not _looks_like_header(header):
            continue
        nxt = next((item for item in separators if item > index), len(lines))
        # 下一个分隔行的**上一行**通常是下一张表的表头：那是下一张表的行，不是本表的数据行。
        # 旧实现直接把 `nxt` 当本表数据结束位置，于是下一张表的表头被当成本表的一行数据，
        # 产出以列名为标题、confidence 0.9 的假事实（`c（P）` / `b：d`）。
        # 只有当那一行确实像表头时才把边界前移一行让给下一张表，否则本表一直吃到 `nxt`。
        end = nxt
        if nxt < len(lines) and nxt - 1 > index:
            candidate = lines[nxt - 1]
            if not is_separator_row(candidate) and _looks_like_header(split_cells(candidate)):
                end = nxt - 1
        score = 0
        for cursor in range(index + 1, end):
            cells = split_cells(lines[cursor])
            if not any(cells):
                continue
            diff = abs(len(cells) - len(header))
            if diff == 0:
                score += 2
            elif diff == 1:
                score += 1      # 末尾多/少一个空单元格很常见，宽容处理
        if score > best_score:
            best, best_score = (index - 1, index, end), score
    return best


def looks_like_table(block: str) -> bool:
    """判断一个块是不是 markdown 表格（trafilatura 会把 HTML 表格转成这种形式）。"""
    lines = [line for line in (block or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    rows = sum(1 for line in lines if is_table_row(line))
    return rows >= max(2, len(lines) * 0.5)


def table_density(text: str) -> float:
    """表格行占全文的比例，用于判断这是不是「数值型页面」。"""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return 0.0
    rows = sum(1 for line in lines if is_table_row(line))
    return rows / len(lines)


def split_table(block: str, size: int) -> List[str]:
    """把超长表格按**行**拆分，并在每一片里重复表头。

    这样每个切片都是自解释的（能看到列名），检索命中后模型才知道
    「输出 / 爆发输出」这些值属于哪一列。
    """
    lines = [line for line in (block or "").splitlines() if line.strip()]
    if not lines:
        return []
    found = find_table_at(lines)
    if found:
        head_index, separator, end = found
        header = lines[head_index : separator + 1]   # 表头 + 分隔行
        body = lines[separator + 1 : end]
        rest = lines[end:]
    else:
        header, body, rest = lines[:1], lines[1:], []
    if not body:
        return [block]

    header_len = sum(len(item) + 1 for item in header)
    pieces: List[str] = []
    current: List[str] = []
    length = header_len
    for row in body:
        if current and length + len(row) + 1 > size:
            pieces.append("\n".join(header + current))
            current, length = [], header_len
        current.append(row)
        length += len(row) + 1
    if current:
        pieces.append("\n".join(header + current))
    if rest:
        pieces.extend(split_table("\n".join(rest), size))
    return pieces or [block]


def parse_tables(text: str) -> List[Tuple[List[str], List[List[str]]]]:
    """从文本里解析出所有 markdown 表格，返回 [(表头, 数据行)]。

    定位表头交给 `find_table_at`（它会挑出列数最吻合的那个分隔行），
    这里只负责把数据行整理干净。
    """
    tables: List[Tuple[List[str], List[List[str]]]] = []
    lines = (text or "").splitlines()
    index = 0
    while index < len(lines):
        found = find_table_at(lines, index)
        if not found:
            break
        head_index, separator, end = found
        header = split_cells(lines[head_index])

        body: List[List[str]] = []
        for line in lines[separator + 1 : end]:
            row = split_cells(line)
            if not any(row):
                continue
            if row == header:            # 有的表格每 N 行重复一次表头
                continue
            if is_separator_row(line):
                continue
            body.append(row)
        if header and body:
            tables.append((header, body))

        if end >= len(lines):
            break
        # 第三个下标是「本表数据结束位置」，也就是下一张表的表头（已在上面让出来），从它继续
        index = max(end, separator + 1)
    return tables


# 表头里出现这些词说明该列只是装饰/导航，没有信息价值
_NOISE_HEADERS = {
    "",
    "-",
    "---",
    "序号",
    "添加角色",
    "添加弧盘",
    "添加装备",
    "添加卡带",
    "添加道具",
    "图片",
    "图标",
    "头像",
    "操作",
}


def _row_to_fact(
    header: Sequence[str],
    row: Sequence[str],
    page_title: str,
) -> Dict[str, Any] | None:
    pairs = [
        (head, value)
        for head, value in zip(header, row)
        if value
        and head not in _NOISE_HEADERS
        and value != head
        and not _NOISE_VALUE.search(value)
    ]
    if len(pairs) < 2:
        return None  # 只有一格信息，价值太低

    # 用第一个有效列（通常是名称列）作为条目标题
    key = pairs[0][1]
    if len(key) > 24:
        key = key[:24]

    detail = "；".join(f"{head}：{value}" for head, value in pairs[1:])
    if not detail:
        return None
    answer = f"{page_title} 中「{key}」的详细信息：{detail}。"
    if len(answer) > _MAX_ANSWER:
        answer = answer[:_MAX_ANSWER] + "…"

    return {
        "title": f"{key}（{page_title}）"[:40],
        "answer": answer,
        "tags": ", ".join([key] + [head for head, _ in pairs[1:4]]),
        "confidence": 0.9,
        # 提取方式：调用方（ingest.store_table_facts）据此派生可信度，
        # 并写入 facts.extraction，便于事后统计「多少条是模型摘写的」。
        "extraction": "table",
    }


def extract_table_facts(
    text: str,
    page_title: str,
    max_tables: int = 8,
    max_rows_per_table: int = 60,
) -> List[Dict[str, Any]]:
    """把页面里的表格逐行转成知识条目。

    返回的每条都带 title / answer / tags / confidence，
    由调用方（ingest）负责去重与入库。
    """
    facts: List[Dict[str, Any]] = []
    for header, rows in parse_tables(text)[:max_tables]:
        for row in rows[:max_rows_per_table]:
            fact = _row_to_fact(header, row, page_title)
            if fact:
                facts.append(fact)
    return facts
