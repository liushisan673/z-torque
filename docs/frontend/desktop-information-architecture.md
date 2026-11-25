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

## Sidebar navigation decision (2026-11-25)

The desktop console uses a persistent left sidebar instead of the previous horizontal top navigation.

- Keep 工作台、知识库、智能体、技能 in the primary workspace group.
- Keep 运行与审计 in the operations group.
- Keep 模型接入、连接器、Webhook in the system configuration group; preserve existing admin-only visibility.
- Keep 用户与权限、偏好设置 in the account group; preserve the current permission filter as the single source of truth.
- Support a collapsible desktop sidebar and an icon-rail layout at narrow widths.
- Keep the Chat workspace's own conversation/session sidebar separate from the global product navigation.
- Do not change routes, API contracts, authorization rules or feature availability as part of this visual change.
- The active route, keyboard focus, hover state and accessible labels must remain clear in expanded and collapsed modes.

## Follow-up UI implementation order

1. Workbench: separate conversation context from task execution; make current agent, task progress, pending confirmation and result states visible without exposing infrastructure internals.
2. Agent and Skill: use a clear list/detail hierarchy; make create/publish the primary path and keep versioning, tool binding, permissions and runtime limits in an advanced configuration area.
3. Connectors: distinguish configured, healthy, degraded and unavailable states; provide a safe test action and a concise last-test result without exposing credentials.
4. Run & Audit: make task status, elapsed time, failure reason and trace/correlation identifiers scannable; keep detailed event payloads in a secondary inspection panel.
5. Responsive and accessibility pass: verify narrow window behavior, collapsed navigation tooltips, focus order, minimum window size and reduced-motion behavior.


## Interaction and access consistency pass (2026-11-25)

- Agent, Skill and Connector list cards are keyboard-operable: Tab focuses the selected item; Enter or Space opens its detail; `aria-pressed` exposes the current selection state.
- Managed routes for Agents, Skills, Audit and Settings use the same `APP_NAV` permission metadata as the sidebar. Hiding a navigation entry is not treated as an access-control boundary; direct URL navigation is checked as well.
- Keep backend authorization authoritative. These route guards are a console usability/access consistency layer, not a replacement for API permission checks.
- Do not convert list cards into nested interactive controls; keep destructive and configuration actions in the detail pane.
