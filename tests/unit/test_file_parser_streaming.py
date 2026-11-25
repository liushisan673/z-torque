from app.kernel.ports.knowledge_provider import ParsedBlock
from app.services.file_parser import _BLOCK_PARSERS, parse_file_structured


def test_parser_stops_after_block_limit_and_closes_generator(tmp_path, monkeypatch):
    consumed = []
    closed = []

    def parser(_path):
        try:
            for index in range(100):
                consumed.append(index)
                yield ParsedBlock(f"block-{index}")
        finally:
            closed.append(True)

    monkeypatch.setitem(_BLOCK_PARSERS, ".txt", parser)
    result = parse_file_structured(tmp_path / "unused.txt", ".txt", 10_000, max_blocks=2)
    assert len(result.blocks) == 2
    assert result.truncated is True
    assert len(consumed) == 3  # 仅多读一个块以确认达到上限
    assert closed == [True]


def test_parser_enforces_total_character_limit_incrementally(tmp_path, monkeypatch):
    consumed = []

    def parser(_path):
        for index, value in enumerate(("12345", "67890", "never-read")):
            consumed.append(index)
            yield ParsedBlock(value)

    monkeypatch.setitem(_BLOCK_PARSERS, ".txt", parser)
    result = parse_file_structured(tmp_path / "unused.txt", ".txt", 7, max_blocks=100)
    assert result.text == "12345\n6"
    assert len(result.text) <= 7
    assert result.truncated is True
    assert consumed == [0, 1]


def test_text_parser_streams_and_preserves_line_coordinates(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("\n".join(f"line-{index}" for index in range(1, 45)), encoding="utf-8")
    result = parse_file_structured(path, ".md", 10_000, max_blocks=10)
    assert len(result.blocks) == 2
    assert result.blocks[0].line_start == 1
    assert result.blocks[0].line_end == 40
    assert result.blocks[1].line_start == 41
    assert result.blocks[1].line_end == 44
