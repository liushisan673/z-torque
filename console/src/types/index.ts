// Z-Torque 控制台 API 类型定义
// 与后端 app/schemas 及路由序列化保持一致（见 docs/阶段10 4.7 节）

// ---- 统一响应结构 ----
export interface ApiEnvelope<T> {
  code: number | string;
  message: string;
  data: T;
  trace_id: string;
}

export interface ApiErrorInfo {
  code: string;
  message: string;
  traceId?: string;
  status: number;
}

export interface PageResult<T> {
  items: T[];
  offset: number;
  limit: number;
  total: number;
}

// ---- 认证 ----
export interface UserInfo {
  id: string;
  username: string;
  tenant_id: string;
  roles?: string[];
  permissions?: string[];
}

// ---- 会话与消息 ----
export interface Conversation {
  id: string;
  agent_id: string;
  title: string | null;
  status: string;
  last_message_at: string | null;
}

export interface Message {
  id: string;
  conversation_id: string;
  sender_type: "USER" | "ASSISTANT" | string;
  content: string;
  content_parts: unknown[] | null;
  status: string;
  task_id: string | null;
  error_code: string | null;
  created_at: string;
}

// ---- Chat ----
export interface ChatResponse {
  response_type: "CHAT" | "TASK" | string;
  conversation_id: string;
  user_message_id: string;
  assistant_message_id: string | null;
  task_id: string | null;
  status: string;
  // status=WAITING_HUMAN 时的审批挂起信息
  confirmation: ChatPendingConfirmation | null;
}

// 写入类工具触发人工审批时的挂起信息（后端 ChatResponse.confirmation）
export interface ChatPendingConfirmation {
  confirmation_id: string;
  task_id: string;
  tool_version_id?: string | null;
  tool_name?: string | null;
  arguments: Record<string, unknown>;
}

// ---- Task ----
export type TaskStatus =
  | "PENDING"
  | "QUEUED"
  | "RUNNING"
  | "WAITING_HUMAN"
  | "CANCELING"
  | "CANCEL_REQUESTED"
  | "SUCCESS"
  | "FAILED"
  | "TIMEOUT"
  | "CANCELLED"
  | string;

export interface Task {
  id: string;
  status: TaskStatus;
  owner_user_id: string;
  root_task_id: string;
  delegation_depth: number;
  request_summary: string;
  result: Record<string, unknown> | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  conversation_id: string | null;
  trigger_message_id: string | null;
  message_id: string | null;
}

export interface TaskEvent {
  seq: number;
  event_type: string;
  from_state: string | null;
  to_state: string | null;
  summary: string;
  payload_summary: Record<string, unknown> | null;
  trace_id: string | null;
  created_at: string;
}

export interface TaskSnapshot {
  task_id: string;
  status: TaskStatus;
  result: Record<string, unknown> | null;
  root_task_id: string;
  parent_task_id: string | null;
  delegation_depth: number;
  child_sequence: number | null;
}

// ---- Agent ----
export interface Agent {
  id: string;
  name: string;
  description: string | null;
  source_type: string;
  source_agent_id: string | null;
  source_agent_version_id: string | null;
  current_published_version_id: string | null;
  status: string;
  is_default_parent: boolean;
  owner_user_id: string | null;
  created_at: string;
}

export interface AgentVersion {
  id: string;
  agent_id: string;
  version_number: number;
  status: string;
  config_snapshot: Record<string, unknown>;
  snapshot_hash: string | null;
  delegation_enabled: boolean;
  allowed_child_agent_versions: string[] | null;
  child_necessity_policy: Record<string, unknown> | null;
  delegation_capability_version: string | null;
  published_at: string | null;
}

// Agent 版本已绑定的 Skill（GET /agents/{id}/versions/{vid}/skills）
export interface BoundSkill {
  skill_version_id: string;
  skill_id: string;
  skill_name: string;
  version_number: number;
  status: string;
  script_source: string | null;
  instructions: string | null;
  skill_type: string;
  official: boolean;
}

// ---- Skill / Tool ----
export interface SkillListItem {
  id: string;
  name: string;
  description: string | null;
  skill_type: string;
  official: boolean;
  source_type: string;
  owner_user_id: string | null;
  status: string;
  created_at: string | null;
  current_published_version_id: string | null;
  latest_version_id: string | null;
  latest_version_number: number | null;
  latest_version_status: string | null;
  has_script: boolean;
  // MD-Skill 展示补充字段（来自最新版本快照）
  implementation?: string | null;
  relative_path?: string | null;
  requires_confirmation?: boolean | null;
}

export interface SkillVersionRef {
  id: string;
  version_number: number;
  status: string;
  snapshot_hash: string | null;
  published_at: string | null;
}

export interface SkillDetail {
  id: string;
  name: string;
  description: string | null;
  skill_type: string;
  official: boolean;
  source_type: string;
  owner_user_id: string | null;
  status: string;
  created_at: string | null;
  current_published_version_id: string | null;
  versions: SkillVersionRef[];
}

export interface SkillVersionDetail {
  id: string;
  skill_id: string;
  version_number: number;
  status: string;
  instructions: string | null;
  script_source: string | null;
  required_permissions: string[];
  requires_confirmation: boolean;
  tool_version_ids: string[];
  snapshot_hash: string | null;
  published_at: string | null;
  implementation: string | null;
  allow_scripts: boolean | null;
  input_schema: Record<string, unknown> | null;
  output_schema: Record<string, unknown> | null;
  script_files: string[] | null;
  declared_version: string | null;
  relative_path: string | null;
}

// SkillVersion 绑定的 Tool
export interface SkillToolLink {
  tool_version_id: string;
  tool_id: string;
  tool_name: string;
  tool_type: string;
  description: string | null;
  version_number: number;
  status: string;
}

export interface ToolListItem {
  id: string;
  name: string;
  description: string | null;
  tool_type: string;
  official: boolean;
  versions: SkillVersionRef[];
}

export interface ToolDetail {
  id: string;
  name: string;
  description: string | null;
  tool_type: string;
  official: boolean;
  versions: SkillVersionRef[];
}

export interface ToolVersionDetail {
  id: string;
  tool_id: string;
  version_number: number;
  status: string;
  config_snapshot: Record<string, unknown>;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown> | null;
  required_permissions: string[];
  requires_confirmation: boolean;
  snapshot_hash: string | null;
}

// ---- Model ----
export interface ModelProvider {
  id: string;
  name: string;
  provider_type: string;
  status: string;
  base_url: string | null;
  secret_configured: boolean;
  secret_ref_masked: string | null;
  description: string | null;
  config: Record<string, unknown>;
  created_at: string;
}

export interface ModelResource {
  id: string;
  provider_id: string;
  name: string;
  model_identifier: string;
  interface_type: string | null;
  status: string;
  supports_streaming: boolean;
  supports_function_calling: boolean;
  context_window: number | null;
  max_output_tokens: number | null;
  description: string | null;
  config: Record<string, unknown>;
  created_at: string;
  provider?: ModelProvider;
  endpoints?: ModelEndpoint[];
}

export interface ModelEndpoint {
  id: string;
  provider_id: string;
  model_id: string;
  name: string;
  endpoint_url: string;
  status: string;
  priority: number;
  description: string | null;
  config: Record<string, unknown>;
  created_at: string;
}

export interface ModelTestCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface ModelTestResult {
  model_id: string;
  status: string;
  provider: { id: string; name: string; status: string };
  endpoint: { id: string; name: string; endpoint_url: string; status: string } | null;
  checks: ModelTestCheck[];
  duration_ms: number;
  trace_id: string | null;
  probe_mode: string;
  http_status: number | null;
  summary: string | null;
  error_summary: string | null;
  supports_streaming: boolean;
  supports_function_calling: boolean;
  secret_configured: boolean;
}

// ---- Connector ----
export interface Connector {
  id: string;
  name: string;
  connector_type: string;
  status: string;
  config: Record<string, unknown>;
  secret_configured: boolean;
  secret_ref_masked?: string | null;
  target_policy: Record<string, unknown>;
  description: string | null;
  created_at?: string | null;
}

// POST /connectors/{id}/test 返回（DB 类型额外返回 database 与库内表清单；MCP 类型额外返回 endpoint 与发现的工具）
export interface ConnectorTestResult {
  connector_id: string;
  status: string;
  connector_type: string;
  latency_ms: number;
  checked_at: string;
  trace_id: string;
  database?: string;
  tables?: string[];
  endpoint?: string;
  tools?: ConnectorDiscoveredToolSummary[];
}

// MCP /test 与 /discover 返回的工具摘要
export interface ConnectorDiscoveredToolSummary {
  name: string;
  description: string;
  input_schema?: Record<string, unknown>;
}

// GET /connectors/{id}/tools 返回（联查 Tool 后的已注册能力）
export interface ConnectorToolLink {
  id: string;
  external_name: string | null;
  approved: boolean;
  name: string | null;
  tool_type: string | null;
  description: string | null;
  registered_at: string | null;
}

// ---- Audit ----
export interface AuditEvent {
  id: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  actor_user_id: string | null;
  actor_username: string | null;
  summary: string;
  detail: Record<string, unknown> | null;
  trace_id: string | null;
  result: string | null;
  duration_ms: number | null;
  created_at: string;
}

// GET /audit 返回
export interface AuditListResponse {
  items: AuditEvent[];
  offset: number;
  limit: number;
  total: number;
  scope: "all" | "self";
}

// ---- IAM / Users ----
export interface IamRole {
  id: string;
  name: string;
  permissions: string[];
}

export interface IamUser {
  id: string;
  username: string;
  display_name: string | null;
  tenant_id: string;
  is_active: boolean;
  failed_login_count: number;
  locked_until: string | null;
  last_login_at: string | null;
  created_at: string;
  roles: { id: string; name: string }[];
  permissions: { id: string; name: string }[];
}

// ---- File ----
export interface FileMetadata {
  id: string;
  name: string;
  media_type: string;
  size_bytes: number;
  status: string;
  created_at: string;
}

export interface FileContent {
  id: string;
  content: string;
}

export interface FileStatus {
  id: string;
  status: string;
  error: string | null;
}

// ---- Confirmation ----
export interface Confirmation {
  id: string;
  status: string;
  requested_by_user_id: string;
  confirmed_by_user_id: string | null;
  reason: string | null;
  task_id: string | null;
  skill_version_id: string | null;
  tool_version_id: string | null;
  resolved_at: unknown | null;
}

// ---- Chat / Task SSE 事件 ----
export interface ChatSseEvent {
  event: string;
  data: { content?: string; status?: string; code?: string; message?: string };
}

export interface TaskSseEvent {
  event: string;
  data: Record<string, unknown>;
  lastEventId: string;
}
