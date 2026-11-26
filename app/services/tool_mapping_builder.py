"""工具映射构建：AgentVersion 绑定的 SkillVersion/ToolVersion → 模型可见工具定义与执行映射。

供 TaskRuntime（worker 执行链路）与流式聊天分流（/chat/stream 初始模型判断）共用，
保证两条链路暴露给模型的工具集合完全一致。
"""
import json
from uuid import UUID

from sqlalchemy import select

from app.config import get_settings

from app.models import (
    Agent,
    AgentVersion,
    AgentVersionSkill,
    Skill,
    SkillVersion,
    SkillVersionTool,
    Tool,
    ToolVersion,
)
from app.repositories import visible_owner_clause
from app.services.function_calling import ToolMapping, safe_function_name


def _sanitize_schema(node):
    """递归剥离模型不可解析的 $ref（OpenAPI 导入残留的 '#/components/...' 悬空引用
    会被上游以 400 拒绝："Invalid schema for function ... Pointer does not exist"，
    并会使执行侧 jsonschema 校验抛 RefResolutionError）。

    含 $ref 的子树替换为 object 类型并保留 description；未引用 $ref 的 schema 原样通过。
    """
    if isinstance(node, dict):
        if "$ref" in node:
            cleaned: dict = {"type": "object"}
            if node.get("description"):
                cleaned["description"] = node["description"]
            return cleaned
        return {key: _sanitize_schema(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_sanitize_schema(item) for item in node]
    return node


def _dependency_ordered_links(links: list) -> list:
    """按 depends_on 依赖优先排序；排序键回退到既有 sort_order，保证稳定前缀。"""
    by_id = {str(link.tool_version_id): link for link in links}
    ordered: list = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visited or key not in by_id:
            return
        if key in visiting:
            return
        visiting.add(key)
        for dep in by_id[key].depends_on or []:
            visit(str(dep))
        visiting.discard(key)
        visited.add(key)
        ordered.append(by_id[key])

    for link in links:
        visit(str(link.tool_version_id))
    return ordered


def build_tool_mappings(db, agent_version: AgentVersion | None, tenant_id: UUID, *, exclude_requires_confirmation: bool = False, user_id: UUID | None = None) -> tuple[dict[str, ToolMapping], list[dict]]:
    if agent_version is None:
        return {}, []
    # Phase 2 属主收口：预取当前用户可见的 Skill / Tool id 集合，避免循环内逐条查询。
    # user_id=None 仅用于无用户上下文的内核内部调用；所有面向用户的入口必须显式传入。
    visible_skill_ids = None
    visible_tool_ids = None
    if user_id is not None:
        visible_skill_ids = set(db.scalars(select(Skill.id).where(Skill.tenant_id == tenant_id, visible_owner_clause(Skill, tenant_id, user_id))))
        visible_tool_ids = set(db.scalars(select(Tool.id).where(Tool.tenant_id == tenant_id, visible_owner_clause(Tool, tenant_id, user_id))))
    mappings: dict[str, ToolMapping] = {}
    tools: list[dict] = []
    links = db.query(AgentVersionSkill).filter(
        AgentVersionSkill.agent_version_id == agent_version.id,
        AgentVersionSkill.tenant_id == tenant_id,
    ).all()
    for agent_skill in links:
        skill = db.get(SkillVersion, agent_skill.skill_version_id)
        if skill is None or skill.tenant_id != tenant_id or skill.status != "PUBLISHED":
            continue
        # 父级 Skill 已归档时其版本不可再被映射为工具
        parent_skill = db.get(Skill, skill.skill_id)
        if parent_skill is None or parent_skill.status != "ACTIVE":
            continue
        if visible_skill_ids is not None and parent_skill.id not in visible_skill_ids:
            continue
        tool_links = db.query(SkillVersionTool).filter(
            SkillVersionTool.skill_version_id == skill.id,
            SkillVersionTool.tenant_id == tenant_id,
        ).order_by(SkillVersionTool.sort_order, SkillVersionTool.created_at).all()
        for tool_link in _dependency_ordered_links(tool_links):
            tool_version = db.get(ToolVersion, tool_link.tool_version_id)
            tool = db.get(Tool, tool_version.tool_id) if tool_version else None
            if tool_version is None or tool is None or tool_version.tenant_id != tenant_id or tool.status != "ACTIVE" or tool_version.status != "PUBLISHED":
                continue
            if visible_tool_ids is not None and tool.id not in visible_tool_ids:
                continue
            # 委派子代理不可见需人工确认的工具：子执行链无任务级 checkpoint 挂起语义，
            # 若不排除，写入类工具会绕过人工审批直接执行（fail-closed）
            if exclude_requires_confirmation and (skill.requires_confirmation or tool_version.requires_confirmation):
                continue
            name = tool.name
            if name in mappings:
                continue
            # 净化后同时用于"发给模型的工具定义"与"执行侧参数校验"，保证两侧一致
            raw_schema = (tool_version.config_snapshot or {}).get("_mcp_raw_input_schema")
            schema = _sanitize_schema(raw_schema or tool_version.input_schema or {"type": "object", "properties": {}})
            alias = safe_function_name(name)
            # 同时记录原始工具名：模型可能直接输出系统提示里的工具原名
            mappings[alias] = ToolMapping(alias, skill.id, tool_version.id, schema, original_name=name)
            # 工具描述只取工具自身 description，禁止回退到 skill.instructions——
            # 流程指令属于 system prompt，塞进单个工具描述会误导模型把该函数当作流程中的其他工具
            desc = f"{name}：{(tool.description or '').strip()}".rstrip("：")[:512]
            tools.append({"type": "function", "function": {"name": alias, "description": desc, "parameters": schema}})
    # 站内 A2A：所有 Agent 使用同一动态发现与派发入口；递归深度由运行时策略限制。
    # 只暴露 Agent 当前发布版本，历史 PUBLISHED 版本继续保留给旧任务快照，
    # 但不得作为新请求的路由候选，避免同一 Agent 重复出现在工具目录。
    system_agent = db.get(Agent, agent_version.agent_id)
    if system_agent is not None:
        candidate_query = (
            select(AgentVersion)
            .join(Agent, Agent.id == AgentVersion.agent_id)
            .where(
                AgentVersion.tenant_id == tenant_id,
                AgentVersion.status == "PUBLISHED",
                Agent.current_published_version_id == AgentVersion.id,
                Agent.status == "ACTIVE",
                Agent.id != system_agent.id,
            )
        )
        # 用户可见性与任务/会话入口保持一致：本人、官方、管理员创建。
        # 无用户上下文的内核调用仅允许发现官方 Agent，默认拒绝暴露用户私有资源。
        if user_id is not None:
            candidate_query = candidate_query.where(visible_owner_clause(Agent, tenant_id, user_id))
        else:
            candidate_query = candidate_query.where(Agent.source_type == "OFFICIAL")
        candidates = db.scalars(candidate_query.order_by(Agent.name, Agent.id).limit(15)).all()
        route_options: list[dict[str, str]] = []
        candidate_ids: list[UUID] = []
        for target in candidates:
            target_agent = db.get(Agent, target.agent_id)
            if target_agent is None:
                continue
            # 路由描述除 Agent 自身介绍外，补充其当前版本绑定且当前用户可见的 Skill 能力。
            capability_labels: list[str] = []
            capability_links = db.query(AgentVersionSkill).filter(
                AgentVersionSkill.tenant_id == tenant_id,
                AgentVersionSkill.agent_version_id == target.id,
            ).all()
            for capability_link in capability_links:
                capability_version = db.get(SkillVersion, capability_link.skill_version_id)
                capability_skill = db.get(Skill, capability_version.skill_id) if capability_version else None
                if (
                    capability_version is None
                    or capability_version.status != "PUBLISHED"
                    or capability_skill is None
                    or capability_skill.status != "ACTIVE"
                    or (visible_skill_ids is not None and capability_skill.id not in visible_skill_ids)
                    or (user_id is None and capability_skill.official is not True)
                ):
                    continue
                label = capability_skill.name
                if capability_skill.description:
                    label += f"：{capability_skill.description.strip()[:100]}"
                if label not in capability_labels:
                    capability_labels.append(label)
            capability_text = "；".join(capability_labels[:8])
            candidate_ids.append(target.id)
            route_options.append({
                "id": str(target.id),
                "name": target_agent.name[:80],
                "description": (target_agent.description or "").strip()[:80],
                "skills": capability_text[:120],
            })
        # 不把 N 个 Agent 暴露成 N 个独立执行工具。模型只看到一个结构化路由入口，
        # 并且 agent_version_id 使用服务端候选 ID 枚举约束，执行侧还会再次校验成员资格。
        if route_options:
            route_description = "从下列当前用户可访问的专业 Agent 中选择最匹配的一位。"
            route_description += "候选能力：" + json.dumps(route_options, ensure_ascii=False)[:7000]
            schema = {
                "type": "object",
                "properties": {
                    "agent_version_id": {
                        "type": "string",
                        "enum": [item["id"] for item in route_options],
                        "description": "必须选择候选列表中的一个 AgentVersion ID",
                    },
                    "task": {
                        "type": "string",
                        "description": "交给所选 Agent 的完整任务，包含目标、约束、必要上下文和期望输出",
                    },
                },
                "required": ["agent_version_id", "task"],
                "additionalProperties": False,
            }
            alias = "route_specialist"
            suffix = 2
            while alias in mappings:
                alias = f"route_specialist_{suffix}"
                suffix += 1
            mappings[alias] = ToolMapping(
                alias,
                None,
                None,
                schema,
                dispatch_candidate_ids=tuple(candidate_ids),
            )
            tools.append({
                "type": "function",
                "function": {
                    "name": alias,
                    "description": route_description[:8000],
                    "parameters": schema,
                },
            })
    # 平台内置联网搜索（只读 MCP）：默认暴露给所有 Agent，子执行链不暴露。
    # 工具定义在此，真实调用由 execution_loop 的 builtin_kind 分派到 SkillGateway。
    settings = get_settings()
    if settings.builtin_mcp_search_enabled and not exclude_requires_confirmation:
        search_tools = [
            (
                "web_search",
                "平台内置联网搜索：用国内搜索引擎检索网页并返回结构化结果。"
                "涉及时效、最新事实、外部网页内容或需要核验的信息时优先调用；"
                "需要正文摘录时传 need_content=true，不要连续调用多次 scrape。",
                {
                    "type": "object",
                    "properties": {
                        "q": {"type": "string", "description": "搜索关键词（中文或英文）"},
                        "engine": {"type": "string", "enum": ["auto", "baidu", "sogou", "so360", "toutiao"], "default": "auto", "description": "auto 表示多引擎聚合"},
                        "num": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                        "type": {"type": "string", "enum": ["web", "news"], "default": "web"},
                        "time_range": {"type": "string", "enum": ["any", "day", "week", "month", "year"], "default": "any"},
                        "after": {"type": "string", "description": "仅保留此日期之后的结果，YYYY-MM-DD"},
                        "before": {"type": "string", "description": "仅保留此日期之前的结果，YYYY-MM-DD"},
                        "need_content": {"type": "boolean", "default": False, "description": "是否同时读取前几条结果正文摘录"},
                        "content_chars": {"type": "integer", "minimum": 200, "maximum": 4000, "default": 800},
                        "include_domains": {"type": "array", "items": {"type": "string"}},
                        "exclude_domains": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["q"],
                    "additionalProperties": False,
                },
            ),
            (
                "web_search_and_read",
                "平台内置联网搜索并读取正文：一次调用同时拿到搜索结果与前几条网页正文摘录。"
                "需要基于网页内容回答问题时优先用它，避免先 search 再逐条 scrape。",
                {
                    "type": "object",
                    "properties": {
                        "q": {"type": "string", "description": "搜索关键词（中文或英文）"},
                        "engine": {"type": "string", "enum": ["auto", "baidu", "sogou", "so360", "toutiao"], "default": "auto"},
                        "num": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                        "read": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3, "description": "抓取正文的条数"},
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": 8000, "default": 2000, "description": "每篇正文最多保留的字符数"},
                        "type": {"type": "string", "enum": ["web", "news"], "default": "web"},
                        "time_range": {"type": "string", "enum": ["any", "day", "week", "month", "year"], "default": "any"},
                        "after": {"type": "string", "description": "仅保留此日期之后的结果，YYYY-MM-DD"},
                        "before": {"type": "string", "description": "仅保留此日期之前的结果，YYYY-MM-DD"},
                        "include_domains": {"type": "array", "items": {"type": "string"}},
                        "exclude_domains": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["q"],
                    "additionalProperties": False,
                },
            ),
        ]
        for original_name, description, schema in search_tools:
            alias = original_name
            suffix = 2
            while alias in mappings:
                alias = f"{original_name}_{suffix}"
                suffix += 1
            mappings[alias] = ToolMapping(
                alias, None, None, schema,
                original_name=original_name, builtin_kind="mcp_search",
            )
            tools.append({
                "type": "function",
                "function": {"name": alias, "description": description, "parameters": schema},
            })
    # 平台内置默认技能：生成文件变更提案，只有用户 Keep 后才落地为会话附件。
    # 子执行链（exclude_requires_confirmation=True）不暴露，避免重复生成文件。
    if not exclude_requires_confirmation:
        alias = "output_document"
        suffix = 2
        while alias in mappings:
            alias = f"output_document_{suffix}"
            suffix += 1
        schema = {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["md", "pdf", "docx"], "description": "输出格式：md / pdf / docx"},
                "filename": {"type": "string", "description": "文件名，不含扩展名也可以，系统会自动补全"},
                "content": {"type": "string", "description": "修改后的完整文档正文，Markdown 内容会原样保留"},
                "purpose": {"type": "string", "description": "本次改动目的，一句话描述，不要输出大段 diff"},
                "risk": {"type": "string", "description": "本次改动风险，一句话描述；无风险可留空"},
            },
            "required": ["format", "filename", "content"],
            "additionalProperties": False,
        }
        mappings[alias] = ToolMapping(alias, None, None, schema, original_name="output_document", builtin_kind="document_output")
        tools.append({
            "type": "function",
            "function": {
                "name": alias,
                "description": "系统内置文档技能：当用户需要新建或修改 Markdown/PDF/Word 文件时调用。传入修改后的完整正文，系统会自动生成文件变更提案并展示给用户评审；此时磁盘上的原文件不变，只有用户 Keep 后才会真正保存。不要直接写文件，不要在回复里输出大段 diff。",
                "parameters": schema,
            },
        })
    # §2.4 稳定前缀：工具目录按 name 排序，保证同一会话内 prompt 只追加不改写。
    tools.sort(key=lambda item: item["function"]["name"])
    return mappings, tools
