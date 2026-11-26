"""多格式结构化文档解析：保留文档结构与可回查位置。

支持 TXT/Markdown/CSV/PDF/DOCX/XLSX/PPTX。Word/PPTX 按段落、标题、
表格行和幻灯片组织；PDF 按页；表格按行；长文本按段落/句子边界拆分。
"""

from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from app.kernel.ports.knowledge_provider import ParsedBlock, ParsedFile

SUPPORTED = {".txt", ".md", ".csv", ".pdf", ".docx", ".xlsx", ".pptx"}

_LINES_PER_BLOCK = 40
_MAX_PDF_PAGES = 100
_MAX_CSV_ROWS = 10000
_MAX_XLSX_SHEETS = 20
_MAX_XLSX_ROWS = 10000
_MAX_XLSX_COLS = 100
_DEFAULT_MAX_BLOCKS = 4000
_MAX_SEMANTIC_CHARS = 1400


def _split_semantic(text: str, max_chars: int = _MAX_SEMANTIC_CHARS) -> list[str]:
    """优先按段落/句子拆分，避免固定字符截断破坏语义。"""
    normalized = re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not normalized:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", normalized) if part.strip()]
    if not paragraphs:
        paragraphs = [normalized]
    result: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            sentences = re.split(r"(?<=[。！？!?；;])\s*", paragraph)
            pieces = []
            carry = ""
            for sentence in sentences:
                if not sentence:
                    continue
                if carry and len(carry) + len(sentence) > max_chars:
                    pieces.append(carry)
                    carry = ""
                if len(sentence) > max_chars:
                    for offset in range(0, len(sentence), max_chars):
                        pieces.append(sentence[offset:offset + max_chars])
                else:
                    carry += sentence
            if carry:
                pieces.append(carry)
        else:
            pieces = [paragraph]
        for piece in pieces:
            if current and len(current) + len(piece) + 1 > max_chars:
                result.append(current)
                current = ""
            current = f"{current}\n{piece}".strip() if current else piece
    if current:
        result.append(current)
    return result


def _text_blocks(path: Path, *, limit: int):
    """文本按段落语义分块，同时保留近似行号范围。"""
    buffer: list[str] = []
    start = 1
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            buffer.append(line.rstrip("\r\n"))
            if len(buffer) >= _LINES_PER_BLOCK:
                text = "\n".join(buffer)
                for piece in _split_semantic(text):
                    yield ParsedBlock(piece, line_start=start, line_end=line_no)
                start = line_no + 1
                buffer.clear()
        if buffer:
            for piece in _split_semantic("\n".join(buffer)):
                yield ParsedBlock(piece, line_start=start, line_end=start + len(buffer) - 1)


def _csv_blocks(path: Path, *, limit: int):
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for index, row in enumerate(csv.reader(handle)):
            if index >= min(_MAX_CSV_ROWS, limit):
                return
            values = [str(value).strip() for value in row[:_MAX_XLSX_COLS]]
            if any(values):
                yield ParsedBlock(" | ".join(values), line_start=index + 1, line_end=index + 1)


def _pdf_blocks(path: Path, *, limit: int):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF parser unavailable") from exc
    reader = PdfReader(path)
    for index, page in enumerate(reader.pages):
        if index >= min(limit, _MAX_PDF_PAGES):
            return
        text = page.extract_text() or ""
        for piece in _split_semantic(text):
            yield ParsedBlock(piece, page_no=index + 1)


_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _word_paragraph_text(paragraph) -> tuple[str, str | None]:
    text = "".join(node.text or "" for node in paragraph.iter(f"{_WORD_NS}t")).strip()
    style_node = paragraph.find(f"{_WORD_NS}pPr/{_WORD_NS}pStyle")
    style = style_node.get(f"{_WORD_NS}val") if style_node is not None else None
    return text, style


def _docx_blocks(path: Path, *, limit: int):
    """按 Word 原始 body 顺序读取标题、段落与表格；标题会作为后续正文上下文。"""
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find(f"{_WORD_NS}body")
    if body is None:
        return
    headings: list[str] = []
    pending: list[str] = []
    paragraph_no = 0
    emitted = 0

    def flush():
        nonlocal emitted, pending
        if not pending:
            return
        prefix = " / ".join(headings[-3:])
        raw = "\n".join(pending)
        for piece in _split_semantic(raw):
            contextual = (f"所属章节：{prefix}\n" if prefix else "") + piece
            yield ParsedBlock(contextual, line_start=max(1, paragraph_no - len(pending) + 1), line_end=paragraph_no)
            emitted += 1
            if emitted >= limit:
                return
        pending = []

    for child in list(body):
        if child.tag == f"{_WORD_NS}p":
            text, style = _word_paragraph_text(child)
            if not text:
                continue
            paragraph_no += 1
            if style and (style.lower().startswith(("heading", "标题")) or style.lower() in {"title", "subtitle"}):
                yield from flush()
                headings.append(text)
                headings[:] = headings[-3:]
            else:
                pending.append(text)
                if sum(map(len, pending)) >= _MAX_SEMANTIC_CHARS:
                    yield from flush()
            if emitted >= limit:
                return
        elif child.tag == f"{_WORD_NS}tbl":
            yield from flush()
            for row in child.findall(f"{_WORD_NS}tr"):
                cells = []
                for cell in row.findall(f"{_WORD_NS}tc"):
                    value = " ".join(
                        "".join(node.text or "" for node in para.iter(f"{_WORD_NS}t")).strip()
                        for para in cell.findall(f"{_WORD_NS}p")
                    ).strip()
                    cells.append(value)
                row_text = " | ".join(value for value in cells if value)
                if row_text:
                    prefix = " / ".join(headings[-3:])
                    yield ParsedBlock((f"所属章节：{prefix}\n" if prefix else "") + row_text)
                    emitted += 1
                    if emitted >= limit:
                        return
    yield from flush()


_PPT_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _pptx_blocks(path: Path, *, limit: int):
    """按幻灯片读取文本框和表格行，保留幻灯片序号。"""
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.search(r"slide(\d+)", name).group(1)),
        )
        for slide_no, name in enumerate(names[:limit], start=1):
            root = ET.fromstring(archive.read(name))
            slide_parts: list[str] = []
            for shape in root.iter(f"{_PPT_NS}sp"):
                paragraphs = []
                for paragraph in shape.iter(f"{_A_NS}p"):
                    value = "".join(node.text or "" for node in paragraph.iter(f"{_A_NS}t")).strip()
                    if value:
                        paragraphs.append(value)
                if paragraphs:
                    slide_parts.append("\n".join(paragraphs))
            for table in root.iter(f"{_A_NS}tbl"):
                for row in table.findall(f"{_A_NS}tr"):
                    cells = []
                    for cell in row.findall(f"{_A_NS}tc"):
                        value = "".join(node.text or "" for node in cell.iter(f"{_A_NS}t")).strip()
                        cells.append(value)
                    row_text = " | ".join(value for value in cells if value)
                    if row_text:
                        slide_parts.append(row_text)
            raw = "\n\n".join(slide_parts)
            for piece in _split_semantic(raw):
                yield ParsedBlock(f"幻灯片 {slide_no}\n{piece}", page_no=slide_no)


def _xlsx_blocks(path: Path, *, limit: int):
    """按行合并 Excel 单元格，并把列标题带入数据行，避免单元格脱离上下文。"""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValueError("XLSX parser unavailable") from exc
    emitted = 0
    with path.open("rb") as handle:
        workbook = load_workbook(handle, read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets[:_MAX_XLSX_SHEETS]:
                header: list[str] = []
                for row in sheet.iter_rows(max_row=_MAX_XLSX_ROWS, max_col=_MAX_XLSX_COLS):
                    values = [str(cell.value).strip() if cell.value is not None else "" for cell in row]
                    if not any(values):
                        continue
                    if not header:
                        header = values
                        yield ParsedBlock(f"工作表：{sheet.title}\n字段：{' | '.join(v or f'列{i + 1}' for i, v in enumerate(header))}", sheet=sheet.title, row_no=row[0].row, col_no=1)
                    else:
                        pairs = [f"{header[i] if i < len(header) and header[i] else f'列{i + 1}'}：{value}" for i, value in enumerate(values) if value]
                        if pairs:
                            yield ParsedBlock(f"工作表：{sheet.title}\n" + "；".join(pairs), sheet=sheet.title, row_no=row[0].row, col_no=1)
                    emitted += 1
                    if emitted >= limit:
                        return
        finally:
            workbook.close()


_BLOCK_PARSERS = {
    ".txt": _text_blocks, ".md": _text_blocks, ".csv": _csv_blocks,
    ".pdf": _pdf_blocks, ".docx": _docx_blocks, ".xlsx": _xlsx_blocks, ".pptx": _pptx_blocks,
}


def parse_file_structured(
    path: Path, suffix: str, max_chars: int, *, max_blocks: int = _DEFAULT_MAX_BLOCKS,
    max_block_chars: int = 2000,
) -> ParsedFile:
    parser = _BLOCK_PARSERS.get(suffix.lower())
    if parser is None:
        raise ValueError("unsupported file type")
    blocks: list[ParsedBlock] = []
    truncated = False
    hard_limit = min(max_blocks, _MAX_PDF_PAGES if suffix.lower() == ".pdf" else (
        _MAX_CSV_ROWS if suffix.lower() == ".csv" else max_blocks
    ))
    for block in parser(path, limit=hard_limit + 1):
        if len(blocks) >= hard_limit:
            truncated = True
            break
        pieces = _split_semantic(block.text, max_block_chars)
        for piece in pieces:
            if len(blocks) >= hard_limit:
                truncated = True
                break
            blocks.append(ParsedBlock(
                piece, block.page_no, block.sheet, block.row_no, block.col_no,
                block.line_start, block.line_end,
            ))
        if truncated:
            break
    joined = "\n".join(block.text for block in blocks)
    if len(joined) > max_chars:
        remaining = max_chars
        kept: list[ParsedBlock] = []
        for block in blocks:
            if remaining <= 0:
                truncated = True
                break
            value = block.text[:remaining]
            kept.append(ParsedBlock(value, block.page_no, block.sheet, block.row_no, block.col_no, block.line_start, block.line_end))
            remaining -= len(value) + 1
            if len(value) < len(block.text):
                truncated = True
                break
        blocks = kept
        joined = "\n".join(block.text for block in blocks)
    return ParsedFile(joined, tuple(blocks), truncated)


def parse_file(path: Path, suffix: str, max_chars: int) -> str:
    return parse_file_structured(path, suffix, max_chars).text
