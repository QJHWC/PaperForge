# PaperForge 2.0 改进方案

> 日期：2026-03-30
> 状态：已确认的收敛版规划（已同步 2026-03-30 Alpha 实装状态）
> 定位：基于当前已落地的 `research_partner` Proposal-first Alpha 执行链，继续补齐 contracts、evidence 与 critique 的后续增量

---

## 1. 核心目标

PaperForge 2.0 不再以“全自动端到端论文生成”为默认目标，而是转向：

**证据摄入 → Idea 挖掘 → Claim 建模 → Critique/Committee 校验 → 可选实验验证 → 可选写作装配**

系统默认输出不再是全文论文草稿，而是高密度、可审校、可追溯的 **Proposal 套件**。

---

## 2. 已确认的五项关键决策

### 2.1 默认最终产物：Proposal 套件

**决策**：将 2.0 默认最终产物锁定为 Proposal 套件，将 Full Draft 降级为后期可选扩展。

**Proposal 套件建议包含**：
- Motivation / Problem Framing
- 核心方法论假设
- Claim Graph
- 实验设计蓝图
- 预期图表占位
- Critique 结果与风险列表

**原因**：
- 更契合“聚焦 Idea 闭环”的真实需求
- 避免大模型直接产出冗长、空洞的 10 页初稿
- 先立住结构骨架，再决定是否扩写成全文

### 2.2 第一批支持格式：只做 PDF + CSV

**决策**：1.x / 2.0 初期只支持：
- PDF：文献输入
- CSV：实验指标/消融结果输入

**明确不做**：
- DOCX
- PPTX
- XLSX
- 富文本工作流

**原因**：
- 核心科研高价值输入主要就是 PDF 和 CSV
- 富文本格式解析成本高、噪音大、收益低
- 应优先把 PDF/CSV I/O 做稳、做透、做不崩

### 2.3 Scitex 接入方案：独立服务仓库（Microservice）

**决策**：Scitex 必须以独立仓库、独立服务形式接入，例如 FastAPI / gRPC / HTTP 微服务。

**主仓只保留**：
- adapter / client
- 配置与超时控制
- 标准化输入输出合同

**主仓明确禁止**：
- 直接 import scitex 到核心引擎
- vendoring 其源码进入主仓
- 将其依赖并入主项目核心运行链

**原因**：
- 规避 AGPL-3.0 传染性风险
- 保持架构解耦
- 让 Scitex 成为可拔插增强能力，而不是硬依赖

### 2.4 Critique Loop 阈值：采用可配置 Rubric

**决策**：放弃硬编码固定规则，采用 YAML/JSON Rubric 评分量表。

**建议支持**：
- `default`
- `cvpr`
- `journal_q2`
- 后续可扩展 venue/profile

**Rubric 建议维度**：
- novelty
- methodology
- feasibility
- evidence_support
- experimental_design
- clarity

**原因**：
- 不同目标 venue 的评价标准不同
- 顶会与普通期刊的权重分配不一样
- 可配置 Rubric 能让系统更专业、可迁移、可调优

### 2.5 前端策略：本轮按住，只做 CLI + Artifacts

**决策**：本轮重构不做前端 UI，全部精力集中在：
- CLI
- 控制台日志
- JSON artifacts
- Markdown / LaTeX 静态输出

**原因**：
- 开发期最关键的是可调试性与可观测性
- CLI 更适合暴露多模型路由、工具调用、critique 互相推翻过程
- 等后端闭环稳定后，再做前端只是包装层问题

---

## 3. 与当前本地已跑通改动的衔接

当前仓库已经具备 2.0 Alpha 的“统一入口 + 合同约束 + Proposal 物化”基础：

- `agents/coordinator.py`
  - 已加入 `research_partner` 请求收敛
  - 已定义 `planned_workflow`
  - 已定义 `planned_output_contracts`
  - 已定义 `planned_outputs`
  - 已在 `execute=True` 且 workspace 可用时接入真实 Proposal 工件落盘
- `agents/schemas/research_partner.schema.json`
  - 已有独立 schema
- `launch_user_entry.py`
  - 已有 `research_partner` CLI 子命令
  - 已在轻量 bootstrap 完成后追加 Proposal 物化步骤
- `engine/research_partner/`
  - 已具备 `contracts.py`、`idea_pipeline.py`、`critique_loop.py`、`proposal_bundle.py`
- `tests/test_agent_bridges.py`
  - 已覆盖 research_partner 合同测试与真实工件生成测试

**当前判断**：
当前不再是单纯的 `bootstrap-only / planned-only` 形态，而是双层状态：
- `execute=False` 与 `status_snapshot` 仍保留 planned-only 合同，用于 bridge / frontend 暴露 planned outputs
- CLI `launch_user_entry.py research_partner` 与 `coordinator` 的 `execute=True` 执行态，已经进入 Proposal-first Alpha，可在轻量 bootstrap 后真实落盘 Proposal 套件
- `research_partner` 仍复用 mvp bootstrap 作为 workspace 初始化前置步骤，但不再停留在“只生成 skeleton”的阶段

**2026-03-30 首轮 Alpha 已完成**：
- `engine/research_partner/contracts.py`
  - 已抽离 research_partner 合同常量、输出路径与 skeleton 定义
- `engine/research_partner/idea_pipeline.py`
  - 已根据 `workflow_idea.json`、`notes.txt` 与 `EvidenceContext` 生成最小 idea package
- `engine/research_partner/critique_loop.py`
  - 已接入 rubric/profile、LLM committee critique 与 deterministic fallback
- `engine/research_partner/proposal_bundle.py`
  - 已实现最小 Proposal 物化器
  - 可生成 `idea_brief.json`、`claim_graph.json`、`critique_report.json`、`proposal_brief.md`、`experiment_blueprint.json`、`evidence_index.json`、`manifest.json`
- `launch_user_entry.py`
  - 已支持 `--rubric-profile`、repeated `--evidence-file` 与 CLI 侧物化闭环
- `tests/test_agent_bridges.py`
  - 已覆盖 `proposal_bundle` 直接物化、`coordinator execute=True` 与 CLI 参数拼装三条路径

**当前状态更新**：
`research_partner` 已从“只返回 planned skeleton”推进到“执行态可真实落盘 Alpha Proposal 套件”，当前已具备：
- bootstrap-first
- PDF/CSV 外部证据摄入
- rubric/profile CLI 接入
- LLM committee critique + deterministic fallback
- manifest / evidence index 可追溯输出

因此下一步不应该回头重写入口，而应继续围绕前端可视化、evidence parsing 细化与 committee 持久化增强做扩展。

---

## 4. 2.0 目标架构

建议目标主链路为：

`search -> parse(pdf/csv) -> evidence index -> idea framing -> claim graph -> critique -> proposal bundle`

推荐分层：

### 4.1 Evidence Intake
负责：
- PDF 解析
- CSV 解析
- 元数据归一化
- evidence chunk/index 生成

### 4.2 Idea Synthesis
负责：
- 问题定义
- novelty framing
- 方法论假设
- 初版 claim graph

### 4.3 Critical Review
负责：
- rubric 驱动评分
- 多 reviewer critique
- 风险识别
- 未决问题收敛

### 4.4 Optional Verification
负责：
- 可选实验验证
- 可选 Scitex 证据增强
- execution manifest

### 4.5 Optional Writing Assembly
负责：
- 将 Proposal 套件扩写为 markdown / latex / full draft
- 不作为默认主线

---

## 5. 默认 Proposal 套件组成

建议在保留现有 3 个核心 JSON 的基础上，形成以下最小 Proposal 套件：

### 5.1 核心结构化工件
- `idea_brief.json`
- `claim_graph.json`
- `critique_report.json`

### 5.2 Proposal 专属工件
- `proposal_brief.md`
- `experiment_blueprint.json`
- `expected_figures.json`
- `rubric_scorecard.json`
- `evidence_index.json`
- `manifest.json`

### 5.3 建议目录

```text
artifacts/research_partner/
├── idea_brief.json
├── claim_graph.json
├── critique_report.json
├── proposal_brief.md
├── experiment_blueprint.json
├── expected_figures.json
├── rubric_scorecard.json
├── evidence_index.json
└── manifest.json
```

> 注：当前 Alpha 实装已真实落盘：`idea_brief.json`、`claim_graph.json`、`critique_report.json`、`proposal_brief.md`、`experiment_blueprint.json`、`expected_figures.json`、`rubric_scorecard.json`、`evidence_index.json`、`manifest.json`。

---

## 6. 分阶段实施计划

## Phase 0：冻结 2.0 产品边界

### 目标
先把“2.0 做什么、不做什么”冻结下来。

### 核心动作
1. 正式将 `research_partner` 定义为 2.0 默认主路径
2. 冻结 Proposal-first 的对外叙事
3. 冻结 PDF + CSV 输入边界
4. 冻结 CLI-first、frontend-later 策略
5. 冻结 Scitex 微服务接入边界

### 验收标准
- 文档、CLI、测试叙事统一
- 不再将 Full Draft 作为默认 happy path
- 不再将富文本作为近期范围

---

## Phase 1：将 research_partner 从 planned-only 升级为真实 Proposal pipeline（已完成首轮 Alpha 落地）

### 目标
让 `research_partner` 真实产出 Proposal 套件，而不是只输出 skeleton。

### 建议新增模块
- `engine/research_partner/idea_pipeline.py`
- `engine/research_partner/proposal_bundle.py`
- `engine/research_partner/critique_loop.py`

### 实施原则
- 保留现有入口壳层
- 保留现有 schema
- 保留现有合同名称
- 不先重写 `launch_user_entry.py`
- 不先重写 `PaperForgeCoordinator` 整体形态

### 验收标准
真实生成：
- `idea_brief.json`
- `claim_graph.json`
- `critique_report.json`
- `proposal_brief.md`
- `experiment_blueprint.json`

### 当前进展（2026-03-30）
已完成首轮 Alpha 落地：
- 已新增 `engine/research_partner/contracts.py`
- 已新增 `engine/research_partner/idea_pipeline.py`
- 已新增 `engine/research_partner/critique_loop.py`
- 已新增 `engine/research_partner/proposal_bundle.py`
- 已在 `agents/coordinator.py` 中接入执行态真实物化
- 已新增 `manifest.json` 与 `evidence_index.json` 作为可追溯工件
- 已完成 rubric/profile CLI 接入、PDF/CSV 外部证据接入，以及 LLM committee critique 闭环
- 已通过 bridge tests 与定向 pytest 验证（当前定向回归为 `52 passed`）

当前已完成：
- `expected_figures.json`
- `rubric_scorecard.json` 独立落盘
- 更细粒度的 `blocking_issues` / recommendation 评分归档

当前仍待后续 phase 完成：
- 更细粒度的 evidence parsing / evidence refs
- 前端可视化包装

---

## Phase 2：只做 PDF + CSV 的解析底座

### 目标
建立稳定且最小的输入解析底座。

### 建议新增模块
- `engine/parsing/pdf_parser.py`
- `engine/parsing/csv_parser.py`
- `engine/parsing/contracts.py`
- `engine/evidence/index_builder.py`

### PDF 侧输出建议
- 标题
- 摘要
- section chunks
- 引文锚点
- 页码信息
- 图表标题（若可提取）

### CSV 侧输出建议
- 列名
- 数值列候选
- 指标列候选
- 分组字段
- 表格摘要
- 与实验蓝图的映射候选

### 验收标准
- PDF 可形成结构化 evidence chunks
- CSV 可形成实验指标摘要
- Proposal 中的 evidence basis 可回链至 PDF/CSV

---

## Phase 3：Critique Loop Rubric 化

### 目标
让 critique 不再依赖硬编码规则，而是由配置驱动。

### 建议配置
- `configs/rubrics/default.yaml`
- `configs/rubrics/cvpr.yaml`
- `configs/rubrics/journal_q2.yaml`

### 建议 reviewer 角色
- novelty reviewer
- feasibility reviewer
- methodology reviewer
- evidence reviewer
- meta chair

### 输出建议
- `rubric_scorecard.json`
- `committee_decision.json`
- `critique_report.json`

### 验收标准
- 可切换不同 rubric profile 重评同一 Proposal
- 阈值、轮数、停止条件可配置
- critique 输出可持久化、可复盘

---

## Phase 4：Scitex 微服务适配

### 目标
在不污染主仓的前提下提供增强能力。

### 方案
独立仓库，例如：
- `paperforge-scitex-engine`

提供：
- REST API / gRPC / MCP 任一窄接口

主仓仅保留：
- `integrations/scitex_client.py`
  或
- `mcp_servers/scitex_adapter/`

### 验收标准
- Scitex 服务关闭时主流程仍可运行
- 主仓不直接 import Scitex 核心代码
- 输入输出合同清晰

---

## Phase 5：可选实验验证与可选写作装配

### 目标
在 Proposal 闭环稳定后，再恢复更重能力。

### 原则
- execution / sandbox 作为后续增强，不作为第一阶段阻塞项
- full draft 作为可选扩展，不作为默认输出
- `perform_writeup.py` 可保留为后续装配后端

### 验收标准
- Proposal 套件已能独立完成闭环
- 写作链条仅消费已通过 critique 的结构化工件

---

## 7. 当前阶段明确不做的事项

本轮不做：
- 默认 Full Draft 生成
- DOCX / PPTX / XLSX 支持
- 前端 UI 大改
- 主仓内嵌 Scitex
- 复杂 truth-verification 承诺

---

## 8. 风险清单

### 高风险
1. 过早推翻现有 `research_partner` 合同
2. PDF 解析稳定性不足
3. CSV 语义不统一
4. Scitex 边界没守住

### 中风险
1. Rubric 过于复杂，影响早期调试
2. Critique Loop 成本过高
3. Proposal 工件过散，缺统一骨架

### 风险缓解
- 先保留现有合同名称
- 先最小输入集（PDF+CSV）
- 先 CLI 调试，不做 UI
- Scitex 最后接，且默认关闭

---

## 9. 成功标准

PaperForge 2.0 第一阶段成功，不以“能否自动写出整篇论文”衡量，而以以下指标衡量：

1. 能否稳定生成 Proposal 套件
2. Proposal 是否经过多轮 Critique 后仍保持结构自洽
3. Proposal 中的关键 claim 是否可回链到 PDF / CSV 证据
4. Rubric 评分是否可配置、可复盘
5. CLI 输出与 artifacts 是否足以支持调试与人工审校

---

## 10. 一句话结论

**PaperForge 2.0 的第一目标不是“更强的自动写稿”，而是“一个以 Proposal 套件为核心产物、以 PDF+CSV 为最小输入、以 Rubric 驱动 Critique、以 CLI+Artifacts 为主工作台、并为 Scitex 预留微服务边界的证据优先研究伙伴”。**

---

## 11. 微实施任务表

本节将前述 Phase 规划进一步拆成可直接执行的微任务，默认遵循以下原则：
- 默认主线：`research_partner`
- 默认产物：Proposal 套件，不是 Full Draft
- 首批输入：PDF + CSV
- 本轮不做：前端、DOCX/PPTX/XLSX、主仓内嵌 Scitex
- 实施策略：先保留当前已跑通的壳层，再把 `planned_only` 升级为真实产物链

### 11.1 Phase 0：冻结边界与合同

#### P0-1 文档基线收口
- **目标**：把 2.0 的产品边界写死
- **涉及文件**：
  - `docs/paperforge-2.0-improvement-plan.md`
  - `docs/paperforge-safe-upgrade-roadmap.md`
- **动作**：
  1. 明确默认最终产物是 Proposal 套件
  2. 明确首批只支持 PDF + CSV
  3. 明确前端暂停
  4. 明确 Scitex 只走微服务
  5. 明确 Critique 使用 Rubric
- **产物**：
  - 更新后的 2.0 总方案文档
- **验收**：
  - 文档中不再出现“默认全文草稿”
  - 文档中不再把 DOCX/PPTX/XLSX 放入近期范围

#### P0-2 冻结 Proposal 套件合同
- **目标**：先确定输出长什么样
- **建议工件**：
  - `idea_brief.json`
  - `claim_graph.json`
  - `critique_report.json`
  - `proposal_brief.md`
  - `experiment_blueprint.json`
  - `expected_figures.json`
  - `rubric_scorecard.json`
  - `evidence_index.json`
- **动作**：
  1. 先定义字段，不急着实现全部内容
  2. 对齐当前 `research_partner` 已有 skeleton
- **涉及文件**：
  - `agents/coordinator.py`
  - `agents/schemas/research_partner.schema.json`
  - `docs/paperforge-json-artifact-contracts.md`
- **验收**：
  - 每个工件有最小字段清单
  - 路径统一到 `artifacts/research_partner/`

#### P0-3 确认 CLI 主路径
- **目标**：避免后面入口漂移
- **涉及文件**：
  - `launch_user_entry.py`
  - `agents/coordinator.py`
- **动作**：
  1. 明确 `research_partner` 是 2.0 主入口
  2. `scientist` / `mvp` 定位为兼容路径
- **验收**：
  - 文档和测试叙事一致
  - 不新增第四套入口名

### 11.2 Phase 1：把 research_partner 从 planned_only 变成真实 Proposal pipeline

#### P1-1 抽离 research_partner 执行层
- **目标**：不要继续把逻辑堆在 `coordinator.py`
- **建议新增文件**：
  - `engine/research_partner/idea_pipeline.py`
  - `engine/research_partner/proposal_bundle.py`
  - `engine/research_partner/critique_loop.py`
  - `engine/research_partner/contracts.py`
- **动作**：
  1. `coordinator` 只负责收敛参数和路由
  2. 新 pipeline 负责真实产物生成
- **涉及文件**：
  - `agents/coordinator.py`
- **验收**：
  - `coordinator.py` 不继续膨胀成超大业务文件
  - research 逻辑集中到新目录
- **当前状态**：Alpha 已完成
  - 已完成：`contracts.py`、`idea_pipeline.py`、`critique_loop.py`、`proposal_bundle.py`、`coordinator` 执行态物化、`expected_figures.json`、`rubric_scorecard.json` 独立落盘
  - 当前剩余重点：前端展示接入、evidence parsing 细化、committee 相关持久化进一步拆分

#### P1-2 落地真实 `idea_brief.json`
- **目标**：先从最小 Proposal 核心件开始
- **字段建议**：
  - `title`
  - `problem`
  - `motivation`
  - `novelty_claim`
  - `evidence_basis`
  - `method_hypothesis`
- **动作**：
  1. 基于 literature bootstrap 生成真实内容
  2. 保持兼容已有 skeleton
- **涉及文件**：
  - `agents/coordinator.py`
  - `engine/research_partner/idea_pipeline.py`
- **验收**：
  - 不再是空字符串 skeleton
  - 至少填充 `title/problem/novelty/evidence_basis`
- **当前状态**：首轮 Alpha 已完成
  - 当前核心内容由 `engine/research_partner/idea_pipeline.py` 基于 `workflow_idea.json`、`notes.txt` 与外部 `EvidenceContext` 生成
  - 已真实落盘，`motivation` / `method_hypothesis` 等扩展字段仍待后续增强

#### P1-3 落地真实 `claim_graph.json`
- **目标**：让 Proposal 有“论点骨架”
- **字段建议**：
  - `nodes`
    - `id`
    - `type`（`problem/hypothesis/method/expected_result`）
    - `text`
    - `evidence_refs`
  - `edges`
    - `source`
    - `target`
    - `relation`
- **动作**：
  1. 从 `idea_brief` 派生初版 claim graph
  2. 先做轻量图，不做复杂知识图谱
- **验收**：
  - `nodes` 和 `edges` 有真实内容
  - 至少能表达“问题 -> 方法假设 -> 预期收益”
- **当前状态**：首轮 Alpha 已完成
  - 当前 claim graph 由 `idea_pipeline.py` 生成轻量节点/边结构
  - 已满足最小真实落盘，但节点语义仍偏模板化，后续可继续增强 evidence refs 与 claim 细粒度

#### P1-4 落地真实 `critique_report.json`
- **目标**：让 Proposal 有第一轮找茬
- **字段建议**：
  - `summary`
  - `strengths`
  - `risks`
  - `open_questions`
  - `blocking_issues`
- **动作**：
  1. 基于 `idea_brief + claim_graph` 产出第一轮 critique
  2. 暂时可以先单 reviewer，再扩多 reviewer
- **涉及文件**：
  - `agents/coordinator.py`
  - `engine/research_partner/critique_loop.py`
- **验收**：
  - 每次运行都能真实生成 critique
  - 不只是输出 planned status
- **当前状态**：首轮 Alpha 已完成
  - 当前由 `critique_loop.py` 提供 rubric 驱动的 deterministic critique，并复用 `engine.perform_review` / `engine.llm` 支持 LLM committee review
  - 已覆盖 `summary/strengths/risks/open_questions/blocking_issues`，更细粒度 committee artifact 仍待后续 phase 完成

#### P1-5 生成 `proposal_brief.md`
- **目标**：让用户能直接阅读 Proposal 套件
- **建议结构**：
  1. Problem
  2. Motivation
  3. Core Hypothesis
  4. Method Sketch
  5. Experiment Blueprint
  6. Expected Figures
  7. Risks / Open Questions
- **动作**：
  1. 从 JSON 工件拼装 Markdown
  2. 不直接进入全文写作
- **验收**：
  - 可直接阅读
  - 长度控制在 1-2 页逻辑密度
- **当前状态**：首轮最小实现已完成
  - 当前已由 `proposal_bundle.py` 从 JSON 工件拼装最小 Markdown brief
  - 当前结构偏简化版，已含 `Expected Figures` 与 rubric 相关配套工件引用，章节内容后续仍可继续增强

#### P1-6 更新 tests：从合同测试升级为真实工件测试
- **目标**：保证不是“看起来像有 2.0”
- **涉及文件**：
  - `tests/test_agent_bridges.py`
- **动作**：
  1. 保留现有合同测试
  2. 新增真实 artifact 存在性测试
  3. 新增字段完整性测试
- **验收**：
  - 既验证 schema/command
  - 也验证真实 JSON/MD 工件
- **当前状态**：已完成首轮落地
  - 已新增 `proposal_bundle` 直接物化测试
  - 已新增 `research_partner execute=True` 工件落盘测试
  - 当前已覆盖核心 JSON、`proposal_brief.md`、`experiment_blueprint.json`、`expected_figures.json` 与 `rubric_scorecard.json` 的存在性验证

### 11.3 Phase 2：只做 PDF + CSV 的证据底座

#### P2-1 定义统一 evidence contract
- **目标**：先统一结构，再写 parser
- **建议新增文件**：
  - `engine/parsing/contracts.py`
  - `engine/evidence/contracts.py`
- **建议结构**：
  - `source_id`
  - `source_type`（`pdf/csv`）
  - `metadata`
  - `chunks`
  - `tables`
  - `figures`
  - `citations`
- **验收**：
  - PDF 和 CSV 都能映射到统一结构
  - 后续 critique 只消费 contract，不直接读原文件

#### P2-2 实现 PDF parser
- **目标**：稳定读取文献
- **建议新增文件**：
  - `engine/parsing/pdf_parser.py`
- **最小能力**：
  - 标题
  - 摘要
  - section chunk
  - 页码
  - 参考文献块
- **不强求**：
  - 复杂公式 AST
  - 高级版面重建
- **验收**：
  - 对典型 arXiv PDF 不崩
  - 至少能输出 chunk + page anchoring

#### P2-3 实现 CSV parser
- **目标**：稳定读取实验指标
- **建议新增文件**：
  - `engine/parsing/csv_parser.py`
- **最小能力**：
  - 列名识别
  - 数值列识别
  - 候选 metric 列识别
  - 基础统计摘要
- **验收**：
  - 常见实验结果 CSV 可读
  - 能生成结构化指标摘要

#### P2-4 建 evidence index
- **目标**：让 Proposal 和 evidence 建立引用关系
- **建议新增文件**：
  - `engine/evidence/index_builder.py`
- **动作**：
  1. PDF chunks 建索引
  2. CSV metrics 建索引
  3. 给 `idea_brief` / `claim_graph` 提供 evidence refs
- **验收**：
  - `evidence_index.json` 可落盘
  - claim 能指到具体 `source/chunk/table`
- **当前状态**：Alpha 最小版已接入
  - 已能落盘 `evidence_index.json`
  - 当前仍是 source-level 索引，尚未细化到 chunk / table 级引用

#### P2-5 接入现有 research_partner pipeline
- **目标**：让 Phase 1 的 Proposal 开始消费真实 evidence
- **动作**：
  1. `search` 后不直接做 idea
  2. 先 `parse/normalize/index`
  3. 再走 `idea -> claim -> critique`
- **验收**：
  - Proposal 不再只是“凭空生成”
  - `evidence_basis` 有真实来源
- **当前状态**：Alpha 最小版已接入
  - 已在 `proposal_bundle.py` 中消费 PDF / CSV 摘要并写入 `evidence_basis`
  - 仍未实现统一 evidence contract 与 chunk-level normalize / index

### 11.4 Phase 3：Critique Loop Rubric 化

#### P3-1 定义 Rubric 配置格式
- **目标**：先统一配置模型
- **建议新增文件**：
  - `configs/rubrics/default.yaml`
  - `configs/rubrics/cvpr.yaml`
  - `configs/rubrics/journal_q2.yaml`
- **建议字段**：
  - `weights`
  - `thresholds`
  - `max_rounds`
  - `blocking_rules`
  - `target_venue`
- **验收**：
  - 三份 profile 能被统一读取
- **当前状态**：首轮最小实现已完成
  - 三份 YAML profile 已落地并可被运行时读取

#### P3-2 实现 Rubric loader
- **建议新增文件**：
  - `engine/research_partner/rubric_loader.py`
- **动作**：
  1. 支持默认 profile
  2. 支持 CLI 指定 profile
  3. 支持 YAML/JSON 两种读取
- **验收**：
  - `research_partner` 可切换 rubric profile 运行
- **当前状态**：首轮最小实现已完成
  - `rubric_loader.py` 已接入运行链
  - 当前以 YAML 为主，JSON 兼容仍可继续补强

#### P3-3 reviewer 角色拆分
- **目标**：从单一 critique 升级为多视角
- **建议角色**：
  - novelty reviewer
  - feasibility reviewer
  - methodology reviewer
  - evidence reviewer
  - meta chair
- **建议新增文件**：
  - `engine/research_partner/reviewers.py`
- **验收**：
  - 不同 reviewer 输出不同维度意见
  - 结果可聚合

#### P3-4 实现 committee 聚合
- **建议新增文件**：
  - `engine/research_partner/committee.py`
- **输出**：
  - `rubric_scorecard.json`
  - `committee_decision.json`
- **验收**：
  - 有总分
  - 有 blocking issues
  - 有 `go / revise / reject` 结论
- **当前状态**：部分完成
  - `critique_loop.py` 已支持 `reviewer_count`、committee decision 与总分计算
  - `rubric_scorecard.json` 已独立落盘；独立 `committee.py` 与 `committee_decision.json` 仍待拆出

#### P3-5 实现 critique loop 停止条件
- **目标**：防止死循环
- **规则建议**：
  - 达到阈值则 stop
  - blocking issues 清零则 stop
  - 达到 `max_rounds` 强制 stop
- **验收**：
  - 每次运行都可预测退出
  - 不依赖人工打断才结束
- **当前状态**：部分完成
  - `approval_threshold` 与 `max_rounds` 已在 `critique_loop.py` 中生效
  - 仍未引入基于 `blocking_issues` 的停止条件

### 11.5 Phase 4：Scitex 微服务边界预留

#### P4-1 定义 adapter contract
- **目标**：主仓先预留接口，不急着接真实服务
- **建议新增文件**：
  - `integrations/scitex_client.py`
  - 或 `mcp_servers/scitex_adapter/server.py`
- **最小接口**：
  - `parse_document`
  - `verify_claims`
  - `scholar_search`
- **验收**：
  - 主仓代码里不直接 import scitex

#### P4-2 写微服务接入文档
- **目标**：先定边界再实现
- **建议新增文档**：
  - `docs/paperforge-scitex-microservice-boundary.md`
- **内容**：
  - 部署方式
  - 输入输出 contract
  - 超时与错误语义
  - 启用/禁用策略
- **验收**：
  - 任何人可按文档独立实现 sidecar

#### P4-3 适配 research pipeline
- **目标**：让 Scitex 是增强项，不是阻塞项
- **动作**：
  1. 默认关闭
  2. 开启后只增强 evidence / verification
  3. 不改变 Proposal 主流程核心控制权
- **验收**：
  - 服务不可用时主链不崩

### 11.6 Phase 5：CLI 与可观测性增强

#### P5-1 补 CLI 参数
- **目标**：支持 Proposal-first 配置
- **建议新增参数**：
  - `--rubric-profile`
  - `--proposal-format`
  - `--csv-path`
  - `--pdf-path`
- **涉及文件**：
  - `launch_user_entry.py`
- **验收**：
  - CLI 能独立配置 2.0 主链

#### P5-2 补 trace / 日志输出
- **目标**：便于 debug
- **动作**：
  1. 打印当前 stage
  2. 打印 rubric profile
  3. 打印 evidence source 数量
  4. 打印 critique round
- **验收**：
  - 终端日志可定位失败阶段

#### P5-3 补 artifacts manifest
- **目标**：方便审查输出
- **建议新增**：
  - `artifacts/research_partner/manifest.json`
- **验收**：
  - 一次运行生成了什么文件一眼可见

### 11.7 推荐执行顺序

#### 第一批必须先做
1. P0-2 冻结 Proposal 套件合同
2. P1-1 抽离 research_partner 执行层
3. P1-2 / P1-3 / P1-4 真实生成三个核心 JSON
4. P1-5 生成 `proposal_brief.md`
5. P1-6 补真实工件测试

> 当前进展：第 2～5 项已完成首轮最小落地；P0-2 仅完成核心合同抽离，完整 Proposal 套件字段冻结仍待补齐。

#### 第二批再做
6. P2-1 ~ P2-5：PDF + CSV 底座
7. P3-1 ~ P3-5：Rubric + committee
8. P5-1 ~ P5-3：CLI / trace / manifest

#### 最后做
9. P4-1 ~ P4-3：Scitex 微服务适配

### 11.8 任务编号版 Checklist

#### A. 合同与文档
- [ ] A1 冻结 Proposal 套件字段
- [x] A2 更新 2.0 文档到 contracts
- [ ] A3 补 research_partner 目录与命名规范

#### B. research_partner 真执行化
- [x] B1 新建 `engine/research_partner/contracts.py`
- [x] B2 新建 `idea_pipeline.py`
- [ ] B3 新建 `claim_graph.py`
- [x] B4 新建 `critique_loop.py`
- [x] B5 生成 `proposal_brief.md`
- [x] B6 coordinator 接新 pipeline
- [x] B7 更新 bridge tests

#### C. PDF + CSV 底座
- [ ] C1 定义 evidence contract
- [ ] C2 实现 PDF parser
- [ ] C3 实现 CSV parser
- [ ] C4 实现 evidence index
- [ ] C5 proposal 引用 evidence refs

#### D. Rubric / committee
- [x] D1 default rubric
- [x] D2 cvpr rubric
- [x] D3 journal_q2 rubric
- [x] D4 rubric loader
- [x] D5 multi-reviewer committee
- [x] D6 stop conditions

#### E. CLI / 可观测性
- [ ] E1 CLI 参数扩展
- [ ] E2 trace 增强
- [x] E3 manifest 输出

#### F. Scitex 预留
- [ ] F1 adapter contract
- [ ] F2 boundary doc
- [ ] F3 feature flag / optional integration
