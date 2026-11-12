# 运行日志与执行链路

`./start.sh` 前台显示监督进程及 API、Worker、Scheduler 的安全应用日志，同时保留每个子进程原有的私有日志文件。格式包含模块、带时区的时间、级别和事件；终端额外标注进程角色。交互方式参考 [TiMEM 简单日志](https://github.com/TiMEM-AI/TiMEM/blob/7e05ffda855602a378d4f4a45a3e97e1d1ce4cb9/timem/utils/logging.py) 的时间与步骤提示，使用 Python 标准库 logging 实现。

## 判断当前状态

| 事件 | 含义 |
| --- | --- |
| memory_process_started | API、Worker 或 Scheduler 子进程已创建 |
| memory_runtime_ready | 使用本轮 Token 完成 MCP 工具发现，服务可连接 |
| memory_runtime_alive | 每 30 秒报告一次进程存活；不表示收到 Agent 请求 |
| memory_operation_started / finished | 开始/结束一次 Engine 操作或内部步骤，含 operation、namespace、trace_id、span_id、status、duration_ms |
| memory_task_started / finished / failed | 后台任务领取、结束或失败，含 task_id、attempt、trace_id、outcome |
| memory_runtime_stopped | 本次监督的子进程已退出 |

Agent 自己推理、读取行情或运行策略时，记忆服务无法获知其进度。只有 Agent 实际调用 MCP 记忆工具，才会出现对应操作日志；同时存在 Scheduler 自发的定期扫描，需通过角色与 operation 区分。不能将没有记忆操作日志直接解释为 Agent 卡死。

## 日志位置与隐私

普通启动的文件位于 `data/runtime/api.log`、`worker.log`、`scheduler.log`。隔离测试的文件位于对应 `data/test-runs/<run_id>/`。子进程日志追加保存且权限为 0600，终端只转发本次启动后新增的 mem_plugin 应用日志，不重放旧文件或输出第三方 SDK 的请求体/堆栈。端口就绪和重复启动保护维持原契约。

操作日志使用固定字段白名单，记录标识、模型名称、Token 用量、候选数量与错误类型；不输出查询正文、记忆原文、Prompt、完整模型响应、API Key 或 Bearer Token。实际字段取决于该 Span 提供的元数据。失败日志不复制异常正文。后台任务重试仍受现有租约和次数限制。

PostgreSQL Trace/Span 与 Audit 保留原有持久化规则；终端日志不替代它们。按 trace_id 可关联 API、Embedding、检索、整合和后台任务。

更新配置或日志实现后，先按 Ctrl+C 停止现有进程，再执行 `./start.sh`。脚本不会清空数据、迁移数据库或启动外部 Agent。
