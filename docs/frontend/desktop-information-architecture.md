# Z-Torque Desktop Console — Information Architecture & UI Decisions

## Product direction

The desktop edition is a local-first AI workspace, not a cloud SaaS administration portal. The first screen should help a user start work; configuration and diagnostics should remain available without dominating the primary workflow.

## Navigation

### Primary workspace
- **工作台**: conversations, agent selection, execution progress, confirmations and final results.
- **知识库**: local document management, parsing status and retrieval verification.
- **智能体**: create, publish and manage agents.
- **技能**: reusable capabilities and tool bindings.

### System and maintenance
- **运行与审计**: inspect task execution, failures, event history and audit records.
- **模型接入**: configure model providers and model availability.
- **连接器**: configure external APIs, MCP servers and data connections.
- **Webhook**: inbound/outbound event integration.
- **用户与权限 / 偏好设置**: account, access and local application preferences.

Knowledge remains a primary navigation item. It is a core product capability and needs a visible lifecycle: import → parse/index → retrieval test → use from the workbench. The UI must not imply that every document is automatically available to every agent; backend authorization and retrieval scope remain authoritative.

## Knowledge page improvements

- Show the retrieval service state clearly without hiding the document workflow.
- Present the three-step usage path so first-time users understand what to do next.
- Surface total, parsed, processing and failed document counts.
- Add local filename search and processing-state filters to the file list.
- Keep retrieval testing on the same page; this avoids forcing users to navigate away to validate indexing.
- Provide a direct route back to the workbench after verification.
- Preserve upload limits, supported formats, ownership scope, preview, download and delete behavior.

## Visual system

- Keep the existing neutral surface palette, theme tokens and Ant Design components.
- Strengthen hierarchy with restrained spacing, clear active navigation, consistent card borders and keyboard focus indicators.
- Avoid excessive gradients, large hero banners, decorative charts and redundant status cards.
- Keep dense configuration pages information-rich; use progressive disclosure inside existing forms rather than removing advanced options.
- Respect reduced-motion preferences and keep navigation usable at narrow widths.

## Explicitly out of scope

- No API contract changes.
- No changes to authentication, authorization, tenant/user data scope, task lifecycle, A2A, knowledge retrieval or connector execution.
- No claims that local knowledge is automatically bound to a specific agent.
- No removal of advanced configuration or audit functionality.

## Next review areas

1. Workbench: make the selected agent, current conversation, task execution state and confirmation state easy to distinguish.
2. Agent and Skill editors: simplify first-time creation while retaining advanced versioning and binding controls.
3. Connectors and Models: prioritize connection health and test actions; keep secrets masked.
4. Run & Audit: make task status, duration, failure reason and trace/correlation identifiers easy to inspect.
5. Desktop packaging: verify minimum window size, keyboard navigation, scaling and offline/local startup states.
