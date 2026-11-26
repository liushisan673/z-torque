"""会话内置文档输出：把文本内容生成 md / pdf / docx 文件。

仅使用标准库，便于在没有 Office/LaTeX 依赖的环境里直接运行。
PDF 使用 Adobe 中文字体 STSong-Light（非嵌入字体，主流 PDF 阅读器可显示中文）。
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

FORMATS = {"md", "pdf", "docx"}
MEDIA_TYPES = {
    "md": "text/markdown",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def safe_filename(name: str, fmt: str) -> str:
    base = re.sub(r"[\\/:*?\"<>|\r\n]", "_", (name or "document").strip())[:120] or "document"
    return base if base.lower().endswith(f".{fmt}") else f"{base}.{fmt}"


def _escape_xml(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _wrap(value: str, width: int = 90) -> list[str]:
    lines: list[str] = []
    for raw in value.splitlines() or [""]:
        while len(raw) > width:
            lines.append(raw[:width])
            raw = raw[width:]
        lines.append(raw)
    return lines


def _pdf(value: str) -> bytes:
    lines = _wrap(value)
    commands = ["BT", "/F1 12 Tf", "50 780 Td"]
    for line in lines:
        hex_text = line.encode("utf-16-be").hex()
        commands.append(f"<{hex_text}> Tj")
        commands.append("0 -16 Td")
    commands.append("ET")
    stream = ("\n".join(commands) + "\n").encode("ascii")
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /UniGB-UCS2-H /DescendantFonts [6 0 R] >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream",
        "<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light /CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 2 >> /DW 1000 >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        if isinstance(body, str):
            body = body.encode("ascii")
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


def _docx(value: str) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t xml:space='preserve'>{_escape_xml(line)}</w:t></w:r></w:p>"
        for line in (value.splitlines() or [""])
    )
    document = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
        "<Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/>"
        "<Default Extension='xml' ContentType='application/xml'/>"
        "<Override PartName='/word/document.xml' ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/>"
        "</Types>"
    )
    rels = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
        "<Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='word/document.xml'/>"
        "</Relationships>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def build_document(fmt: str, filename: str, content: str) -> tuple[bytes, str, str]:
    """返回 (字节, 安全文件名, media_type)。"""
    fmt = (fmt or "").lower()
    if fmt not in FORMATS:
        raise ValueError("unsupported export format")
    name = safe_filename(filename, fmt)
    value = content or ""
    if fmt == "md":
        return value.encode("utf-8"), name, MEDIA_TYPES[fmt]
    if fmt == "pdf":
        return _pdf(value), name, MEDIA_TYPES[fmt]
    return _docx(value), name, MEDIA_TYPES[fmt]


def suffix_for(fmt: str) -> str:
    return f".{fmt.lower()}"
