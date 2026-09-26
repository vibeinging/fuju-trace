# Fuju Trace

Fuju Trace 记录 AI Agent 的执行过程，并让应用直接查询这些记录。SDK 把一次运行拆成 trace、span 和事件；存储适配器可写入 **VexDB**、本地 TraceDB、SQLite、DuckDB 或 PostgreSQL。当前项目不提供独立 HTTP/OTLP 服务或 Web 控制台。

[English](README.md) · [VexDB 连接参数](fuju-trace-vexdb/README.zh-CN.md) · [当前实现范围](docs/CURRENT_STATE.md)

## 为什么用 Trace

日志能说明发生了什么；Trace 还能说明一次请求里**谁调用了谁、先后顺序、耗时和结果**。Fuju Trace 保留原始 start/log/end 事件，并将它们合成为可查询的 span。你可以按会话或 trace 找回一条执行路径，按文本和属性检索相关运行，再对失败步骤做定位。

当前版本的技术特点：

- **可重复写入**：`event_id` 由 `ext_span_id`、`seq` 和事件类型确定；Python、TypeScript、Rust SDK 与本地引擎的计算一致。重试和恢复不会把同一事件重复计数。
- **存储可选择**：Python SDK 通过 `Exporter` 接存储。VexDB 使用原生 BM25 和向量索引；本地 TraceDB 有自己的 WAL、中文 BM25 和向量索引；通用 SQL 插件先提供事务写入和基础文本查询。
- **按执行上下文查询**：保留 session、trace、span 的关联，并支持按租户、时间、Agent、状态及属性过滤。文本、向量和混合检索的具体能力以所选存储适配器为准。
- **按需使用向量**：没有 embedding 时可以只写事件并做文本检索。VexDB 当前建表仍要求一个 `vector_dim`，也会创建向量列和索引；业务无需调用 `set_embedding()`。

## 5 分钟接入 VexDB

Python 3.10 或更新版本：

```bash
python -m pip install 'fuju-trace[vexdb]==0.1.11'
```

```python
import os
from fuju_trace import DbExporter, Tracer, connect

params = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

with connect(vexdb_params=params, tenant_id=1, vector_dim=384,
             initialize=True) as db:
    tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=1)
    with tracer.trace("风控复核", session_id=1001, tenant_id=1) as trace:
        with trace.span("研判交易") as span:
            span.log("疑似盗刷，需要人工复核")
    tracer.close()
    print(db.search(text="盗刷", k=10))
```

`vector_dim` 要与将来使用的 embedding 模型维度一致；示例的 `384` 只是占位值。首次运行的 `initialize=True` 会建表和索引，账号需要相应权限。后续使用同一套表时可省略。已有安全管理的 DSN 也可以传 `vexdb_dsn=os.environ["VEXDB_DSN"]`。详细的连接参数、会话读取和无向量场景见 [VexDB 接入文档](fuju-trace-vexdb/README.zh-CN.md)。

安装 extra 会带上 `fuju-trace-vexdb` 和通用 `psycopg2-binary>=2.9.5,<3`；已有兼容版本会被复用。若项目自行提供兼容的 `psycopg2` 驱动，可单独安装 `fuju-trace-vexdb==0.1.11`。

## 写入与查询怎么选

| 需求 | 用法 | 说明 |
| --- | --- | --- |
| 每次写入都要等数据库确认 | `DbExporter(db)` | 简单、同步，适合先验证接入正确性。 |
| 多个 session 持续写入 | `BufferedDbExporter(db, max_batch=128, drop_when_full=False)` | 后台批量写入；读取前调用 `flush()`，再检查 `health()` 中的写入错误和丢弃数。`flush()` 成功只表示队列已处理，不保证每条事件已落库。 |
| 只做文本检索 | `db.search(text="盗刷", k=10)` | 不需要生成或写入 embedding。 |
| 语义或混合检索 | `db.set_embedding(...)`，再 `db.search(vector=...)` 或同时传 `text` | 向量由业务模型提供；适配器不生成 embedding。 |
| 本地进程内存储 | 从源码构建 `fuju-trace-db` | 适合同一机器上的应用；本次 PyPI 发布未包含 native DB wheel。 |

同一进程的多个 session 可共用一个 VexDB store；它的单个数据库连接会串行使用。多个进程分别打开连接，并为各写入进程配置不同的 `node_id`（0–1023）。VexDB 适配器目前提供写入、文本/向量/混合检索、trace/span 点读和按会话过滤；它没有覆盖本地 TraceDB 的全部接口。

## 本地 TraceDB

本地引擎以 Rust 实现 WAL、不可变段、恢复、span 折叠、中文 BM25 和向量索引。Python 绑定在源码仓库中构建：

```bash
python -m pip install -e ./fuju-trace-sdk/python -e ./fuju-trace-db-python
```

把上面示例的 `connect(vexdb_params=..., ...)` 换为 `connect(path="./trace-data", tenant_id=1)`，其余 `Tracer` 和 `DbExporter` 用法相同。同机多个进程可以共享一个本地数据目录；跨主机或网络文件系统共享不在支持范围内。Node/Electron 和 Rust 的嵌入式绑定也保留在仓库中。

## SQLite、DuckDB、PostgreSQL 插件

三个数据库现在各有独立安装包：`fuju-trace-sqlite`、`fuju-trace-duckdb`、`fuju-trace-postgresql`。也可通过 `fuju-trace[sqlite]`、`fuju-trace[duckdb]`、`fuju-trace[postgresql]` 安装。它们依赖共用实现 `fuju-trace-sql`；原来的 SQL 包仍可直接安装。三个后端共用事件幂等、事务内折叠、租户隔离、会话/Trace 点读和精确属性过滤；文本查询暂用数据库 `LIKE` 子串匹配，**没有 BM25 或向量检索**。大数据量检索请先评估查询耗时；需要原生 BM25 和向量检索时选 VexDB。

| 数据库 | 独立安装包 | 连接参数 | 适合的场景 |
| --- | --- | --- | --- |
| SQLite | `fuju-trace-sqlite` | `connect(sqlite_path="./trace.sqlite", tenant_id=1, initialize=True)` | 单机应用，文件型数据库 |
| DuckDB | `fuju-trace-duckdb` | `connect(duckdb_path="./trace.duckdb", tenant_id=1, initialize=True)` | 单进程写入、偏分析的本地应用 |
| PostgreSQL | `fuju-trace-postgresql` | `connect(postgresql_dsn="...", tenant_id=1, initialize=True)` | 已有 PostgreSQL 实例的服务 |

源码安装和驱动选择见 [SQL 插件文档](fuju-trace-sql/README.md)。SQLite、DuckDB 已用真实数据库文件测试；PostgreSQL 已用本机临时 PostgreSQL 16 实例测试，CI 也已配置独立测试库。MySQL 暂不在支持范围内。

## 发布与验证

0.1.11 在 PyPI 发布基础 SDK、VexDB 适配器、SQL 共用实现及三个独立数据库插件。可直接安装数据库插件，也可使用基础 SDK 的 extra。[SQL 真实库测试报告](docs/reports/2026-09-25_sql-adapter-real-tests.md)记录了存储验证范围。

[Python SDK](fuju-trace-sdk/python/README.md) · [VexDB 适配器](fuju-trace-vexdb/README.zh-CN.md) · [仓库结构与开发约定](AGENTS.md) · [MIT 许可](LICENSE)

`fuju-rsi` 是独立项目。两者计划通过可选插件集成；Fuju Trace 不依赖 RSI。
