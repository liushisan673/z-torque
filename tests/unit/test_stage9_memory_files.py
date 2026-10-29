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


def test_xlsx_parser_accepts_suffixless_storage_key(tmp_path: Path):
    """存储层用无扩展名 UUID 落盘：解析器必须按内容而非文件名后缀识别格式。"""
    from openpyxl import Workbook

    source = tmp_path / "source.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["name", "value"])
    worksheet.append(["alpha", 1])
    workbook.save(source)

    stored = tmp_path / "01a181a1-0000-7000-8000-000000000000"
    stored.write_bytes(source.read_bytes())
    assert "alpha" in parse_file(stored, ".xlsx", 1000)