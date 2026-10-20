// 会话执行块（run-block）共享模块：主 Chat 页与嵌入对话页（/embed/chat）共用。
// 抽取自 Chat.tsx（chat.md 定稿：过程在上、结论在下；逐事件完整时间线，禁止合并删减），
// 保证两端的实时链路展示永远一致。样式复用 index.css 的 .chat-run-* / .chat-tool-* / .chat-confirm-*。
import { useMemo } from "react";

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
    TASK_MODEL_SUCCEEDED: "模型推理完成",
    TASK_SKILL_SUCCEEDED: "工具执行完成",
    TASK_SUCCEEDED: "任务完成",
    TASK_FAILED: "任务失败",
    TASK_TIMEOUT: "任务超时",
    TASK_CANCELLED: "任务已取消",
    TASK_CANCEL_REQUESTED: "请求取消",
    TASK_WAITING_HUMAN: "等待人工确认",
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
export function runStatusInfoOf(activeRun: ActiveRun | null): { cls: string; label: string } {
  if (!activeRun) return { cls: "run", label: "进行中" };
  if (activeRun.stopped) return { cls: "fail", label: "已停止" };
  if (activeRun.waiting || activeRun.status === "WAITING_HUMAN") return { cls: "wait", label: "等待确认" };
  if (activeRun.status === "SUCCESS") return { cls: "ok", label: "已完成" };
  if (TERMINAL.has(activeRun.status)) return { cls: "fail", label: "已结束" };
  return { cls: "run", label: "进行中" };
}

// 内联单步状态：末步运行中高亮（active），已发生步骤 done，等待/失败单独着色
export function runStepState(s: TaskStep, idx: number, total: number, activeRun: ActiveRun | null): "active" | "done" | "waiting" | "fail" {
  if (s.eventType.includes("FAILED") || s.eventType.includes("ERROR") || s.eventType === "task.error") return "fail";
  if (s.eventType === "LOCAL_STOPPED") return "fail";
  if (s.eventType === "TASK_WAITING_HUMAN" || s.eventType === "task.waiting_human") return "waiting";
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

/**
 * 内联执行块渲染组件：与主 Chat 页对话流中的执行过程块完全一致
 * （chat-run-block 时间线 + 工具内联卡片 + 人工确认框 + 失败原因）。
 */
export function RunBlock({ activeRun, taskError, onResolve }: {
  activeRun: ActiveRun;
  taskError: { message: string; code: string | null } | null;
  onResolve: (approved: boolean) => void;
}) {
  const runStatusInfo = useMemo(() => runStatusInfoOf(activeRun), [activeRun]);
  const waitingToolName = useMemo(() => waitingToolNameOf(activeRun), [activeRun]);
  const waitingConfirmArgs = useMemo(() => waitingConfirmArgsOf(activeRun), [activeRun]);
  return (
    <div className={`chat-run-block ${runStatusInfo.cls}`}>
      <div className="chat-run-head">
        <span className="chat-run-title">执行过程</span>
        <span className={`chat-run-status ${runStatusInfo.cls}`}>{runStatusInfo.label}</span>
      </div>
      <div className="chat-run-steps">
        {activeRun.steps.length === 0 ? (
          <div className="chat-run-empty"><span className="spinner" /> 正在创建任务…</div>
        ) : (
          activeRun.steps.map((s, idx) => {
            const cls = runStepState(s, idx, activeRun.steps.length, activeRun);
            const tool = toolCardOf(s);
            return (
              <div key={s.seq} className={`chat-run-step ${cls}`}>
                <div className="chat-run-rail"><span className="node" /><span className="line" /></div>
                <div className="chat-run-body">
                  <div className="chat-run-label">{eventLabel(s.eventType)}</div>
                  {runStepDesc(s) && <div className="chat-run-desc">{runStepDesc(s)}</div>}
                  {/* 工具调用/返回内联卡片：参数与结果摘要 */}
                  {tool && (
                    <div className="chat-tool-card">
                      <div className="chat-tool-name"><span className="chat-tool-tag">工具</span>{tool.name}</div>
                      {tool.args && <pre className="chat-tool-args">{tool.args}</pre>}
                      {tool.result && <div className="chat-tool-result">{tool.result}</div>}
                    </div>
                  )}
                  <div className="chat-run-time">{s.time}</div>
                </div>
              </div>
            );
          })
        )}
      </div>
      {/* 写入类工具等待审批：确认框内联在执行块中（批准/拒绝） */}
      {activeRun.waiting && (
        <div className="chat-run-confirm">
          <p>工具「{waitingToolName ?? "未知工具"}」为写入操作，需人工审批后才会执行。</p>
          {waitingConfirmArgs && <pre className="chat-confirm-args">{waitingConfirmArgs}</pre>}
          <div className="chat-confirm-actions">
            <button className="chat-btn-confirm" type="button" onClick={() => onResolve(true)}>批准执行</button>
            <button className="chat-btn-reject" type="button" onClick={() => onResolve(false)}>拒绝</button>
          </div>
        </div>
      )}
      {taskError && (
        <div className="chat-run-error">{taskError.message}{taskError.code ? `（${taskError.code}）` : ""}</div>
      )}
    </div>
  );
}
