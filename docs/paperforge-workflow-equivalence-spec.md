# PaperForge 工作流等价规格说明

## 1. 文档目的

本文档用于定义 **PaperForge 当前工作流的行为等价基线**，作为后续架构升级、Agent/Skill/MCP 拆分、可视化前端接入时的唯一流程对齐标准。

核心原则：

- **流程顺序不变**
- **阶段语义不变**
- **多轮调用语义不变**
- **关键产物不变**
- **许可证合规机制不变**
- **checkpoint / resume 语义不变**
- **入口兼容面不变**
- **隐式数据契约先冻结，再重构**

后续所有优化，都必须满足：

> 升级后的系统在外部行为上，应与当前 PaperForge 的执行流程保持一致。  
> 这里的“一致”指 **行为等价**，而不是字节级输出一致；对于依赖多轮 LLM 调用、外部检索与时间戳的流程，应以状态流转、工件集合、控制参数语义、恢复语义、合规语义为验收主轴，而非简单要求 PDF 字节完全相同。

---

## 2. 依据文件

本文档依据以下仓库文件整理：

- `README.md`
- `LICENSE`
- `launch_user_entry.py`
- `launch_mvp_workflow.py`
- `launch_scientist.py`
- `engine/mvp_workflow.py`
- `engine/perform_writeup.py`

---

## 3. 当前仓库保留的合规约束

### 3.1 法律与合规要求

根据当前仓库的 `LICENSE`，系统仍受以下限制：

1. 不得用于商业用途。
2. 不得用于 surveillance、deceptive media、未授权医疗/犯罪预测等场景。
3. **生成或传播论文类文档、研究报告、技术报告时，必须显著披露 AI 参与。**

### 3.2 当前代码中的落实方式

在 `engine/perform_writeup.py` 中，当前系统已通过以下机制落实：

- `_DISCLOSURE_BLOCK`
- `_ensure_disclosure()`
- `_sanitize_template_tex_file()`
- 多处 prompt 中明确要求 **不得删除声明区块**

### 3.3 等价重构时必须保留

后续重构时，以下行为属于 **强制保留项**：

- 自动注入声明区块
- 防止模型或修订步骤删除声明区块
- 最终导出前再次校验声明区块存在
- 所有新的写作/修订 skill 不得绕过该机制

---

## 4. 总体入口结构

### 4.1 统一入口：`launch_user_entry.py`

该文件是统一入口路由层，不直接承载主流程，而是负责：

- 选择入口模式：
  - `scientist`
  - `mvp`
- 处理 API 协议与模型配置
- 组装命令并透传参数
- 统一环境变量注入
- 打印实际生效配置
- 提供 `dry-run`

### 4.2 两类入口模式

#### A. `scientist`
全自动主线：

- idea
- experiment
- writeup
- review
- optional improvement
- re-review

#### B. `mvp`
分阶段主线：

- bootstrap
- feedback
- optimize
- refine
- cloud
- all

### 4.3 统一入口兼容面

未来升级后，统一入口层可重构，但必须保留以下兼容面：

- `scientist` 与 `mvp` 两种入口语义
- 参数透传能力
- 模型分阶段路由能力
- 现有环境变量优先级逻辑（CLI > ENV > default）
- `--claude-protocol` 路由语义
- `--dry-run` 语义
- 生效配置打印语义
- 现有 API key/base url 环境变量兼容面

---

## 5. scientist 主工作流状态机

主工作流由 `launch_scientist.py` 定义。

### 5.1 scientist 主线阶段

当前 scientist 主线真实流程为：

1. 校验模板完整性
2. 校验 LaTeX 依赖
3. 解析并确定 stage models
4. 生成 ideas
5. 可选 novelty check
6. 过滤 novel ideas
7. 对每个 idea 执行：
   - 创建结果目录
   - 复制模板
   - 初始化 notes
   - 执行 experiments
   - 执行 writeup
   - 执行 review
   - 若 `--improvement` 启用：
     - 执行 improvement
     - 重新生成 improved PDF
     - 执行 re-review

### 5.2 scientist 的并行模式

当 `--parallel > 0` 时：

- 使用 multiprocessing queue 分发 idea
- worker 绑定 GPU
- 每个 worker 处理独立 idea 目录
- 并行语义建立在 **idea 级隔离** 上，而非同一 workspace 并发写

### 5.3 scientist 等价要求

未来重构必须保留：

- idea generation → novelty → experiment → writeup → review 的主线语义
- `--improvement` 对应 improvement + re-review 语义
- stage-specific model resolution 语义
- 并行是 **跨 idea 并行**，不是同目录并行写
- `results/<experiment>/<timestamp>_<idea_name>` 的产物隔离语义

---

## 6. MVP 主工作流状态机

主工作流由 `launch_mvp_workflow.py` 定义。

### 6.1 Phase 列表

当前合法 phase：

- `bootstrap`
- `feedback`
- `optimize`
- `refine`
- `cloud`
- `all`

这是当前系统对外暴露的稳定阶段语义，后续必须保留。

---

## 7. 各阶段行为规范（MVP）

## 7.1 bootstrap

函数：`_phase_bootstrap`

### 输入
- `experiment`
- `idea_name`
- `title`
- `description`
- `engine`
- `writeup_model`
- `skip_writeup`
- `skip_mvp_run`
- `refresh_literature`
- `bootstrap_run_index`

### 执行步骤
1. 创建 workspace（若未指定）
2. 写入 idea metadata
3. 初始化 `notes.txt`
4. 初始化上传接口
5. 可选执行 literature refresh
6. 可选执行一次 MVP experiment
7. 执行 plotting
8. 将 run feedback 刷入 notes
9. 可选执行 writeup
10. 更新 workflow state

### 关键输出
- workspace
- `notes.txt`
- 初始实验产物
- `paper_mvp_draft.pdf`
- `phase="bootstrap_completed"`

### 等价要求
未来重构必须保留：
- workspace 初始化职责
- 初始 notes 构建
- run_0 / baseline 初始化语义
- 文献刷新入口
- 首稿 PDF 产物

---

## 7.2 feedback

函数：`_phase_feedback`

### 输入
- 已存在 workspace
- 上传文件
- 可选 literature refresh
- writeup 参数

### 执行步骤
1. 确保上传接口存在
2. ingest 用户上传文件
3. 将上传反馈追加到 notes
4. 将 run feedback 刷入 notes
5. 可选刷新 literature
6. 再次执行 writeup
7. 更新 workflow state

### 关键输出
- `paper_with_feedback.pdf`
- `upload_manifest.json`
- `phase="feedback_completed"`

### 等价要求
未来重构必须保留：
- 用户上传驱动的回填语义
- 上传清单产物
- notes 融合逻辑
- feedback 阶段独立 PDF 里程碑

---

## 7.3 optimize

函数：`_phase_optimize`

### 输入
- workspace
- `optimize_runs`
- `python_bin`
- `writeup_model`

### 执行步骤
1. 按 `optimize_runs` 循环执行实验
2. 任意实验失败则中止本阶段循环
3. 统一执行 plotting
4. 将 run feedback 刷入 notes
5. 再次执行 writeup
6. 更新 workflow state

### 关键输出
- 新增 run 结果
- 更新图表
- `paper_after_optimize.pdf`
- `phase="optimize_completed"`

### 等价要求
未来重构必须保留：
- 多次实验运行语义
- run fail 时中断优化循环的逻辑
- 实验结果驱动文稿再写作
- optimize 阶段独立 PDF 里程碑

---

## 7.4 refine

函数：`_phase_refine`

### 输入
- workspace
- `refine_profile`
- `writeup_model`

### 执行步骤
1. 再次执行 writeup
2. writeup 强度由 profile 控制：
   - `fast`
   - `balanced`
   - `deep`
3. 更新 workflow state

### 关键输出
- `paper_refined.pdf`
- `phase="refine_completed"`

### 等价要求
未来重构必须保留：
- refine 作为独立阶段存在
- profile 控制写作强度
- refine 阶段独立 PDF 里程碑

---

## 7.5 cloud

函数：`_phase_cloud`

### 输入
- workspace
- `remote_config`
- `cloud_run_dir`
- `pipeline_root`
- `pipeline_config`
- `pipeline_run_name`
- `pipeline_mode`
- `pipeline_hardware_profile`
- `pipeline_device`
- `cloud_skip_run`
- `cloud_skip_sync`

### 执行步骤
1. 调用 `run_cloud_pipeline_cycle.py`
2. 执行远程任务运行/同步逻辑
3. 出错时终止阶段

### 关键输出
- 远程训练结果
- 下载/同步后的本地产物
- 可供后续反馈与修订消费的数据

### 等价要求
未来重构必须保留：
- cloud 作为横向基础设施阶段
- 远程执行 + 下载 + 同步的组合能力
- skip-run / skip-sync 等控制参数语义

---

## 7.6 all

### 当前执行顺序

`all` 的真实顺序是：

1. `bootstrap`
2. 如果 `run_cloud_cycle=True`，执行 `cloud`
3. `feedback`
4. `optimize`
5. `refine`

### 等价要求
未来重构必须严格保留该顺序，尤其是：

- `cloud` 在 `all` 模式下不是最后运行
- 而是位于 `bootstrap` 之后、`feedback` 之前的可选阶段

---

## 8. Writeup 内部状态机

核心函数：`perform_writeup()`

当前 writeup 不是单轮生成，而是一个内部多阶段状态机。

### 8.1 状态列表

- `start`
- `init`
- `cite`
- `refine`
- `latex_fix`
- `done`

### 8.2 checkpoint 结构

当前 checkpoint 文件：`writeup_checkpoint.json`

默认字段：

```json
{
  "stage": "start",
  "current_round": 0,
  "latest_tex_file": null,
  "updated_at": null
}
```

### 8.3 等价要求

未来重构必须保留：

- 分阶段 checkpoint
- 当前轮次记录
- 最新 tex 快照路径
- resume 能力
- done 后跳过重复 writeup 的语义

---

## 9. Writeup 详细行为

## 9.1 init 阶段

### 当前行为
1. 先填充 Title 和 Abstract
2. 立即 refine Abstract
3. 逐节填充以下内容：
   - Introduction
   - Background
   - Method
   - Experimental Setup
   - Results
   - Conclusion
4. 每节生成后立即执行一次 refinement
5. 最后生成 Related Work 的注释式结构草图

### 关键特征
- **逐节写**
- **逐节 refine**
- Related Work 初始阶段只是 sketch，不是最终定稿

### 等价要求
未来重构必须保留：
- section-by-section 生成方式
- per-section refinement
- Related Work 先 sketch 后补 citation 的语义

---

## 9.2 cite 阶段

### 当前行为
受 `WRITEUP_CITE_ROUNDS` 控制。

每一轮：
1. 读取当前 LaTeX draft
2. 识别“当前最缺失的一条 citation”
3. 生成检索 query
4. 调用文献搜索
5. 选择要加入的 citation
6. 将 bibtex 插入 `references.bib`
7. 通过 coder 将 citation 融入正文
8. sanitize tex
9. 保存 checkpoint

若模型判断 `No more citations needed`，则提前停止。

cite rounds 结束后：
- 再 refine 一次 Related Work

### 等价要求
未来重构必须保留：
- citation 为 **多轮补全式**
- 每轮只补最重要缺口的语义
- 可提前停止
- cite 后追加 Related Work refinement

---

## 9.3 refine 阶段

### 当前行为
受 `WRITEUP_SECOND_REFINEMENT` 控制。

启用时：
1. 先重新思考 Title
2. 再依次精修：
   - Abstract
   - Related Work
   - Introduction
   - Background
   - Method
   - Experimental Setup
   - Results
   - Conclusion

### 关键特征
- 这是 **全稿形成后的第二轮全局精修**
- 与 init 阶段的局部 refine 不同

### 等价要求
未来重构必须保留：
- second refinement 的独立开关
- 标题重思考步骤
- 全节顺序 refine

---

## 9.4 latex_fix 阶段

### 当前行为
受 `WRITEUP_LATEX_FIX_ROUNDS` 控制。

每一轮：
1. 执行 `chktex`
2. 收集错误输出
3. 让 coder 做最小修改修复
4. sanitize tex
5. 保存 checkpoint

若本轮无错误，可提前停止。

随后执行固定编译流程：

1. `pdflatex template.tex`
2. `bibtex template`
3. `pdflatex template.tex`
4. `pdflatex template.tex`

### 等价要求
未来重构必须保留：
- 多轮 LaTeX 修错
- 最小修改原则
- 编译命令顺序
- 失败时可恢复继续

---

## 10. 评审 / 改进链路（scientist）

### 10.1 review

在 `launch_scientist.py` 中，writeup 结束后会：

1. 加载生成的 PDF 文本
2. 执行 `perform_review()`
3. 生成 `review.txt`

### 10.2 improvement

当 `--improvement` 启用时：

1. 执行 `perform_improvement(review, coder)`
2. 生成 improved PDF
3. 再次加载 improved PDF
4. 再执行一次 `perform_review()`
5. 生成 `review_improved.txt`

### 10.3 等价要求

未来重构必须保留：

- review 是 scientist 主线的标准阶段
- improvement 是显式开关，而非默认总是执行
- improvement 后必须 re-review
- review 与 review_improved 是独立工件

---

## 11. 写作轮次控制参数

当前系统中，以下参数属于外部稳定语义，后续必须映射保留。

### 11.1 环境变量

- `WRITEUP_CITE_ROUNDS`
- `WRITEUP_LATEX_FIX_ROUNDS`
- `WRITEUP_SECOND_REFINEMENT`
- `WRITEUP_ENABLE_CHECKPOINT`
- `WRITEUP_RESET_CHECKPOINT`
- `PAPERFORGE_PROMPT_LIBRARY_PATH`
- `PAPERFORGE_AIDER_EDIT_FORMAT`

### 11.2 refine profile 到环境变量的映射

在 `launch_mvp_workflow.py` 中：

#### fast
- `WRITEUP_CITE_ROUNDS=2`
- `WRITEUP_LATEX_FIX_ROUNDS=1`
- `WRITEUP_SECOND_REFINEMENT=0`

#### deep
- `WRITEUP_CITE_ROUNDS=6`
- `WRITEUP_LATEX_FIX_ROUNDS=3`
- `WRITEUP_SECOND_REFINEMENT=1`

#### balanced
- 不显式覆盖，走下游默认值

### 11.3 scientist 主线相关显式控制面

在 `launch_scientist.py` 中，还必须保留以下外部稳定语义：

- `--skip-idea-generation`
- `--skip-novelty-check`
- `--parallel`
- `--gpus`
- `--num-ideas`
- `--improvement`
- stage-specific model flags
- `NUM_REFLECTIONS`
- review 阶段中的：
  - `num_reflections=5`
  - `num_fs_examples=1`
  - `num_reviews_ensemble=5`

### 11.4 等价要求

未来重构时即使引入新配置体系，也必须保留这些参数的语义兼容。

---

## 12. Prompt / 风格控制的当前语义

### 12.1 当前机制
`perform_writeup.py` 会从 `prompt_library.py` 读取 practical prompt cues，并构造：

- 写作风格政策
- 主题匹配提示
- 反 AI 痕迹风格要求
- 逻辑与语法增强提示

### 12.2 当前策略特征
- 不直接暴露为独立 workflow
- 但已经作为 style policy 注入到写作和 refinement 中

### 12.3 等价要求
未来重构必须保留：
- 风格政策在写作阶段的系统级注入
- prompt cues 可基于主题匹配附加
- 关键“降重/自然化/逻辑优化”资产不丢失

---

## 13. 隐式数据契约冻结

当前系统存在一批尚未 schema 化、但已经被流程隐式依赖的数据契约。  
这些契约必须在 Agent / Skill / MCP / Frontend 重构前先冻结。

### 13.1 `workflow_idea.json`
来源：`engine/mvp_workflow.py`

当前核心字段：
- `Name`
- `Title`
- `Experiment`

### 13.2 `workflow_state.json`
来源：`engine/mvp_workflow.py`

当前特征：
- 动态 key-value 结构
- 默认会加入 `updated_at`
- 各 phase 用于记录：
  - `phase`
  - `mvp_completed`
  - `upload_interface_ready`
  - `ingested_uploads`
  - `upload_manifest`
  - 其他运行时字段

### 13.3 `writeup_checkpoint.json`
来源：`engine/perform_writeup.py`

当前核心字段：
- `stage`
- `current_round`
- `latest_tex_file`
- `updated_at`

### 13.4 `upload_manifest.json`
来源：`engine/mvp_workflow.py`

当前核心字段：
- `timestamp`
- `code_files`
- `figure_files`
- `paper_figure_files`
- `user_notes`

### 13.5 `notes.txt`
当前虽为自由文本，但实际包含稳定结构块：
- 标题区
- baseline 区
- authoring policy 区
- `AUTO:RUN_FEEDBACK`
- `AUTO:LITERATURE`
- `AUTO:UPLOAD_FEEDBACK`

### 13.6 等价要求

在 P0/P1 阶段必须先冻结这些契约：

- 字段名
- 最小必填字段
- 可选字段
- 文件路径
- 更新时机
- 谁拥有写权限

在正式 schema 化之前，任何新模块不得私自扩展或重定义这些契约。

---

## 14. 工作区并发 / 重入语义

### 14.1 当前事实

当前 workspace 文件存在以下特征：

- `workflow_state.json` 直接覆盖写
- `template.tex` 被多轮原地修改
- `notes.txt` 被多次 block upsert
- upload / artifacts / checkpoint 都在同一 workspace 下写入

### 14.2 当前安全边界

当前系统默认安全边界是：

- scientist 并行只在 **不同 idea 目录** 间进行
- mvp workflow 默认假设 **单 workspace 单写者**
- 当前没有统一的 workspace lease / lock 作为跨入口保护层

### 14.3 等价重构要求

在前端触发、OpsAgent、MCP 接入前，必须新增：

- single-writer workspace lock / lease
- 明确的锁粒度（建议 workspace 粒度）
- 重入策略
- stale lock 恢复策略

否则新入口接入后会破坏当前隐含的单写者假设。

---

## 15. 当前关键工件

以下工件应视为当前外部可见的阶段性产物：

### scientist
- `ideas.json`
- `<idea_folder>/notes.txt`
- `<idea_folder>/<idea_name>.pdf`
- `<idea_folder>/review.txt`
- `<idea_folder>/<idea_name>_improved.pdf`
- `<idea_folder>/review_improved.txt`
- `<idea_folder>/log.txt`（在 log_file 模式下）

### mvp / writeup
- `paper_mvp_draft.pdf`
- `paper_with_feedback.pdf`
- `paper_after_optimize.pdf`
- `paper_refined.pdf`
- `writeup_checkpoint.json`
- `latex/checkpoints/*.tex`
- `notes.txt`
- `artifacts/upload_manifest.json`
- `workflow_state.json`
- `workflow_idea.json`

### 等价要求
未来升级后，至少应保留这些工件的存在语义；若路径变化，应提供兼容层或映射层。

---

## 16. 行为不变型重构边界

以下内容允许优化：

- 内部模块拆分
- Agent / Skill / MCP 引入
- 可视化前端接入
- 状态追踪增强
- 文献管理器增强
- 云任务管理增强
- 图表/架构图工具增强
- schema 显式化
- workspace 锁机制补强

以下内容 **第一阶段不允许改变**：

1. phase 名称与顺序
2. scientist 主线的 review / improvement / re-review 语义
3. writeup 内部状态机
4. 轮次控制语义
5. 当前声明机制
6. 阶段 PDF 里程碑
7. checkpoint/resume 语义
8. all 模式中 cloud 的插入顺序
9. 统一入口兼容面（CLI > ENV > default、dry-run、配置打印）
10. scientist 的跨 idea 并行、单 workspace 单写者假设

---

## 17. 推荐的等价升级策略

### 17.1 第一阶段目标
做 **behavior-preserving refactor**：

- 现有函数行为不变
- 仅将单步动作抽象为 skill
- 将调度外提为 orchestrator/agent
- 将外部能力抽为 MCP
- 将状态暴露给前端
- 先冻结 schema 与写权限边界
- 先补 single-writer workspace lock

### 17.2 推荐分层

#### Orchestrator / Agent 层
保留现有状态机与轮次逻辑。

#### Skill 层
承接单轮原子动作，例如：
- 写某一节
- refine 某一节
- 查 citation 缺口
- 选择 citation
- 修复 latex error
- 注入 / 检查声明区块

#### MCP 层
承接外部世界连接，例如：
- 文献检索
- 远程服务器任务
- 文件上传下载
- 架构图生成
- 文献管理器

#### Frontend 层
仅作为可视化观察与人工触发界面，不替换流程控制权。

---

## 18. 后续设计文档建议

当前文档配套情况如下：

1. `docs/paperforge-equivalence-matrix.md`
   - 已存在
   - 用于行为等价验收矩阵
   - 按 phase / state / artifact / resume / env precedence 验收

2. `docs/paperforge-json-artifact-contracts.md`
   - 已存在
   - 用于显式 schema 与文件契约说明

3. `docs/paperforge-single-writer-locking.md`
   - 已存在
   - 用于 workspace 锁与重入策略

---

## 19. 最终结论

PaperForge 当前系统不是简单的“研究 → 写作 → 评审 → 修订”概念流程，而是：

- 具有 **明确阶段状态机** 的 MVP workflow
- 具有 **完整 scientist 主线** 的全自动 workflow
- 具有 **明确内部状态机** 的 writeup engine
- 具有 **review / improvement / re-review** 链路
- 具有 **多轮 citation / refine / latex_fix** 控制
- 具有 **许可证合规注入与保护机制**
- 具有 **cloud → feedback 回填闭环**
- 具有 **隐式数据契约与单写者假设**

因此，后续优化必须采用：

> **先冻结契约与单写者边界，再做等价重构，最后再做能力增强**

否则极易在引入 Agent / Skill / MCP / Frontend 后丢失现有流程细节，或破坏当前隐式但关键的数据一致性与单写者语义。

本文件即为后续所有升级工作的基准线。
