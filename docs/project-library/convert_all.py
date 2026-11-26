"""Convert all .doc/.ppt/.pptx under 初中物理9上 into platform-supported formats.
Keeps original directory hierarchy under 初中物理9上_converted.
"""
import shutil
import sys
import time
from pathlib import Path

import win32com.client as win32

ROOT = Path(r"d:\project\z-torque-community-m1\docs\project-library")
SRC = ROOT / "初中物理9上"
DST = ROOT / "初中物理9上_converted"
LOG = ROOT / "_convert_log.txt"

WORD_EXTS = {".doc", ".docx"}
PPT_EXTS = {".ppt", ".pptx"}
SUPPORTED = {".docx", ".pdf", ".txt", ".md", ".csv", ".xlsx"}

lines = []


def log(msg):
    text = str(msg)
    lines.append(text)
    print(text, flush=True)
    LOG.write_text("\n".join(lines), encoding="utf-8")


def main():
    DST.mkdir(exist_ok=True)
    word = None
    ppt = None
    converted = skipped = failed = 0

    files = sorted(p for p in SRC.rglob("*") if p.is_file())
    log(f"total files: {len(files)}")

    for i, src in enumerate(files, 1):
        rel = src.relative_to(SRC)
        suffix = src.suffix.lower()

        if suffix == ".downloading" or ".downloading" in src.name:
            skipped += 1
            log(f"[{i}/{len(files)}] SKIP downloading: {rel}")
            continue

        if suffix in SUPPORTED:
            target = DST / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            skipped += 1
            log(f"[{i}/{len(files)}] COPY {suffix}: {rel}")
            continue

        if suffix not in WORD_EXTS and suffix not in PPT_EXTS:
            skipped += 1
            log(f"[{i}/{len(files)}] SKIP unsupported: {rel}")
            continue

        is_word = suffix in WORD_EXTS
        new_suffix = ".docx" if is_word else ".pdf"
        target = (DST / rel).with_suffix(new_suffix)
        target.parent.mkdir(parents=True, exist_ok=True)

        for attempt in range(3):
            try:
                if is_word:
                    if word is None:
                        word = win32.gencache.EnsureDispatch("Word.Application")
                        word.Visible = False
                        word.DisplayAlerts = 0
                    doc = word.Documents.Open(str(src), ReadOnly=True)
                    doc.SaveAs(str(target), FileFormat=16)
                    doc.Close(False)
                else:
                    if ppt is None:
                        ppt = win32.gencache.EnsureDispatch("PowerPoint.Application")
                    pres = ppt.Presentations.Open(str(src), WithWindow=False)
                    pres.SaveAs(str(target), FileFormat=32)
                    pres.Close()
                converted += 1
                log(f"[{i}/{len(files)}] CONVERT {suffix}->{new_suffix}: {rel} ({target.stat().st_size/1024/1024:.2f}MB)")
                break
            except Exception as exc:
                log(f"[{i}/{len(files)}] attempt {attempt+1} FAILED {rel}: {type(exc).__name__} {str(exc)[:200]}")
                if is_word:
                    try:
                        word.Quit()
                    except Exception:
                        pass
                    word = None
                else:
                    try:
                        ppt.Quit()
                    except Exception:
                        pass
                    ppt = None
                time.sleep(2)
        else:
            failed += 1
            log(f"[{i}/{len(files)}] GIVE UP: {rel}")

    try:
        if word:
            word.Quit()
    except Exception:
        pass
    try:
        if ppt:
            ppt.Quit()
    except Exception:
        pass

    log(f"\nDONE converted={converted} copied/skipped={skipped} failed={failed}")
    out = [p for p in DST.rglob("*") if p.is_file()]
    log(f"output files={len(out)}")


if __name__ == "__main__":
    main()
