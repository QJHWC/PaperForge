# PaperForge 本地进度与发布准备状态（2026-03-31）

## 本次目标

本轮工作围绕 `research_partner` 主线落地、bridge/CLI/schema 收敛、安全脱敏，以及发布前收口检查展开。

当前用户要求已经明确：

- `CLAUDE.md` **只作本地参考，不提交、不上传**
- 必须先清掉阻塞项，再判断是否进入正式 git 提交阶段
- 当前进度与本地代码状态需要沉淀到 `docs/` 目录中

---

## 当前代码状态概览

### 已修改/新增的主要范围

- `launch_user_entry.py`
  - 新增 `research_partner` 入口
  - 增加 CLI 输出路径脱敏
  - materialize proposal bundle
  - materialize 阶段异常统一转为安全 `SystemExit`
- `launch_mvp_workflow.py`
  - 新增 `rubric_profile` / `evidence_file` 参数入口
- `agents/coordinator.py`
  - 将 `research_partner` 作为对 MVP bootstrap 的薄别名进行桥接
  - 暴露独立 schema / contract version / planned outputs / planned workflow
  - 对 workspace/artifact 路径进行脱敏输出
- `agents/mvp_workflow_agent.py`
  - 透传 `rubric_profile` 与 `evidence_files`
- `agents/schemas/*.schema.json`
  - 增补 `research_partner` mode 与输入字段
- `engine/research_partner/`
  - 新增 proposal contracts / idea pipeline / critique loop / rubric loader / proposal bundle
- `tests/`
  - 新增/扩展 research partner、rubric、bridge、materialize、安全脱敏等测试
- `README.md`
  - 已同步 2.0 Alpha 主线叙事、入口命令、结构概览
- `docs/paperforge-json-artifact-contracts.md`
  - 已补 `research_partner` artifacts contract
- `RELEASE_NOTES_v2.0_Alpha.md`
  - Alpha 发布说明已存在

---

## 发布前阻塞项处理结果

### 已解决

此前存在的唯一明确阻塞项：

- `tests/test_smoke.py::SmokeTest::test_readme_mentions_current_layers`

根因：

- `README.md` 的项目结构段缺少 `skills/`，与 smoke contract 不一致。

处理：

- 在 `README.md` 的项目结构中补入 `skills/` 条目。

结果：

- `python -m pytest tests/test_smoke.py -q` → `4 passed`
- `python -m pytest -q` → `70 passed, 5 warnings`

这意味着：

- 当前已清掉已知硬阻塞
- full pytest 已全绿
- 代码状态已从“接近可提交”提升到“具备进入 git 提交评估阶段的条件”

---

## 当前测试状态

### Full pytest

```bash
source .venv311/bin/activate
python -m pytest -q
```

结果：

- `70 passed, 5 warnings`

### Smoke test

```bash
source .venv311/bin/activate
python -m pytest tests/test_smoke.py -q
```

结果：

- `4 passed`

### 定向主线回归（之前已验证）

```bash
source .venv311/bin/activate
python -m pytest tests/test_idea_pipeline.py tests/test_research_partner_pipeline.py tests/test_agent_bridges.py -q
```

结果：

- `52 passed, 5 warnings`

---

## CLAUDE.md 处理策略

用户已明确要求：

- `CLAUDE.md` 不随本次提交进入仓库
- 只保留为本地参考文件

当前落实方式：

- 已将 `CLAUDE.md` 加入 `.git/info/exclude`

这意味着：

- 它不会被误纳入 `git status` 的未跟踪文件清单
- 也不会进入本次建议提交范围

注意：

- `.git/info/exclude` 是本地仓库级忽略规则，不会污染项目共享 `.gitignore`

---

## 当前是否已到 git 提交级别

### 结论

**是，当前已经达到“可以进入正式 git 提交/PR 准备阶段”的状态。**

判断依据：

- 已知 smoke blocker 已清除
- full pytest 已全绿
- 核心 research_partner / CLI / bridge / materialize / artifact contract / 安全脱敏链路已有测试覆盖
- `CLAUDE.md` 已按要求保留本地、不纳入提交范围

### 但仍需注意的非阻塞项

- 当前 venv 未安装 `ruff`，因此没有补做 lint 校验
- 这属于工具链缺口，不是当前已知 commit blocker
- 在提交前仍建议做一次最终 diff 审核，确认提交范围只包含本轮用户预期的文件

---

## 当前建议的提交范围

建议重点关注以下改动是否纳入本次提交：

- `README.md`
- `RELEASE_NOTES_v2.0_Alpha.md`
- `docs/paperforge-2.0-improvement-plan.md`
- `docs/paperforge-json-artifact-contracts.md`
- `launch_user_entry.py`
- `launch_mvp_workflow.py`
- `agents/coordinator.py`
- `agents/mvp_workflow_agent.py`
- `agents/schemas/coordinator.schema.json`
- `agents/schemas/mvp_workflow_agent.schema.json`
- `agents/schemas/research_partner.schema.json`
- `engine/research_partner/*`
- `tests/test_agent_bridges.py`
- `tests/test_idea_pipeline.py`
- `tests/test_research_partner_pipeline.py`
- `tests/test_rubric_loader.py`
- `configs/rubrics/*`

不应纳入：

- `CLAUDE.md`

---

## 下一步建议

1. 做最终一次 diff 审核，确认无意外文件
2. 起草 commit message
3. 再决定是否直接本地 commit，或继续整理 PR 描述 / 发布说明

---

## 状态一句话摘要

截至 2026-03-31，本地代码已清除 README smoke 阻塞，full pytest 全绿，`CLAUDE.md` 已明确仅本地保留；当前状态已达到进入正式 git 提交准备阶段的门槛。
