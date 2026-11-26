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


def test_semantic_split_keeps_sentence_boundaries():
    text = "第一句说明规则。第二句说明例外。\n\n第三段补充流程。"
    pieces = file_parser._split_semantic(text, max_chars=12)
    assert pieces
    assert "".join(pieces).replace("\n", "") == text.replace("\n\n", "").replace("\n", "")
    assert all(len(piece) <= 12 for piece in pieces)


def test_pptx_parser_is_registered():
    assert ".pptx" in file_parser.SUPPORTED
    assert ".pptx" in file_parser._BLOCK_PARSERS


def test_pptx_parser_extracts_slide_text_and_location(tmp_path):
    import zipfile

    slide = """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
        xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>图书借阅流程</a:t></a:r></a:p>
      <a:p><a:r><a:t>读者提交借阅申请</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
    </p:sld>"""
    path = tmp_path / "sample.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", slide)

    blocks = list(file_parser._pptx_blocks(path, limit=5))

    assert blocks
    assert blocks[0].page_no == 1
    assert "图书借阅流程" in blocks[0].text
    assert "读者提交借阅申请" in blocks[0].text
