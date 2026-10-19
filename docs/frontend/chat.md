# Chat 会话内联实时执行 · 实现方案

基于当前 `chat.html` 的交互定稿，供前后端按同一合同实现。

---

## 1. 目标

用户发消息后，若产生 Task / 工具调用：

1. **先在会话流中实时展开执行链路**（理解 → 建任务 → 排队 → 运行 → 调工具 → 回结果）  
2. **全部关键步骤结束后，再在同一轮助手回复中流式输出最终结论文本**  
3. **不使用右侧过程面板作为主路径**（避免主对话出现长时间空白）

无工具的纯问答：不出现执行块，仅流式文本即可。

---

## 2. 信息架构（一轮助手回复）

```text
msg-row.assistant
└─ assistant-turn
   ├─ run-block          ← 有 Task 时立刻出现，SSE 追加步骤
   │   ├─ 头：执行过程 + 状态（进行中 / 已完成 / 失败 / 等待确认）
   │   └─ run-steps[]    ← 按事件 append，禁止等终态一次性渲染
   └─ msg-bubble         ← 仅在可输出最终答复时显示并流式填充
```

视觉顺序固定：**过程在上，结论在下。**

---

## 3. 时序（前后端合同）

```text
T0  用户发送
T1  POST 消息成功 → 返回 conversation_id；若需执行则带 task_id
T2  前端：插入 assistant-turn，显示 run-block（进行中）
T3  前端：订阅 Task 事件流（SSE / Fetch stream，after_seq）
T4  每来一个事件 → append 一步（人话标题 + 可选详情/工具卡）
T5  收到终态可展示的结果（result / 最终文本通道）
T6  将 run-block 标为已完成 → 显示 bubble → 流式写结论
```

**禁止：** 先流式长结论，再补全执行步骤。  
**允许：** 极短的「理解中」作为执行块的**第一步**，不算最终答复。

---

## 4. 事件 → 步骤映射

| 后端事件（示例） | 步骤标题 | 详情 / 附加 |
|------------------|----------|-------------|
| （本地）开始处理 | 理解需求 | 识别是否需要工具 |
| task created | 任务已创建 | 可选短 task 编号 |
| queued | 排队中 | |
| running | 开始执行 | |
| tool_call | 正在调用能力 | 工具名 + 参数摘要卡片 |
| tool_result | 能力返回结果 | 脱敏摘要，非全量原始数据 |
| waiting_human | 需要你确认 | 内联确认/拒绝按钮 |
| human_decision | 已确认 / 已拒绝 | |
| result / completed | 汇总结论（可选） | 然后进入文字气泡 |
| error | 执行失败 | 短错误说明 |

状态标签：

- 进行中 · 等待确认 · 已完成 · 失败 / 已停止  

主文案用中文；`TASK_RUNNING` 等枚举仅调试或次要灰字。

---

## 5. 前端模块职责

| 模块 | 职责 |
|------|------|
| `sendMessage` | 发消息；拿到 `task_id` 后创建 `assistant-turn` |
| `subscribeTaskEvents(taskId, afterSeq)` | SSE；`onEvent` → `appendStep` |
| `appendStep` | 时间线追加；上一步 active→done |
| `showFinalAnswer(text)` | 过程结束后显示 bubble 并流式/整段写入 |
| `mapEventToStep(ev)` | 事件 → `{ label, detail, extraHtml }` |
| 停止 | 取消订阅 + 调 cancel API；过程标「已停止」 |

状态建议：

- `timeline: Step[]`（按 `seq` 去重排序）  
- `phase: 'running' | 'waiting_human' | 'answering' | 'done' | 'error'`  
- `finalText` 仅在 `answering/done` 写入 UI  

---

## 6. 后端需保证

1. 状态变化、tool_call、tool_result、等待人、结束都写 **TaskEvent + 递增 seq**，并推流。  
2. **不要**只在任务结束写一条汇总事件。  
3. SSE 支持 `after_seq` 续传；断开不等于任务失败。  
4. Chat 文本流与 Task 事件流可分离：最终答复可走 `task.result` 或独立 chat 终态消息，但**前端必须等过程阶段完成（或明确可并行的策略）再展示结论文案**。  
5. 工具参数/结果对展示做脱敏与长度限制。

---

## 7. 人工确认

- 事件 `waiting_human` → 在对应 step 内嵌确认区（同一会话流，不跳右侧）。  
- 用户确认/拒绝 → API → 继续收 SSE 后续步骤 → 最后再出结论。

---

## 8. 与旧版右侧面板

| 项 | 决策 |
|----|------|
| 默认 UI | 会话内联执行块 |
| 右侧过程栏 | 不做主路径；可废弃或仅「查看任务详情」入口 |
| 文档验收 | 「实时查看执行过程」由内联时间线满足 |

---

## 9. 验收清单

- [ ] 发会调工具的消息后，**先**出现执行块，**再**出现结论文字  
- [ ] 运行中步骤逐条增加，不必等完成  
- [ ] 能看到「正在调用能力」与返回摘要  
- [ ] 纯聊天无执行块  
- [ ] 刷新后按 seq 恢复已发生步骤  
- [ ] 停止/失败状态正确  
- [ ] 等待确认时内联可操作  

---

## 10. 参考实现

静态演示与 DOM 结构见：

`chat.html`

将其中 `simulateAgentTurn` 换为真实 `subscribeTaskEvents` + `showFinalAnswer` 即可上生产逻辑；**DOM 顺序与阶段 A/B 划分保持不变。**

---

**一句话：** 一轮助手回复 = **先实时执行链路，后流式结论**；Task SSE 驱动步骤，终态结果驱动文字气泡。