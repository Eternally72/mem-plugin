# 当前数据模型

以 `engine/models.py` 与 Alembic `0001`～`0010` 为当前实现基准。MCP 与 Python 共用 NoteAPI，旧五字段模型已退役。

## Memory Node

| 字段 | 已实现规则 |
| --- | --- |
| `id` | Engine 生成 UUID；身份、所有权与范围不可原地改写 |
| `user_id / namespace / agent_id` | 必填；持久化前与可信 AccessContext 对齐 |
| `scope` | user_global、domain_shared、agent_private；默认 Agent 私有 |
| `level / node_kind` | level 只能为 0～3；kind 由 level 确定，数据库同时约束 |
| `memory_type / domain` | 独立于层级；必填，初始显式写入默认 event/general |
| `content / context` | content 非空；原始 L0 content 不可覆盖，context 可用于后续受控演化 |
| `context_revision / is_dirty / dirty_revision` | context 与 Dirty 独立修订；后台整合消费 Dirty，旧代次不能覆盖新变化 |
| `consolidation_key / consolidation_hash` | 自动分组身份和输入指纹；不变的刷新输入可避免重复模型调用 |
| `attributes / domain_payload / source_ref` | JSONB；NoteAPI/MCP 已支持输入，校验大小和有限 JSON；纳入幂等指纹 |
| `confidence / importance` | 可为空，已知值为 0～1；不会默认伪造为完全可信 |
| `created_at / updated_at` | 带时区系统时间 |
| `occurred_at / valid_from / valid_until / last_confirmed_at / expires_at` | 可为空、带时区；未知时间保持空，不用创建时间代替发生时间 |
| `lifecycle_status` | active、stale、archived、deleted；支持按类型自动生命周期任务 |
| `version` | 正整数，初始 1；派生版本通过条件更新递增，L0 不可覆盖 |
| `embedding_status` | pending、ready、failed；写入任务后 pending，向量成功保存后 ready |
| `idempotency_key / request_hash` | 用户/namespace/Agent 内键唯一；相同输入复用，不同输入冲突 |

新增 archived_status（归档事实的有效状态）、needs_review、last_decay_at、restored_at、lifecycle_revision，memory_archive_manifests 保存校验与引用索引。详见 [生命周期](memory-lifecycle.md)。

默认值只代表基础接口的显式写入约定，不代表已经由模型完成分类或价值判断。`valid_until` 与 `expires_at` 分离；检索按有效区间过滤，按类型/显式保留期执行归档；仅明确配置 retention_action=forget 才自动彻底删除。

## 可见性与来源

所有读写先限定 user 和 namespace。Agent 私有仅对所属 Agent 可见；领域共享需该访问上下文显式授权领域；用户全局仍不得跨用户读取。

派生记忆的受众不能大于来源受众。私有事实不能整合进用户全局或领域共享；领域共享事实不能整合进用户全局或其他共享领域。Engine 与层级 Trigger 均检查此规则。

AccessContext 是可信宿主交付的已授权上下文，不承担身份认证本身。当前共享 Token 已绑定服务配置中的用户、Agent、namespace 白名单和领域授权；不是由工具参数自行声明身份。

## 图与版本

- `memory_hierarchy_edges`：同租户、同 namespace，相邻层级由低向高；唯一约束防重复；可多父归属。
- `memory_semantic_edges`：独立于层级边，可同层或跨层、可有语义循环。端点 FK 防止跨租户关系。
- 自动语义边增加 confidence、context_note 和 created_by_event；context 从当前 active 的自动边重建，不自由覆写原始事实。
- `memory_versions`：派生节点每次提交的内容、原因和版本；旧版本保留。
- `memory_version_sources`：每个派生版本的直接来源 ID 和来源版本；同来源 ID 去重。

版本来源保存生成当时的快照，不根据来源当前版本重新解释历史。递归沿 source_version 解析 atomic_source_ids，多路径到达同一 L0 只计一次；当前 member_of 边与最新版本直接来源一致，历史快照继续保留。版本新增 confidence 和 conflict_resolution，未知历史值不伪造补齐。

## 向量与任务

`memory_embeddings` 固定 qwen3.7-text-embedding-flash / 1024 维，按 Node 保存当前向量，包含 Node 版本、内容 Hash 和模型维度。当前确定性输入只使用 `content`，不拼接任意 JSON 或凭证。

向量写入锁定 Node，检查版本与 Hash；读取在 SQL 内先限定可见性，只返回 active 且版本/Hash 一致的向量。派生正文变化后设为 pending，旧向量不再参与召回，新向量异步覆盖。正文不变且已有当前向量时更新向量版本并复用。Embedding 输入规则变化必须同时调整 Hash 与重新向量化方案。

Outbox 的 `embedding.requested` 任务与 Note/Audit 原子提交，保存版本、Hash、身份授权快照及 Trace 关联，不复制正文。Worker 使用 SKIP LOCKED、租约 token、attempt 和失败字段执行任务；成功状态为 completed，暂时失败回到 pending 并延迟，终止失败设为 failed。版本、Hash、当前状态与租约写回前再次验证。未启动 Worker 时任务保持 pending。

0006 为 Node 增加由 content 生成的 search_vector 与两个 GIN 索引；不改变原始正文。关键词和向量通道共享 SQL 过滤规则。

## Audit 与 Trace

Audit 保存操作、Actor、Node ID、版本和原因，不复制正文；与变更共用事务。Trace/Span 使用独立短事务保存用例耗时、状态和错误类型。Trace 故障记录带关联 ID 的错误日志，不回滚已提交 Note。

当前 API 与 Worker 延续同一 Trace；Embedding/检索步骤记录模型、用量、尝试次数和降级，当前没有后台清理任务或采样。feedback.correct 保留原始事实并失效受影响派生内容；forget 选择纠正链与派生依赖，在事务内清理所有相关数据库副本。删除 Audit 不保存正文。模型 Key、Prompt、查询和用户正文均不进入 Trace 数据。

图任务通过 applied_at 标记应用状态，并记录 context 修订审计与 dirty 传播。自动上下文不参与当前向量输入；模型关系说明本身也不是事实来源。关联遗忘和纠正行为见 [MCP 接口](api.md)。

Outbox generation、确定性分组、整合阈值与 Dirty 刷新共同保证派生内容的一致性。明确遗忘的目标保存不含正文的 memory_forget_tombstones，阻止相同自动派生 ID 重建。摘要和向量使用相同正文 Hash 时复用，包括文本 A→B→A、B 的向量尚未生成的情形。
