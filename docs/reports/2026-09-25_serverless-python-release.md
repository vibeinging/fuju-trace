# Fuju Trace 0.1.9 进程内版本发版记录

日期：2026-09-25。源码提交 `3071232`，标签 `v0.1.9-only-python-sdk-python-vexdb`。标签流水线 [Tag Package](https://github.com/vibeinging/fuju-trace/actions/runs/36126416861) 完成且全绿，Python SDK 和 VexDB 适配器构建成功。

## 已发布

- [fuju-trace 0.1.9](https://pypi.org/project/fuju-trace/0.1.9/)：基础 Python SDK，提供 VexDB extra；不包含 HTTP Exporter、远程客户端，也不声明尚未公开发布的本地 DB extra。
- [fuju-trace-vexdb 0.1.9](https://pypi.org/project/fuju-trace-vexdb/0.1.9/)：VexDB 存储和检索适配器。`driver` extra 安装通用 `psycopg2-binary>=2.9.5,<3`。

用户安装命令：

```bash
pip install 'fuju-trace[vexdb]==0.1.9'
```

从公开 PyPI 索引在全新虚拟环境安装成功，导入 `fuju_trace`、`fuju_trace_vexdb`、`psycopg2` 成功，`pip check` 无冲突。发布包检查确认不存在 `HttpExporter` 和 `FujuTraceClient`。

## 验证

- Rust 引擎 `cargo test --offline` 全量通过：核心 158 项通过、1 项忽略，其余 crate 和集成测试通过。
- Python SDK 33 项、VexDB 适配器 13 项、TypeScript SDK 11 项、Rust SDK 5 项通过。
- Node 嵌入式包 `npm run build && npm test`、Python 嵌入式包 pytest、Rust 嵌入式 crate 测试通过。
- 本地 DB wheel 在干净环境完成写入、重开、检索；3 轮 kill -9 崩溃恢复通过。
- 用旧版 v0.1.5 数据目录做升级回归，当前版本通过进程内探针完成查询、去重和边车索引检查。
- wheel 与 sdist 均通过 Twine 检查，标签流水线正式产物构建成功。

## 范围

本次 PyPI 只发布 Python SDK 与 VexDB 适配器。本地 native `fuju-trace-db` wheel 已构建并验证，但没有随本次标签上传 PyPI。当前环境未设置 `VEXDB_DSN`，本次没有重新运行在线 VexDB smoke；适配器实现未在本次去服务化改动中修改，其离线单元测试已通过。生产接入仍需用目标 VexDB 实例验证实际数据的查询计划、召回和写入延迟。
