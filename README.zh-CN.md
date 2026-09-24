# Fuju Trace

**让 AI Agent 的运行过程可回放、可搜索。** Fuju Trace 把一次运行中的模型调用、工具调用、输出、错误和 token 用量连成完整记录。先接入轻量 SDK，就能复盘发生了什么、查找相似运行；需要在应用内查询时再加入嵌入式存储。

中文 · [English](README.md) · [MIT 许可证](LICENSE)

> **仓库状态：** 这是拆分后的独立本地仓库。新的 Python 和 npm 包名已经配置，尚未发布。下面的源码快速体验不依赖包仓库。已验证的能力与发布状态见[当前状态](docs/CURRENT_STATE.md)。

![Fuju Trace 回放控制台](docs/images/console-overview.png)

## 为什么用 Fuju Trace

log 记录一件事发生了。排查 Agent 的一次运行，还需要知道哪些步骤属于同一次运行、步骤之间如何调用，以及结果在哪一步发生变化。Fuju Trace 用 trace 和 span 保留这些关系；每个 span 仍可附带 log。

- **解释结果和失败：** 按 session 回放嵌套的模型与工具调用，查看输入输出、错误、耗时和 token 用量。
- **从历史运行中找规律：** 用中文 BM25、带过滤的向量或混合检索找相似案例，并按租户、Agent、状态、时间和支持的属性缩小范围。可用标注和数据集关联保存评测所需的案例。
- **按需要选择接入深度：** Python、TypeScript、Rust SDK 和 OTLP/HTTP JSON 都能写入；查询可以走本地 HTTP 服务，也可以把同一 Rust 引擎嵌入 Python、Node/Electron 或 Rust 应用。
- **正确处理重试与重启：** 确定性的事件 ID 用于识别重复事件；持久化引擎有 WAL、快照和恢复机制，核心 Rust 工作区不依赖第三方 crate。

Fuju Trace 保存执行证据；提示词优化和独立验收在另一个 `fuju-rsi` 项目中。

## 从源码快速体验

下面的命令都从仓库根目录执行。需要 Rust 1.80+ 和 Python 3.8+。示例服务自带演示 trace，数据保存在**内存中**；需要持久化时使用嵌入式 DB 或 Python DB 服务。

**终端 1：启动示例服务。**

```bash
cargo run --offline --manifest-path fuju-trace-engine/Cargo.toml \
  -p fuju-trace-engine --example server
```

**终端 2：用 Python SDK 写入一条 trace，再搜索它。**

```bash
PYTHONPATH=fuju-trace-sdk/python python3 - <<'PY'
from fuju_trace import HttpExporter, Tracer

tracer = Tracer(
    exporter=HttpExporter("http://127.0.0.1:7878/v1/ingest", tenant_id=1),
    node_id=1,
)
with tracer.trace("风控复核", tenant_id=1) as trace:
    with trace.span("查询交易") as span:
        span.log("疑似盗刷")
tracer.close()
PY

curl -fsS http://127.0.0.1:7878/v1/search \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: 1' \
  -d '{"text":"盗刷","k":10}'
```

`tracer.close()` 会发送缓冲中的事件。写入和查询使用相同的 `X-Tenant-Id`。线格式、搜索过滤、鉴权和 OTLP 入口见 [API 文档](docs/API_REFERENCE.md)。

### 打开回放控制台

全新源码检出需要先构建前端，Rust 二进制才能内嵌页面。从仓库根目录执行以下命令，**然后重启**示例服务：

```bash
npm --prefix fuju-trace-console ci
VITE_API=http npm --prefix fuju-trace-console run build
python3 scripts/sync_console.py
```

打开 [http://127.0.0.1:7878/](http://127.0.0.1:7878/)。控制台与其他客户端使用同一套 `/v1/*` API。

## 选择接入方式

下表是**计划发布的包名**，目前还不能当作已发布的安装目标。当前可使用链接中的源码包。

| 你的应用 | 包名 / 源码 | 用途 |
|---|---|---|
| Python 只向服务发送 trace | `fuju-trace` · [Python SDK](fuju-trace-sdk/python/README.md) | 只依赖标准库的轻量打点 |
| TypeScript 只向服务发送 trace | `@fuju/trace-sdk` · [TypeScript SDK](fuju-trace-sdk/typescript/README.md) | 浏览器 / Node 打点 |
| Rust 只向服务发送 trace | [Rust SDK](fuju-trace-sdk/rust/README.md) | 只依赖标准库的打点 |
| Python 本地写入和查询 | `fuju-trace` + `fuju-trace-db` · [Python DB](fuju-trace-db-python/README.md) | 嵌入式 DB、可选 FastAPI 服务 |
| Node 或 Electron 本地写入和查询 | `@fuju/trace-db` · [Node DB](fuju-trace-node/README.md) | 通过 Node-API 嵌入 Rust 引擎 |
| Rust 本地写入和查询 | [Rust DB](fuju-trace-db-rs/README.md) | 嵌入式引擎封装 |
| 已使用 OpenTelemetry/OpenInference | `POST /v1/traces` · [API 文档](docs/API_REFERENCE.md) | OTLP/HTTP JSON 摄入 |

例如，从仓库根目录安装 Python 源码包以使用嵌入式存储：

```bash
python3 -m pip install ./fuju-trace-sdk/python ./fuju-trace-db-python
```

`fuju-trace-db` 会构建 Rust 原生扩展。安装后，每个进程打开一次 DB 并复用：

```python
from fuju_trace import DbExporter, Tracer, connect

with connect(path="./fuju-trace-data", tenant_id=1) as db:
    tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=1)
    with tracer.trace("风控复核", tenant_id=1) as trace:
        with trace.span("查询交易") as span:
            span.log("疑似盗刷")
    tracer.close()
    print(db.search(text="盗刷", k=10))
```

FastAPI、ARQ、Celery 应在每个进程启动时初始化一次、退出时关闭一次；具体做法见 [Python 服务端接入指南](docs/design/2026-07-14_python-service-integration.md)。Node/Electron 本地包需先构建 native 模块，再在 [`fuju-trace-node/`](fuju-trace-node/README.md) 执行 `npm run pack:verify`。

**部署边界：** 嵌入式模式支持同一台机器上的多个进程共享**本地**数据目录。多台机器或跨主机容器应运行一个服务，通过 HTTP 接入；不要在网络文件系统上共享嵌入式数据目录。

## 工作原理

SDK 和 OTLP 事件通过 HTTP 或进程内 `EngineJsonApi` 进入引擎。引擎把事件写入 WAL 和数据段，读取时再把 start、log、end 事件合成完整 span。搜索、回放和控制台使用同一份 trace 数据。事件 ID 按以下方式确定：

```text
event_id = hash(ext_span_id, seq, event_type)
```

因此重试和 WAL 重放能识别同一个事件。检索侧默认使用中文词级分词与 BM25，以及落盘的图式向量索引；混合检索用 RRF 合并两路结果。需要向量检索时，由调用方提供 embedding；引擎本身不调用 embedding 模型。

Rust 引擎主体只使用标准库；各语言原生绑定和可选存储适配放在工作区外。已测试能力与尚未完成的生产能力见[当前状态](docs/CURRENT_STATE.md)。

## 开发与验证

从仓库根目录执行：

```bash
cargo test --offline --manifest-path fuju-trace-engine/Cargo.toml
PYTHONPATH=fuju-trace-sdk/python python3 fuju-trace-sdk/python/tests/test_sdk.py
python3 scripts/check_release_versions.py
```

各包构建、干净环境验证、升级测试和发布步骤见 [AGENTS.md](AGENTS.md)。包元数据中配置的主页地址不表示已经创建 Git 远端或发布了包。

## 项目目录

- [`fuju-trace-engine/`](fuju-trace-engine/) — Rust 引擎和 HTTP 示例
- [`fuju-trace-sdk/`](fuju-trace-sdk/) — Python、TypeScript、Rust SDK
- [`fuju-trace-console/`](fuju-trace-console/) — 回放页面
- [`fuju-trace-db-python/`](fuju-trace-db-python/)、[`fuju-trace-node/`](fuju-trace-node/)、[`fuju-trace-db-rs/`](fuju-trace-db-rs/) — 嵌入式 DB 包
- [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md) — HTTP 和嵌入式 JSON 契约
- [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md) — 当前实现与边界
- [对外介绍口径](docs/analysis/2026-09-24_fuju-trace-positioning.md) — 可复用的话术与表述边界

`fuju-rsi` 是独立项目。它的可选遥测插件在 Fuju Trace 可用时上报事件；不可用时写本地遥测日志。Fuju Trace 的运行不依赖 RSI。

## 许可证

[MIT](LICENSE)
