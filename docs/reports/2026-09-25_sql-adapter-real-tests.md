# SQL 适配器真实数据库测试

日期：2026-09-25。范围：尚未发布的 `fuju-trace-sql`，仅含 SQLite、DuckDB、PostgreSQL。MySQL 暂不支持。

## 本机结果

| 后端 | 测试环境 | 已验证 |
| --- | --- | --- |
| SQLite | Python 3.12.2，自带 SQLite，临时数据库文件 | SDK 写入、乱序事件、幂等重试、事务回滚、过滤、租户隔离、并发线程、重开 |
| DuckDB | Python 3.12.2、DuckDB 1.5.0，临时数据库文件 | 建表、乱序写入、幂等重试、冲突回滚、文本查询、租户隔离、重开 |
| PostgreSQL | PostgreSQL 16.14、psycopg2 2.9.10，临时本机实例和私有 Unix socket | 建表、SDK 写入、乱序事件、幂等重试、冲突回滚、租户隔离、双连接并发写同一 Span、文本查询、重开 |

运行命令：

```bash
/opt/anaconda3/bin/python3.12 -m unittest discover -s fuju-trace-sql/tests -p 'test_*.py' -v
FUJU_SQL_PYTHON=/opt/anaconda3/bin/python3.12 ./fuju-trace-sql/tests/run_local_postgresql.sh
python3 scripts/check_release_versions.py
```

结果：SQL 单元与真实文件测试 7 项通过、无跳过；PostgreSQL 真实库脚本通过；版本检查 31 项通过。另用 `python3 fuju-trace-sdk/python/tests/test_sdk.py` 验证 Python SDK 33 项通过。临时 PostgreSQL 实例已停止，测试目录已删除。

## 复现方式

本机已安装 PostgreSQL 命令和 `psycopg2` 时，从仓库根目录运行 `FUJU_SQL_PYTHON=python3 ./fuju-trace-sql/tests/run_local_postgresql.sh`。脚本创建独立数据库目录，禁用 TCP 监听，测试完成后关闭并清理实例。也可以给一次性测试库设置 `POSTGRESQL_DSN`，运行 `python fuju-trace-sql/tests/live_smoke.py`；测试仅创建和删除随机 `fuju_smoke_*` 前缀的四张表。

CI 工作流已配置 PostgreSQL 16 服务容器，在 SQL 适配器测试后对该服务运行 `live_smoke.py`，并构建 wheel 进行独立安装验证。当前工作树尚未推送，因此这里不声称远端 CI 已通过。

## 仍需验证

- 当前 SQL 文本检索是 `LIKE` 子串扫描，没有 BM25、向量索引、混合检索或性能结论。VexDB-Lite 扩展尚未接入本插件。
- PostgreSQL 本机测试只覆盖 16.14 和小规模并发；目标环境的版本、权限、实际负载及故障恢复需要独立验收。
- DuckDB 同文件多进程写入不在支持范围内；当前测试验证的是单连接文件库。
- SQL 插件和对应 SDK extras 尚未发布到 PyPI；本报告验证源码运行路径，不代表包安装验收。
