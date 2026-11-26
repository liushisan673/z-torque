// 会话执行块（run-block）共享模块：主 Chat 页与嵌入对话页（/embed/chat）共用。
// 抽取自 Chat.tsx（chat.md 定稿：过程在上、结论在下；逐事件完整时间线，禁止合并删减），
// 保证两端的实时链路展示永远一致。
// 折叠外壳用 @ant-design/x `Think`（阶段13 第 4 批）：标题行/展开动画/进行中 spinner 由组件库提供，
// 内部步骤行仍用 .chat-run-* / .chat-tool-* / .chat-confirm-*（逐事件链路是业务语义，不外借样式）。
import { useEffect, useMemo, useState } from "react";
import { Think } from "@ant-design/x";

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

/**
 * 工具调用/返回的紧凑卡片。
 * 借鉴 DSH：默认只显示「工具名 + 结果摘要」，参数折叠在「详情」里，
 * 避免长 JSON 把执行链路撑开、把结论挤到屏幕外。
 */
function ToolChip({ tool }: { tool: { name: string; args?: string | null; result?: string | null } }) {
  const [openArgs, setOpenArgs] = useState(false);
  const hasArgs = Boolean(tool.args);
  return (
    <div className="chat-tool-chip">
      <div className="chat-tool-chip-head">
        <span className="chat-tool-tag">工具</span>
        <span className="chat-tool-chip-name">{tool.name}</span>
        {hasArgs && (
          <button
            className="chat-tool-chip-toggle"
            type="button"
            onClick={() => setOpenArgs((v) => !v)}
            aria-expanded={openArgs}
          >
            {openArgs ? "收起参数" : "参数"}
          </button>
        )}
      </div>
      {tool.result && <div className="chat-tool-result">{tool.result}</div>}
      {openArgs && tool.args && <pre className="chat-tool-args">{tool.args}</pre>}
    </div>
  );
}

/**
 * 内联执行链路（借鉴 DSH 的 DisclosureRow）：
 *
 * - 折叠时只占一行：状态 + 步数 + 已进行时间，右侧 chevron 提示可展开；
 * - 执行中自动展开，进入终态后自动收起（结论回到首屏，不用滚）；
 * - 用户手动点过之后不再自动接管，避免"想看着却被自动收起"；
 * - 等待人工确认时保持展开，因为批准按钮在块内；
 * - 执行中每秒刷新「已进行 Ns」，用户能判断是卡住还是在跑（借鉴 DSH 的执行行）。
 */
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

export interface WaitingConfirmationSummary {
  kind?: string;
  title?: string;
  script_name?: string;
  script_file?: string;
  sandbox_mode?: string;
  sandbox_isolation?: string;
}

export function RunBlock({ activeRun, taskError, onResolve, initiallyOpen, waitingSummary }: {
  activeRun: ActiveRun;
  taskError: { message: string; code: string | null } | null;
  onResolve: (approved: boolean) => void;
  /** 显式指定初始展开态（历史链路点开时用）；缺省按运行态自动决定 */
  initiallyOpen?: boolean;
  /** 后端返回的脱敏确认摘要，不包含脚本正文、原始参数或绝对路径 */
  waitingSummary?: WaitingConfirmationSummary | null;
}) {
  const runStatusInfo = useMemo(() => runStatusInfoOf(activeRun), [activeRun]);
  const waitingToolName = useMemo(() => waitingToolNameOf(activeRun), [activeRun]);
  const waitingConfirmArgs = useMemo(() => waitingConfirmArgsOf(activeRun), [activeRun]);

  // 运行中 / 等待确认 → 展开；终态 → 收起
  const live = runStatusInfo.cls === "run" || runStatusInfo.cls === "wait";
  const [open, setOpen] = useState(initiallyOpen ?? true);
  const [userToggled, setUserToggled] = useState(false);

  // 新任务到来时恢复自动接管
  useEffect(() => {
    setUserToggled(false);
  }, [activeRun.taskId]);

  useEffect(() => {
    if (userToggled) return;
    setOpen(initiallyOpen ?? live);
  }, [live, userToggled, initiallyOpen]);

  const stepCount = activeRun.steps.length;
  const elapsed = useElapsed(activeRun.startedAt ?? null, live);
  const meta = [stepCount === 0 ? "准备中" : `${stepCount} 步`, elapsed].filter(Boolean).join(" · ");

  const errorText = taskError ? `${taskError.message}${taskError.code ? `（${taskError.code}）` : ""}` : "";

  return (
    <div className={`chat-run-block ${runStatusInfo.cls}`}>
      {/* 折叠外壳用 antd X Think：标题行、展开动画、进行中 spinner 由组件库承担 */}
      <Think
        className="chat-run-think"
        title={
          <span className="chat-run-head">
            <span className="chat-run-name">执行过程</span>
            <span className={`chat-run-status ${runStatusInfo.cls}`}>{runStatusInfo.label}</span>
            <span className="chat-run-meta">{meta}</span>
          </span>
        }
        loading={live}
        blink={live}
        expanded={open}
        onExpand={(next) => {
          setUserToggled(true);
          setOpen(next);
        }}
        // 必须保持默认 destroyOnHidden=true：X 的 CSSMotion 收敛后只挂 leavedClassName
        // （ant-think-content-hidden），而 @ant-design/x 并没有为它生成 display:none，
        // 内容会重新显形 —— 表现就是"点一下能展开、再点收不起来"。卸载才是可靠的收起。
      >
        {stepCount === 0 ? (
          <div className="chat-run-empty"><span className="spinner" /> 正在创建任务…</div>
        ) : (
          <ol className="chat-run-steps">
            {activeRun.steps.map((s, idx) => {
              const cls = runStepState(s, idx, stepCount, activeRun);
              const tool = toolCardOf(s);
              return (
                <li key={s.seq} className={`chat-run-step ${cls}`}>
                  <span className={`chat-run-step-dot ${cls}`} aria-hidden="true" />
                  <div className="chat-run-step-main">
                    <div className="chat-run-step-row">
                      <span className="chat-run-label">{eventLabel(s.eventType)}</span>
                      <span className="chat-run-time">{s.time}</span>
                    </div>
                    {runStepDesc(s) && <div className="chat-run-desc">{runStepDesc(s)}</div>}
                    {tool && <ToolChip tool={tool} />}
                  </div>
                </li>
              );
            })}
          </ol>
        )}

        {/* 所有需要人工确认的操作统一在执行块中审批；等待时保持展开，任务已释放执行位。 */}
        {activeRun.waiting && (
          <div className="chat-run-confirm" role="alert" aria-live="polite">
            <p>
              {waitingSummary?.kind === "md_script"
                ? <>即将执行 Python 脚本「{waitingSummary.script_name || "未命名 Skill"}」，批准后才会启动沙箱。</>
                : <>操作「{waitingToolName ?? "待审批操作"}」需要人工确认，批准后才会继续执行。</>}
            </p>
            {waitingSummary?.kind === "md_script" && (
              <div className="chat-run-desc">
                {waitingSummary.script_file && <div>脚本文件：{waitingSummary.script_file}</div>}
                <div>执行范围：{waitingSummary.sandbox_mode === "full" ? "完整工作区（受限容器）" : "Skill 目录（受限模式）"}</div>
                <div>隔离方式：{waitingSummary.sandbox_isolation === "container" ? "Docker 容器" : "独立进程"}</div>
                <div>安全提示：批准仅授权本次执行，不会扩大文件访问权限或解除资源限制。</div>
              </div>
            )}
            {waitingConfirmArgs && <pre className="chat-confirm-args">{waitingConfirmArgs}</pre>}
            <div className="chat-confirm-actions">
              <button className="chat-btn-confirm" type="button" onClick={() => onResolve(true)}>批准执行</button>
              <button className="chat-btn-reject" type="button" onClick={() => onResolve(false)}>拒绝</button>
            </div>
          </div>
        )}

        {errorText && <div className="chat-run-error">{errorText}</div>}
      </Think>

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
