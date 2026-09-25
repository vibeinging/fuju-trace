# Fuju Trace 进程内接入方案

2026-09-25 决定：只保留 SDK 直连存储。Python SDK 通过 Exporter 写 VexDB 或本地 embedded DB；TypeScript/Rust SDK 提供事件和 Exporter 接缝；Node/Python/Rust 嵌入式绑定直接调用 Rust 引擎。HTTP/OTLP 端点、独立 server、FastAPI/CLI 入口和回放控制台下线。

`EngineJsonApi` 仍是进程内绑定的调用边界。它使用方法和路径字符串分派读写，但没有监听端口。删除这层会同时破坏 Node/Python embedded 包，所以保留其内部用途。

验证重点：SDK 不再导出网络客户端；所有包仍能通过原生绑定写入、点查和检索；本地 WAL 在 kill -9 后恢复；VexDB 适配器的文本检索不要求 embedding。当前源码版本 0.1.9；Python SDK 与 VexDB 适配器已在包级测试、干净环境安装和标签流水线通过后上传 PyPI。
