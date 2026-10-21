"""资源治理阈值：避免 kernel 服务之间为共享常量产生循环依赖。"""

# 模型侧有效工具暴露上限：Skill 绑定与 AgentVersion 发布双闸门共用
MAX_SKILL_BOUND_TOOLS = 30
