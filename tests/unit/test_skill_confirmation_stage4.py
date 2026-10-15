from uuid import uuid4

from app.kernel.skill_confirmation import _call_hash


def test_checkpoint_call_hash_is_canonical():
    first = {"tool_version_id": str(uuid4()), "arguments": {"b": 2, "a": 1}}
    second = {"arguments": {"a": 1, "b": 2}, "tool_version_id": first["tool_version_id"]}
    assert _call_hash(first) == _call_hash(second)
