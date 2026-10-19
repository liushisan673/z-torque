
## Summary of findings

### 插件化现状 (Plugin Architecture)

**1. 工具执行器插件体系（最成熟的部分）— SkillGateway 注册表模式**
- [skill_gateway.py](file:///d:\project\z-torque-community-m1\app\kernel\skill_gateway.py) 定义了 `ToolExecutor` / `AsyncToolExecutor` 两个 Protocol（结构化接口，非继承），执行器只需带 `tool_type` 属性 + `execute()`/`execute_async()` 方法即可注册
- `SkillGateway.register(executor)` 按 tool_type 注册到 `self._executors` 字典
- 当前已注册 7 类执行器：
  - MOCK（skill_gateway.py）
  - HTTP（builtin_executors.py）— 模板变量/secret 注入/allowed_hosts
  - DB_QUERY（builtin_executors.py）
  - DB_SQL（db_executor.py ConnectorDbExecutor）— pg8000/mysql/sqlite，白名单
  - MCP（skill_gateway.py McpExecutor 占位）+ MCP_NEGOTIATED（真实协商执行）
  - MD_SCRIPT（MdSkillScriptExecutor，受控脚本运行时）
- 分发逻辑：同步执行器走 `asyncio.to_thread`，异步执行器直接 await；统一超时、重试（指数退避）、审计、权限检查、写入确认（WAITING_HUMAN）

**2. MD-Skill 插件（目录扫描式插件）**
- md-skills/ 目录下每个子目录 = 一个技能插件（data-analysis、test-skill、text-summary）
- md_skill_registry.py: `register_skill_dir` / `scan_directory` — 按 front-matter 解析、指纹去重、自动创建草稿版本
- md_skill_watcher.py: 每租户一个 watcher 定时扫描目录自动注册
- md_skill_runtime.py: 脚本执行有严格隔离（Windows 上不可用，禁用）
- 上传接口（md_skill_upload.py）复用统一注册入口

**3. MCP 连接器插件**
- StreamableHttpMcpConnector（HTTP SSE）+ StdioMcpConnector（StdioServerRegistry）
- 支持工具发现（tools/list）→ 快捷注册

**4. builtin_plugins 是空壳**
- `app/builtin_plugins/__init__.py` 是空文件 — 插件包已规划但内置执行器实际是在 [main.py](file:///d:\project\z-torque-community-m1\app\main.py) lifespan 里手工逐个 `gateway.register(...)` 硬编码装配的，没有自动发现/entry_points 机制

### 接口封装现状 (Interface Encapsulation)

**后端分层：**
- `app/kernel/ports/` — 端口抽象层（目前只有 QueueDriver：asyncio 内存队列封装；Executor 类型别名）。port 数量少，worker/dispatcher 直接依赖具体类
- `app/kernel/model_gateway/contracts.py` — 冻结 dataclass 契约（ModelRequest/ModelResponse/ModelMessage/ModelToolCall/ModelUsage）+ ModelGateway 基类（generate/stream/stream_response），Mock 与 OpenAICompatible 两个实现
- `app/core/` — 统一响应封装（success_response/error_response）、ApiError 错误体系、trace 中间件
- API 路由层 → services 层 → kernel 层 分层清晰

**前端封装：**
- console/src/services/api.ts — 统一请求层：ApiEnvelope 解包、ApiRequestError、Bearer 认证、401 单飞刷新重试、Idempotency-Key、trace 透传、文件上传
- sse.ts — SSE 流式封装

**未完成/薄弱点：**
1. `app/builtin_plugins` 空置 — 装配硬编码在 main.py lifespan
2. `app/kernel/ports/` 只有 queue_driver 一个端口 — "六边形架构"的 ports 层有名无实，TaskRuntime 直接 import SessionLocal、具体模型
3. QueueDriver 是纯内存队列 — 无 Redis/持久化后端实现可替换（重启恢复靠 outbox 补偿而非队列持久化）
4. ModelGateway 的 Mock 实现还在生产装配路径中作为 fallback
5. 执行器扩展仍需改 main.py（注册） — 没有 entry_points/目录自动发现
6. skill_gateway.py 已经 500+ 行，包含了 Protocol、多个执行器、MCP 连接器实现、网关本身 — 职责过多

Let me present this to the user in a clear, structured way in Chinese. I could use a diagram via Inline Visuals — this is an architecture explanation, which benefits from a visual. The user asks to "了解情况" — an architecture overview. Per inline_visuals guidance, architecture explanations benefit from diagrams. Let me create a concise diagram showing the plugin architecture.

Actually, let me consider: the user wants to understand the current state. A structured text answer with file links is the core deliverable. A diagram would genuinely aid understanding here since it's an architecture overview. Let me load the dynamic-ui skill first for the diagram module... Actually the guidance says to load "dynamic-ui" skill before generating visuals. That might be worth doing, but it also adds overhead. The instructions say I should proactively use Inline Visuals for architecture topics. Let me do one compact architecture diagram.

Let me load the dynamic-ui skill to get the diagram module specs.

I need to read the architecture-and-flow scene file and check the templates manifest to see if there's a suitable material. Let me read the scene file and manifest.

Now I'll build the summary and an architecture diagram. The diagram should show the plugin architecture of the project:

Layered view:
1. API 路由层 (app/api/routes) → services 层 → kernel 层
2. Kernel 层关键组件:
   - SkillGateway (tool executor registry): MOCK / HTTP / DB_QUERY / DB_SQL / MCP / MD_SCRIPT executors
   - ModelGateway (contracts): Mock / OpenAICompatible
   - TaskRuntime: QueueDriver (ports) + Worker + OutboxDispatcher
   - MD-Skill 体系: md_skill_registry / watcher / runtime (目录扫描式插件)
   - MCP connectors: HTTP + Stdio

The diagram needs to fit within the complexity budget: 1 focal point, 2-5 main nodes... but architecture diagrams can use group boundaries. Let me use the architecture-elements style with grouped boundaries.

Design plan (SVG, viewBox 0 0 720 H):

Layered vertical architecture:

- Top band: 接口层 app/api/routes (→ services)
- Middle: Kernel 层 (boundary):
  - SkillGateway (brand focal — registry pattern: register() by tool_type)
    - executor chips: MOCK, HTTP, DB_QUERY, DB_SQL, MCP, MD_SCRIPT
  - ModelGateway: Mock + OpenAICompatible
  - TaskRuntime: QueueDriver (ports) + Worker/OutboxDispatcher
  - MD-Skill 插件: registry/watcher/runtime
- Bottom: md-skills/ 目录 + 外部系统 (HTTP API/DB/MCP Server)

That's a lot of nodes. Budget says custom architecture ≤6 ungrouped modules with group boundaries allowed. Let me organize:

Boundary 1: 接入层 (API routes / schemas) — 1 container
Boundary 2: kernel 内核 (main):
  - SkillGateway + 6 executor chips (as a sub-group with chips rather than nodes)
  - ModelGateway contract + 2 impls
  - TaskRuntime + ports.QueueDriver
  - MD-Skill registry/watcher
External: md-skills 目录, 外部 HTTP/DB/MCP

To keep it readable, I'll draw a grouped architecture with 2-3 nesting levels:

Row 1 (top): [Console 前端 api.ts/sse.ts] → [API 路由层]
Row 2 (boundary: kernel):
  - Left column: SkillGateway (with 6 tool_type chips inside)
  - Right top: ModelGateway (contracts + 2 impls)
  - Right bottom: TaskRuntime (QueueDriver + Worker + Dispatcher)
Row 3: [MD-Skill 目录扫描 registry/watcher] [外部系统: HTTP API / 数据库 / MCP Server]

Hmm, maybe simpler: one vertical stack of 3 layers with annotations, plus a focus on SkillGateway registry.

Let me plan coordinates carefully. viewBox 0 0 720 H.

Structure (top→bottom):

1. Title omitted (widget title optional; chart-only rule applies to charts; for diagrams a short title is ok but I can skip and let text response explain).

Layer 1 (y=20..70): two nodes side by side:
- Console 前端 (services/api.ts 统一请求层 + sse.ts) — x 60..320
- API 路由层 (app/api/routes, schemas) — x 400..660
Arrow between them.

Actually maybe better to make the flow: 前端 → API 路由 → services → kernel. That's 4 boxes horizontal. Then kernel boundary below with 4 modules. Then bottom external row.

Total nodes: 4 (top) + 4 (kernel) + external chips. Might be too much. Let me simplify:

Top row (horizontal, 3 nodes):
- 前端 Console (api.ts / sse.ts)
- API 路由 + services (app/api, app/services)
- (no third)

Then a big boundary "app/kernel 内核" containing 4 modules in 2x2 grid:
- SkillGateway 执行器注册表 (brand focal) — chips: MOCK/HTTP/DB_QUERY/DB_SQL/MCP/MD_SCRIPT
- ModelGateway 契约 — Mock / OpenAICompatible
- TaskRuntime — ports.QueueDriver + Worker + Outbox
- MD-Skill 注册/扫描 — registry + watcher

Bottom row: external: md-skills/ 目录、HTTP API、数据库、MCP Server (as external-styled small nodes)

Connections: 前端 → API 路由 (arrow down into kernel); SkillGateway → executors → external; ModelGateway → OpenAICompatible → 上游模型; TaskRuntime → QueueDriver.

Let me do a cleaner layout:

Canvas: 720 wide. Safe area 40.

Row A (y=48..100): 
- Node A1 "控制台前端" subtitle "api.ts / sse.ts 统一封装" x=60 w=280
- Node A2 "API 路由 / services" subtitle "app/api · app/services" x=400 w=260
Arrow A1→A2 horizontal at y=74.

Then arrow down from A2 (x=530) to kernel boundary top.

Boundary B "app/kernel（内核分层）" y=140..420, x=40..680, dashed border.
Inside 2x2 grid:
- B1 SkillGateway (brand) x=70..370, y=170..290: title "SkillGateway 执行器注册表", subtitle "ToolExecutor Protocol · register(tool_type)", chips row: MOCK HTTP DB_QUERY DB_SQL MCP MD_SCRIPT (6 chips in 2 rows of 3)
- B2 ModelGateway x=400..650, y=170..290: "ModelGateway 契约", subtitle "dataclass 请求/响应契约", chips: MockModelGateway / OpenAICompatibleGateway
- B3 TaskRuntime x=70..370, y=320..400: "TaskRuntime 任务运行时", subtitle "ports.QueueDriver · Worker · Outbox"
- B4 MD-Skill x=400..650, y=320..400: "MD-Skill 插件", subtitle "目录扫描 · watcher · 版本草稿"

Row C (y=460..520): external nodes:
- E1 "md-skills/ 目录" x=60 w=150
- E2 "HTTP API / 数据库" x=240 w=170
- E3 "MCP Server" x=440 w=140
- E4 "上游模型服务" x=610? too tight. 

Hmm 4 external nodes: widths: md-skills 目录 (7 chars * 7 + 24 ≈ 73... Chinese chars wider: 14px font, Chinese ~14px/char → "md-skills/ 目录" ≈ 9 ASCII + 2 Chinese ≈ 9*8+2*14=100 +24 = 124. Let me give w=150.
"HTTP API / 数据库": ~ 11 ascii*8 + 3 chinese*14 = 88+42=130 +24 = 154 → w=170
"MCP Server": 10*8=80+24=104 → w=130
"上游模型": 4*14=56+24=80 → w=120

Total with gaps: 150+170+130+120 = 570 + 3 gaps of ~30 = 660 > 640 available (720-80). Tight but ok if gaps=23... too tight. Use gap 24: 570+72=642 > 640. Hmm. Reduce: E1 w=140, E2 w=160, E3 w=120, E4 w=110 → 530 + 72 = 602 ✓ fits in 40..680.

x positions: E1 x=40..180, E2 x=204..364, E3 x=388..508, E4 x=532..642. Hmm safe right edge 680; x=532+110=642 ✓.

Connectors from kernel bottom to externals:
- B1 (SkillGateway) center-bottom → E1/E2/E3 — three arrows from bottom of B1 fanning out. B1 bottom center x=220. Draw lines from (220,420) down then to tops of E1 (110), E2 (284), E3 (448)? E3 is under B2 mostly. That crosses under B3/B4 region... B3 bottom is at y=400, boundary bottom 420. External row at 460. Arrows from boundary bottom edge to external tops: from boundary bottom at specific x positions aligned with each external: E1 x=110 ← from SkillGateway; E2 x=284 ← SkillGateway/DB; E3 x=448 ← MCP (skill gateway too); E4 x=587 ← ModelGateway.

Simpler: draw 4 short vertical arrows from boundary bottom (y=420) to each external top (y=460), and add a small label above each external or rely on node names. To attribute source: E1/E2/E3 come from executors, E4 from model gateway. I can add small text labels on the arrows: "MD_SCRIPT", "HTTP/DB_SQL", "MCP", "OpenAI 兼容". Those are connector labels — need capsules, 1-3 words. Might be too busy. Alternative: put attribution as subtitle inside external nodes: E1 subtitle "MD_SCRIPT 加载", E2 subtitle "HTTP / DB_SQL 执行", E3 subtitle "MCP_NEGOTIATED", E4 subtitle "stream / generate". That avoids connector labels entirely. 

Arrows: vertical lines from y=420 to y=460 at each external center x, with marker-end. But arrows from boundary bottom look like they come from the boundary, not specific modules. To fix attribution, draw from module bottom edges: B3/B4 bottom is y=400, boundary bottom 420. Arrows would cross the boundary edge — acceptable (external outputs outside container per rules; the arrow passes through the boundary line which is fine visually as it exits the container).

Let me attribute:
- E1 (md-skills/ 目录) ← from B4 MD-Skill (x center 525)... but E1 is at far left. Crossing lines problem. 

Reorder externals to match kernel columns:
- Left column (B1 SkillGateway, B3 TaskRuntime): E1 "HTTP API" (x=40..180), E2 "数据库 DB_SQL" (x=204..364)
- Right column (B2 ModelGateway, B4 MD-Skill): E3 "MCP Server" (x=388..508), E4 "md-skills/ 目录" (x=532..672, w=140)

Hmm but MD-Skill → md-skills 目录 and SkillGateway → HTTP API/DB. Actually SkillGateway executors call external systems; MD-Skill watcher scans md-skills directory. Both connect down.

Arrows (all vertical, no crossings):
- B1 bottom (x=110 area) → E1 top: from SkillGateway bottom-left down. B1 is y=170..290 though; B3 sits below (320..400). An arrow from B1 straight down would pass through B3! Problem.

Alternative routing: executors' arrows can exit from the boundary bottom edge with small labels instead. Or simpler: skip per-module attribution arrows; draw ONE arrow from boundary bottom center to a horizontal "外部系统" group, and label external nodes with their source. Cleaner: draw a single wide bracket? No decorative brackets.

Simplest readable approach: draw 4 vertical arrows from boundary bottom edge y=420 to each external top y=460 at x = external centers (110, 284, 448, 602). Then in each external node subtitle, name the source executor: 
- E1 "HTTP API" subtitle "HttpExecutor" 
- E2 "数据库" subtitle "ConnectorDbExecutor"  (long: 18 chars * 7 = 126 +24=150 → w=160 ok)
- E3 "MCP Server" subtitle "McpNegotiatedExecutor" (21*7=147+24=171 → w=180, too wide for x=388..508. Adjust widths.)

Subtitle widths at 12px ~7px/char ASCII:
- "HttpExecutor" 12 chars = 84+24=108
- "ConnectorDbExecutor" 19 chars = 133+24=157
- "MCP executor" → "NegotiatedMcpExecutor" 21 → 171; shorten to "MCP executor" (12) = 108
- "MdSkillWatcher" 14 = 98+24=122

Node titles: "HTTP API" (8*8=64), "数据库" (3*14=42), "MCP Server" (10*8=80), "md-skills 目录" (~12 chars mixed ≈ 10*8+2*14=108). Node width = max(title, subtitle)+24:
- E1: max(64, 84)=84+24=108 → w=120
- E2: max(42,133)=133+24=157 → w=165
- E3: max(80,84)=84+24=108 → w=120
- E4: max(108,98)=108+24=132 → w=140

Total: 120+165+120+140=545 + 3 gaps ×32 = 641. Available 640 (40..680). gap=31 → 638 ✓ barely. Use x: E1 40..160, E2 192..357, E3 389..509, E4 541..681 — 681 slightly over 680; shift: start x=36? Safe area is ≥40 or 6% (43.2). Hmm 6% of 720 = 43.2. So safe zone 43..677. Tight.

Reduce E2 subtitle to "DbExecutor"? The class is ConnectorDbExecutor. Or title "数据库 SQL" subtitle "ConnectorDbExecutor" — must fit. Alternatively stack external nodes as 2×2? That doubles height.

Alternative simpler design: put externals in one row of 4 pills without subtitles, and put attribution via 4 connector labels? Labels also need width.

Or: reduce to ONE arrow from kernel boundary to an external group container holding 4 pills inside. Group container "外部目标" x=43..677, y=456..536, with 4 pills inside at comfortable spacing. One arrow from boundary bottom center (360, 420) → (360, 456) with label "执行器出站调用"? That's clean and honest: all executors call out. The MD-Skill watcher relationship: watcher reads md-skills dir — it's inbound (dir → kernel). Eh, mixing directions.

Actually, let me reconsider: maybe two separate small external containers:
- Left container under left column: "执行器外部目标" with pills HTTP API / 数据库 / MCP (3 pills horizontal inside, width 43..330)
- Right container under right column: "MD-Skill 目录" pill (x=400..600)

Arrows: boundary bottom → each container top. That's 2 arrows. Attribution: left container arrow from SkillGateway side (x≈220 down), right arrow from B4 side (x≈500 down). But B3 is below B1... the arrow at x=220 passes boundary bottom only (B3 spans 70..370, so x=220 passes through B3!). 

Ugh. Column stacking blocks vertical arrows. Fix: make kernel modules side by side in 2 columns but arrows exit from boundary bottom edge (not from modules). The visual attribution is then by horizontal position: left arrow under SkillGateway column, right arrow under ModelGateway/MD-Skill column. Good enough without crossing.

Final plan:

Canvas viewBox 0 0 720 600ish.

Top row (y=52..104, h=52):
- A1 c-neutral: x=43..303 (w=260) title "控制台前端" subtitle "services/api.ts · sse.ts"
  - title 5 chars ×14=70; subtitle ~24 ascii chars ×7=168+24=192 → w=220. Set w=230: x=43..273.
- A2 c-neutral: x=417..677 (w=260): title "API 路由 / services" subtitle "app/api/routes · app/services" (29×7=203+24=227 → w=240). x=427..677.
- Arrow A1→A2: y=78, from 273+8 to 427-8... gap 273..427 = 154 wide. Draw from x=281 to x=419 with marker.

Arrow down from A2 bottom center (x=552, y=104) to boundary top (y=140). Vertical arrow with marker. Also maybe an arrow from A1 down? Frontend also talks SSE directly to tasks. Keep single main arrow A2→kernel; A1→A2 horizontal implies flow.

Boundary B (dashed): x=43..677, y=140..430. Label "app/kernel 内核" top-left inside (x=59, y=166).

Kernel modules 2×2:
- Row 1 y=186..300 (h=114):
  - B1 c-brand (focal): x=59..355 (w=296). Title "SkillGateway 执行器注册表" (mixed ~14 chars: 11 ascii×8 + 4 chinese×14 = 88+56=144... "SkillGateway " =13 ascii=104, "执行器注册表"=6 chinese=84 → 188+24=212 < 296 ✓). Subtitle "ToolExecutor Protocol · register(tool_type)" 43×7=301 too long → shorten: "ToolExecutor Protocol 注册" ~ 19×7+2×14=133+28=161 ✓ (plus 24 = 185).
    Chips inside: 6 tool_type chips in 2 rows of 3: MOCK, HTTP, DB_QUERY / DB_SQL, MCP, MD_SCRIPT. Chip w=76, h=24, gaps 8. Row width 3×76+2×8=244 < 296-32 ✓.
  - B2 c-neutral: x=381..661 (w=280). Title "ModelGateway 契约" subtitle "dataclass 请求/响应 · stream". Chips: "MockModelGateway" (16×7=112+16=128 → chip w=132) and "OpenAICompatibleGateway" (23×7=161+16=177 → chip w=180) — two chips stacked vertically, w fits 280-32=248 ✓.
- Row 2 y=324..406 (h=82):
  - B3 c-neutral x=59..355: title "TaskRuntime 任务运行时" subtitle "ports.QueueDriver · Worker · Outbox" (35×7=245+24=269 < 296 ✓)
  - B4 c-neutral x=381..661: title "MD-Skill 插件" subtitle "目录扫描 · watcher · 版本草稿" (subtitle ~ 4×7+9×14=28+126=154 ✓)

Wait — also builtin_plugins 空壳 and ports 层单薄 should be annotated. I'll mention in text, not in diagram. Maybe add a tiny caption inside boundary? Keep diagram clean.

Bottom externals (y=470..546):
- Left container (dashed, c-neutral style): x=43..350, y=470..546: label "执行器外部目标" (7×14=98) at top, pills inside: "HTTP API" w=90, "数据库" w=70, "MCP" w=56; 3 pills: 90+70+56+2×8=232 < 350-32=318 ✓. Pills y=506..530.
- Right container: x=420..677, y=470..546: label "MD-Skill 源目录", pill "md-skills/" w=100.

Arrows: 
- From boundary bottom at x=196 (left container center) y=430 → y=470 marker-end. 
- From boundary bottom at x=548 y=430 → y=470 marker-end.
These exit the boundary — fine.

Also arrow from A1 (frontend) — skip, keep one flow line.

Wait, boundary bottom is y=430 but row-2 modules end at y=406; ok.

Height total: 546+ padding → viewBox H = 580.

Connector colors: arrows neutral (--text-muted); main flow A2→kernel could be --brand? The focal point is SkillGateway (brand node). Keep main path neutral, brand only on B1. Rules say use --brand for main path; with 4 arrows, keep all neutral, B1 brand = focal. Good.

Chip styles: fill --surface-muted, stroke --border, text caption. Brand node chips can stay neutral — actually inside brand node, chips with surface fill... The brand node itself has fill --brand-soft. Chips inside with fill var(--surface) would look ok. Let me use chips fill: var(--surface) stroke var(--border), text --text-muted... In dark mode brand-soft is #004D26, surface #171717 — fine.

Legend: I use brand (focal) + neutral + dashed boundary. Two block styles → legend optional (rules: legend if >2 connector styles or >2 block styles). I have 2 block styles (neutral, brand) + dashed boundary. Skip legend; add a caption line "绿色 = 插件化核心：注册表模式" in text response instead.

Text in SVG: use classes .t .th .ts .arr from token template.

Total node/box count: A1, A2, boundary, B1..B4, 2 containers, 4 pills, 6+2 chips. Chips are content inside nodes, fine. Ungrouped modules: A1, A2, B1..B4 = 6 ✓. Connectors: 4 arrows ✓.

Now double check y coordinates:
- A row: y=52..104 (h=52). Title at y=74, subtitle y=92.
- A1→A2 arrow y=78.
- A2→boundary arrow: from (552,104) to (552,140). Hmm boundary top edge y=140; arrow marker at 138.
- Boundary y=140..430. Boundary label "app/kernel 内核" at (59,164) — but B1 starts y=186, label needs its own slot: place label text at y=163, it sits above B1 ✓.
- B1: y=186..302. Title y=210, subtitle y=228. Chips rows: row1 y=242..266, row2 y=274..298. Bottom padding 302-298=4 — too tight. Extend B1 to y=186..310. Chips row2 y=276..300, padding 10. OK B1 h=124. B2 same y=186..310: title 210, subtitle 228, chips stacked: chip1 y=244..270 (h=26), chip2 y=278..304, padding 6 — make chip2 y=276..300, padding 10 ✓.
- Row2 modules: y=336..412 (gap 26 from 310). B3/B4: title y=362, subtitle y=382. h=76. Boundary bottom 430, padding 18 ✓.
- Arrows from boundary bottom y=430 → containers y=470.
- Containers y=470..550: label y=494, pills y=508..532, bottom padding 18 ✓.
- viewBox H = 590.

Chip text: 12px caption, centered. MOCK(4), HTTP(4), DB_QUERY(8), DB_SQL(6), MCP(3), MD_SCRIPT(9). At 12px mono-ish... use font-mono caption 12px: 9 chars ×7=63 < 76-8=68 ✓ barely. Make chips w=78: 3×78+16=250 < 296-2×16=264 ✓.

B1 inner chips x start: 59 + (296-250)/2 = 59+23=82. Chips at x=82, 168, 254 (each +86... 78+8=86 ✓). Row2 same.

B2 chips: chip w=180 max; "MockModelGateway" 16×7=112+16 pad=128 → w=132; "OpenAICompatibleGateway" 23×7=161 → w=180. x centered: 381+(280-132)/2=455; chip2 x=381+(280-180)/2=431.

Hmm wait B2 x=381..661 (w=280) ✓.

Boundary label: "app/kernel 内核" at x=59 y=164, 12px muted.

Also add port annotation? Skip.

Arrowheads: standard marker per spec: viewBox 0 0 8 8, refX 7, refY 4, markerWidth 8, markerHeight 8, markerUnits userSpaceOnUse, orient auto, path M1 1 L7 4 L1 7 Z.

Interactions: none needed (static diagram). No script.

Widget: data-dynamic-ui-widget, data-template="architecture-elements" (borrowing style). Root class .widget with padding? Top-level card should fill width; use a card with --surface? Rules: card surfaces neutral. I'll wrap SVG in minimal container without extra card border — actually diagrams often render directly. Keep a subtle container: background var(--surface), radius-card, padding var(--spacer-16). That's allowed (quiet panel).

Now the text response. Structure:

1. 总体结论 (一句话)
2. 插件化现状 — SkillGateway 注册表（7 种执行器）、MD-Skill 目录插件、MCP 连接器；builtin_plugins 空壳、装配硬编码在 main.py
3. 接口封装现状 — 后端：contracts.py dataclass 契约、ports 层（仅 QueueDriver）、core 统一响应/错误；前端：api.ts 统一请求层（信封解包、401 单飞刷新、幂等键）、sse.ts
4. 薄弱点/待办建议

Keep concise with file links. Chinese language.
内核、网关、任务运行时和前端封装层都已读完，下面是梳理结果，先看整体架构图。

<pure_show_widget style="display:none">

```widget_code
<style>
:root {
  color-scheme: light;
  --surface: #F7F7F8;
  --surface-muted: #EFEFF2;
  --text: #171717;
  --text-muted: #52525B;
  --border: rgba(23, 23, 23, 0.12);
  --brand: #0FDC78;
  --brand-soft: #E5FFF2;
  --brand-text: #004D26;
  --radius: 8px;
  --radius-card: 12px;
  --spacer-8: 8px;
  --spacer-12: 12px;
  --spacer-16: 16px;
  --font-sans: "SF Pro Text", "PingFang SC", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --weight-regular: 400;
  --weight-medium: 500;
  --text-caption: 12px/18px;
  --text-body: 14px/20px;
}
:root[data-widget-theme="dark"] {
  color-scheme: dark;
  --surface: #171717;
  --surface-muted: #262626;
  --text: #E5E5E5;
  --text-muted: #A1A1AA;
  --border: rgba(229, 229, 229, 0.12);
  --brand: #32F08C;
  --brand-soft: #004D26;
  --brand-text: #E5FFF2;
}
.widget {
  color: var(--text);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  padding: var(--spacer-16);
  font: var(--weight-regular) var(--text-body) var(--font-sans);
}
.t { fill: var(--text); font: var(--weight-regular) var(--text-body) var(--font-sans); }
.th { fill: var(--text); font: var(--weight-medium) var(--text-body) var(--font-sans); }
.ts { fill: var(--text-muted); font: var(--weight-regular) var(--text-caption) var(--font-sans); }
.tl { fill: var(--text-muted); font: var(--weight-medium) var(--text-caption) var(--font-sans); }
.arr { stroke: var(--text-muted); stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; fill: none; }
g.c-neutral > rect { fill: var(--surface); stroke: var(--border); }
g.c-brand > rect { fill: var(--brand-soft); stroke: var(--brand); }
.c-brand > .th, .c-brand > .t { fill: var(--brand-text); }
.c-brand > .ts { fill: var(--brand-text); }
.boundary { fill: none; stroke: var(--border); stroke-dasharray: 6 5; }
.chip { fill: var(--surface-muted); stroke: var(--border); }
.ct { fill: var(--text-muted); font: var(--weight-medium) var(--text-caption) var(--font-mono); }
</style>
<div data-dynamic-ui-widget data-template="architecture-elements" data-mounted="false">
<div class="widget" id="zt-arch">
<svg viewBox="0 0 720 590" width="100%" height="auto" role="img" aria-label="Z-Torque 插件化与接口封装架构图">
  <defs>
    <marker id="zt-arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto">
      <path d="M1 1 L7 4 L1 7 Z" fill="var(--text-muted)"></path>
    </marker>
  </defs>

  <!-- 顶层：前端 -> API 路由 -->
  <g class="c-neutral">
    <rect x="43" y="52" width="230" height="52" rx="8"></rect>
    <text class="th" x="59" y="74">控制台前端</text>
    <text class="ts" x="59" y="92">services/api.ts · sse.ts</text>
  </g>
  <g class="c-neutral">
    <rect x="437" y="52" width="240" height="52" rx="8"></rect>
    <text class="th" x="453" y="74">API 路由 / services</text>
    <text class="ts" x="453" y="92">app/api/routes · app/services</text>
  </g>
  <path class="arr" d="M281 78 L429 78" marker-end="url(#zt-arr)"></path>

  <!-- 主链路箭头 -->
  <path class="arr" d="M557 104 L557 132" marker-end="url(#zt-arr)"></path>

  <!-- kernel 边界 -->
  <rect class="boundary" x="43" y="140" width="634" height="290" rx="12"></rect>
  <text class="tl" x="59" y="164">app/kernel 内核</text>

  <!-- SkillGateway（焦点） -->
  <g class="c-brand">
    <rect x="59" y="186" width="296" height="124" rx="8"></rect>
    <text class="th" x="75" y="210">SkillGateway 执行器注册表</text>
    <text class="ts" x="75" y="228">ToolExecutor Protocol 注册</text>
  </g>
  <rect class="chip" x="82" y="242" width="78" height="24" rx="8"></rect>
  <text class="ct" x="121" y="258" text-anchor="middle">MOCK</text>
  <rect class="chip" x="168" y="242" width="78" height="24" rx="8"></rect>
  <text class="ct" x="207" y="258" text-anchor="middle">HTTP</text>
  <rect class="chip" x="254" y="242" width="78" height="24" rx="8"></rect>
  <text class="ct" x="293" y="258" text-anchor="middle">DB_QUERY</text>
  <rect class="chip" x="82" y="276" width="78" height="24" rx="8"></rect>
  <text class="ct" x="121" y="292" text-anchor="middle">DB_SQL</text>
  <rect class="chip" x="168" y="276" width="78" height="24" rx="8"></rect>
  <text class="ct" x="207" y="292" text-anchor="middle">MCP</text>
  <rect class="chip" x="254" y="276" width="78" height="24" rx="8"></rect>
  <text class="ct" x="293" y="292" text-anchor="middle">MD_SCRIPT</text>

  <!-- ModelGateway -->
  <g class="c-neutral">
    <rect x="381" y="186" width="280" height="124" rx="8"></rect>
    <text class="th" x="397" y="210">ModelGateway 契约</text>
    <text class="ts" x="397" y="228">dataclass 请求/响应 · stream</text>
  </g>
  <rect class="chip" x="455" y="244" width="132" height="26" rx="8"></rect>
  <text class="ct" x="521" y="261" text-anchor="middle">MockModelGateway</text>
  <rect class="chip" x="431" y="278" width="180" height="26" rx="8"></rect>
  <text class="ct" x="521" y="295" text-anchor="middle">OpenAICompatibleGateway</text>

  <!-- TaskRuntime -->
  <g class="c-neutral">
    <rect x="59" y="336" width="296" height="76" rx="8"></rect>
    <text class="th" x="75" y="362">TaskRuntime 任务运行时</text>
    <text class="ts" x="75" y="382">ports.QueueDriver · Worker · Outbox</text>
  </g>

  <!-- MD-Skill -->
  <g class="c-neutral">
    <rect x="381" y="336" width="280" height="76" rx="8"></rect>
    <text class="th" x="397" y="362">MD-Skill 插件</text>
    <text class="ts" x="397" y="382">目录扫描 · watcher · 版本草稿</text>
  </g>

  <!-- 出站箭头 -->
  <path class="arr" d="M196 430 L196 462" marker-end="url(#zt-arr)"></path>
  <path class="arr" d="M548 430 L548 462" marker-end="url(#zt-arr)"></path>

  <!-- 外部目标 -->
  <rect class="boundary" x="43" y="470" width="307" height="80" rx="12"></rect>
  <text class="tl" x="59" y="494">执行器外部目标</text>
  <rect class="chip" x="59" y="506" width="90" height="26" rx="8"></rect>
  <text class="ct" x="104" y="523" text-anchor="middle">HTTP API</text>
  <rect class="chip" x="157" y="506" width="70" height="26" rx="8"></rect>
  <text class="ct" x="192" y="523" text-anchor="middle">数据库</text>
  <rect class="chip" x="235" y="506" width="56" height="26" rx="8"></rect>
  <text class="ct" x="263" y="523" text-anchor="middle">MCP</text>

  <rect class="boundary" x="420" y="470" width="257" height="80" rx="12"></rect>
  <text class="tl" x="436" y="494">MD-Skill 源目录</text>
  <rect class="chip" x="436" y="506" width="100" height="26" rx="8"></rect>
  <text class="ct" x="486" y="523" text-anchor="middle">md-skills/</text>
</svg>
</div>
</div>
```

</pure_show_widget>

## 插件化现状

项目的插件化核心是 **SkillGateway 注册表模式**，围绕三类插件机制展开：

**1. 工具执行器插件（最成熟）**
- [skill_gateway.py](file:///d:/project/z-torque-community-m1/app/kernel/skill_gateway.py#L45-L52) 定义 `ToolExecutor` / `AsyncToolExecutor` 两个 **Protocol（结构化鸭子类型接口）**，执行器只需声明 `tool_type` 属性 + `execute()` / `execute_async()` 方法即可通过 `register()` 挂载，无需继承基类
- 已注册 6 种执行器：`MOCK`、`HTTP`（[builtin_executors.py](file:///d:/project/z-torque-community-m1/app/kernel/builtin_executors.py)）、`DB_QUERY`、`DB_SQL`（[db_executor.py](file:///d:/project/z-torque-community-m1/app/kernel/db_executor.py#L236)）、`MCP` / `MCP_NEGOTIATED`（Streamable HTTP + Stdio 双传输）、`MD_SCRIPT`
- 网关统一收口：超时控制、指数退避重试、jsonschema 参数校验、权限检查、写入工具审批挂起（WAITING_HUMAN）、审计落库

**2. MD-Skill 目录插件**
- `md-skills/` 下每个子目录即一个技能插件，[md_skill_registry.py](file:///d:/project/z-torque-community-m1/app/kernel/md_skill_registry.py#L60-L76) 按 front-matter 解析、指纹去重、自动建草稿版本；[md_skill_watcher.py](file:///d:/project/z-torque-community-m1/app/kernel/md_skill_watcher.py) 按租户定时扫描自动注册；zip 上传复用同一注册入口
- 脚本执行有严格隔离门槛（[md_skill_runtime.py](file:///d:/project/z-torque-community-m1/app/kernel/md_skill_runtime.py#L32-L33) Windows 上直接禁用）

**3. MCP 连接器插件**
- [StreamableHttpMcpConnector](file:///d:/project/z-torque-community-m1/app/kernel/skill_gateway.py#L143)（HTTP/SSE）+ [StdioMcpConnector](file:///d:/project/z-torque-community-m1/app/kernel/mcp_connectors.py#L67)，支持 `tools/list` 发现 → 快捷注册为工具版本

## 接口封装现状

**后端**
- **模型网关契约**：[contracts.py](file:///d:/project/z-torque-community-m1/app/kernel/model_gateway/contracts.py) 用 frozen dataclass 定义 `ModelRequest/ModelResponse/ModelToolCall/ModelUsage`，`ModelGateway` 基类提供 `generate/stream/stream_response` 三个接口，Mock 与 OpenAICompatible 两个实现可互换
- **端口层**：[ports/queue_driver.py](file:///d:/project/z-torque-community-m1/app/kernel/ports/queue_driver.py) 封装了内存队列 + `Executor` 类型别名，[TaskRuntime](file:///d:/project/z-torque-community-m1/app/kernel/task/runtime.py#L42) 依赖它驱动 Worker/OutboxDispatcher
- **统一响应**：`app/core/` 收口 `success_response/error_response`、`ApiError` 错误码体系、Trace ID 中间件

**前端**
- [api.ts](file:///d:/project/z-torque-community-m1/console/src/services/api.ts) 统一请求层：ApiEnvelope 解包、`ApiRequestError`、Bearer 头注入、**401 单飞刷新并重试**、Idempotency-Key、文件上传；[sse.ts](file:///d:/project/z-torque-community-m1/console/src/services/sse.ts) 封装 SSE 流式
