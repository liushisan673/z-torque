from pathlib import Path

import pytest

from app.core.errors import ApiError
from app.kernel.md_skill import parse_skill
from app.kernel.md_skill_runtime import MdSkillRuntime


@pytest.mark.asyncio
async def test_script_runtime_is_unavailable_by_default():
    manifest = parse_skill(Path("md-skills"), "data-analysis")
    with pytest.raises(ApiError) as exc:
        await MdSkillRuntime("md-skills").execute(manifest, {"values": [1, 2]})
    assert exc.value.code == "AI_CAPABILITY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_script_runtime_validates_input_before_execution():
    manifest = parse_skill(Path("md-skills"), "data-analysis")
    with pytest.raises(ApiError) as exc:
        await MdSkillRuntime("md-skills").execute(manifest, {"values": "not-array"})
    assert exc.value.code == "AI_PARAM_INVALID"
