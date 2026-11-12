# MCP 接口

通过 MCP Streamable HTTP 提供记忆操作，所有工具调用统一的 NoteAPI。响应保留结构化记忆、检索通道和来源信息。

## 连接与身份

宿主配置 MEM_PLUGIN_MCP_URL 和 MEM_PLUGIN_MCP_TOKEN，或添加 Authorization: Bearer 请求头。Token 不放在 URL 或工具参数中。无效凭证返回 401，session ID 不能替代凭证。

该凭证映射到服务端 MEM_PLUGIN_USER_ID、MEM_PLUGIN_AGENT_ID、MEM_PLUGIN_NAMESPACES 与 MEM_PLUGIN_DOMAINS。工具中的 namespace 必须位于白名单；未授权分组拒绝访问。省略或 null 的 namespace 使用服务端绑定值，普通入口为 MEM_PLUGIN_NAMESPACES 的首个值（默认 default），测试入口为本轮独立空间。显式 namespace 仍检查白名单。默认 scope 为 agent_private；domain_shared 还需领域授权。

GET /health 不要求 Token，只表示进程存活。没有重复的 REST CRUD 入口，也没有 stdio。

## 工具

| 工具 | 必填参数 | 其他参数 | 返回 |
| --- | --- | --- | --- |
| memory.record | content | namespace（可省略）、scope=agent_private、domain=general、memory_type=event、idempotency_key、attributes、domain_payload、source_ref、occurred_at、valid_from、valid_until、expires_at | Memory |
| memory.get | memory_id | namespace（可省略） | Memory |
| memory.search | query | namespace（可省略）、limit=20、mode=hybrid、scope、domain、level、memory_type、occurred_after/before、as_of、attributes、domain_payload | memories、mode、degraded_reasons |
| memory.feedback | memory_id、action | namespace（可省略）、content、idempotency_key | action、memory、可选 replacement |
| memory.forget | memory_id | namespace（可省略） | deleted 布尔值 |
| memory.archive | memory_id | namespace（可省略）、repair=false | 校验并提交后的 Memory |
| memory.restore | memory_id | namespace（可省略） | 恢复后的 Memory |

Memory 保留 ID、正文、namespace、scope、层级、类型、领域、置信度、版本、生命周期与 Embedding 状态，以及系统/事实时间；读取或搜索派生节点时返回直接 source_ids。未知事实时间为空。内部用户身份不需要由 Agent 传入。

record 在 Note、Audit、Outbox 提交后返回，embedding_status=pending 不等于已经向量化。同一用户/namespace/Agent 下，同一幂等键和同一输入复用记录，不同输入冲突。正文去除两端空白，默认最多 16000 字符；结构化字段各自最多 16000 个 JSON 编码字符，要求有限 JSON 对象。基础准入拦截明确的私钥、Bearer 凭证和结构化密钥字段；这不是完整敏感信息识别器。时间必须带时区，未知值保持空。

search 默认 hybrid，词法和向量召回后按 RRF 融合。mode=keyword 使用 FTS/子串/trigram，mode=literal 使用字面子串；limit 为 1～100，空查询无效。模式、结构化与时间过滤、中文支持、降级语义见 [retrieval.md](retrieval.md)。缺少 Embedding Key 时仍可正常使用关键词检索，响应明确给出原因。

Memory 补充 attributes、domain_payload 与 source_ref；搜索条目还包含 score、channels 和 channel_scores。score 是 RRF 排名分数，不是事实置信度。直接 record/get 的 score 为空、channels 为空。派生结果仍保留直接 source_ids。模型请求期间失效或版本变化的候选不返回。

Memory 另包含 context、context_revision、is_dirty。context 是自动关系产生的可追溯标注，不替代 content；is_dirty 表示派生内容等待重新整合，Worker 已支持自动刷新。memory.get 的 relations 返回最多 100 条双方可见的语义关系及其方向、confidence、context_note 和 created_by_event；其他操作的 relations 默认为空。自动边不包括 supersedes。

get/search 的派生条目包含 atomic_source_ids（递归去重的 L0 来源）和 conflict_resolution（该版本的冲突/不确定性处理说明），source_ids 表示直接来源。L0 的 atomic_source_ids 是自身 ID。来源按生成时的 source_version 快照解析，不根据最新子节点内容重写历史。memory.forget 删除明确派生目标后保留无正文的 UUID 标记，防止相同自动分组身份被后台重建；需要遗忘事实本身时定位原始 L0。来源、版本与整合状态见 [数据模型](memory-data-model.md)。

feedback 的 action：

- confirm：清除 needs_review 并记录确认时间，不覆盖原文，也不自动恢复 stale 状态。
- reject：将目标及依赖其内容的派生记忆标记 stale。
- correct：要求 content，为原始 L0 创建新事实，返回 replacement 的新 ID；原事实保留并标记 stale，派生结果失效。同一纠正幂等键可安全复用；需要继续纠正时使用最新 replacement ID。

forget 同步删除目标的纠正链及依赖它的派生记忆，清理相关版本、关系、向量、Outbox 和旧 Audit，保留不含正文的删除审计。其他无关来源节点保留。不存在或不可见时 deleted=false；失败回滚，不返回伪成功。该承诺覆盖本系统数据库，不代表数据库物理介质、WAL 或外部备份已安全擦除；归档文件及同键临时文件也在成功返回前清理；文件删除失败时保留数据库原始数据，操作失败，可重试。

旧的五个下划线工具名称及正文覆盖语义已经退役，请客户端重连并重新发现工具。

Memory 另返回 expires_at、archived_status、needs_review、last_decay_at、restored_at。归档从普通搜索移出，get 按 ID 仍可追溯。restore 校验副本后恢复，过期/被否定/dirty 的事实保持 stale；缺失或损坏副本返回 archive_error，可明确使用 archive 的 repair=true 重建。恢复不覆盖原始事实。完整语义见 [生命周期](memory-lifecycle.md)。

## 错误

invalid_input、not_found、access_denied、conflict、storage_error、provider_error、archive_error 转换为 MCP ToolError。错误不暴露 SQL、Key 或堆栈。查不到与看不到某个 ID 不泄露其内容。

FastMCP Client 默认对工具错误抛异常；设置 raise_on_error=False 时检查 is_error。写入超时可能已经提交，使用原幂等键核对，不盲目创建新请求。
