# 本地安装 Fuju Trace VexDB 适配器

这份文档用于在自己的 Python 项目里试用 `fuju-trace-vexdb`。它把 Fuju Trace 的事件写入 VexDB，并用 VexDB 的全文索引和向量索引查询。发布版支持通过 PyPI 安装；本地联调包仍可离线安装。

## 1. 安装

从 PyPI 安装（Python 3.10 或更新版本）：

```bash
python -m pip install 'fuju-trace[vexdb]==0.1.9'
```

这条命令会安装 `fuju-trace` SDK、`fuju-trace-vexdb` 适配器和通用 `psycopg2-binary` 驱动。当前 VexDB 联调使用过普通 `psycopg2`；不需要专用的 `vexdb-psycopg2`。如果你的环境已经提供兼容驱动，可只安装 `fuju-trace-vexdb==0.1.9`，由它自动安装 SDK。

拿到原来的 `dist/local-vexdb-0.1.9/` 联调包时，也可离线安装两个 wheel：

```bash
python -m pip install --no-index --find-links /path/to/local-vexdb-0.1.9 'fuju-trace-vexdb==0.1.9'
```

这个旧联调包没有自动安装数据库驱动；在同一 Python 环境中另装已验证的 `psycopg2` 兼容驱动。旧联调 wheel 与 PyPI 发布 wheel 的元数据不同，不能把旧联调包用作发布验证。安装后先检查：

```bash
python -c 'import fuju_trace, fuju_trace_vexdb, psycopg2; print("imports ok")'
```

## 2. 提供数据库参数

推荐使用 `vexdb_params` 传入连接参数。它直接交给驱动的 `psycopg2.connect(**params)`，密码中有 `@` 等特殊字符时也不用自己拼 DSN。参数来自环境变量，不要写进源码或提交到仓库。

| 环境变量 | 用途 | 示例 |
| --- | --- | --- |
| `DB_HOST` | VexDB 地址 | `127.0.0.1` |
| `DB_PORT` | 端口 | `5432` |
| `DB_NAME` | 数据库名，对应驱动的 `dbname` | `traces` |
| `DB_USER` | 用户名 | `trace_user` |
| `DB_PASSWORD` | 密码 | 由环境或密钥管理器提供 |
| `DB_SSLMODE` | 可选的 TLS 模式 | 按数据库配置填写 |
| `FUJU_TRACE_TENANT_ID` | 租户 ID，同一租户的写入与查询使用同一值 | `1` |
| `FUJU_TRACE_VECTOR_DIM` | 未来要使用的 embedding 维度 | `384` |
| `FUJU_TRACE_TABLE_PREFIX` | 测试表前缀，避免碰到已有表 | `ft_trial` |

最小的连接和写入示例（保存为 `trial.py`）：

```python
import os

from fuju_trace import DbExporter, Tracer, connect

tenant_id = int(os.environ["FUJU_TRACE_TENANT_ID"])
params = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
    "connect_timeout": 10,
}
if os.environ.get("DB_SSLMODE"):
    params["sslmode"] = os.environ["DB_SSLMODE"]

with connect(
    vexdb_params=params,
    tenant_id=tenant_id,
    vector_dim=int(os.environ["FUJU_TRACE_VECTOR_DIM"]),
    table_prefix=os.environ["FUJU_TRACE_TABLE_PREFIX"],
    initialize=True,
) as db:
    tracer = Tracer(exporter=DbExporter(db, tenant_id=tenant_id))
    with tracer.trace("本地试用", session_id=1001, tenant_id=tenant_id) as trace:
        with trace.span("检索测试") as span:
            span.log("客户申请需要人工复核")
    tracer.close()

    hits = db.search(text="人工复核", k=5)
    print(hits)
```

在运行环境里设置这些变量，再执行 `python trial.py`。不要把真实密码填进示例、命令历史或提交文件。`initialize=True` 会创建 `<prefix>_config`、`<prefix>_events`、`<prefix>_spans`、`<prefix>_attrs` 四张表及索引，因此用户需要建表和建索引权限。后续打开同一套表可以设为 `initialize=False`。测试时用独立前缀；代码不会替你删除表。前缀只能用小写字母、数字和下划线，以字母开头，最多 36 个字符。

已有 `VEXDB_DSN` 的项目也可以使用 `connect(vexdb_dsn=os.environ["VEXDB_DSN"], tenant_id=..., vector_dim=..., table_prefix=..., initialize=True)`。`vexdb_dsn` 与 `vexdb_params` 只能选一个。VexDB 文档提示 DSN 对部分特殊字符有限制，所以新接入建议用上面的分项参数。

## 3. 不写向量时

只要调用 `span.log(...)`、`db.search(text=...)` 等文本接口即可；不要调用 `set_embedding(s)` 或 `search(vector=...)`。`vector_dim` 目前仍是建表必填项，首次创建时会建向量列和索引，即使没有写入向量。因此当前版本**不是纯文本表结构**。请选定一个计划使用的维度；同一表前缀以后不能直接换维度。若要比较无向量写入的真实性能，请按这个实际结构测。

## 4. 真实场景注意点

- 一个 `session_id` 可以包含多条 trace；每次 `tracer.trace(..., session_id=...)` 会把它写进这条 trace 的事件。不同 session 可以同时写入同一租户的表，`session_id` 不是数据库分表或锁的单位。可用 `list_spans(filters={"externalSessionId": session_id})` 分页读取 Span，或用 `list_trace_ids(filters={"externalSessionId": session_id})` 找到该会话的 Trace ID；这还不是完整的 Fuju Trace session API。
- 同一进程推荐复用一个 `Tracer`、一个 exporter 和一个 `VexDBTraceStore`。多个线程可共用 store，但它只有一个数据库连接，会把读写事务排队；这保证连接使用安全，也限制高并发吞吐。多个进程应各自打开连接，不要把连接对象跨进程共享。
- 同一进程里即使每个 session 都新建 `Tracer`，同一 `node_id` 的 ID 计数器也会共享。跨进程或跨机器仍必须确保 `node_id`（0–1023）不重复；默认用 PID 低 10 位，只适合单机便捷试用，不能保证跨机器唯一。服务采用多 worker 时，应在每个 worker 启动后建 Tracer，并显式分配不同的 `node_id`。
- `tenant_id` 绑定到一个连接，查询只看这个租户。不同租户应分别打开连接。
- `DbExporter` 每条事件等待数据库提交，适合先确认数据正确；大量写入可改为 `BufferedDbExporter(db, tenant_id=tenant_id, max_batch=128, drop_when_full=False)`。调用 `tracer.close()` 或 `flush()` 后再查，并查看 `exporter.health()` 的 `dropped` 和 `write_errors`。`flush()` 只表示这些事件已处理，写入失败并被丢弃时也可能返回成功；确认落库还需检查健康状态或查询结果。
- 适配器不生成 embedding；由你的模型生成向量后调用 `db.set_embedding(trace_id, span_id, vector)` 或 `db.set_embeddings([...])`。查询时才传相同维度的 `vector`。
- 连接失败先用同一个 Python 环境下的 `psycopg2.connect(**params)` 验证网络、账号和驱动；建表失败检查权限及 VexDB 的 `fulltext`、`graph_index` 支持。
- 本地包提供事件写入、trace/span 点读、按会话分页读取、整条 Trace 的保留期清理，以及文本、向量、混合检索；尚未覆盖 Fuju Trace 嵌入式 DB 的所有 API。`prune_before(cutoff_ns)` 会删除早于截止时间的整条 Trace 及其原始事件，应由调用方设置并审核保留天数。

源代码、完整接口和测试命令见 [适配器 README](README.md)。
