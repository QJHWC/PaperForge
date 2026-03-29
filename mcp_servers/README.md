# MCP Servers

本目录现在不仅是占位，还提供了一套 repo 内统一 MCP bridge 约定：

- `base.py`: 通用 server / tool / resource 抽象
- `runner.py`: 统一启动与调用入口
- `literature/`
- `bibliography/`
- `file-gateway/`
- `remote-runner/`
- `diagram/`
- `aigc-eval/`

## 当前状态

- `literature`: live bridge，接现有文献检索逻辑
- `bibliography`: live bridge，接最小文献管理器
- `file-gateway`: skeleton bridge，锁感知 workspace 文件网关
- `remote-runner`: skeleton bridge，接 cloud cycle / sync 入口
- `diagram`: skeleton bridge，轻量结构图与图注生成
- `aigc-eval`: skeleton bridge，轻量文本风险检查

## 设计原则

- MCP 只负责外部能力连接
- 不直接接管主状态机
- 写入型操作必须经过 workspace lock
- 先做等价 bridge，再逐步增强
