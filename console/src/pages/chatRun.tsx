// 会话执行块（run-block）共享模块：主 Chat 页与嵌入对话页（/embed/chat）共用。
// 默认只展示一行「已处理 Xs · 当前动作」；展开后也只列关键节点，完整事件按需查看。
// 样式统一走 .chat-run-* / .chat-tool-* / .chat-confirm-*。
import { useEffect, useMemo, useState } from "react";

import { request } from "../services/api";

export interface TaskStep {
  seq: number;
  eventType: string;
  summary: string;
  time: string;
  payload?: Record<string, unknown> | null;
}

// 内联执行块（run-block）状态：当前正在展示的任务（过程在上、结论在下）
export interface ActiveRun {
  taskId: string;
  steps: TaskStep[];
  status: string;
  waiting: boolean;
  // 用户主动停止（区别于后端失败）：过程标记为「已停止」
  stopped?: boolean;
  /** 本轮开始时间（Date.now()），用于「已进行 Ns」实时计时；历史回放不传 */
  startedAt?: number;
}

export const TERMINAL = new Set(["SUCCESS", "FAILED", "TIMEOUT", "CANCELLED"]);

// 本地步骤：不来自后端事件（理解需求 / 汇总结论 / 已停止），用负 seq 与真实事件隔离
let LOCAL_SEQ = -1;
export function localStep(eventType: string, summary: string): TaskStep {
  return { seq: LOCAL_SEQ--, eventType, summary, time: new Date().toLocaleTimeString(), payload: null };
}

// 长文本截断：工具参数/结果做脱敏与长度限制（文档 6.5）
function clipText(text: string, limit: number): string {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

// 参数/结果统一转展示文本：后端 payload 可能是字符串（_brief）或对象
function toDisplayText(value: unknown, limit: number): string | null {
  if (value === null || value === undefined) return null;
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return text.trim() ? clipText(text, limit) : null;
}

// SSE 事件数据中的框架字段：剔除后剩下的即业务 payload（tool_name / arguments / result …）
const EVENT_META_KEYS = new Set([
  "task_id", "seq", "summary", "status", "root_task_id", "parent_task_id",
  "delegation_depth", "child_sequence", "from_state", "to_state", "created_at", "conversation_id",
]);
export function payloadOf(d: Record<string, unknown>): Record<string, unknown> | null {
  const rest: Record<string, unknown> = {};
  let has = false;
  for (const [k, v] of Object.entries(d)) {
    if (EVENT_META_KEYS.has(k)) continue;
    rest[k] = v;
    has = true;
  }
  return has ? rest : null;
}

export function eventLabel(eventType: string): string {
  const map: Record<string, string> = {
    // 状态事件
    TASK_CREATED: "任务已创建",
    TASK_QUEUED: "排队中",
    TASK_STARTED: "开始执行",
    TASK_MODEL_STARTED: "模型推理中",
    TASK_MODEL_SUCCEEDED: "模型推理完成",
    TASK_SKILL_SUCCEEDED: "工具执行完成",
    TASK_SUCCEEDED: "任务完成",
    TASK_FAILED: "任务失败",
    TASK_TIMEOUT: "任务超时",
    TASK_CANCELLED: "任务已取消",
    TASK_CANCEL_REQUESTED: "请求取消",
    TASK_WAITING_HUMAN: "等待人工确认",
    TASK_WAITING_CHILD: "等待子智能体完成",
    TASK_CHILD_COMPLETED: "子智能体已完成",
    TASK_APPROVAL_GRANTED: "已批准",
    TASK_APPROVAL_REJECTED: "已拒绝",
    TASK_SKILL_FAILED: "工具执行失败",
    TASK_MODEL_FAILED: "模型推理失败",
    // 工具执行事件（细粒度时间线）
    TASK_TOOL_CALL: "正在调用工具",
    TASK_TOOL_RESULT: "工具返回结果",
    // 本地阶段事件（前端生成，不来自后端，文案对齐 chat.html）
    LOCAL_UNDERSTAND: "理解需求",
    LOCAL_SUMMARY: "汇总结论",
    LOCAL_STOPPED: "已停止",
    // 流式阶段事件（与文档 5.3 节事件名对齐，不虚构未发生步骤）
    "task.snapshot": "任务状态快照",
    "task.status": "状态更新",
    "task.delta": "执行进展",
    "task.tool_call": "正在调用工具",
    "task.tool_result": "工具返回结果",
    "task.waiting_human": "等待人工确认",
    "task.human_decision": "人工决策",
    "task.result": "最终结果",
    "task.error": "执行出错",
    "task.done": "执行完成",
    // 普通聊天事件（备用，本面板主要展示 Task 事件）
    "message.delta": "生成中",
    "message.result": "生成完成",
    "message.done": "回复结束",
    "message.error": "回复出错",
  };
  return map[eventType] ?? eventType;
}

// 内联执行块整体状态 → 样式类与文案（run/wait/ok/fail）
// 文案与页面顶部 live-pill「执行中」保持一致，避免同一状态出现两种说法
export function runStatusInfoOf(activeRun: ActiveRun | null): { cls: string; label: string } {
  if (!activeRun) return { cls: "run", label: "执行中" };
  if (activeRun.stopped) return { cls: "fail", label: "已停止" };
  if (activeRun.status === "WAITING_CHILD") return { cls: "wait", label: "等待子任务" };
  if (activeRun.waiting || activeRun.status === "WAITING_HUMAN") return { cls: "wait", label: "等待确认" };
  if (activeRun.status === "SUCCESS") return { cls: "ok", label: "已完成" };
  if (TERMINAL.has(activeRun.status)) return { cls: "fail", label: "已结束" };
  return { cls: "run", label: "执行中" };
}

// 内联单步状态：末步运行中高亮（active），已发生步骤 done，等待/失败单独着色
export function runStepState(s: TaskStep, idx: number, total: number, activeRun: ActiveRun | null): "active" | "done" | "waiting" | "fail" {
  if (s.eventType.includes("FAILED") || s.eventType.includes("ERROR") || s.eventType === "task.error") return "fail";
  if (s.eventType === "LOCAL_STOPPED") return "fail";
  if (s.eventType === "TASK_WAITING_HUMAN" || s.eventType === "task.waiting_human" || s.eventType === "TASK_WAITING_CHILD") return "waiting";
  if (!activeRun) return "done";
  if (activeRun.stopped || TERMINAL.has(activeRun.status)) return "done";
  return idx === total - 1 ? "active" : "done";
}

// 内联步骤描述：工具类步骤的详情由工具卡片承载，此处只留简短提示
export function runStepDesc(s: TaskStep): string {
  const p = s.payload as Record<string, unknown> | null;
  const toolName = p && typeof p.tool_name === "string" ? p.tool_name : null;
  if ((s.eventType === "TASK_TOOL_CALL" || s.eventType === "task.tool_call") && toolName) return "正在请求工具，请稍候";
  if ((s.eventType === "TASK_TOOL_RESULT" || s.eventType === "task.tool_result") && toolName) return "";
  const label = eventLabel(s.eventType);
  return s.summary && s.summary !== label ? s.summary : "";
}

// 工具卡片：调用步骤展示参数，返回步骤展示结果摘要（脱敏 + 截断，文档 6.5）
export function toolCardOf(s: TaskStep): { name: string; args?: string | null; result?: string | null } | null {
  const p = s.payload as Record<string, unknown> | null;
  const toolName = p && typeof p.tool_name === "string" ? p.tool_name : null;
  if (!toolName) return null;
  if (s.eventType === "TASK_TOOL_CALL" || s.eventType === "task.tool_call") {
    return { name: toolName, args: toDisplayText(p?.arguments, 600) };
  }
  if (s.eventType === "TASK_TOOL_RESULT" || s.eventType === "task.tool_result") {
    const failed = String(p?.status ?? "SUCCEEDED") !== "SUCCEEDED";
    const raw = toDisplayText(p?.result ?? p?.error, 600);
    return { name: toolName, result: raw ?? (failed ? "执行失败" : "返回成功") };
  }
  return null;
}

// 等待审批时挂起的工具名：取最后一个「调用工具」步骤的 payload（TASK_WAITING_HUMAN 事件本身不带工具信息）
export function waitingToolNameOf(activeRun: ActiveRun | null): string | null {
  if (!activeRun?.waiting) return null;
  for (let i = activeRun.steps.length - 1; i >= 0; i--) {
    const s = activeRun.steps[i];
    if (s.eventType === "TASK_TOOL_CALL") {
      const name = (s.payload as Record<string, unknown> | null)?.tool_name;
      if (typeof name === "string") return name;
    }
  }
  return null;
}

// 等待审批时的参数（截断展示，完整参数由后端确认记录留存）
export function waitingConfirmArgsOf(activeRun: ActiveRun | null): string | null {
  if (!activeRun?.waiting) return null;
  for (let i = activeRun.steps.length - 1; i >= 0; i--) {
    const s = activeRun.steps[i];
    if (s.eventType === "TASK_TOOL_CALL") {
      const args = toDisplayText((s.payload as Record<string, unknown> | null)?.arguments, 400);
      if (args) return args;
    }
  }
  return null;
}

// ---- 摘要展示：默认收敛成关键节点，完整事件不占首屏 ----
type DigestState = ReturnType<typeof runStepState>;

interface DigestStep {
  key: string;
  label: string;
  detail: string;
  state: DigestState;
  tool?: { name: string; args?: string | null; result?: string | null };
}

// 状态/流式框架事件：结论与状态徽标已覆盖，展开链路时不再重复占位
const QUIET_EVENTS = new Set([
  "TASK_CREATED", "TASK_QUEUED", "TASK_STARTED",
  "TASK_MODEL_SUCCEEDED", "TASK_SUCCEEDED", "TASK_CANCEL_REQUESTED",
  "TASK_APPROVAL_GRANTED", "TASK_APPROVAL_REJECTED",
  "TASK_CHILD_COMPLETED", "TASK_WAITING_CHILD",
  "LOCAL_UNDERSTAND", "LOCAL_SUMMARY",
  "task.snapshot", "task.status", "task.delta", "task.done", "task.result",
  "message.delta", "message.result", "message.done", "message.error",
]);

const COMPACT_LABEL: Record<string, string> = {
  TASK_MODEL_STARTED: "模型推理",
  TASK_MODEL_FAILED: "模型推理失败",
  TASK_TOOL_CALL: "调用工具",
  TASK_TOOL_RESULT: "工具返回",
  "task.tool_call": "调用工具",
  "task.tool_result": "工具返回",
  TASK_WAITING_HUMAN: "等待确认",
  "task.waiting_human": "等待确认",
  TASK_FAILED: "执行失败",
  TASK_TIMEOUT: "执行超时",
  TASK_CANCELLED: "已取消",
  LOCAL_STOPPED: "已停止",
  "task.error": "执行出错",
};

function clipDetail(text: string, limit = 46): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > limit ? `${clean.slice(0, limit)}…` : clean;
}

function toolNameOf(s: TaskStep): string | null {
  const p = s.payload as Record<string, unknown> | null;
  return p && typeof p.tool_name === "string" ? p.tool_name : null;
}

function isToolCall(s: TaskStep): boolean {
  return s.eventType === "TASK_TOOL_CALL" || s.eventType === "task.tool_call";
}

function isToolResult(s: TaskStep): boolean {
  return s.eventType === "TASK_TOOL_RESULT" || s.eventType === "task.tool_result";
}

function buildDigest(steps: TaskStep[], activeRun: ActiveRun | null): DigestStep[] {
  const consumed = new Set<number>();
  const out: DigestStep[] = [];

  for (let i = 0; i < steps.length; i += 1) {
    if (consumed.has(i)) continue;
    const s = steps[i];
    if (QUIET_EVENTS.has(s.eventType)) continue;
    const name = toolNameOf(s);

    // 调用 + 返回合并成一条：既保留工具名，也不让两行长 JSON 撑开链路
    if (isToolCall(s) && name) {
      let result: TaskStep | null = null;
      for (let j = i + 1; j < steps.length && j <= i + 3; j += 1) {
        const candidate = steps[j];
        if (consumed.has(j) || !isToolResult(candidate) || toolNameOf(candidate) !== name) continue;
        result = candidate;
        consumed.add(j);
        break;
      }
      const failed = Boolean(result && (result.eventType.includes("FAILED") || toolCardOf(result)?.result?.includes("失败")));
      const state: DigestState = result
        ? (failed ? "fail" : "done")
        : runStepState(s, i, steps.length, activeRun);
      out.push({
        key: `tool-${s.seq}`,
        label: COMPACT_LABEL[s.eventType] ?? "调用工具",
        detail: `${name}${result ? (failed ? " · 失败" : " · 完成") : " · 进行中"}`,
        state,
        tool: { name, args: toolCardOf(s)?.args, result: result ? toolCardOf(result)?.result : null },
      });
      continue;
    }

    if (isToolResult(s) && name) {
      const failed = Boolean(s.eventType.includes("FAILED") || toolCardOf(s)?.result?.includes("失败"));
      out.push({
        key: `result-${s.seq}`,
        label: COMPACT_LABEL[s.eventType] ?? "工具返回",
        detail: `${name}${failed ? " · 失败" : " · 完成"}`,
        state: failed ? "fail" : runStepState(s, i, steps.length, activeRun),
        tool: { name, result: toolCardOf(s)?.result },
      });
      continue;
    }

    const label = COMPACT_LABEL[s.eventType] ?? eventLabel(s.eventType);
    const raw = runStepDesc(s) || (s.summary && s.summary !== eventLabel(s.eventType) ? s.summary : "");
    const detail = clipDetail(raw);
    const state = runStepState(s, i, steps.length, activeRun);
    const prev = out[out.length - 1];
    // 连续同类进度（多轮推理）只保留最新一条，避免同一件事刷屏
    if (prev && !prev.tool && prev.label === label && state !== "active") {
      prev.detail = detail || prev.detail;
      prev.state = state;
      continue;
    }
    out.push({ key: `step-${s.seq}`, label, detail, state });
  }
  return out;
}

function headlineOf(digest: DigestStep[], activeRun: ActiveRun): string {
  // 等待子任务与等待人工确认是两种完全不同的状态：前者不需要用户操作，
  // 不能复用「等待确认」文案，否则用户会以为自己在等一个不存在的审批按钮。
  if (activeRun.status === "WAITING_CHILD") {
    const tool = waitingToolNameOf(activeRun);
    return tool ? `等待子智能体 · ${tool}` : "等待子智能体";
  }
  if (activeRun.waiting || activeRun.status === "WAITING_HUMAN") {
    const tool = waitingToolNameOf(activeRun);
    return tool ? `等待确认 · ${tool}` : "等待确认";
  }
  const current = [...digest].reverse().find((s) => s.state === "active" || s.state === "waiting" || s.state === "fail")
    ?? digest[digest.length - 1];
  if (!current) return TERMINAL.has(activeRun.status) ? "无关键步骤" : "正在准备";
  return clipDetail([current.label, current.detail].filter(Boolean).join(" · "), 30);
}

/** 工具详情：折叠态只显示工具名，参数/结果点「详情」才展开。 */
function ToolChip({ tool }: { tool: NonNullable<DigestStep["tool"]> }) {
  const [open, setOpen] = useState(false);
  const hasDetail = Boolean(tool.args || tool.result);
  return (
    <span className="chat-tool-chip">
      <span className="chat-tool-chip-head">
        <span className="chat-tool-chip-name">{tool.name}</span>
        {hasDetail && (
          <button
            className="chat-tool-chip-toggle"
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
          >
            {open ? "收起" : "详情"}
          </button>
        )}
      </span>
      {open && (
        <span className="chat-tool-detail">
          {tool.args && <pre className="chat-tool-args">参数：{tool.args}</pre>}
          {tool.result && <pre className="chat-tool-result">结果：{tool.result}</pre>}
        </span>
      )}
    </span>
  );
}

// 执行中每秒刷新「已进行 Ns」，用户不用展开链路也能判断是否卡住
function useElapsed(startedAt: number | null, active: boolean): string {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active || startedAt === null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, startedAt]);
  if (startedAt === null || !active) return "";
  const seconds = Math.max(0, Math.round((now - startedAt) / 1000));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, "0")}s`;
}

/**
 * 内联执行链路：
 * - 默认折叠成一行「已处理 Xs · 当前动作」，结论不被过程挤走；
 * - 等待人工确认自动展开（审批按钮在块内），其余状态交给用户点开；
 * - 展开后只列关键节点，工具参数/结果再折叠一层，必要时可查看完整事件。
 */
export interface WaitingConfirmationSummary {
  kind?: string;
  title?: string;
  script_name?: string;
  script_file?: string;
  sandbox_mode?: string;
  sandbox_isolation?: string;
}

export function RunBlock({ activeRun, taskError, onResolve, initiallyOpen, waitingSummary, canResolve = false }: {
  activeRun: ActiveRun;
  taskError: { message: string; code: string | null } | null;
  onResolve: (approved: boolean) => void;
  /** 显式指定初始展开态（历史链路点开时用）；缺省按等待审批决定 */
  initiallyOpen?: boolean;
  /** 后端返回的脱敏确认摘要 */
  waitingSummary?: WaitingConfirmationSummary | null;
  /** 是否允许当前用户处理本确认单，由服务端判定 */
  canResolve?: boolean;
}) {
  const runStatusInfo = useMemo(() => runStatusInfoOf(activeRun), [activeRun]);
  const waitingToolName = useMemo(() => waitingToolNameOf(activeRun), [activeRun]);
  const waitingConfirmArgs = useMemo(() => waitingConfirmArgsOf(activeRun), [activeRun]);
  const digest = useMemo(() => buildDigest(activeRun.steps, activeRun), [activeRun]);
  const headline = useMemo(() => headlineOf(digest, activeRun), [digest, activeRun]);

  const live = runStatusInfo.cls === "run" || runStatusInfo.cls === "wait";
  // WAITING_CHILD 也会把 activeRun.waiting 置真（父任务在等子任务），
  // 但它不是审批：必须与 WAITING_HUMAN 分开，否则会渲染出一个没有按钮的「需要确认」提示。
  const waitingChild = activeRun.status === "WAITING_CHILD";
  const confirming = activeRun.waiting && !waitingChild;
  const autoOpen = activeRun.waiting || activeRun.status === "WAITING_HUMAN";
  const [open, setOpen] = useState(initiallyOpen ?? autoOpen);
  const [userToggled, setUserToggled] = useState(false);
  const [showAll, setShowAll] = useState(false);

  // 新任务到来时恢复自动接管
  useEffect(() => {
    setUserToggled(false);
    setShowAll(false);
  }, [activeRun.taskId]);

  useEffect(() => {
    if (userToggled) return;
    setOpen(initiallyOpen ?? autoOpen);
  }, [autoOpen, initiallyOpen, userToggled]);

  const elapsed = useElapsed(activeRun.startedAt ?? null, live);
  const timeText = elapsed ? `已处理 ${elapsed}` : "执行过程";
  const errorText = taskError ? `${taskError.message}${taskError.code ? `（${taskError.code}）` : ""}` : "";

  return (
    <div className={`chat-run-block ${runStatusInfo.cls}`}>
      <button
        className="chat-run-toggle"
        type="button"
        onClick={() => {
          setUserToggled(true);
          setOpen((v) => !v);
        }}
        aria-expanded={open}
      >
        <span className={`chat-run-dot ${runStatusInfo.cls}`} aria-hidden="true" />
        <span className="chat-run-toggle-label">{timeText}</span>
        <span className="chat-run-headline">{headline}</span>
        <span className={`chat-run-status ${runStatusInfo.cls}`}>{runStatusInfo.label}</span>
        <span className={`chat-run-chevron ${open ? "open" : ""}`} aria-hidden="true" />
      </button>

      {open && (
        <div className="chat-run-body">
          {digest.length === 0 ? (
            <div className="chat-run-empty">
              {live ? <><span className="spinner" /> 正在准备…</> : "无关键步骤，可查看完整事件"}
            </div>
          ) : (
            <ol className="chat-run-steps">
              {digest.map((s) => (
                <li key={s.key} className={`chat-run-step ${s.state}`}>
                  <span className={`chat-run-step-dot ${s.state}`} aria-hidden="true" />
                  <div className="chat-run-step-main">
                    <span className="chat-run-label">{s.label}</span>
                    {s.detail && (
                      <>
                        <span className="chat-run-sep">·</span>
                        <span className="chat-run-desc">{s.detail}</span>
                      </>
                    )}
                    {s.tool && <ToolChip tool={s.tool} />}
                  </div>
                </li>
              ))}
            </ol>
          )}

          {activeRun.steps.length > digest.length && (
            <button className="chat-run-more" type="button" onClick={() => setShowAll((v) => !v)}>
              {showAll ? "收起完整事件" : `查看完整事件（${activeRun.steps.length}）`}
            </button>
          )}
          {showAll && (
            <ol className="chat-run-raw">
              {activeRun.steps.map((s) => (
                <li key={`raw-${s.seq}`}>
                  <span>{eventLabel(s.eventType)}</span>
                  <span>{clipDetail(s.summary, 80)}</span>
                </li>
              ))}
            </ol>
          )}

          {/* 委派子任务：父任务释放执行位等待子任务，不需要任何用户操作 */}
          {waitingChild && (
            <div className="chat-run-child-wait" role="status" aria-live="polite">
              <span className="spinner" aria-hidden="true" />
              <p>
                {waitingToolName
                  ? `已委派给子智能体（${waitingToolName}），正在等待其执行完成，完成后会自动继续。`
                  : "正在等待子智能体执行完成，完成后会自动继续。"}
              </p>
            </div>
          )}

          {/* 所有需要人工确认的操作统一在执行块中审批；等待时任务已释放执行位。 */}
          {confirming && (
            <div className="chat-run-confirm" role="alert" aria-live="polite">
              <p>
                {waitingSummary?.kind === "md_script"
                  ? <>即将执行 Python 脚本「{waitingSummary.script_name || "未命名 Skill"}」，批准后才会启动沙箱。</>
                  : <>操作「{waitingToolName ?? "待审批操作"}」需要人工确认，批准后才会继续执行。</>}
              </p>
              {waitingSummary?.kind === "md_script" && (
                <div className="chat-run-desc">
                  {waitingSummary.script_file && <div>脚本文件：{waitingSummary.script_file}</div>}
                  <div>权限范围：{waitingSummary.sandbox_mode === "full" ? "工作区级权限（必须容器隔离）" : "受限目录策略"}</div>
                  <div>执行边界：{waitingSummary.sandbox_isolation === "container" ? "Docker 容器：无网络、只读根文件系统和资源上限" : "独立进程：共享服务账户的宿主机文件/网络权限，不是完整 OS 沙箱"}</div>
                  <div>安全提示：批准仅授权本次执行，不会扩大权限或解除资源限制。</div>
                </div>
              )}
              {waitingConfirmArgs && <pre className="chat-confirm-args">{waitingConfirmArgs}</pre>}
              {canResolve ? (
                <div className="chat-confirm-actions">
                  <button className="chat-btn-confirm" type="button" onClick={() => onResolve(true)}>批准执行</button>
                  <button className="chat-btn-reject" type="button" onClick={() => onResolve(false)}>拒绝</button>
                </div>
              ) : (
                <p className="chat-run-desc">
                  {waitingSummary
                    ? "当前操作需要确认，但你没有处理该确认单的权限。请联系发起人或管理员处理。"
                    : "未找到待处理的确认记录（可能已被处理或已过期），任务将自动继续，无需操作。"}
                </p>
              )}
            </div>
          )}

          {errorText && <div className="chat-run-error">{errorText}</div>}
        </div>
      )}

      {/* 折叠态也要能看见失败原因，否则用户不知道"为什么没有结果" */}
      {!open && errorText && (
        <div className="chat-run-error chat-run-error-inline">{errorText}</div>
      )}
    </div>
  );
}

interface HistoricalEventRow {
  seq: number;
  event_type: string;
  summary: string;
  created_at: string;
  payload_summary?: Record<string, unknown> | null;
}

/**
 * 历史任务的内联执行链路。
 *
 * 替代原来的「点击 → 右侧滑窗查看执行链路」：链路直接展开在这条回复下方，
 * 位置与发生顺序一致，不用视线横跳。
 * 事件按需拉取（首次点开才请求），避免一屏几十条历史消息同时打后端。
 */
export function HistoricalRun({ taskId }: { taskId: string }) {
  const [run, setRun] = useState<ActiveRun | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  async function load() {
    if (run || loading) return;
    setLoading(true);
    setFailed(false);
    try {
      const [task, events] = await Promise.all([
        request<{ status: string }>(`/api/z-torque/tasks/${taskId}`),
        request<{ items: HistoricalEventRow[] }>(`/api/z-torque/tasks/${taskId}/events`),
      ]);
      setRun({
        taskId,
        status: task.status,
        waiting: false,
        stopped: false,
        steps: events.items.map((e) => ({
          seq: e.seq,
          eventType: e.event_type,
          summary: e.summary,
          time: new Date(e.created_at).toLocaleTimeString(),
          payload: e.payload_summary ?? null,
        })),
      });
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }

  // 已加载：交给 RunBlock 的折叠行渲染（点开即展开一次，之后由用户控制）
  if (run) {
    return <RunBlock activeRun={run} taskError={null} onResolve={() => {}} initiallyOpen={expanded} />;
  }

  return (
    <div className="chat-run-block">
      <button
        className="chat-run-toggle"
        type="button"
        onClick={() => {
          setExpanded(true);
          void load();
        }}
        aria-expanded={false}
      >
        <span className="chat-run-dot" aria-hidden="true" />
        <span className="chat-run-toggle-label">执行过程</span>
        <span className="chat-run-toggle-meta">{loading ? "加载中…" : failed ? "加载失败，点击重试" : "点击查看"}</span>
        <span className="chat-run-chevron" aria-hidden="true" />
      </button>
    </div>
  );
}
