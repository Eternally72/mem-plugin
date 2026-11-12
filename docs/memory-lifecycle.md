# 生命周期、归档与恢复

生命周期规则由 Engine 实现；Scheduler 只创建 `lifecycle.maintain` Outbox 任务，Worker 调用同一用例。手动归档与恢复通过 NoteAPI/MCP 提供，不调用模型。

## 三个独立维度

- `valid_until`：事实有效截止时间。到期变为 stale，历史原文保留；来源失效会传播到派生祖先，标记 dirty。归档节点以 `archived_status` 保存 active/stale，有效期扫描也覆盖冷数据。
- `confidence`：已知置信度按类型半衰期衰减；未知值保持空，不凭空生成评分。`needs_review` 只提示复核，不自行否定事实。confirm 清除此标记、记录确认时间；不会把 stale 改回 active。
- `expires_at`：数据保留截止时间。默认到期归档，和事实是否有效无关。事件变旧不自动成为错误事实。

年龄规则从 `max(created_at, last_confirmed_at)` 计算，不猜测事件时间。衰减从最近衰减/确认时间计算，按实际间隔累计。所有时间带时区。

## 默认类型策略

下表天数为当前默认值，0 表示停用对应年龄规则。它们是可调整的起始策略，尚未经过真实业务语料评测。

| memory_type | 复核间隔 | 归档年龄 | 置信度半衰期 |
| --- | ---: | ---: | ---: |
| temporary_plan | 7 | 30 | 30 |
| short_term_state | 7 | 30 | 30 |
| preference | 90 | 0 | 365 |
| habit | 180 | 0 | 730 |
| event | 0 | 365 | 0 |
| investment_event | 0 | 730 | 0 |
| project | 90 | 0 | 0 |
| 其他类型 | 0 | 0 | 0 |

系统不推断“计划完成”或“项目结束”。调用方可提供明确有效期/保留期，结束后显式 archive，需要纠正原事实时使用 feedback。没有统一两年 TTL。

环境配置：

```dotenv
LIFECYCLE_ENABLED=true
LIFECYCLE_INTERVAL_SECONDS=3600
LIFECYCLE_DECAY_INTERVAL_SECONDS=86400
LIFECYCLE_RESTORE_GRACE_DAYS=7
LIFECYCLE_TYPE_POLICIES={}
ARCHIVE_DIR=data/archive
ARCHIVE_MAX_BYTES=8388608
```

`LIFECYCLE_TYPE_POLICIES` 是按类型覆盖的 JSON，例如 `{"event":{"archive_after_days":730},"preference":{"confidence_half_life_days":0}}`。可覆盖 review_after_days、archive_after_days、confidence_half_life_days、retention_action。默认 retention_action=archive；明确配置 forget 后，达到该类型归档年龄或 expires_at 会执行彻底遗忘，包含纠正链和依赖派生记忆。该选项有真实删除效果，默认不启用。

`LIFECYCLE_ENABLED=false` 停止后台领取和扫描生命周期任务，手动 archive/restore 仍可使用。恢复默认提供 7 天免重新归档窗口；明确配置的 retention_action=forget 不受此窗口豁免，已达到删除期限时拒绝恢复。

Trace 和 Audit 分别永久保留，独立于 Note TTL；当前没有自动日志裁剪。显式 forget 仍会清除相关旧 Audit，只留下无正文删除审计和链路元数据。

## 归档事务与文件

1. 在租户范围锁内生成有界结构化快照，提交 writing Manifest。Manifest 包含节点 ID、存储键、操作 ID、SHA-256、记录数、字节数、节点版本、有效状态和引用索引。
2. 新事务重新核对节点快照和操作 ID，在同一范围锁内完成有界本地文件写入与读回校验。文件采用私有权限、临时文件、fsync 和原子替换；不执行模型或远程网络调用。
3. 校验成功后，同一事务设置 archived、保存有效状态、移除向量、结束关联后台任务并写 Audit。FTS/trigram GIN 索引只覆盖 active 节点，普通搜索不返回归档数据。

JSONL 保存节点原文、结构化领域 Payload、时间、该节点所有派生版本及来源快照、双方可见的层级/语义关系。向量可重建，不作为事实副本保存。单节点快照默认最多 8 MiB，超限明确失败。

PostgreSQL 仍保留原始节点、版本和关系，作为来源与身份校验依据；这是从热索引移出的逻辑冷存储，不是释放所有 PostgreSQL 正文空间的迁移，也不是全库灾难恢复备份。`memory.get` 按 ID 读取归档节点及历史来源；普通 search 不检索冷数据。

写文件或校验失败保留热数据。文件写入后数据库提交失败时，writing Manifest 仍能定位最终文件和临时文件；重试可重新校验和完成，forget 可清理。任务按稳定 ID、生命周期代次、租约 fencing 和 applied_at 幂等处理。失败超过 WORKER_MAX_ATTEMPTS 后不无限循环，修复存储后可手动 archive 重试；没有绕过校验的后台复活流程。

## 恢复与修复

`memory.restore(memory_id)` 先验证完整性、身份与版本，再切回 active/stale。它不把文件中的旧原文覆盖到数据库。valid_until 已过期、archived_status=stale 或派生内容 dirty 时仍为 stale；需要刷新的派生节点重新入队，active 节点创建新的 Embedding 代次。旧任务不能覆盖恢复后的状态。

文件缺失或损坏时返回 archive_error，节点仍保持归档；明确调用 `memory.archive(memory_id, repair=true)` 可用保留的 PostgreSQL 原始记录重建，再 restore。处于 writing 且节点仍在热区时，restore 可取消未完成归档，清理已跟踪文件并保留现有向量状态。

## 彻底遗忘

forget 与归档使用同一租户范围锁，防止删除结束后产生迟到副本。它先删除被遗忘节点及依赖派生节点的归档文件、同键临时文件，再在事务中删除节点、版本、来源、边、向量、任务和旧 Audit。删除归档失败时不返回成功，数据库原始记录保留供重试。

若仍保留的邻居归档包含被删除节点的关系或 context，副本也会被清除，Manifest 标记 invalidated；数据库邻居内容刷新，后续 restore 从已清理的原始记录重建这类已知失效副本。普通损坏不会被当成这种情况自动修复。

文件系统和 PostgreSQL 不支持跨资源原子提交：若副本已删除后数据库事务失败，数据库仍保留原始事实，但旧副本可能缺失；需要重试 forget 或显式 repair。本阶段保证不虚报删除成功、不丢失唯一事实来源，不承诺磁盘介质、WAL、数据库备份或外部副本的安全擦除。
