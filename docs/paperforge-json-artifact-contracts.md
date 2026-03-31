# PaperForge JSON / Artifact 契约说明

## 1. 文档目的

本文档用于把当前 PaperForge 中已经被实际流程依赖、但尚未正式 schema 化的 JSON 文件、文本工件和目录约定显式冻结下来。

本文件服务于以下目标：

- 为后续 Agent / Skill / MCP / Frontend 提供统一数据契约
- 避免不同模块各自理解一套 `workflow_state.json` / `upload_manifest.json` / `notes.txt`
- 作为 single-writer lock、等价封装、前端接入前的实施基线
- 配合 `docs/paperforge-equivalence-matrix.md` 作为验收依据

---

## 2. 总体原则

### 2.1 契约优先
在新增 schema、服务层、前端或 agent 编排前，先冻结已有文件契约。

### 2.2 兼容优先
未来即使新增正式 schema，也必须做到：

- 老字段仍可读
- 老路径仍可映射
- 老工件仍可识别
- 老流程仍可恢复

### 2.3 单写者优先
所有这些契约文件默认都运行在当前系统的隐式假设下：

> **同一 workspace 默认只有一个写者。**

后续前端、OpsAgent、MCP 接入时，必须由 workspace lock / lease 统一保护。

---

## 3. 契约范围总览

当前冻结的核心契约包括：

### scientist 主线相关
- `ideas.json`
- `<idea_folder>/notes.txt`
- `<idea_folder>/review.txt`
- `<idea_folder>/review_improved.txt`
- `<idea_folder>/<idea_name>.pdf`
- `<idea_folder>/<idea_name>_improved.pdf`
- `<idea_folder>/log.txt`（条件工件，仅在 `log_file` 模式下创建）

### mvp / writeup 主线相关
- `workflow_idea.json`
- `workflow_state.json`
- `writeup_checkpoint.json`
- `artifacts/upload_manifest.json`
- `notes.txt`
- `latex/checkpoints/*.tex`
- `paper_mvp_draft.pdf`
- `paper_with_feedback.pdf`
- `paper_after_optimize.pdf`
- `paper_refined.pdf`

---

## 4. `workflow_idea.json`

### 4.1 文件位置
- `<workspace>/workflow_idea.json`

### 4.2 生产者
- `engine/mvp_workflow.write_idea_metadata()`

### 4.3 消费者
- `engine/mvp_workflow.load_idea_metadata()`
- writeup 阶段
- 后续 Orchestrator / Frontend / MCP 展示层

### 4.4 当前最小字段契约

```json
{
  "Name": "string",
  "Title": "string",
  "Experiment": "string"
}
```

### 4.5 字段说明

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `Name` | string | 是 | 规范化后的 idea/workspace 名称 |
| `Title` | string | 是 | 项目或论文标题 |
| `Experiment` | string | 是 | 当前实验或项目描述 |

### 4.6 兼容规则
- 缺失 `Name` 时，允许回退为目录名 slug
- 缺失 `Title` 时，允许回退为 `Name`
- 缺失 `Experiment` 时，允许回退为空字符串

### 4.7 冻结结论
未来升级中不得：
- 改字段名
- 改默认回退语义
- 把该文件拆成多个来源不清的配置片段

---

## 5. `workflow_state.json`

### 5.1 文件位置
- `<workspace>/workflow_state.json`

### 5.2 生产者
- `engine/mvp_workflow.save_workflow_state()`
- `launch_mvp_workflow.py` 各 phase

### 5.3 消费者
- `engine/mvp_workflow.load_workflow_state()`
- phase 编排层
- 未来前端状态展示
- 未来 Agent 恢复逻辑

### 5.4 当前结构特征
该文件不是严格固定 schema，而是：

- 一个动态 key-value JSON
- 默认总会补入 `updated_at`
- phase 会向其中追加运行态字段

### 5.5 当前已稳定字段集合

| 字段 | 类型 | 必填 | 来源 |
| --- | --- | --- | --- |
| `updated_at` | string | 是 | `save_workflow_state()` 自动写入 |
| `phase` | string | 否 | 各 phase 更新 |
| `mvp_completed` | boolean | 否 | bootstrap |
| `upload_interface_ready` | boolean | 否 | bootstrap |
| `ingested_uploads` | boolean | 否 | feedback |
| `upload_manifest` | string | 否 | feedback |

### 5.6 阶段级语义

#### bootstrap 后常见状态
```json
{
  "updated_at": "...",
  "phase": "bootstrap_completed",
  "mvp_completed": true,
  "upload_interface_ready": true
}
```

#### feedback 后常见状态
```json
{
  "updated_at": "...",
  "phase": "feedback_completed",
  "ingested_uploads": true,
  "upload_manifest": "..."
}
```

### 5.7 冻结规则
在 P0 / P1 / P2 阶段：

- 不允许改 `updated_at` 自动写入语义
- 不允许把 `phase` 拆成不可兼容的新结构
- 不允许前端直接写该文件
- 不允许 MCP 直接覆盖写该文件
- 所有未来扩展字段都应：
  - 向后兼容
  - 有明确 owner
  - 经过 workspace lock

---

## 6. `writeup_checkpoint.json`

### 6.1 文件位置
- `<workspace>/writeup_checkpoint.json`

### 6.2 生产者
- `engine/perform_writeup.py`

### 6.3 消费者
- writeup resume 逻辑
- 未来 Agent 恢复逻辑
- 未来前端 trace 展示

### 6.4 当前最小字段契约

```json
{
  "stage": "start",
  "current_round": 0,
  "latest_tex_file": null,
  "updated_at": null
}
```

### 6.5 字段说明

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `stage` | string | 是 | 当前 writeup 所处阶段 |
| `current_round` | integer | 是 | 当前阶段轮次 |
| `latest_tex_file` | string/null | 是 | 最新 tex 快照相对路径 |
| `updated_at` | string/null | 是 | 最近更新时间 |

### 6.6 `stage` 合法值
- `start`
- `init`
- `cite`
- `refine`
- `latex_fix`
- `done`

### 6.7 冻结规则
未来升级时不得：

- 修改 stage 值集合
- 更改 `current_round` 的语义
- 将 `latest_tex_file` 改成不可恢复的非路径引用
- 让前端直接改写 checkpoint

---

## 7. `upload_manifest.json`

### 7.1 文件位置
- `<workspace>/artifacts/upload_manifest.json`

### 7.2 生产者
- `engine/mvp_workflow.ingest_user_uploads()`

### 7.3 消费者
- feedback 阶段 notes 回填
- 未来前端 artifact 展示
- 未来文件管理 / 文献管理 MCP

### 7.4 当前最小字段契约

```json
{
  "timestamp": "string",
  "code_files": [],
  "figure_files": [],
  "paper_figure_files": [],
  "user_notes": "string"
}
```

### 7.5 字段说明

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `timestamp` | string | 是 | ingest 时间 |
| `code_files` | string[] | 是 | 上传代码文件相对路径 |
| `figure_files` | string[] | 是 | 上传图文件相对路径 |
| `paper_figure_files` | string[] | 是 | 已复制到 workspace 根目录供 LaTeX 引用的图名 |
| `user_notes` | string | 是 | 用户上传的 notes.md 内容 |

### 7.6 冻结规则
未来升级时：

- 不得删除现有 5 个字段
- 不得改变 `paper_figure_files` 的含义
- 可新增字段，但必须向后兼容
- 不允许多个模块同时竞争写该文件

---

## 8. `notes.txt`

### 8.1 文件位置
- `<workspace>/notes.txt`
- `<idea_folder>/notes.txt`（scientist）

### 8.2 角色
虽然是自由文本，但在当前系统中，实际上承担了：

- 运行摘要载体
- upload 反馈载体
- 文献快照载体
- writeup 上下文输入载体

### 8.3 当前稳定结构

#### 头部结构
- `# Title: ...`
- `# Experiment description: ...`

#### baseline / workflow policy 区
- baseline results
- staged generation / authoring policy 描述

#### 自动块结构
通过 HTML 注释边界维护：

- `AUTO:RUN_FEEDBACK`
- `AUTO:LITERATURE`
- `AUTO:UPLOAD_FEEDBACK`

### 8.4 自动块边界格式

```text
<!-- AUTO:BLOCK_KEY:START -->
...
<!-- AUTO:BLOCK_KEY:END -->
```

### 8.5 冻结规则
未来升级时必须保留：

- notes 仍然可作为 writeup 上下文输入
- 自动块边界格式不变
- block key 不随意重命名
- 前端展示可解析这些 block
- 任何写入必须经过锁保护

---

## 9. PDF / TeX / 目录工件契约

## 9.1 research_partner 工件

| 工件 | 位置 | 含义 |
| --- | --- | --- |
| `idea_brief.json` | `<workspace>/artifacts/research_partner/` | Proposal 的问题定义、novelty 与证据基础 |
| `claim_graph.json` | `<workspace>/artifacts/research_partner/` | Proposal 的轻量 claim graph |
| `critique_report.json` | `<workspace>/artifacts/research_partner/` | critique 摘要、优势、风险与开放问题 |
| `proposal_brief.md` | `<workspace>/artifacts/research_partner/` | 面向阅读的 Proposal Markdown 摘要 |
| `experiment_blueprint.json` | `<workspace>/artifacts/research_partner/` | 实验轨道、验证问题与成功信号 |
| `expected_figures.json` | `<workspace>/artifacts/research_partner/` | 预期图表清单与对应实验轨道 |
| `rubric_scorecard.json` | `<workspace>/artifacts/research_partner/` | rubric 总分、维度评分、blocking issues 与 recommendation |
| `evidence_index.json` | `<workspace>/artifacts/research_partner/` | 证据源索引，路径应保持 workspace 相对化 |
| `manifest.json` | `<workspace>/artifacts/research_partner/` | 本次 proposal bundle 的总清单、生成文件与流程元数据 |

### 冻结规则
未来升级时：

- `research_partner` 工件路径统一保持在 `<workspace>/artifacts/research_partner/`
- 对外暴露路径必须保持 workspace 相对化，不能泄露绝对路径
- `manifest.json` 必须能回链 `generated_files` 与输入 `evidence_files`
- 新增工件时应在不破坏现有键名的前提下扩展

## 9.2 scientist 工件

| 工件 | 位置 | 含义 |
| --- | --- | --- |
| `ideas.json` | 模板目录 | 生成 ideas 的落盘结果 |
| `<idea_name>.pdf` | idea 目录 | 主写作 PDF |
| `review.txt` | idea 目录 | review 结果 |
| `<idea_name>_improved.pdf` | idea 目录 | improvement 后 PDF |
| `review_improved.txt` | idea 目录 | improvement 后 review |
| `log.txt` | idea 目录 | 条件工件，仅在 `log_file` 模式下生成的日志 |

## 9.2 mvp 工件

| 工件 | 位置 | 含义 |
| --- | --- | --- |
| `paper_mvp_draft.pdf` | workspace | bootstrap 输出 |
| `paper_with_feedback.pdf` | workspace | feedback 输出 |
| `paper_after_optimize.pdf` | workspace | optimize 输出 |
| `paper_refined.pdf` | workspace | refine 输出 |
| `latex/checkpoints/*.tex` | workspace | writeup checkpoint snapshots |

### 冻结规则
未来升级时：

- 阶段 PDF 的存在语义必须保留
- checkpoint tex 快照能力必须保留
- 若路径改变，必须提供兼容映射层

---

## 10. owner 与写权限边界

在单写者 lock 正式接入前，先冻结逻辑 owner：

| 工件 | 当前 owner |
| --- | --- |
| `workflow_idea.json` | MVP workflow |
| `workflow_state.json` | MVP workflow orchestrator |
| `writeup_checkpoint.json` | Writeup engine |
| `upload_manifest.json` | Upload ingest flow |
| `research_partner/*` | Research partner proposal bundle |
| `notes.txt` | workflow / feedback / writeup |
| `template.tex` | writeup engine |
| 阶段 PDF | writeup / compile flow |

### 规则
未来任何新模块：

- 不得绕过 owner 直接写
- 不得绕过 lock 直接写
- 必须通过 Agent / Service / LockManager 间接操作

---

## 11. 与 single-writer lock 的关系

本文件只冻结“写什么”和“谁拥有写权”，不定义具体锁实现。

锁实现将在后续文档中定义：

- `docs/paperforge-single-writer-locking.md`

但从当前开始，所有实施都应默认遵守：

> 同一 workspace 任一时刻只能有一个写者持锁写入这些契约工件。

---

## 12. 与验收矩阵的关系

本文件中的契约将被以下矩阵消费：

- `docs/paperforge-equivalence-matrix.md`

重点对应验收项：

- JSON / artifact 契约
- resume / recovery
- 单写者与并发
- scientist / mvp / writeup 工件集合

---

## 13. 实施前结论

在真正开始：

- Agent 封装
- Frontend 接入
- MCP 写入能力
- 文献管理器落盘
- AIGC / 降重闭环回写

之前，必须先把本文档视为当前 PaperForge 的 **最小数据契约基线**。

一句话总结：

> **现在这些 JSON / artifact / notes 规则，不再是“代码里碰巧这么写”，而是后续升级必须兼容的正式契约。**
