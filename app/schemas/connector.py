from typing import Any, Literal
from uuid import UUID

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
    # 业务标签：登记时录入，OpenAPI 导入时以 spec tags 为初值
    tags: list[str] = Field(default_factory=list)
    is_business_tool: bool = False
    # DB_SQL 专用：SQL 模板（只读 SELECT 或 INSERT/UPDATE/DELETE），{{参数}} 会在执行期转为数据库绑定变量；写入语句强制人工审批
    sql_template: str | None = None


class ConnectorToolReconfigureRequest(BaseModel):
    """更新已登记工具的执行配置：生成新发布版本，历史版本与既有 Skill 绑定不受影响。

    工具名称、标签等元数据不在此接口范围（走 PATCH /skills-tools/tools/{id}）。
    """

    external_name: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    required_permissions: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    # DB_SQL 专用：新的 SQL 模板
    sql_template: str | None = None
    # 缺省沿用当前语义（非 DB 工具默认 False，DB 写入语句强制 True）
    requires_confirmation: bool | None = None


class CompositeStep(BaseModel):
    key: str = Field(min_length=1, max_length=32)
    tool_version_id: UUID
    # 参数值支持 $input.<name> 与 $steps.<key>.output[.<path>] 模板
    arguments: dict[str, Any] = Field(default_factory=dict)


class ConnectorCompositeToolRegisterRequest(BaseModel):
    """把同一连接器下的多个已发布工具登记为一个线性业务封装 Tool。"""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)
    steps: list[CompositeStep] = Field(min_length=1, max_length=10)
    # 输出选取：$steps.<key>.output[.<path>]；缺省返回全部步骤结果
    output_path: str | None = None
    requires_confirmation: bool = False


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
