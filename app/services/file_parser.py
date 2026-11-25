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
from pathlib import Path

from app.kernel.ports.knowledge_provider import ParsedBlock, ParsedFile

SUPPORTED = {".txt", ".md", ".csv", ".pdf", ".xlsx"}

_LINES_PER_BLOCK = 40
_MAX_PDF_PAGES = 100
_MAX_CSV_ROWS = 10000
_MAX_XLSX_SHEETS = 20
_MAX_XLSX_ROWS = 10000
_MAX_XLSX_COLS = 100
_DEFAULT_MAX_BLOCKS = 4000


def _truncate_block(text: str, max_block_chars: int) -> tuple[str, bool]:
    if len(text) <= max_block_chars:
        return text, False
    return text[:max_block_chars], True


def _text_blocks(path: Path, *, limit: int):
    """按行流式分块；只读取 max_blocks + 1 个 block，避免整文件读入内存。"""
    block_lines: list[str] = []
    block_start = 1
    emitted = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            block_lines.append(line.rstrip("\r\n"))
            if len(block_lines) < _LINES_PER_BLOCK:
                continue
            yield ParsedBlock("\n".join(block_lines), line_start=block_start, line_end=line_no)
            emitted += 1
            if emitted >= limit:
                return
            block_start = line_no + 1
            block_lines.clear()
    if block_lines and emitted < limit:
        yield ParsedBlock("\n".join(block_lines), line_start=block_start, line_end=block_start + len(block_lines) - 1)


def _csv_blocks(path: Path, *, limit: int):
    """CSV 按行惰性读取；不再先构造最多 1 万行的完整 list。"""
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for index, row in enumerate(csv.reader(handle)):
            if index >= min(_MAX_CSV_ROWS + 1, limit):
                return
            yield ParsedBlock("\t".join(row[:_MAX_XLSX_COLS]), line_start=index + 1, line_end=index + 1)


def _pdf_blocks(path: Path, *, limit: int):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF parser unavailable") from exc
    reader = PdfReader(path)
    for index, page in enumerate(reader.pages):
        if index >= limit:
            return
        yield ParsedBlock(page.extract_text() or "", page_no=index + 1)


def _xlsx_blocks(path: Path, *, limit: int):
    """只产出需要的非空单元格；旧实现会先把 20×10000×100 个单元格装入 list。"""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValueError("XLSX parser unavailable") from exc
    # 存储层文件名为无扩展名 UUID，openpyxl 会按后缀拒收；传二进制流按内容校验
    emitted = 0
    with path.open("rb") as handle:
        workbook = load_workbook(handle, read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets[:_MAX_XLSX_SHEETS]:
                for row in sheet.iter_rows(max_row=_MAX_XLSX_ROWS, max_col=_MAX_XLSX_COLS):
                    for cell in row:
                        if cell.value is None or str(cell.value).strip() == "":
                            continue
                        yield ParsedBlock(str(cell.value), sheet=sheet.title, row_no=cell.row, col_no=cell.column)
                        emitted += 1
                        if emitted >= limit:
                            return
        finally:
            workbook.close()


_BLOCK_PARSERS = {
    ".txt": _text_blocks,
    ".md": _text_blocks,
    ".csv": _csv_blocks,
    ".pdf": _pdf_blocks,
    ".xlsx": _xlsx_blocks,
}

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
    hard_limit = min(
        max_blocks,
        _MAX_PDF_PAGES if suffix.lower() == ".pdf" else (
            _MAX_CSV_ROWS if suffix.lower() == ".csv" else max_blocks
        ),
    )
    # 额外读取一个 block 用于判断是否因 max_blocks 截断；解析器按需产出，不预先构造全量结果。
    for block in parser(path, limit=hard_limit + 1):
        if len(blocks) >= hard_limit:
            truncated = True
            break
        text, block_truncated = _truncate_block(block.text, max_block_chars)
        truncated = truncated or block_truncated
        if not text.strip():
            continue
        blocks.append(ParsedBlock(text, block.page_no, block.sheet, block.row_no, block.col_no, block.line_start, block.line_end))
    joined = "\n".join(block.text for block in blocks)
    if len(joined) > max_chars:
        joined = joined[:max_chars]
        truncated = True
    return ParsedFile(joined, tuple(blocks), truncated)


def parse_file(path: Path, suffix: str, max_chars: int) -> str:
    """兼容入口：返回整段 extracted_text（旧上传路径语义不变）。"""
    return parse_file_structured(path, suffix, max_chars).text
