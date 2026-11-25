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

- Run & Audit now starts with a compact page heading, a live total-record count and a clear all-users/self data-scope indicator; the detailed filters and event table remain unchanged.


## Workbench execution clarity pass (2026-11-25)

- The empty workbench state now explains the product boundary: ordinary questions remain chat messages; Task is created only when tools/skills or explicit execution are required.
- Live execution status is announced accessibly and distinguishes running from waiting for human confirmation.
- Entering `WAITING_HUMAN` from a task snapshot, event stream or restored task detail automatically opens the execution panel so the approval action is not hidden.
- The approval panel explains the checkpoint behavior: the current task pauses and releases its execution slot while awaiting confirmation; approval resumes from the checkpoint. The UI explicitly tells users to switch conversations to continue other work, then return to approve/reject.
- Task history includes a direct detail action, and terminal task labels distinguish success, failure, timeout and cancellation.
- Keep the workbench centered on conversation and task progress; do not surface sandbox/runtime infrastructure as a peer navigation destination.


## Agent and Skill onboarding clarity pass (2026-11-25)

- The Agent creation dialog now explains the minimum first-publish path: name, model and system rules; skill binding is optional, while delegation and runtime strategy can be configured in a later draft version.
- Agent delegation configuration explicitly distinguishes the parent AgentVersion's child-agent allowlist from user authorization. The allowlist narrows which child versions this parent version may delegate to; every execution still checks the initiating user's identity, resource scope and operation permissions. Being allowlisted does not grant a child Agent additional user permissions.
- The Skill creation dialog distinguishes Standard Skill authoring from MD-Skill file import and explains that script mode is an implementation characteristic of a version. Users are reminded to review version content and tool bindings before publishing.
- These are explanatory UI changes only: no API payloads, publication rules, version snapshots, delegation enforcement or authorization behavior were changed.


## Connector health and audit accessibility pass (2026-11-25)

- Connector test failures are now associated with the connector that produced them. Switching to another connector no longer carries the previous connector's failure banner or health summary into the selected detail pane.
- Connector health distinguishes a complete successful check, a partial check, a failed check, and a connector that has not been checked in the current session. Partial results are not presented as fully healthy.
- Audit event rows can be opened with keyboard focus plus Enter/Space, matching their existing click behavior. Trace ID copy remains a separate action and does not open the event row.
- These changes are presentation/state-scoping improvements; no connector test API, audit API, backend health persistence, or authorization rules were changed. Connector test results remain session-local in the current UI.

## Task and audit cross-inspection pass (2026-11-25)

- Audit events whose resource type is `TASK` now attempt to load the corresponding task detail and ordered task-event stream in the event drawer, using the existing `GET /tasks/{task_id}` and `GET /tasks/{task_id}/events` endpoints.
- The task panel displays the persisted task status, task/root-task identifiers, delegation depth, timestamps, task-level error information and ordered event summaries/state transitions. Failure emphasis is derived only from the returned event type/state; it does not invent a model/tool/agent failure stage when the event stream does not identify one.
- The task API is owner-scoped. The UI only requests task detail when the audit event actor matches the current user; global audit visibility does not grant permission to inspect another user's task. Other users' task payloads are intentionally not fetched or exposed.
- The task event sequence is presented as the task's own execution history. The existing audit `by-trace` view remains explicitly labelled “同 Trace”; it is correlation by trace ID, not proof of parent/child causality. Task root ID and delegation depth are shown as returned by the task API; no parent-task relationship is fabricated.
- No backend route, schema, permission, audit retention or task lifecycle behavior was changed.

## Connector test history pass (2026-11-25)

- The Connector detail page now loads the latest 10 `CONNECTOR_TESTED` audit events for the selected connector using the existing audit list endpoint and its `action`, `resource_type` and `resource_id` filters.
- The list is persisted audit history, distinct from the in-memory detailed probe result. It survives page reloads to the extent that the backend has recorded the event; the detailed REST/MCP/database probe payload remains session-local.
- The history view shows timestamp, success/partial/failure classification, recorded latency when available and a shortened Trace ID. It does not display raw error payloads or connector credentials.
- The audit API enforces its own user scope. A regular user sees only their own audit events; users with the relevant global audit permission receive the broader scope from the backend.
- Some failures rejected before the connector probe writes a `CONNECTOR_TESTED` audit event may not appear in this history. The UI describes the list as recorded audit events rather than claiming it is an exhaustive log of every attempted request.
- No new endpoint, audit schema, connector test behavior or persistence table was introduced.

## Direct task delegation and execution-detail pass (2026-11-25)

- Task responses now expose the persisted `agent_id`, `parent_task_id` and `child_sequence` alongside the existing `root_task_id` and `delegation_depth`. These fields already existed on the Task model; this change makes the actual direct relationship available to clients without adding a new table or migration.
- Workbench child-task loading now filters by `parent_task_id === currentTask.id` and orders by `child_sequence`. It no longer labels every task sharing a root ID as a direct child of whichever task happens to be open. A terminal child task can navigate back to its direct parent; the navigation control is disabled while that child is still executing so the live child stream is not confused with the parent task view.
- Historical task timelines now render the existing sanitized tool-call/result summaries through the shared `toolCardOf` formatter, including compact tool names/results and collapsible arguments when the event payload contains them. Missing payload fields remain absent rather than being inferred.
- Audit task inspection now shows Agent ID and the direct parent/child sequence fields returned by the task API. Existing owner-scoped task access remains unchanged.
- Added an integration test for root/child Task API response fields and direct-parent relationship. This test has been committed but must still pass CI before the change is considered verified.
- No task delegation policy, execution behavior, user SecurityContext, event schema, trace semantics or audit visibility rules were changed.
