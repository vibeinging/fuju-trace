# SQL 存储插件设计与当前范围

日期：2026-09-25。

## 目标

让已经使用 SQLite、DuckDB 或 PostgreSQL 的 Python 应用直接保存和查询 Fuju Trace，不必部署独立 Trace 服务。自 0.1.11 起，三个入口分别用 `fuju-trace-sqlite`、`fuju-trace-duckdb`、`fuju-trace-postgresql` 安装，共用 `fuju-trace-sql` 的事件模型和读写接口；数据库驱动按需安装。旧的 `fuju-trace-sql` 安装方式仍有效。MySQL 暂不支持。

## 数据与事务

每个表前缀有四张私有表：配置、原始事件、折叠 Span、属性。租户、Trace、Span、事件身份在索引里使用固定长度摘要，原始事件和 Span JSON 保留原值。表前缀只能是经过校验的小写 SQL 标识符。

写入先验证 `event_id` 与 `ext_span_id + seq + event_type` 一致，再以租户和事件身份去重。新事件、折叠 Span 和属性在同一事务中更新。PostgreSQL 在更新一个 Span 前锁定对应行；SQLite 通过写事务串行；DuckDB 使用同一连接和显式事务。一次重试不会增加事件数；错误会回滚本次写入。

SQL 插件绑定单个租户。过滤条件始终附加租户键，调用方不能在查询体里改租户。支持 Trace ID、会话 ID、时间、Agent、状态及精确属性过滤。

## 搜索能力

当前三种数据库只做转义后的 `LIKE` 子串查询，范围是 input/output text 和日志。没有 BM25、向量索引、RRF、embedding 写入或性能保证；`capabilities()` 明确报告 `substring_scan` 与 `vector=None`。向量查询会报错。保持统一的读写语义后，再按真实负载分别接原生检索：SQLite FTS5、PostgreSQL 全文检索，以及 DuckDB 更适合的批量索引策略。

DuckDB 官方说明其 FTS 索引[不会随着表写入自动更新](https://duckdb.org/docs/current/core_extensions/full_text_search)，因此不能在持续写入 Trace 的插件里直接把它当作实时检索；其[同一文件并发写入](https://duckdb.org/docs/stable/connect/concurrency.html)也应按单写进程使用。在完成端到端验证前，不把这些能力写成现有功能。

## VexDB-Lite 对选择的影响

[VexDB-Lite](https://github.com/VexDB-THU/VexDB-Lite) 已把同一图索引实现接到 PostgreSQL、DuckDB 和 SQLite。它属于**这三个数据库的可选向量扩展**，不是第四套事件存储；Fuju Trace 的原始事件与折叠 Span 仍由 SQL 插件管理。当前 Fuju Trace 尚未加载或调用这些扩展，`capabilities().vector` 仍为 `None`。

| 宿主 | VexDB-Lite 接口 | 接入前提 |
| --- | --- | --- |
| PostgreSQL | `floatvector(N)`、`USING vexdb_graph`、`<=>` 等距离运算符 | 安装 PostgreSQL 扩展、配置并重启服务；当前发布包面向 PG 16–19 |
| DuckDB | `FLOAT[N]`、`USING GRAPH_INDEX`、`cosine_distance()` 等函数 | 匹配 DuckDB 版本，每次连接显式加载扩展；当前发布包主要面向 Linux |
| SQLite | `GRAPH_INDEX` 虚拟表，`embedding MATCH ? AND k = ?` | 明确提供可信的本机扩展路径；移动端需静态注册 |

这三个向量接口的 DDL、数据类型和查询语法不同，不能把现有商业 VexDB 适配器直接换一个 DSN 来复用。商业版适配器还依赖 `USING fulltext`、`bm25_score()` 和 `USING graph_index`，VexDB-Lite 的项目说明未承诺提供相同的 BM25 接口。实现时应保留统一 `set_embedding/search(vector)/capabilities` API，在各数据库模块下使用对应 SQL；未安装扩展时保持文本路径可用，向量请求明确报错。

扩展启用应由用户显式配置，不自动下载或加载本机代码。启动时校验版本、向量维度和距离指标，并用 `EXPLAIN` 验证真实索引路径。没有 embedding 的场景不创建向量表或索引。首版使用普通图索引；PQ/RaBitQ 保持可选，待真实 Trace 负载验证空间、写入和召回后再启用。

## 验证状态与后续

- SQLite：使用真实文件数据库验证 SDK 写入、乱序事件、重试、回滚、过滤、租户隔离、并发线程及重开。
- DuckDB：用真实文件数据库验证建表、乱序写入、重试、回滚、文本查询、租户隔离及重开。
- PostgreSQL：用临时 PostgreSQL 16 实例验证建表、SDK 写入、乱序事件、幂等重试、冲突回滚、租户隔离、双连接并发写同一 Span、文本查询及重开；CI 已配置独立 PostgreSQL 16 服务容器，远端运行结果待工作树交付后观察。目标版本和业务负载仍需单独验证。
- 本机可运行 `FUJU_SQL_PYTHON=python3 ./fuju-trace-sql/tests/run_local_postgresql.sh`，脚本只使用临时 Unix socket 实例并在退出时清理；也可在一次性测试库设置 `POSTGRESQL_DSN`，运行 `python fuju-trace-sql/tests/live_smoke.py`。
- 发布前统一调整包版本，打包并在干净环境安装三个 extra；0.1.9 的公开 wheel 不会因为源码改动自动增加 extra。
