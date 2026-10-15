from pathlib import Path

from app.kernel.md_skill import parse_skill


def test_parse_instruction_skill():
    manifest = parse_skill(Path("md-skills"), "text-summary")
    assert manifest.name == "text-summary"
    assert manifest.implementation == "md_instruction"
    assert manifest.script_files == []
    assert manifest.file_fingerprint


def test_parse_script_skill():
    manifest = parse_skill(Path("md-skills"), "data-analysis")
    assert manifest.implementation == "md_script"
    assert manifest.script_files == ["scripts/summarize.py"]
