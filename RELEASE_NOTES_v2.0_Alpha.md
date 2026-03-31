# PaperForge 2.0: 你的全自动硬核科研合伙人

## Alpha 发布说明

发布日期：2026-03-30

## 本次发布亮点

- `research_partner` 从 bootstrap-only 合同壳层升级为可真实落盘的 Proposal-first Alpha 主线
- 新增 PDF/CSV 外部证据接入，自动归一化到 `EvidenceContext`
- critique 升级为基于现有 `engine.perform_review` / `engine.llm` 的 LLM reviewer/committee 闭环
- 若 LLM 不可用，自动回退到 deterministic critique，保证流程稳定
- README 与改进计划文档已同步到 Alpha 状态

## 新增能力

### 1. Rubric / Profile CLI 接入

`research_partner` 现支持：

- `default`
- `cvpr`
- `journal_q2`

示例：

```bash
python launch_user_entry.py research_partner \
  --title "Evidence-first topic" \
  --description "Use structured evidence intake to shape proposal generation." \
  --rubric-profile cvpr
```

### 2. PDF/CSV → EvidenceContext

新增能力：

- repeated `--evidence-file`
- PDF 摘要进入 `evidence_basis`
- CSV 指标摘要进入 `metrics_summary`
- 自动生成 `evidence_index.json`
- `manifest.json` 记录 `evidence_files`

示例：

```bash
python launch_user_entry.py research_partner \
  --title "Frequency-Domain Global Regression for Artifact Suppression" \
  --description "构建动态伪影抑制结合频率域全局回归的全色锐化网络架构，并设计消融实验。" \
  --rubric-profile cvpr \
  --evidence-file /absolute/path/paper.pdf \
  --evidence-file /absolute/path/results.csv
```

### 3. Reviewer / Committee Critique 闭环

当前默认由 proposal materializer 触发：

- `review_mode="llm_committee"`
- `review_model="claude-sonnet-4-6"`
- `reviewer_count=3`

底层复用：

- `engine.perform_review`
- `engine.llm`

回退策略：

- reviewer client 初始化失败 → fallback deterministic critique
- LLM review 执行异常 → fallback deterministic critique

## 当前 Alpha 产物

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

## 测试状态

本次 Alpha 冲刺后的定向回归：

```bash
source .venv311/bin/activate
python -m pytest tests/test_idea_pipeline.py tests/test_research_partner_pipeline.py tests/test_agent_bridges.py -q
```

结果：

- `52 passed`

## 已知边界

本次 Alpha 仍未完成：

- 前端可视化包装
- 更细粒度的 evidence parsing 策略

## 适合下一步推进的方向

1. 将 `research_partner` 结果接入前端控制台展示
2. 继续补足配置异常与边界输入回归用例
3. 增强 evidence parsing / evidence refs 细粒度
4. 视需要继续拆分 committee 相关持久化工件
