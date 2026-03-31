# PaperForge

**面向研究论文生成、实验驱动写作与 Proposal-first 研究收敛的自动化流水线。**

PaperForge 将文献检索、实验编排、云端训练、LaTeX 写作与研究提案收敛串成一套可脚本化流程，支持多种 LLM 后端（Anthropic、OpenAI、Gemini、DeepSeek）。

## 当前主线

- `research_partner` — 2.0 Alpha 默认主线：证据摄入 → idea framing → claim graph → critique → proposal artifacts
- `mvp` — 分阶段论文流水线：bootstrap → feedback → optimize → refine → cloud
- `scientist` — 全自动主线：idea → experiment → writeup → review → improvement

![PaperForge 界面预览](docs/images/screenshot.png)

## 文档导航

README 只保留高层叙事、入口命令与仓库导航；字段级 contract、验收矩阵与阶段细节以下列独立文档为准：

- [`RELEASE_NOTES_v2.0_Alpha.md`](RELEASE_NOTES_v2.0_Alpha.md) — 本轮 Alpha 发布摘要、已交付能力与已知边界
- [`docs/paperforge-2.0-improvement-plan.md`](docs/paperforge-2.0-improvement-plan.md) — 2.0 收敛版规划、当前 Alpha 状态与后续 phase
- [`docs/paperforge-workflow-equivalence-spec.md`](docs/paperforge-workflow-equivalence-spec.md) — 既有 scientist / mvp / writeup 行为等价基线
- [`docs/paperforge-equivalence-matrix.md`](docs/paperforge-equivalence-matrix.md) — 行为等价验收矩阵
- [`docs/paperforge-json-artifact-contracts.md`](docs/paperforge-json-artifact-contracts.md) — workspace / JSON / artifact 合同说明

## 快速开始

### 1. 环境准备

```bash
python3.11 -m venv .venv311
source .venv311/bin/activate
pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
cp key.example.sh key.sh
# 编辑 key.sh，填入 API Key
source key.sh
```

### 3. 运行 Research Partner（推荐）

```bash
python launch_user_entry.py research_partner \
  --experiment paper_writer \
  --title "Frequency-Domain Global Regression for Artifact Suppression" \
  --description "构建动态伪影抑制结合频率域全局回归的全色锐化网络架构，并设计消融实验。" \
  --engine openalex \
  --rubric-profile cvpr \
  --evidence-file /absolute/path/paper.pdf \
  --evidence-file /absolute/path/results.csv
```

如只想查看将执行的命令而不真正运行：

```bash
python launch_user_entry.py research_partner --dry-run
```

### 4. 运行 MVP 流程

```bash
# 完整流程
python launch_mvp_workflow.py \
  --phase all \
  --experiment paper_writer \
  --idea-name "My Research Idea" \
  --engine openalex

# 单阶段运行
python launch_mvp_workflow.py --phase bootstrap --experiment paper_writer
```

### 5. 运行 Scientist 流程

```bash
python launch_scientist.py \
  --experiment paper_writer \
  --num-ideas 1 \
  --skip-novelty-check
```

### 6. 查看前端控制台

```bash
# 列出所有 workspace
python -m frontend.console

# 查看单个 workspace 详情
python -m frontend.console --workspace results/<experiment>/<run>/

# 自动刷新（每 5 秒）
python -m frontend.console --watch 5

# 查看写作 Prompt 目录
python -m frontend.console --prompts
```

## Research Partner 输出概览

`research_partner` 当前会在 workspace 下生成：

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

如需查看字段语义、Alpha 已交付范围和后续 roadmap，请分别参考：

- [`RELEASE_NOTES_v2.0_Alpha.md`](RELEASE_NOTES_v2.0_Alpha.md)
- [`docs/paperforge-2.0-improvement-plan.md`](docs/paperforge-2.0-improvement-plan.md)
- [`docs/paperforge-json-artifact-contracts.md`](docs/paperforge-json-artifact-contracts.md)

## 项目结构

```text
PaperForge/
├── launch_user_entry.py        # 统一入口
├── launch_mvp_workflow.py      # MVP 流程编排
├── launch_scientist.py         # Scientist 流程
├── engine/                     # 核心引擎
│   ├── llm.py                  # LLM client 统一封装
│   ├── mvp_workflow.py         # workspace 生命周期
│   ├── perform_review.py       # reviewer / committee 评审底座
│   └── research_partner/       # Proposal-first pipeline
├── agents/                     # Agent bridge 层
├── frontend/                   # 前端控制台
├── mcp_servers/                # MCP 工具接口层
├── skills/                     # 写作 / 引用 / 修订 skills
├── configs/rubrics/            # rubric profiles
├── templates/                  # 实验模板
├── results/                    # 运行结果（git-ignored）
└── tests/                      # 测试套件
```

## 关键环境变量

| 变量 | 用途 |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic 原生协议 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | OpenAI 兼容端点 |
| `OPENALEX_MAIL_ADDRESS` | OpenAlex 文献检索（礼貌池访问） |
| `S2_API_KEY` | Semantic Scholar API（可选） |
| `WRITEUP_CITE_ROUNDS` | 引用轮数（默认 4） |
| `WRITEUP_LATEX_FIX_ROUNDS` | LaTeX 修错轮数（默认 2） |
| `MPLBACKEND=Agg` | macOS 无头 matplotlib |
| `PAPERFORGE_ALLOW_SYSTEM_PYTHON=1` | 跳过 venv 检查 |

## MVP 阶段说明

| 阶段 | 功能 |
|---|---|
| `bootstrap` | 创建 workspace、初始化 notes、文献检索、基线实验 |
| `feedback` | 读取上传结果、更新 notes、初版写作 |
| `optimize` | 多轮实验迭代（aider 代码 Agent） |
| `refine` | LaTeX 精修 + 降重 + 引用补全 |
| `cloud` | 上传至远程 GPU 训练、下载结果 |
| `all` | 依次执行 bootstrap → feedback → optimize → refine |

## 测试

当前定向回归命令：

```bash
source .venv311/bin/activate
python -m pytest tests/test_idea_pipeline.py tests/test_research_partner_pipeline.py tests/test_agent_bridges.py -q
```

当前结果：`52 passed`。

## 使用限制

- 禁止商用
- 仅限个人、学术研究、非营利教育使用
- 禁止用于 surveillance、deceptive media、未授权医疗/犯罪预测等场景
- 使用本工具生成的论文必须在显著位置声明 AI 辅助生成

详见 `LICENSE`。
