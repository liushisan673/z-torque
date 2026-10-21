"""连接器工具注册/更新配置的公共逻辑：DB_SQL 参数骨架自动补全。"""

from uuid import uuid4

from types import SimpleNamespace

from app.api.routes.connectors import _resolve_tool_register_config


def _db_connector():
    return SimpleNamespace(
        id=uuid4(),
        connector_type="DB",
        config={"allowed_tables": ["tombs"], "driver": "sqlite"},
        target_policy={},
    )


def test_db_sql_autofills_parameter_skeleton_from_template():
    """用户不声明参数 Schema 时：{{参数}} 自动按 string 生成（非专业用户兜底）。"""
    snapshot, tool_type, is_write, schema = _resolve_tool_register_config(
        _db_connector(), tool_type="DB_SQL", payload_config={},
        sql_template="SELECT name, price FROM tombs WHERE status = {{status}} LIMIT {{limit}}",
        input_schema={}, external_name=None, fallback_name="q",
    )
    assert tool_type == "DB_SQL"
    assert is_write is False
    assert set(schema["required"]) == {"status", "limit"}
    assert schema["properties"]["status"]["type"] == "string"


def test_db_sql_keeps_user_declared_types_and_descriptions():
    """用户已声明的属性（类型/描述）保留，缺失参数自动补齐，required 合并去重。"""
    _, _tt, _iw, schema = _resolve_tool_register_config(
        _db_connector(), tool_type="DB_SQL", payload_config={},
        sql_template="SELECT name FROM tombs WHERE status = {{status}}",
        input_schema={"type": "object", "properties": {"status": {"type": "integer", "description": "0=在售 1=停售"}}},
        external_name=None, fallback_name="q",
    )
    assert schema["properties"]["status"] == {"type": "integer", "description": "0=在售 1=停售"}
    assert schema["required"] == ["status"]


def test_http_tool_schema_passthrough_untouched():
    """非 DB 工具：入参 Schema 原样透传，不做骨架补全。"""
    schema_in = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    _snapshot, _tt, _iw, schema = _resolve_tool_register_config(
        SimpleNamespace(id=uuid4(), connector_type="REST", config={"base_url": "https://x"}, target_policy={}),
        tool_type="HTTP", payload_config={"method": "GET", "url": "https://x/api"},
        sql_template=None, input_schema=schema_in, external_name=None, fallback_name="w",
    )
    assert schema == schema_in
