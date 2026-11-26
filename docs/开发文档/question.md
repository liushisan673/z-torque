
你现在接手 d:/project/z-torque-community-m1 项目。

开始前必须先读取并理解：
1. docs/memmemeory.md（实际文件名是 memmemeory.md）
2. docs/Z-Torque_平台能力优化路线图_v1.md（文件名是 v1，但正文是定稿 v2.0）
3. 当前前端相关文件：
   - console/src/pages/Chat.tsx
   - console/src/components/Phase1Panels.tsx
   - console/src/components/PaneResizer.tsx
   - console/src/index.css
   - console/src/services/api.ts
   - console/src/types/index.ts
4. 当前后端相关文件：
   - app/main.py（capabilities 生成）
   - app/api/routes/usage.py
   - app/api/routes/chat_events.py
   - app/services/stream_service.py
   - app/services/execution_loop.py
   - app/services/function_calling.py
   - app/kernel/model_gateway/contracts.py
   - app/config.py
   - app/api/router.py
   - tests/conftest.py
5. 当前项目记忆里记录的 Phase 1 状态。

当前状态必须正确理解：
- Phase 1 后端 P1a-1~9 已工程实现。
- Phase 1 前端 P1b-1~6 只是做了一版技术入口，不满足真实用户可用性。
- 现在不能再说“Phase 1 真正完成”。
- 必须做“P1b UX 返工 + 必要后端支持”，完成后才能把项目状态更新为“Phase 1 真正完成”。

当前基线：
- pytest：662 passed / 24 skipped
- ruff：All checks passed
- npm run build：成功
- 当前 alembic head：b1c2d3e4f5a6
- 工作区已有大量历史改动，禁止 git reset、git clean、git checkout 覆盖用户改动。
- 不要删除 .codebuddy 目录。

必须遵守三项最高约束：
1. 单机安装：不做租户矩阵，不引入跨租户逻辑。
2. 内核 Port 冻结：PORT_SPECS 仍保持 21 个 Port，不新增内核 Port；必要能力放 builtin plugin 或 app/services。
3. exe 打包：不新增 Python/Node 依赖；尤其不要引入 shiki、diff、motion、framer-motion、monaco 等前端依赖。

工作方式：
- 严格一次只做一个返工任务。
- 每个任务完成后必须给我：
  1. 改动文件清单
  2. diff 摘要
  3. 真实入口验收结果
  4. pytest / ruff / npm run build 结果
  5. 写入 docs/memmemeory.md 的记录
- 等我确认后再做下一项。
- 不允许只凭 npm run build 就说 UI 完成；必须启动前端并用浏览器实际点击验证。
- 如果浏览器工具可用，必须使用浏览器自动化完成点击验证。
- 如果后端接口能力不足，必须补后端，不允许前端伪造状态或假装生效。
- 每次都先补测试或验收用例，再实现。

本轮目标：
把当前 P1b 前端从“技术入口”返工成“用户真正看得懂、用得明白、工作流自然，并且前后端真实联动”的阶段完成状态。

---

# 任务 1：能力/用量头部横向自然语言化 + 后端状态原因和动作支持

## 当前问题
- [能力/用量] 当前是按钮 + 抽屉 + 专业状态标签。
- 用户看不到真实状态。
- AVAILABLE / DEGRADED / UNAVAILABLE、MCP、token、execution、lifecycle 等词普通用户看不懂。
- 当前只显示不可用，没有原因和解决动作。
- 前端不能自己编造原因，后端必须提供结构化信息。

## 前端要求
1. 把能力/用量直接横向展示在 Chat 头部，不要只藏在弹窗或按钮里。
2. 使用自然语言：
   - MCP → 外部工具
   - AVAILABLE → 可用
   - DEGRADED → 受限
   - UNAVAILABLE → 不可用
   - token → 用量
   - unknown → 未上报
3. 头部至少显示：
   - 外部工具：可用 / 受限 / 不可用
   - 知识检索：可用 / 受限 / 不可用
   - 模型：可用 / 受限 / 不可用
   - 今日用量：输入 / 输出简洁数字
4. 不可用时必须展示：
   - 状态
   - 原因
   - 解决动作
   示例：
   - 外部工具不可用：未连接 MCP 服务 → [去配置连接器] [重新测试连接]
   - 知识检索不可用：知识库未初始化 → [去上传资料] [重建索引]
   - 模型不可用：未配置模型 → [去模型管理]
5. 点击能力项或动作按钮能跳转到对应管理页。
6. 使用文字优先，不要纯图标表达关键状态。
7. 窄屏可折叠，但不能丢失文字状态。
8. 不允许展示 AVAILABLE、unknown、MCP、execution、lifecycle 等裸术语。

## 后端要求
1. 扩展 /api/z-torque/capabilities 返回，保持旧字段向后兼容。
2. 为外部工具、知识检索、模型等能力补充结构化字段：
   - state: AVAILABLE / DEGRADED / UNAVAILABLE
   - label: 给用户展示的中文名
   - reason_code: 机器码
   - reason: 用户能看懂的原因
   - actions: 可点击动作列表，每项包含 label、route、method 或 action_code
3. 示例：
   ```json
   {
     "external_tools": {
       "state": "UNAVAILABLE",
       "label": "外部工具",
       "reason_code": "MCP_NOT_CONNECTED",
       "reason": "未连接 MCP 服务",
       "actions": [
         {"label": "去配置连接器", "route": "/connectors"},
         {"label": "重新测试连接", "action_code": "RETEST_MCP"}
       ]
     }
   }
   ```
4. reason/action 必须从真实装配状态推导，不得写死“可用”。
5. 补 pytest：
   - capabilities 返回新字段
   - 不可用时 reason/actions 正确
   - 旧字段仍存在
6. 补 ruff。

## 验收
- 浏览器打开 Chat，头部直接看到自然语言能力状态和用量。
- 构造或模拟不可用时，能看到原因和动作按钮。
- 页面没有专业裸术语。
- 点击动作能跳到正确页面或触发正确动作。
- pytest / ruff / npm run build 全通过。

---

# 任务 2：移除当前代码 Diff，改成“两版回答对比”

## 当前问题
- 当前 Diff 面向代码块。
- 显示 + / - 和代码行 diff。
- 用户实际做的是业务文字处理，不使用代码，也不理解专业 diff。

## 前端要求
1. 移除当前基于代码块的 Diff 按钮和实现。
2. 新功能命名为“对比上一版”。
3. 触发条件：
   - 同一会话至少有两次助手输出。
   - 对比“上一版助手回答”和“当前助手回答”。
4. 对比对象是普通文本，不是代码。
5. 展示方式必须业务化：
   - 本次新增
   - 本次删减
   - 未变化
   或左右两栏：
   - 上一版
   - 当前版
6. 不要暴露 Diff、LCS、add、remove、hunk、commit 等术语。
7. 没有上一版时按钮隐藏或禁用。
8. 如果用户实际不需要这个功能，可以移除；但不能保留当前代码 Diff。

## 后端要求
- 默认不需要后端。
- 如果为了稳定获取“上一版助手输出”发现前端状态不可靠，允许增加最小只读接口按 conversation 返回最近 N 条 assistant 消息，但必须：
  - 沿用 tenant 过滤
  - 不改内核 Port
  - 补测试

## 验收
- 浏览器实际发送两次内容相近但不完全相同的请求。
- 点击“对比上一版”能看到业务文字层面的新增/删减。
- 页面没有代码 diff 专业术语。
- npm run build、pytest / ruff 全通过。

---

# 任务 3：真正左右分栏双会话，两边都可查看和发送消息

## 当前问题
- 当前只是底部只读面板。
- 用户要求：当前对话页面左右分栏，可查看对照，也可发消息。

## 前端要求
1. 删除当前底部只读 SplitConversationPanel 形态。
2. 实现真正左右双会话工作区：
   - 左侧：当前会话
   - 右侧：可选择另一个会话
   - 两边都有消息列表
   - 两边都有独立输入框
   - 两边都能独立发送消息
3. 保留中间分隔条。
4. PaneResizer 继续支持拖拽和键盘方向键 resize。
5. 每栏独立维护：
   - conversationId
   - 输入内容
   - 发送状态
   - 消息列表
   - 滚动位置
6. 不允许一边能发、另一边只能看。
7. 关闭分栏后恢复单栏，且不破坏原会话状态。
8. 如果实现复杂，先输出设计方案，但目标必须按上述标准实现。

## 后端要求
- 发送接口已支持 conversation_id 的情况下，直接复用。
- 检查 /messages、/chat/events 是否支持任意 conversation_id 独立发送。
- 如发现后端假设“当前只存在一个活动会话”，必须修正并补测试。
- 不允许通过前端 hack 绕过会话隔离。

## 验收
- 浏览器打开 Chat，点击“分栏”。
- 左侧发送任务 A，右侧选择另一个会话并发送任务 B。
- 两边消息都能更新且互不影响。
- 拖动/键盘调整分隔条有效。
- 关闭分栏后单栏正常。
- pytest / ruff / npm run build 全通过。

---

# 任务 4：@提及语义澄清并实现前后端真实语义

## 当前问题
- 当前 @提及只是把 @智能体名称 插入输入框。
- 它不会调用智能体，不是 A2A，不是父子调用，也不会查看内容。
- 用户不知道提及后会发生什么。

## 必须先确认
开始前必须向用户确认语义，给出以下选项：
A. @智能体 = 调用该智能体执行任务
B. @智能体 = 查看该智能体信息，不调用
C. @知识库 = 指定检索范围
D. 移除 @提及

用户未确认前，不得自行实现。

## 若选择 A：@智能体 = 调用该智能体
前端要求：
- 点击 @智能体 时，输入中插入可识别的 mention token，例如 `@[agent:智能体名]`，而不是纯文本。
- 界面明确提示“将调用该智能体”。
- 发送时 mention token 随消息一起提交。

后端要求：
- ChatRequest 增加 mentions 或结构化解析 mention token。
- 服务层解析 mentions。
- 若 mention 指向智能体，路由到该智能体执行，而不是当前默认智能体。
- 如果已有父子委派/A2A 链路，优先复用；不要另造一套。
- 补审计：记录 mention 类型、目标 id、路由结果。
- 补 pytest：单 mention、未知 mention、无权限 mention、执行成功/失败。

## 若选择 B：@智能体 = 查看信息
前端要求：
- 点击 @智能体 后弹出智能体卡片：简介、技能、状态、版本。
- 不会调用智能体。
- 文案明确“仅查看，不会执行”。

后端要求：
- 复用现有智能体详情接口。
- 如缺字段，最小补齐并测试。

## 若选择 C：@知识库 = 指定检索范围
前端要求：
- 点击 @知识库 插入可识别 mention token。
- 界面明确“将只检索该知识库”。

后端要求：
- ChatRequest 解析 knowledge mention。
- 检索链路只查询指定知识库。
- 补 pytest 验证检索范围生效。

## 若选择 D：移除
- 删除入口、组件、样式和相关状态。
- 确认页面不再出现 @提及。

## 验收
- 语义已由用户确认。
- 浏览器实际点击 @入口，行为与确认语义一致。
- 前端提示和后端真实行为一致。
- 不存在“插入了 @文本但用户不知道有什么用”的状态。
- pytest / ruff / npm run build 全通过。

---

# 任务 5：模型预设用户化 + 后端真实生效

## 当前问题
- 当前模型预设只是前端 Select。
- backend 未确认是否支持。
- balanced/creative/precise 或 temperature/top_p/model id 用户看不懂。
- 如果后端不支持，前端展示会误导用户。

## 前端要求
1. 不允许展示专业参数名：
   - temperature
   - top_p
   - max_tokens
   - model id
2. 使用用户化标签，例如：
   - 简洁准确
   - 平衡
   - 深度分析
   - 创意发散
3. 每个选项必须有解释：
   - 简洁准确：回答更短，适合明确问题。
   - 平衡：默认模式，兼顾速度和完整性。
   - 深度分析：回答更详细，适合复杂问题，等待时间更长。
   - 创意发散：更适合写作、头脑风暴。
4. 选择后必须在请求中真实传递。
5. 如果后端不支持，必须：
   - 移除，或
   - 展示为“即将支持”的禁用态，不得假装生效。

## 后端要求
1. ChatRequest 增加结构化字段，例如：
   - style_preset: concise / balanced / deep / creative
2. 服务层将该字段映射到模型请求参数或模型选择策略：
   - concise：较低 temperature、较短 max_tokens
   - balanced：默认参数
   - deep：更高 max_tokens、更长思考/更详细提示
   - creative：更高 temperature
3. 映射逻辑放在 app/services 或 builtin plugin，不改内核 Port。
4. 保留兼容：不传 style_preset 时行为与当前完全一致。
5. 补 pytest：
   - 每个 preset 对应请求参数正确
   - 不传 preset 时默认行为不变
   - 非法 preset 返回明确 400
6. 补审计或 trace 字段，记录本次调用使用的 preset，但不要记录敏感参数。

## 验收
- 浏览器能看到用户化标签和解释。
- 选择预设后，请求参数确实变化。
- backend 日志/测试证明 preset 生效。
- 页面没有专业参数暴露。
- pytest / ruff / npm run build 全通过。

---

# 任务 6：排队发送融入主发送按钮

## 当前问题
- 现在有单独“排队发送”按钮，不符合用户自然工作流。
- 用户期望任务 1 未完成时继续输入任务 2，点“发送”，任务 2 自动排队。

## 前端要求
1. 忙碌状态下，主发送按钮行为变为“加入队列”。
2. 用户流程必须是：
   - 发送任务 1；
   - 任务 1 正在流式输出；
   - 用户继续输入任务 2；
   - 点击发送；
   - 输入框清空；
   - 提示“任务 2 已排队，将在当前回答完成后自动发送”；
   - 显示“排队中：1”。
3. 队列支持取消。
4. 队列支持显示数量。
5. 当前回答完成后自动 FIFO 发送。
6. 原单独“排队发送”按钮应移除，或降级为辅助入口；主流程必须是主发送按钮。
7. 队列可以是页面状态，刷新不保留可以接受，但界面要明确。

## 后端要求
- 默认不需要新增队列后端。
- 但必须检查：
  - 流式发送期间重复调用接口是否会互相污染 trace/task 状态
  - 取消任务是否正常
  - 当前会话下的消息顺序是否正确
- 如发现问题，补最小修复和测试。
- 不允许通过前端 hack 掩盖后端状态污染。

## 验收
- 浏览器实际测试：
  - 发送任务 1
  - 在流式输出期间输入任务 2
  - 点击主发送按钮
  - 看到排队提示和数量
  - 任务 1 结束后任务 2 自动发送
- 队列取消可用。
- 消息顺序正确。
- pytest / ruff / npm run build 全通过。

---

# 每个任务完成后的统一输出

每完成一个任务，必须输出：

1. 本任务做了什么，用大白话说明。
2. 改动文件清单。
3. diff 摘要。
4. 真实入口验收结果：
   - 是否启动前端？
   - 用浏览器点了什么？
   - 看到了什么？
   - 后端接口返回了什么？
5. 测试结果：
   - `pytest -q`
   - `ruff check .`
   - `npm run build`
6. 写入 docs/memmemeory.md 的记录。
7. 明确说“等待确认后再做下一项”。

---

# 全部完成后的最终要求

所有任务完成后，必须在 docs/memmemeory.md 中把状态更新为：

- Phase 1 后端工程实现完成
- P1b UX 返工完成
- 前后端真实联动验收通过
- 用户可理解、可操作、可闭环
- 可以进入 Phase 2

并且最终输出一份“Phase 1 真正完成总结”，包括：

1. 本轮 UX 返工改动文件清单
2. 前后端 diff 摘要
3. 真实浏览器验收结果
4. pytest / ruff / npm run build 结果
5. 当前 alembic head
6. 仍未解决的限制或已知问题

禁止再把“入口存在”等同于“阶段完成”。
禁止用专业术语掩盖用户不懂的问题。
禁止前端伪造后端状态。
禁止新增 Python/Node 依赖。
```

