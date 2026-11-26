"""OCR image-only courseware PDFs into searchable .md files using Windows OCR."""
import asyncio
import io
import re
from pathlib import Path

import pymupdf
from PIL import Image
from winsdk.windows.globalization import Language
from winsdk.windows.graphics.imaging import BitmapDecoder
from winsdk.windows.media.ocr import OcrEngine
from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

ROOT = Path(r"d:\project\z-torque-community-m1\docs\project-library\初中物理9上_converted")
LOG = ROOT.parent / "_ocr_log.txt"
MAX_PAGES = 50
lines = []


def log(msg):
    text = str(msg)
    lines.append(text)
    print(text, flush=True)
    LOG.write_text("\n".join(lines), encoding="utf-8")


def clean(text: str) -> str:
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


async def ocr_page(engine, page):
    pix = page.get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(buf.getvalue())
    await writer.store_async()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return clean(result.text)


async def main():
    targets = []
    for p in sorted(ROOT.rglob("*.pdf")):
        rel = str(p.relative_to(ROOT))
        if "作业课件" not in rel:
            continue
        targets.append(p)
    log(f"targets: {len(targets)}")

    engine = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
    if engine is None:
        raise RuntimeError("Chinese OCR engine unavailable")

    for i, pdf in enumerate(targets, 1):
        md = pdf.with_suffix(".md")
        if md.exists() and md.stat().st_size > 500:
            log(f"[{i}/{len(targets)}] SKIP exists: {pdf.name}")
            continue
        try:
            doc = pymupdf.open(pdf)
            pages = min(len(doc), MAX_PAGES)
            parts = [f"# {pdf.stem}\n", f"> 由课件《{pdf.stem}》经 OCR 识别生成，共 {len(doc)} 页（本文件识别前 {pages} 页）。\n"]
            for idx in range(pages):
                text = await ocr_page(engine, doc[idx])
                if text:
                    parts.append(f"\n## 第 {idx+1} 页\n\n{text}\n")
            doc.close()
            md.write_text("\n".join(parts), encoding="utf-8")
            total = sum(len(x) for x in parts)
            log(f"[{i}/{len(targets)}] OCR OK: {pdf.name} pages={pages} chars={total}")
        except Exception as exc:
            log(f"[{i}/{len(targets)}] OCR FAIL: {pdf.name} {type(exc).__name__} {str(exc)[:200]}")

    log("\nDONE")


if __name__ == "__main__":
    asyncio.run(main())
