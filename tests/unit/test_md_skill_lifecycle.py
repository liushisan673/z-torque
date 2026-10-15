from uuid import uuid4

import pytest

from app.core.errors import ApiError
from app.kernel.skill_service import publish_skill_version


class DummyQuery:
    def __init__(self, rows=None, scalar_value=None):
        self.rows = rows or []
        self.scalar_value = scalar_value

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self.rows

    def update(self, *args, **kwargs):
        return 1


class DummyDb:
    def __init__(self, version, skill):
        self.version = version
        self.skill = skill

    def get(self, model, item_id):
        return self.version if item_id == self.version.id else self.skill

    def scalar(self, statement):
        return self.skill

    def query(self, model):
        return DummyQuery()


class Obj:
    pass


def test_md_script_publish_requires_allow_scripts_and_files():
    version = Obj()
    version.id = uuid4()
    version.tenant_id = uuid4()
    version.skill_id = uuid4()
    version.status = "DRAFT"
    version.config_snapshot = {"implementation": "md_script", "allow_scripts": False, "script_files": []}
    version.instructions = "run"
    version.script_source = None
    version.required_permissions = []
    skill = Obj()
    skill.id = version.skill_id
    skill.tenant_id = version.tenant_id
    skill.owner_user_id = uuid4()
    skill.source_type = "USER"
    skill.status = "ACTIVE"
    skill.official = False

    with pytest.raises(ApiError) as error:
        publish_skill_version(DummyDb(version, skill), tenant_id=version.tenant_id, user_id=skill.owner_user_id, version_id=version.id)
    assert error.value.code == "AI_SKILL_INVALID_MANIFEST"
