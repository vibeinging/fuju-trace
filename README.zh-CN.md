# Fuju Trace

Fuju Trace 是 AI Agent 的 Trace SDK 和可嵌入的 Trace 数据层。应用在进程内创建 trace/span，并把事件直接写入 VexDB 或本地 TraceDB；查询也直接走数据库适配器。它不需要单独启动 Trace 服务。

[English](README.md) · [VexDB 参数与使用](fuju-trace-vexdb/README.zh-CN.md)

## 推荐接入：VexDB

```bash
pip install 'fuju-trace[vexdb]==0.1.9'
```

```python
import os
from fuju_trace import DbExporter, Tracer, connect

with connect(vexdb_dsn=os.environ["VEXDB_DSN"], tenant_id=1,
             vector_dim=3, initialize=True) as db:
    tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=1)
    with tracer.trace("风控", tenant_id=1) as trace:
        with trace.span("研判") as span:
            span.log("疑似盗刷，需要人工复核")
    tracer.close()
    print(db.search(text="盗刷", k=10))
```

`vector_dim` 必须等于业务 embedding 模型维度。可以只做文本检索，不提供 embedding；需要向量时由业务模型生成并调用 `db.set_embedding(...)`。`initialize=True` 首次建表和索引，后续打开可省略。连接可用 `vexdb_dsn` 或 `vexdb_params`，不要把密码写入代码仓库。适配器使用通用 `psycopg2` 协议；安装 extra 时已存在的兼容 `psycopg2-binary` 可以复用。

高并发写入可用 `BufferedDbExporter` 批量落库，并根据业务要求调用 `flush()` 建立可见性边界；同步确认写入则使用 `DbExporter`。同一进程的多个 session 可以共享同一个 VexDB store。不同进程或机器需要分配不同的 `node_id`。

## 本地嵌入式数据库

从源码仓库构建 native Python 绑定：

```bash
python -m pip install -e ./fuju-trace-sdk/python -e ./fuju-trace-db-python
```

```python
from fuju_trace import DbExporter, Tracer, connect

with connect(path="./trace-data", tenant_id=1) as db:
    tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=1)
    with tracer.trace("一次请求", tenant_id=1) as trace:
        with trace.span("调用工具") as span:
            span.log("完成")
    tracer.close()
    print(db.search(text="完成", k=10))
```

本地引擎用 Rust 标准库实现 WAL、崩溃恢复、trace 折叠、中文 BM25、向量索引和过滤查询。同机多进程可以共享本地目录；跨主机共享目录不在支持范围内。Node/Electron 可以使用 `@fuju/trace-db`，Rust 可以使用 `fuju-trace-db`。

## 技术特点

- `event_id` 由 `ext_span_id + seq + event_type` 确定，Python、TypeScript、Rust SDK 与引擎逐字节一致，重复事件只计一次。
- start、log、end 事件在读取时折叠成 span；本地数据库通过 WAL 和不可变段恢复。
- 文本、向量和混合检索可按 tenant、trace、时间、agent、状态及属性过滤。VexDB 适配器使用其原生 BM25 与向量索引，本地引擎使用自己的索引。
- SDK 与存储通过 Exporter/数据库适配器连接。基础 Python SDK 没有数据库运行时依赖；选择 `db` 或 `vexdb` extra 才安装对应后端。

## 仓库目录

- `fuju-trace-sdk/`：Python、TypeScript、Rust 打点 SDK。
- `fuju-trace-vexdb/`：VexDB 事件存储、span 读模型和检索适配器。
- `fuju-trace-engine/`：本地 Rust TraceDB 引擎。
- `fuju-trace-db-python/`、`fuju-trace-node/`、`fuju-trace-db-rs/`：进程内数据库绑定。
- `docs/CURRENT_STATE.md`：已实现范围和限制。

## 验证

```bash
cargo test --offline --manifest-path fuju-trace-engine/Cargo.toml
./scripts/package_mode_eval.sh
./tests/crash_recovery_kill9.sh 3
```

MIT 许可。当前版本是 alpha；部署前请用自己的数据分布和负载验证查询计划、召回及写入延迟。
