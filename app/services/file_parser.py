"""结构化文件解析（Phase 0 §2.1 前置依赖）。

兼容旧路径：`parse_file()` 仍返回整段 `extracted_text`（截断语义不变）。
新增 `parse_file_structured()` 输出带坐标的 blocks：
- txt/md/csv → line_start/line_end（1-based）
- pdf → page_no（1-based，逐页）
- xlsx → sheet + row_no + col_no（逐非空单元格）

坐标不丢失是 FTS5 chunk 生成「可回查引用」的前提；超限在 ParsedFile.truncated 标记，
由索引层写 doc.truncated + audit，不做静默截断。
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from itertools import islice
from pathlib import Path

from app.kernel.ports.knowledge_provider import ParsedBlock, ParsedFile

SUPPORTED = {".txt", ".md", ".csv", ".pdf", ".xlsx"}

_LINES_PER_BLOCK = 40
_MAX_PDF_PAGES = 100
_MAX_XLSX_SHEETS = 20
_MAX_XLSX_ROWS = 10000
_MAX_XLSX_COLS = 100
_DEFAULT_MAX_BLOCKS = 4000


def _truncate_block(text: str, max_block_chars: int) -> tuple[str, bool]:
    if len(text) <= max_block_chars:
        return text, False
    return text[:max_block_chars], True


def _text_blocks(path: Path) -> Iterator[ParsedBlock]:
    """按固定行数流式读取，不把整个文件一次性载入内存。"""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        line_start = 1
        while lines := list(islice(handle, _LINES_PER_BLOCK)):
            normalized = [line.rstrip("\r\n") for line in lines]
            yield ParsedBlock(
                "\n".join(normalized),
                line_start=line_start,
                line_end=line_start + len(normalized) - 1,
            )
            line_start += len(normalized)


def _csv_blocks(path: Path) -> Iterator[ParsedBlock]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for index, row in enumerate(csv.reader(handle)):
            if index >= _MAX_XLSX_ROWS:
                break
            yield ParsedBlock("\t".join(row[:_MAX_XLSX_COLS]), line_start=index + 1, line_end=index + 1)


def _pdf_blocks(path: Path) -> Iterator[ParsedBlock]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF parser unavailable") from exc
    reader = PdfReader(path)
    for index, page in enumerate(reader.pages):
        if index >= _MAX_PDF_PAGES:
            break
        yield ParsedBlock(page.extract_text() or "", page_no=index + 1)


def _xlsx_blocks(path: Path) -> Iterator[ParsedBlock]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValueError("XLSX parser unavailable") from exc
    # 存储层文件名为无扩展名 UUID，openpyxl 会按后缀拒收；传二进制流按内容校验。
    # 生成器在调用方达到 max_blocks/max_chars 时关闭，finally 确保工作簿句柄释放。
    with path.open("rb") as handle:
        workbook = load_workbook(handle, read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets[:_MAX_XLSX_SHEETS]:
                for row in sheet.iter_rows(max_row=_MAX_XLSX_ROWS, max_col=_MAX_XLSX_COLS):
                    for cell in row:
                        if cell.value is None or str(cell.value).strip() == "":
                            continue
                        yield ParsedBlock(str(cell.value), sheet=sheet.title, row_no=cell.row, col_no=cell.column)
        finally:
            workbook.close()

_BLOCK_PARSERS = {".txt": _text_blocks, ".md": _text_blocks, ".csv": _csv_blocks, ".pdf": _pdf_blocks, ".xlsx": _xlsx_blocks}


def parse_file_structured(
    path: Path,
    suffix: str,
    max_chars: int,
    *,
    max_blocks: int = _DEFAULT_MAX_BLOCKS,
    max_block_chars: int = 2000,
) -> ParsedFile:
    """解析文件并保留坐标；超限时截断并置 `truncated=True`（由调用方落 audit）。"""
    parser = _BLOCK_PARSERS.get(suffix.lower())
    if parser is None:
        raise ValueError("unsupported file type")
    blocks: list[ParsedBlock] = []
    truncated = False
    total_chars = 0
    iterator = iter(parser(path))
    try:
        for block in iterator:
            if len(blocks) >= max_blocks:
                truncated = True
                break
            text, block_truncated = _truncate_block(block.text, max_block_chars)
            truncated = truncated or block_truncated
            if not text.strip():
                continue
            remaining = max_chars - total_chars - (1 if blocks else 0)
            if remaining <= 0:
                truncated = True
                break
            if len(text) > remaining:
                text = text[:remaining]
                truncated = True
            blocks.append(ParsedBlock(text, block.page_no, block.sheet, block.row_no, block.col_no, block.line_start, block.line_end))
            total_chars += len(text) + (1 if len(blocks) > 1 else 0)
            if total_chars >= max_chars:
                # 只探测一个后续块来区分「刚好结束」与「因字符上限截断」，随后立即关闭生成器。
                try:
                    next(iterator)
                    truncated = True
                except StopIteration:
                    pass
                break
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
    joined = "\n".join(block.text for block in blocks)
    return ParsedFile(joined, tuple(blocks), truncated)


def parse_file(path: Path, suffix: str, max_chars: int) -> str:
    """兼容入口：返回整段 extracted_text（旧上传路径语义不变）。"""
    return parse_file_structured(path, suffix, max_chars).text
