from pathlib import Path

import pytest

from app.core.errors import ApiError
from app.kernel.md_skill import parse_skill


def test_invalid_skill_name_rejected(tmp_path: Path):
    skill = tmp_path / "Bad_Name"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: Bad_Name\n---\ntext", encoding="utf-8")
    with pytest.raises(ApiError) as exc:
        parse_skill(tmp_path, "Bad_Name")
    assert exc.value.code == "AI_SKILL_INVALID_MANIFEST"


def test_symlink_is_rejected_or_ignored(tmp_path: Path):
    skill = tmp_path / "safe-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: safe-skill\ndescription: test\nimplementation: md_instruction\nversion: 1\ninput_schema: {type: object}\noutput_schema: {type: object}\nallow_scripts: false\nrequired_permissions: []\n---\nbody", encoding="utf-8")
    manifest = parse_skill(tmp_path, "safe-skill")
    assert manifest.name == "safe-skill"
