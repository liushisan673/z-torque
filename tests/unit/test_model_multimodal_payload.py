"""模型网关多模态 payload：有/无 content_parts 的 OpenAI 兼容形态。"""

from app.builtin_plugins.model_gateway import OpenAICompatibleGateway
from app.kernel.model_gateway.contracts import ModelContentPart, ModelMessage


def test_plain_text_message_unchanged():
    message = ModelMessage("user", "你好")
    assert OpenAICompatibleGateway._message(message) == {"role": "user", "content": "你好"}


def test_content_parts_emit_openai_content_array():
    message = ModelMessage(
        "user",
        "帮我看看这张图",
        content_parts=[ModelContentPart("image_url", image_url="data:image/jpeg;base64,AAAA")],
    )
    payload = OpenAICompatibleGateway._message(message)
    assert payload["content"] == [
        {"type": "text", "text": "帮我看看这张图"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
    ]


def test_empty_text_emits_only_image_part():
    message = ModelMessage(
        "user",
        "",
        content_parts=[ModelContentPart("image_url", image_url="data:image/png;base64,BBBB")],
    )
    payload = OpenAICompatibleGateway._message(message)
    assert payload["content"] == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}]


def test_explicit_text_part_is_not_duplicated():
    message = ModelMessage(
        "user",
        "fallback",
        content_parts=[
            ModelContentPart("text", text="显式文本"),
            ModelContentPart("image_url", image_url="data:image/jpeg;base64,CCCC"),
        ],
    )
    payload = OpenAICompatibleGateway._message(message)
    assert payload["content"][0] == {"type": "text", "text": "显式文本"}
