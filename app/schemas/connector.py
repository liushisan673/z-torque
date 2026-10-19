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


# OpenAPI 批量导入：把外部服务的 OpenAPI 3.x 规范一次性转成 N 个 HTTP_API 工具
# 仅适用 REST 类型连接器；URL 与内联 spec 二选一
class ConnectorOpenapiImportRequest(BaseModel):
    source: Literal["url", "json"] = "url"
    # url 模式：从公网拉取 spec.json；服务端只允许 https，且 host 在 settings.rest_allowed_hosts 之内
    url: str | None = None
    # json 模式：把完整 OpenAPI 3.x 文档直接 POST 进来
    spec: dict[str, Any] | None = None
    # 仅导入这些 tag（可选）；为空表示全部
    only_tags: list[str] = Field(default_factory=list)
    # 仅导入这些 HTTP 方法（默认 GET/POST/PUT/PATCH/DELETE）
    only_methods: list[str] = Field(default_factory=list)
    # 名称前缀：所有注册出的 tool 名称会带上此前缀，避免重名
    name_prefix: str = Field(default="", max_length=32)
