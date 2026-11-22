# PostgreSQL 迁移

此目录是 Alembic 唯一 Schema 入口，revision 为 `0001`～`0011` 的线性链。

在仓库根目录安装 `postgres` extra、配置私有 `DATABASE_URL` 后执行：

```bash
uv run --extra postgres alembic heads
uv run --extra postgres alembic upgrade head
uv run --extra postgres alembic current
```

迁移读取当前工作目录 `.env`，不要求 MCP Token。不要把凭证放进命令行或 `alembic.ini`。
API、Worker 和 Scheduler 不执行迁移。已执行迁移不可修改，Schema 变化新增 revision。
当前 downgrade 明确拒绝破坏性删除；应用版本回退不等于删除数据库或丢弃审计。

`0001` 启用 vector 0.8.6 与 pg_trgm；`0002` 创建 Node 与不可变约束；
`0003` 创建 Trace/Span/Audit 和 Outbox；`0004` 创建图与版本来源；
`0005` 创建 qwen3.7-text-embedding-flash 的固定 1024 维向量表及 HNSW 索引。
`0006` 创建生成 FTS 列、FTS/trigram GIN 索引；`0007` 增加任务身份快照和租约 token，并从所属节点回填现有任务。0001～0005 保持不变，新增迁移不清理记忆数据。
`0008` 增加上下文修订、dirty、自动关系来源/置信度/说明、任务 applied_at 与目标边索引，保留旧节点和关系。0001～0007 均不修改。
`0009` 增加 Dirty/Outbox 代次、整合身份/指纹、版本置信度和冲突说明、明确遗忘标记及扫描索引；调整任务唯一约束以支持同版本多次 Dirty，旧事件代次为 0。0001～0008 保持不变，当前数据库内容不清理。

集成测试通过显式 `MEM_PLUGIN_TEST_DATABASE_URL` 连接测试管理库，创建随机的
`mem_plugin_test_*` 数据库，从空库应用完整迁移，完成后仅删除本次创建的随机测试库。
不要将测试管理 URL 指向生产环境。

`0010` 增加独立有效/复核/衰减/恢复状态与归档 Manifest，将词法 GIN 改为 active 部分索引。升级不删除业务数据，旧迁移保持不变；写归档文件由 Engine 用例执行，迁移不操作文件系统。详见 [生命周期](../docs/memory-lifecycle.md)。
