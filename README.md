# PaperForge

**面向研究论文生成、实验驱动写作与迭代修订的论文生成器。**

PaperForge 目前保留两条真实工作流主线：

- `scientist`: 全自动 idea -> experiment -> writeup -> review -> optional improvement
- `mvp`: 分阶段 bootstrap -> cloud -> feedback -> optimize -> refine

这套仓库已经补到一个“可升级但不失真”的基线状态：

- 现有 CLI / engine 主流程仍然是权威执行面
- `agents/` 提供最小可运行 bridge，统一输出 `status / trace / artifacts / schema`
- `skills/` 为写作相关 skill 补了最小 `schema.json + 示例 I/O + runtime`
- `mcp_servers/` 现在每个展示项都有真实目录与 `server.py`
- `frontend/` 已改为真正由 `app.js` 渲染，而不是静态 HTML 假页面
- `engine/bibliography.py` 落了最小文献管理器存储模型
- `tests/` 落了 smoke / lock / bibliography / agent bridge 回归检查

## 当前状态

### 可运行主线

- `launch_scientist.py`
- `launch_mvp_workflow.py`
- `launch_user_entry.py`
- `engine/perform_writeup.py`

这些入口仍直接驱动真实 scientist / mvp / writeup 逻辑。

### 已接上的 bridge

- `agents/coordinator.py`
- `agents/scientist_workflow_agent.py`
- `agents/mvp_workflow_agent.py`
- `agents/writeup_agent.py`

这些 agent 不重写业务逻辑，而是桥接现有入口，并补统一输入 schema、trace 和 artifact 输出。

### 已有最小 contract 的 skills

- `skills/write-section/`
- `skills/refine-section/`
- `skills/citation-gap/`
- `skills/citation-select/`
- `skills/latex-fix/`
- `skills/de-aigc-rewrite/`

其中前 4 个是原本已存在的真实 skill 文档；后 2 个是为前端 / README / mapping 对齐补上的最小骨架。

### 已有 MCP 目录

- `mcp_servers/literature/`
- `mcp_servers/bibliography/`
- `mcp_servers/file-gateway/`
- `mcp_servers/remote-runner/`
- `mcp_servers/diagram/`
- `mcp_servers/aigc-eval/`

其中：

- `literature` 和 `bibliography` 是实装 bridge
- 其余 4 个是统一启动面 + tool/resource 定义 + 最小骨架

### 仍未完成的部分

- 前端没有接真实 API，只是静态渲染当前仓库状态
- skill 还没有真正替换 `perform_writeup.py` 内部 LLM 循环
- MCP 还没有升级成完整外部服务，只是 repo 内统一协议与入口
- bibliography 是本地文件存储，不是完整多用户文献平台

## 结构图

```text
PaperForge/
├── agents/                         # Agent bridge 层
│   ├── coordinator.py
│   ├── mvp_workflow_agent.py
│   ├── scientist_workflow_agent.py
│   ├── writeup_agent.py
│   ├── runtime.py
│   └── schemas/
├── docs/                           # 等价规格、锁设计、路线图、契约文档
├── engine/                         # 真实业务逻辑
│   ├── bibliography.py             # 新增：最小文献存储模型
│   ├── mvp_workflow.py
│   ├── perform_writeup.py
│   ├── remote_runner.py
│   └── run_lock.py
├── frontend/                       # 由 app.js 渲染的静态控制台
│   ├── app.js
│   └── index.html
├── mcp_servers/                    # MCP 统一协议与运行入口
│   ├── base.py
│   ├── runner.py
│   ├── literature/
│   ├── bibliography/
│   ├── file-gateway/
│   ├── remote-runner/
│   ├── diagram/
│   └── aigc-eval/
├── skills/                         # Skill contract 层
│   ├── runtime.py
│   ├── writeup_bridge.py
│   ├── write-section/
│   ├── refine-section/
│   ├── citation-gap/
│   ├── citation-select/
│   ├── latex-fix/
│   └── de-aigc-rewrite/
├── templates/
├── tests/                          # 新增：最小自动化检查
├── launch_scientist.py
├── launch_mvp_workflow.py
├── launch_user_entry.py
├── run_cloud_pipeline_cycle.py
├── sync_cloud_results_to_uploads.py
└── README.md
```

## 工作流概览

### scientist

```text
idea_generation
→ novelty_check
→ experiment
→ writeup
→ review
→ optional improvement
→ re_review
```

### mvp

```text
bootstrap
→ optional cloud
→ feedback
→ optimize
→ refine
```

### writeup

```text
start
→ init
→ cite
→ refine
→ latex_fix
→ done
```

## Agent / Skill / MCP 当前语义

### Agent

`agents/` 当前不是新的主流程实现，而是对既有入口的薄封装：

- 输入有 schema
- 输出统一有 `status`
- 附带 `trace`
- 附带 `artifacts`
- scientist / mvp / writeup 继续走现有脚本或 engine 逻辑

### Skill

`skills/` 当前提供的是最小执行约定，不是完整 LLM orchestration：

- `SKILL.md`
- `schema.json`
- `example_input.json`
- `example_output.json`
- `skills/runtime.py`
- `skills/writeup_bridge.py`

writeup agent 现在能暴露 phase -> skill map，但还没有把 `perform_writeup.py` 的内部循环完全替换成 skill orchestration。

### MCP

`mcp_servers/` 当前提供：

- 统一 `base.py`
- 统一 `runner.py`
- 每个服务独立目录
- 每个服务具备 tool/resource 定义
- 写入型服务通过 workspace lock 保护

不是完整网络化 MCP 部署；现在是 repo 内统一协议面和桥接面。

## 文献管理器最小落地

`engine/bibliography.py` 当前已支持：

- `library.json` 存储模型
- BibTeX 导入
- BibTeX 导出
- DOI / 标题+年份去重
- section -> citation key 映射
- 给 writeup / citation bridge 复用

默认路径：

```text
<workspace>/artifacts/bibliography/library.json
```

对应 MCP：

- `mcp_servers/bibliography/server.py`

## 锁策略

当前显式锁入口在：

- `engine/run_lock.py`
- `launch_scientist.py`
- `launch_mvp_workflow.py`
- `run_cloud_pipeline_cycle.py`
- `sync_cloud_results_to_uploads.py`
- `engine/perform_writeup.py` 的 CLI 入口
- `mcp_servers` 中所有写入型服务

补充说明：

- `run_lock.py` 现在支持同进程重入
- scientist 仍然只允许跨 idea 目录并行
- mvp / writeup / cloud sync / bibliography 写入仍遵循单 workspace 单写者

设计背景见：

- `docs/paperforge-single-writer-locking.md`

## 自动化检查

当前最小检查集位于 `tests/`：

- `tests/test_smoke.py`
- `tests/test_locking.py`
- `tests/test_bibliography.py`
- `tests/test_agent_bridges.py`

覆盖范围：

- 前端是否真正接线
- skill / MCP 骨架是否真实存在
- bibliography import/export/dedupe/section map
- agent bridge 输出 contract
- 并发锁行为

运行方式：

```bash
python3 -m unittest discover -s tests
```

## 快速开始

### 1. 环境准备

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置密钥

```bash
cp key.example.sh key.sh
source ./key.sh
```

### 3. 预检查

```bash
python -m engine.preflight --workspace ./workspace
```

### 4. 运行 scientist

```bash
python launch_scientist.py \
  --experiment paper_writer \
  --num-ideas 1 \
  --skip-novelty-check
```

### 5. 运行 mvp

```bash
python launch_mvp_workflow.py \
  --phase bootstrap \
  --experiment paper_writer \
  --engine openalex
```

### 6. 查看前端控制台

直接打开：

```text
frontend/index.html
```

它不会直接执行任务，只展示当前 repo 的桥接层、骨架文件和文档状态。

## 文档索引

- `docs/paperforge-workflow-equivalence-spec.md`
- `docs/paperforge-agent-skill-mcp-mapping.md`
- `docs/paperforge-safe-upgrade-roadmap.md`
- `docs/paperforge-equivalence-matrix.md`
- `docs/paperforge-json-artifact-contracts.md`
- `docs/paperforge-single-writer-locking.md`

## 使用限制

当前项目仍保留以下限制：

- 禁止商用
- 禁止转发利用
- 仅限本人使用
- 禁止用于 surveillance、deceptive media、unauthorized healthcare / criminal prediction 等场景

具体合规约束以：

- `LICENSE`
- `LICENSE-UPSTREAM`

为准。
