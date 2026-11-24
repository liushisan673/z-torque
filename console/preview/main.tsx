// 本地交互预览：只为了在真实浏览器里验证「执行链路折叠行」与「实时追加步骤」的行为。
// 直接渲染生产组件（pages/chatRun.tsx + @ant-design/x 的 Bubble），不复制实现。
// 构建：npx vite build --config vite.preview.config.ts；产物用不 spawn 子进程的静态服务器打开。
import { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { Bubble } from "@ant-design/x";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { RunBlock, localStep, type ActiveRun, type TaskStep } from "../src/pages/chatRun";
import "../src/index.css";

/** 模拟一轮真实执行：等待 → 工具调用 → 工具返回 → 模型推理 → 完成 */
function LiveCase() {
  const [steps, setSteps] = useState<TaskStep[]>([localStep("LOCAL_UNDERSTAND", "识别到需要执行，准备创建任务")]);
  const [status, setStatus] = useState("QUEUED");
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => setTick((v) => v + 1), 1200);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (tick === 0) return;
    if (tick === 1) setStatus("RUNNING");
    setSteps((prev) => {
      const next = [...prev];
      if (tick === 2) next.push({ seq: 10, eventType: "TASK_TOOL_CALL", summary: "calling tool orders_query", time: new Date().toLocaleTimeString(), payload: { tool_name: "orders_query", arguments: { limit: 3 } } });
      if (tick === 3) next.push({ seq: 11, eventType: "TASK_TOOL_RESULT", summary: "tool orders_query returned", time: new Date().toLocaleTimeString(), payload: { tool_name: "orders_query", status: "SUCCEEDED", result: "3 条订单" } });
      if (tick === 4) next.push({ seq: 12, eventType: "TASK_MODEL_STARTED", summary: "model round 2 started", time: new Date().toLocaleTimeString(), payload: { round: 2 } });
      if (tick >= 8) setStatus("SUCCESS");
      return next;
    });
  }, [tick]);

  const run = useMemo<ActiveRun>(
    () => ({ taskId: "demo-live", steps, status, waiting: false, stopped: false, startedAt: Date.now() - tick * 1200 }),
    [steps, status, tick],
  );

  // 与生产 Chat.tsx 完全一致的组合：执行块在 Bubble 的 header 槽，正文为空时不得让 loading 吞掉 header
  const turnText = status === "SUCCESS" ? "订单共 3 条，已按时间倒序排列。" : "";
  const loading = turnText.length === 0 && !run;

  return (
    <section id="live-case">
      <h3>1. 执行中：执行块挂在 Bubble header 槽（运行中应自动展开并逐步追加）</h3>
      <Bubble
        className="chat-bubble"
        classNames={{ body: "chat-bubble-body", content: "chat-bubble-content", header: "chat-bubble-header", footer: "chat-bubble-footer", avatar: "chat-bubble-avatar" }}
        placement="start"
        variant="borderless"
        avatar={<span className="chat-avatar-ai" style={{ width: 30, height: 30, borderRadius: "50%", display: "grid", placeItems: "center", fontSize: 12 }}>Z</span>}
        header={<RunBlock activeRun={run} taskError={null} onResolve={() => {}} />}
        loading={loading}
        loadingRender={() => <div className="chat-wait"><span className="spinner" /><span>正在执行…</span></div>}
        content={<div className="chat-md"><Markdown remarkPlugins={[remarkGfm]}>{turnText}</Markdown></div>}
      />
    </section>
  );
}

/** 历史链路：已完成（等价于 HistoricalRun 加载完成后的初始态） */
function HistoryCase() {
  const run: ActiveRun = {
    taskId: "demo-history",
    status: "SUCCESS",
    waiting: false,
    stopped: false,
    steps: [
      { seq: 1, eventType: "TASK_CREATED", summary: "task created", time: "10:00:01", payload: null },
      { seq: 2, eventType: "TASK_STARTED", summary: "task started", time: "10:00:02", payload: null },
      { seq: 3, eventType: "TASK_TOOL_CALL", summary: "calling tool orders_query", time: "10:00:03", payload: { tool_name: "orders_query", arguments: { limit: 3 } } },
      { seq: 4, eventType: "TASK_TOOL_RESULT", summary: "tool returned", time: "10:00:04", payload: { tool_name: "orders_query", status: "SUCCEEDED", result: "3 条订单" } },
      { seq: 5, eventType: "TASK_MODEL_STARTED", summary: "model round 2 started", time: "10:00:05", payload: { round: 2 } },
    ],
  };
  return (
    <section id="history-case">
      <h3>2. 已完成（历史链路加载完成，初始收起；点标题行展开、再点收起）</h3>
      <div id="history-closed">
        <RunBlock activeRun={run} taskError={null} onResolve={() => {}} initiallyOpen={false} />
      </div>
      <h3 style={{ marginTop: 24 }}>3. 已完成（HistoricalRun 点击后：initialOpen=true）</h3>
      <div id="history-open">
        <RunBlock activeRun={{ ...run, taskId: "demo-history-open" }} taskError={null} onResolve={() => {}} initiallyOpen />
      </div>
    </section>
  );
}

createRoot(document.getElementById("root")!).render(
  <ThemeProvider>
    <div style={{ padding: 24, maxWidth: 760, margin: "0 auto", fontFamily: "var(--font)" }}>
      <LiveCase />
      <HistoryCase />
    </div>
  </ThemeProvider>,
);
