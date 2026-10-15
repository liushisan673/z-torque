from typing import Any, Literal
from pydantic import BaseModel, Field


class ConnectorCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    connector_type: Literal["REST", "DB", "MCP"]
    config: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None
    target_policy: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None


class ConnectorPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    config: dict[str, Any] | None = None
    secret_ref: str | None = None
    target_policy: dict[str, Any] | None = None
    description: str | None = None


class ConnectorToolRegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    tool_type: Literal["HTTP", "DB_QUERY", "DB_SQL", "MCP"]
    external_name: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    required_permissions: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    # DB_SQL 专用：SQL 模板（只读 SELECT 或 INSERT/UPDATE/DELETE），{{参数}} 会在执行期转为数据库绑定变量；写入语句强制人工审批
    sql_template: str | None = None
