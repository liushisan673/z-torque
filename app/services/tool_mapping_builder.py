"""工具映射构建：AgentVersion 绑定的 SkillVersion/ToolVersion → 模型可见工具定义与执行映射。

供 TaskRuntime（worker 执行链路）与流式聊天分流（/chat/stream 初始模型判断）共用，
保证两条链路暴露给模型的工具集合完全一致。
"""
import hashlib
from uuid import UUID

from sqlalchemy import select

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
    # 委派：delegation_enabled 时把允许的子智能体发布版暴露为委派工具（仅一层，深度由配置约束）
    if agent_version.delegation_enabled:
        for child_id in agent_version.allowed_child_agent_versions or []:
            try:
                child_ver_id = UUID(str(child_id))
            except (ValueError, AttributeError):
                continue
            child = db.get(AgentVersion, child_ver_id)
            if child is None or child.tenant_id != tenant_id or child.status != "PUBLISHED":
                continue
            child_agent = db.get(Agent, child.agent_id)
            if child_agent is None or child_agent.status != "ACTIVE":
                continue
            alias = f"delegate_{hashlib.md5(str(child_ver_id).encode('utf-8')).hexdigest()[:12]}"
            if alias in mappings:
                continue
            schema = {"type": "object", "properties": {"task": {"type": "string", "description": f"要委派给「{child_agent.name}」处理的完整任务描述，需包含该智能体所需的全部上下文"}}, "required": ["task"]}
            mappings[alias] = ToolMapping(alias, child_ver_id, None, schema, child_agent_version_id=child_ver_id)
            desc = f"委派给子智能体「{child_agent.name}」独立处理任务：{(child_agent.description or '').strip()}".rstrip("：")[:512]
            tools.append({"type": "function", "function": {"name": alias, "description": desc, "parameters": schema}})
    # 站内 A2A：仅系统默认父智能体可发现并派发专业 Agent。
    # 只暴露 Agent 当前发布版本，历史 PUBLISHED 版本继续保留给旧任务快照，
    # 但不得作为新请求的路由候选，避免同一 Agent 重复出现在工具目录。
    system_agent = db.get(Agent, agent_version.agent_id)
    if system_agent is not None and system_agent.is_default_parent and not exclude_requires_confirmation:
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
        candidates = db.scalars(candidate_query).all()
        for target in candidates:
            target_agent = db.get(Agent, target.agent_id)
            if target_agent is None:
                continue
            alias = f"call_agent_{hashlib.md5(str(target.id).encode('utf-8')).hexdigest()[:12]}"
            if alias in mappings:
                continue
            # 路由描述除 Agent 自身介绍外，补充其当前版本绑定且当前用户可见的 Skill 能力。
            # 这样 Supervisor 可以根据实际能力选择，而不是只凭 Agent 名称猜测。
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
                ):
                    continue
                label = capability_skill.name
                if capability_skill.description:
                    label += f"：{capability_skill.description.strip()[:100]}"
                if label not in capability_labels:
                    capability_labels.append(label)
            capability_text = "；".join(capability_labels[:8])
            task_description = f"交给「{target_agent.name}」完整处理的任务描述，必须包含目标、约束、相关上下文和输出要求。该 Agent 能力：{capability_text or '以 Agent 描述为准'}"
            schema = {"type": "object", "properties": {"task": {"type": "string", "description": task_description[:1200]}}, "required": ["task"]}
            mappings[alias] = ToolMapping(alias, target.id, None, schema, dispatch_agent_version_id=target.id)
            desc_parts = [f"调用智能体「{target_agent.name}」处理任务", (target_agent.description or "").strip()]
            if capability_text:
                desc_parts.append(f"已绑定技能：{capability_text}")
            desc = "；".join(part for part in desc_parts if part)[:1200]
            tools.append({"type": "function", "function": {"name": alias, "description": desc, "parameters": schema}})
    # §2.4 稳定前缀：工具目录按 name 排序，保证同一会话内 prompt 只追加不改写。
    tools.sort(key=lambda item: item["function"]["name"])
    return mappings, tools
