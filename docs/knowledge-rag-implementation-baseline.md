# Z-Torque 本地完整 RAG 实施基线

> 状态：实施基线；目标版本：Windows 单机 EXE；不引入 PostgreSQL、Redis 等外部服务依赖。

## 1. 当前代码基线（2026-11-25）

当前知识链路已具备：
- `/files` 上传入口及本地对象存储；
- PDF、TXT、Markdown、CSV、XLSX 结构化解析；
- 带页码、行号、工作表坐标的文本块；
- `KnowledgeProvider` Port 和 SQLite FTS5 全文索引；
- 中文关键词补充召回、文件路径白名单、引用校验、重建索引；
- 知识检索管理页。

当前仍缺少：
- 独立的用户知识库实体及文档归属关系；
- Embedding 模型接入与向量生成；
- 可持久化的本地 VectorStore 实现；
- 语义检索与向量/关键词混合检索；
- AgentVersion 对知识库的版本化绑定；
- Agent Runtime 在模型上下文中调用检索并注入引用；
- 知识库级授权、删除/重建索引一致性及端到端测试。

现有 FTS5 只能证明关键词全文检索可用，不能标记为完整 RAG。

## 2. 已锁定产品约束

1. 仅交付 Windows 本地单机 EXE；用户数据目录与程序安装目录分离。
2. SQLite 是唯一默认业务数据库；知识原文、附件和向量索引保存在本地。
3. 不要求用户安装 PostgreSQL、Redis 或独立向量数据库服务。
4. 知识库由当前用户统一管理；一个知识库可关联多个 Agent，一个 Agent 可关联多个知识库。
5. Agent 只能检索当前用户有权访问且已绑定到当前发布 AgentVersion 的知识库。
6. Agent 运行时只能通过 Knowledge Retrieval 服务检索，不得直接访问向量索引或任意文件路径。
7. 现有 21 个 Port 契约和插件装配边界优先保持；如需扩展，优先窄扩展 `knowledge.provider` 与现有 `vector.store`，不随意增加 Port。
8. 普通聊天仍是 Message-only；只有实际执行工具/任务时才创建 Task。RAG 检索属于 Agent 回答上下文能力，不应仅因一次检索而强制创建 Task。

## 3. 目标架构

```text
Knowledge UI
  -> Knowledge API
    -> Knowledge Service
      -> AIP / SecurityContext authorization
      -> Document Parser -> Chunker
      -> Embedding Provider
      -> VectorStore Adapter ----> local persistent index
      -> FTS5 keyword index
      -> Hybrid Retrieval / optional reranker
  -> AgentVersion knowledge binding
  -> Agent Runtime retrieval context
  -> ModelGateway
  -> answer + source citations
```

## 4. 数据模型目标

- `KnowledgeBase`：用户拥有的知识库，包含名称、描述、状态、配置和所有者。
- `KnowledgeDocument`：知识库与已上传文件的关联，包含处理状态、解析版本、索引版本和失败原因。
- 现有 `KnowledgeDoc` / `KnowledgeChunk`：在兼容现有文件索引的前提下演进，不直接破坏历史数据。
- AgentVersion 绑定：知识库 ID 与检索配置必须进入不可变版本快照/哈希；发布时验证知识库归属、可用状态和权限。
- 向量记录：至少关联 tenant/user、knowledge_base、document、chunk、embedding_model、embedding_dimension 和内容哈希。

具体迁移必须向前兼容 SQLite 现有用户数据库，不得要求用户删除数据库重装。

## 5. 检索要求

第一版必须实现：
- 关键词检索（保留现有 FTS5）；
- Embedding 语义检索；
- 混合召回与统一排序；
- Top-K、阈值和元数据过滤；
- scope/authorization 先于 Top-K，不能先取全局 Top-K 再过滤；
- 返回 chunk_id、document_id、文件名、页码/章节/行号、片段文本和检索分数；
- 无可用知识时明确返回空证据，不允许模型伪造引用；
- 模型回答展示可回查来源。

Reranker 是可选增强项，不作为第一版上线阻塞条件。

## 6. Embedding 与本地向量存储选型门槛

Embedding 必须通过统一适配接口接入，支持用户配置的兼容服务；不得复用 Chat Completions 假装 Embedding。应记录模型标识和向量维度，模型变更时触发重建索引。

本地向量索引必须：
- Windows x64 可打包；
- 持久化，不依赖内存生命周期；
- 支持 upsert/delete/search；
- 有限内存、批量索引和检索结果上限；
- 不需要用户启动额外数据库服务；
- 在 EXE 打包环境有真实回归测试。

在完成 Windows 打包 POC 前，不锁定某个含原生扩展的向量库。禁止用随机向量、哈希向量或关键词分数冒充语义向量检索。

## 7. 实施顺序

### RAG-1：知识库资源与授权
- 增加知识库 CRUD 和文档关联；
- 上传文件可归入指定知识库；
- 知识库列表、详情、文档状态、删除和重试；
- 建立用户归属与 AIP 授权测试；
- 保留旧文件入口兼容。

### RAG-2：Embedding 与本地 VectorStore
- 选定并验证 Embedding Provider；
- 实现真实向量生成、持久化、批量 upsert、删除和重建；
- 处理模型/维度变化和部分失败；
- 增加 Windows x64 安装包 POC。

### RAG-3：混合检索
- 保留 FTS5；
- 增加向量召回；
- 融合排序、过滤、Top-K 和阈值；
- 先做权限范围约束，再召回；
- 检索测试页展示两路召回和最终命中。

### RAG-4：AgentVersion 与 Runtime 集成
- Agent 配置可选择一个或多个知识库；
- 发布快照固定知识库引用及检索配置；
- Runtime 在模型生成前调用 Knowledge Retrieval；
- 将证据片段与来源坐标注入上下文；
- 审计检索范围、命中来源和模型版本；
- 不将敏感原文或密钥写入普通日志。

### RAG-5：验收与交付
- PDF/Word（DOCX）/TXT/MD/CSV/XLSX 支持矩阵；
- 上传、解析、切片、向量化、检索、Agent 问答端到端测试；
- 跨用户/未绑定知识库越权测试；
- 删除、重建、备份恢复和程序升级测试；
- Windows 全新环境 EXE 打包与运行验证。

## 8. 验收标准

1. 用户可创建多个知识库，并将文件加入指定知识库。
2. 文件状态能准确反映解析、切片、Embedding、索引各阶段及失败原因。
3. 用户可以通过语义问题检索到关键词不完全匹配但语义相关的内容。
4. 混合检索能返回可回查的原文来源坐标。
5. Agent 发布版本可绑定多个知识库；运行时只检索该版本已绑定且用户有权访问的知识库。
6. Agent 回答可展示引用来源；无证据时不伪造来源。
7. 删除文档/知识库后，全文索引和向量索引均被一致清理。
8. SQLite 数据库与本地索引重启后可用，备份恢复后可用。
9. 全新 Windows 环境安装 EXE 不需要单独安装 PG、Redis 或向量数据库。
10. 所有新增能力有单元、集成、安全和 EXE 回归测试。

## 9. 明确不做

- 本阶段不实现多租户扩展或企业服务端部署；
- 不引入 PostgreSQL/pgvector、Redis 或独立向量数据库作为运行依赖；
- 不以纯 FTS5、LIKE、随机向量或伪向量冒充完整 RAG；
- 不允许 Agent 绕过 AIP 直接读取本地文件；
- 不因 RAG 检索本身把普通聊天改成 Task。
