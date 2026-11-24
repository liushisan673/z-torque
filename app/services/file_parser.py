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
_MAX_XLSX_SHEETS = 20
_MAX_XLSX_ROWS = 10000
_MAX_XLSX_COLS = 100
_DEFAULT_MAX_BLOCKS = 4000


def _truncate_block(text: str, max_block_chars: int) -> tuple[str, bool]:
    if len(text) <= max_block_chars:
        return text, False
    return text[:max_block_chars], True


def _text_blocks(path: Path) -> list[ParsedBlock]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [
        ParsedBlock("\n".join(lines[start:start + _LINES_PER_BLOCK]), line_start=start + 1, line_end=min(start + _LINES_PER_BLOCK, len(lines)))
        for start in range(0, len(lines), _LINES_PER_BLOCK)
    ]


def _csv_blocks(path: Path) -> list[ParsedBlock]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        rows = list(csv.reader(handle))[:10000]
    return [ParsedBlock("\t".join(row[:100]), line_start=index + 1, line_end=index + 1) for index, row in enumerate(rows)]


def _pdf_blocks(path: Path) -> list[ParsedBlock]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF parser unavailable") from exc
    return [
        ParsedBlock(page.extract_text() or "", page_no=index + 1)
        for index, page in enumerate(PdfReader(path).pages[:_MAX_PDF_PAGES])
    ]


def _xlsx_blocks(path: Path) -> list[ParsedBlock]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValueError("XLSX parser unavailable") from exc
    # 存储层文件名为无扩展名 UUID，openpyxl 会按后缀拒收；传二进制流按内容校验
    blocks: list[ParsedBlock] = []
    with path.open("rb") as handle:
        workbook = load_workbook(handle, read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets[:_MAX_XLSX_SHEETS]:
                for row in sheet.iter_rows(max_row=_MAX_XLSX_ROWS, max_col=_MAX_XLSX_COLS):
                    for cell in row:
                        if cell.value is None or str(cell.value).strip() == "":
                            continue
                        blocks.append(ParsedBlock(str(cell.value), sheet=sheet.title, row_no=cell.row, col_no=cell.column))
        finally:
            workbook.close()
    return blocks


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
    for block in parser(path):
        if len(blocks) >= max_blocks:
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
