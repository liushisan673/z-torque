from app.kernel.ports.knowledge_provider import ParsedBlock
from app.services import file_parser


def test_structured_parser_stops_after_one_over_limit_block(tmp_path, monkeypatch):
    generated = 0

    def blocks(_path, *, limit):
        nonlocal generated
        for index in range(1000):
            generated += 1
            yield ParsedBlock(f"block-{index}")

    monkeypatch.setitem(file_parser._BLOCK_PARSERS, ".xlsx", blocks)
    result = file_parser.parse_file_structured(
        tmp_path / "large.xlsx",
        ".xlsx",
        max_chars=10000,
        max_blocks=3,
    )

    assert len(result.blocks) == 3
    assert result.truncated is True
    # 只额外消费一个 block 来判断是否截断，而不是把全部 1,000 个 block 生成出来。
    assert generated == 4
