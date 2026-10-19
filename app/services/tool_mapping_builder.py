"""工具映射构建：AgentVersion 绑定的 SkillVersion/ToolVersion → 模型可见工具定义与执行映射。

供 TaskRuntime（worker 执行链路）与流式聊天分流（/chat/stream 初始模型判断）共用，
保证两条链路暴露给模型的工具集合完全一致。
"""
import hashlib
from uuid import UUID

from app.models import Agent, AgentVersion, AgentVersionSkill, Skill, SkillVersion, SkillVersionTool, Tool, ToolVersion
from app.services.function_calling import ToolMapping, safe_function_name


def build_tool_mappings(db, agent_version: AgentVersion | None, tenant_id: UUID) -> tuple[dict[str, ToolMapping], list[dict]]:
    if agent_version is None:
        return {}, []
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
        tool_links = db.query(SkillVersionTool).filter(
            SkillVersionTool.skill_version_id == skill.id,
            SkillVersionTool.tenant_id == tenant_id,
        ).all()
        for tool_link in tool_links:
            tool_version = db.get(ToolVersion, tool_link.tool_version_id)
            tool = db.get(Tool, tool_version.tool_id) if tool_version else None
            if tool_version is None or tool is None or tool_version.tenant_id != tenant_id or tool.status != "ACTIVE" or tool_version.status != "PUBLISHED":
                continue
            name = tool.name
            if name in mappings:
                continue
            schema = tool_version.input_schema or {"type": "object", "properties": {}}
            alias = safe_function_name(name)
            mappings[alias] = ToolMapping(alias, skill.id, tool_version.id, schema)
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
    return mappings, tools
