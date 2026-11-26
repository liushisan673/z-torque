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


def test_parse_book_publishing_skill_pack():
    root = Path("md-skills")
    instruction_skills = [
        "book-publishing-workflow",
        "curriculum-standards-analysis",
        "field-research",
        "typesetting-production",
        "proofreader",
        "responsible-editor-review",
    ]
    for name in instruction_skills:
        manifest = parse_skill(root, name)
        assert manifest.implementation == "md_instruction"
        assert manifest.input_schema["type"] == "object"
    checks = parse_skill(root, "book-publishing-deterministic-checks")
    assert checks.implementation == "md_script"
    assert checks.windows_trusted is True
    assert checks.script_files == ["scripts/checks.py"]
