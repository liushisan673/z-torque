from pathlib import Path
from uuid import uuid4

from app.builtin_plugins.working_memory import InProcessWorkingMemory
from app.services.file_parser import parse_file


def test_working_memory_isolated_by_user_and_conversation():
    memory = InProcessWorkingMemory(max_entries=2)
    tenant, user_a, user_b, conversation = uuid4(), uuid4(), uuid4(), uuid4()
    memory.put(tenant, user_a, conversation, {"value": "private"})
    assert memory.get(tenant, user_a, conversation) == {"value": "private"}
    assert memory.get(tenant, user_b, conversation) is None


def test_text_parser_limits_extracted_content(tmp_path: Path):
    path = tmp_path / "input.txt"
    path.write_text("abcdef", encoding="utf-8")
    assert parse_file(path, ".txt", 3) == "abc"