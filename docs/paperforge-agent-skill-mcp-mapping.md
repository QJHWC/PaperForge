# PaperForge Agent / Skill / MCP 等价映射设计

## 1. 文档目的

本文档用于在 **不改变当前 PaperForge 外部行为** 的前提下，将现有实现映射到未来的：

- Agent
- Skill
- MCP
- Frontend

架构中。

本文档的目标不是重新设计一套新流程，而是回答以下问题：

1. 现有哪些逻辑必须继续保留在流程编排层？
2. 现有哪些逻辑适合抽象为 Skill？
3. 现有哪些逻辑适合 MCP 化？
4. 未来前端应该观察什么，而不应该夺取什么控制权？
5. 怎样做到升级后行为和当前版本一模一样？

---

## 2. 设计原则

### 2.1 第一原则：流程不变
保留当前：

- MVP phase 状态机
- writeup 内部状态机
- 轮次控制参数
- 关键工件
- 许可证合规机制

### 2.2 第二原则：单步技能化，流程编排化
后续架构中：

- **Skill 只负责单轮原子动作**
- **Agent / Orchestrator 负责阶段流转、轮次、停止条件、checkpoint**
- **MCP 负责外部能力连接**
- **Frontend 负责可视化、触发、审计**

### 2.3 第三原则：先等价，后增强
第一阶段仅允许做：

- 封装
- 拆分
- 抽象
- 可观测性增强

不允许做：

- 改 phase 顺序
- 改 writeup 内部顺序
- 改环境变量语义
- 改终稿合规策略

---

## 3. 当前系统的逻辑分层

从现有代码看，当前系统逻辑可以粗分为四层：

### 3.1 Entry Layer
文件：
- `launch_user_entry.py`

职责：
- 参数路由
- API 配置
- 模型配置
- CLI → ENV → default 优先级处理

### 3.2 Workflow Layer
文件：
- `launch_mvp_workflow.py`
- `launch_scientist.py`

职责：
- scientist / mvp 双主线控制
- 执行顺序控制
- 阶段级参数
- phase / review / improvement 语义控制

### 3.3 Engine Layer
文件：
- `engine/perform_writeup.py`
- `engine/mvp_workflow.py`
- 相关实验 / review / remote 模块

职责：
- 真实业务逻辑执行
- 多轮迭代
- checkpoint
- 文件产物生成

### 3.4 Prompt / Policy Layer
文件：
- `prompt_library.py`
- `skills/research-writing-prompts/SKILL.md`

职责：
- 风格策略
- 逻辑增强策略
- 降重 / 去 AI 味策略
- reviewer / rewrite / citation 辅助

---

## 4. 未来目标分层

未来推荐结构：

- **Orchestrator / Agent Layer**
- **Skill Layer**
- **MCP Tool Layer**
- **Frontend / Trace Layer**

---

## 5. 哪些逻辑必须保留在 Agent / Orchestrator 层

这一部分非常关键。以下逻辑 **不能下沉到 Skill**，否则流程会失真。

同时，本文档的范围不再仅限 MVP/writeup，而是覆盖：

- `scientist` 主线
- `mvp` 主线
- `writeup` 内部状态机
- 统一入口兼容面
- 单 workspace 单写者语义

---

## 5.1 scientist / MVP 双主线状态机

当前系统存在两条一级 workflow：

### scientist
- idea generation
- novelty check
- experiment
- writeup
- review
- optional improvement
- re-review

### mvp
- `bootstrap`
- `feedback`
- `optimize`
- `refine`
- `cloud`
- `all`

未来这两条 workflow 都必须继续由 Orchestrator 管控，而不是某个单独 skill 决定。

### 原因
这是整个系统最外层行为语义，涉及：

- 工作区/结果目录创建
- 上传回填
- 实验循环
- review / improvement
- 云端同步
- PDF 里程碑产物

### 建议对应
- `PaperForgeCoordinator`
- `ScientistWorkflowAgent`
- `MvpWorkflowAgent`

---

## 5.2 scientist 与 `all` 模式中的固定顺序

### scientist
固定保持：

1. idea generation
2. novelty check
3. experiments
4. writeup
5. review
6. optional improvement
7. re-review

### mvp `all`
固定保持：

1. bootstrap
2. optional cloud
3. feedback
4. optimize
5. refine

这些顺序必须由 Orchestrator 明确编码。

不能交给 skill 自行“选择阶段”。

---

## 5.3 Writeup 内部状态机

当前 writeup 状态：

- `start`
- `init`
- `cite`
- `refine`
- `latex_fix`
- `done`

必须由专门的 `WriteupAgent` 控制。

### 原因
writeup 的复杂度不在某个 prompt，而在：

- 阶段控制
- checkpoint 恢复
- 多轮 citation
- second refinement 开关
- latex_fix 循环
- 终止条件

这些全部属于 orchestration 逻辑，不属于 skill。

---

## 5.4 轮次控制与停止条件

以下逻辑必须保留在 Agent：

- `WRITEUP_CITE_ROUNDS`
- `WRITEUP_LATEX_FIX_ROUNDS`
- `WRITEUP_SECOND_REFINEMENT`
- checkpoint enable / reset
- optimize_runs
- cloud run / sync 开关

原因：
这些是系统级行为，不是单轮技能行为。

---

## 5.5 工件命名与阶段里程碑

以下产物的生成时机应由 Agent 统一管理：

- `paper_mvp_draft.pdf`
- `paper_with_feedback.pdf`
- `paper_after_optimize.pdf`
- `paper_refined.pdf`

Skill 不应该直接决定“当前是否是某阶段最终 PDF”。

---

## 5.6 合规控制与单写者控制

以下能力必须属于 Agent 或系统 Guard：

- 声明区块注入
- 声明区块不得删除
- 导出前合规校验
- single-writer workspace lock / lease
- 重入检测
- stale lock 恢复策略

原因：
这是全局硬约束，不应由下游 skill 自治。

---

## 6. 哪些逻辑适合拆成 Skill

Skill 的定义标准：

> 单轮、单职责、可复用、可替换、不掌握全局阶段流转。

---

## 6.1 写作类 Skill

### A. `write-title-abstract-skill`
对应当前：
- init 阶段中 Title + Abstract 首稿生成

### B. `refine-abstract-skill`
对应当前：
- Abstract 初稿后的 refinement

### C. `write-section-skill`
输入：
- section 名
- notes
- template
- style policy

输出：
- 单节草稿

用于：
- Introduction
- Background
- Method
- Experimental Setup
- Results
- Conclusion

### D. `refine-section-skill`
输入：
- 当前 section
- error list
- style policy

输出：
- 精修后的 section

### E. `sketch-related-work-skill`
对应当前：
- Related Work 注释式草图生成

### F. `refine-related-work-skill`
对应当前：
- cite rounds 完成后 Related Work 再精修

### G. `retitle-skill`
对应当前：
- second refinement 阶段的 title rethink

---

## 6.2 Citation 类 Skill

### A. `citation-gap-skill`
输入：
- 当前 draft
- 当前轮次
- 总轮次

输出：
- 最重要 citation 缺口
- query

对应当前：
- `citation_first_prompt`

### B. `citation-select-skill`
输入：
- 搜索结果
- draft context

输出：
- 选择的文献
- 描述如何插入正文

对应当前：
- `citation_second_prompt`

### C. `citation-merge-skill`
输入：
- bibtex
- description
- current tex

输出：
- 融合后的 tex 修改

对应当前：
- 由 aider/coder 完成的 citation 融合动作

---

## 6.3 LaTeX 修复类 Skill

### A. `latex-chktex-fix-skill`
输入：
- chktex 输出
- template.tex

输出：
- 最小修复后的 template.tex

### B. `latex-sanitize-skill`
输入：
- template.tex

输出：
- 规范化后的 template.tex

包含：
- author block 清洗
- 声明区块补全
- blocked citations 过滤

---

## 6.4 Notes / 回填类 Skill

### A. `ingest-upload-feedback-skill`
对应当前：
- 上传文件清单整理
- 反馈提取

### B. `refresh-notes-skill`
对应当前：
- 将实验反馈或上传反馈写回 notes

### C. `literature-refresh-skill`
对应当前：
- 文献刷新到 notes

---

## 6.5 降重 / 风格类 Skill

虽然当前降重策略尚未形成独立运行阶段，但在未来结构中应沉淀为 skill 资产。

推荐拆分为：

### A. `style-policy-skill`
- 学术风格规范化
- 语言简洁化
- 反模板化表达

### B. `logic-refine-skill`
- 段落逻辑梳理
- 论证增强
- 句间衔接优化

### C. `de-aigc-rewrite-skill`
- 去 AI 味
- 句法变化
- 表达自然化

### D. `similarity-reduction-skill`
- 降重
- 避免连续重复
- 同义改写与语序重构

### E. `consistency-guard-skill`
- 改写前后语义一致性校验
- 引文与术语保护

### 注意
第一阶段中，这些 skill 可以先作为内部 policy 技能，不应直接新增为破坏现流程的新 phase。

---

## 7. 哪些逻辑适合 MCP 化

MCP 的定义标准：

> 与外部世界交互、具有明确输入输出、可复用、与主业务逻辑解耦。

---

## 7.1 文献检索 MCP

推荐 MCP：
- OpenAlex
- Semantic Scholar
- DOI / arXiv 元数据抓取

可承接当前：
- `search_for_papers()`

未来目标：
- citation 检索
- 文献管理器
- 自动生成 BibTeX
- 去重
- 按章节推荐文献

---

## 7.2 文件管理 MCP

推荐职责：
- 上传文件
- 下载文件
- 列出目录
- 版本快照
- 产物归档

可承接当前：
- uploads 回填
- cloud 结果同步
- 远程产物拉取

---

## 7.3 远程任务 MCP

推荐职责：
- 上传代码到服务器
- 启动训练
- 查询状态
- 下载结果
- 增量同步

可承接当前：
- `remote_runner.py`
- `run_cloud_pipeline_cycle.py`
- `sync_cloud_results_to_uploads.py`

---

## 7.4 架构图 / 图表 MCP

推荐职责：
- 方法架构图生成
- workflow 图生成
- Mermaid / draw.io / SVG 导出
- 图标题建议

这部分是增强能力，不改变现有主流程。

---

## 7.5 文献管理器 MCP

推荐职责：
- 管理本地文献库
- BibTeX 导入导出
- 引用去重
- 章节级引用映射
- “当前稿件缺哪些引用”

这是未来升级的重点，但第一阶段应作为旁路增强，不改变 cite phase 逻辑。

---

## 7.6 AIGC / 降重评估 MCP

推荐职责：
- 机器味诊断
- 重复风险诊断
- 模板化表达扫描
- 长句密度分析
- 改写前后差异评分

第一阶段可先做评估，不直接接管 writeup 主流程。

---

## 8. 前端层的职责边界

前端应承担：

- 状态展示
- 进度展示
- 工件展示
- trace 展示
- 参数编辑
- phase / run 触发

前端不应在第一阶段承担：

- 决定阶段顺序
- 决定是否跳过某轮 citation
- 决定是否跳过 latex_fix
- 修改合规规则
- 绕过 workspace lock 直接写文件
- 绕过 Agent 直接修改 `workflow_state.json` / `template.tex`

即：

> 前端是控制台，不是流程引擎，也不是直接写盘者。

---

## 9. 现有模块到未来架构的映射

## 9.1 `launch_user_entry.py`

### 当前职责
- 统一入口
- API 配置
- 模型配置

### 未来映射
- `EntryController`
- `ConfigResolver`
- `ModelRoutingConfig`

### 保留点
- CLI > ENV > default 优先级
- scientist / mvp 双入口语义

---

## 9.2 `launch_mvp_workflow.py`

### 当前职责
- phase 级编排

### 未来映射
- `MvpWorkflowAgent`
- `WorkflowStateMachine`

### 保留点
- 各 phase 顺序
- all 模式语义
- profile 覆盖策略

---

## 9.3 `engine/perform_writeup.py`

### 当前职责
- 写作状态机
- checkpoint
- citation loop
- refinement loop
- latex fix

### 未来映射
- `WriteupAgent`
- 多个细粒度 skill
- 合规 guard
- compile service

### 保留点
- init/cite/refine/latex_fix/done 顺序
- checkpoint 语义
- 环境变量控制语义

---

## 9.4 `prompt_library.py`

### 当前职责
- 风格与写作策略库
- 降重 / 润色 / reviewer 等资产

### 未来映射
- `Skill Policy Registry`
- `Prompt Strategy Library`

### 保留点
- 关键降重策略
- 关键逻辑增强策略
- 关键 reviewer / rewrite 策略

---

## 10. 推荐的未来 Agent 结构

为保证等价，建议不是直接做很多自由自治 Agent，而是采用 **主从式 Agent** 结构。

---

## 10.1 `PaperForgeCoordinator`
系统总调度器。

职责：
- 选择 scientist / mvp 模式
- 初始化 config
- 启动 workflow agent
- 汇总 trace / artifact

---

## 10.2 `ScientistWorkflowAgent`
负责 scientist 主线状态机。

职责：
- idea generation / novelty / experiments / writeup / review / improvement / re-review
- 保留 scientist 全自动主线语义
- 保留 `--improvement` 显式开关语义
- 保留跨 idea 并行、idea 目录隔离语义

---

## 10.3 `MvpWorkflowAgent`
负责 mvp phase 状态机。

职责：
- bootstrap / feedback / optimize / refine / cloud / all
- 保留所有阶段语义
- 调用 writeup agent / experiment / cloud / upload 流程

---

## 10.4 `WriteupAgent`
负责 writeup 内部状态机。

职责：
- init
- cite
- refine
- latex_fix
- done
- checkpoint 恢复

---

## 10.5 `ComplianceGuardAgent`
负责：
- 声明区块注入
- 声明区块校验
- 导出前许可证检查

它可以不是独立 agent，也可先作为系统 guard 模块实现。

---

## 10.6 `WorkspaceLockManager`
负责：
- workspace 级锁/租约
- 单写者保证
- 重入检测
- stale lock 恢复
- 前端 / CLI / OpsAgent 的统一接入门禁

它也可以先作为服务模块实现，而非独立 agent。

---

## 10.7 `OpsAgent`
负责：
- 文件上传下载
- 云任务状态查询
- 远程结果回填

它适合与 MCP 结合。

---

## 11. 推荐的 Skill 目录设计

未来建议按“单轮能力”组织：

```text
skills/
  write-title-abstract/
  write-section/
  refine-section/
  sketch-related-work/
  citation-gap/
  citation-select/
  citation-merge/
  latex-chktex-fix/
  latex-sanitize/
  notes-refresh/
  upload-ingest/
  style-policy/
  logic-refine/
  de-aigc-rewrite/
  similarity-reduction/
  consistency-guard/
```

每个 skill 建议包含：

- `SKILL.md`
- `schema.json`
- `examples/`
- `policies/`

---

## 12. 推荐的 MCP 目录设计

```text
mcp_servers/
  literature/
  file-gateway/
  remote-runner/
  diagram/
  bibliography/
  aigc-eval/
```

---

## 13. 第一阶段不允许做的错误映射

为了避免“看起来升级，实则改坏流程”，以下做法应禁止：

### 错误 1
把一个大 skill 写成：
- 写作
- 审查
- 降重
- 修订
- 定稿

一次全部做完。

### 错误 2
让前端直接控制是否跳过 cite/refine/latex_fix 核心逻辑。

### 错误 3
让 MCP 直接接管 phase 决策。

### 错误 4
把 checkpoint 逻辑删掉，仅保留“重新运行”。

### 错误 5
把当前环境变量语义完全替换掉，不保留兼容层。

---

## 14. 推荐的迁移顺序

### 阶段 0：冻结契约
- 冻结 scientist / mvp / writeup 行为基线
- 冻结 JSON / artifact / notes block 契约
- 明确写权限边界

### 阶段 1：单写者保护层
- 引入 workspace lock / lease
- 所有入口统一经过 lock 管理
- 前端触发尚不可绕过此层

### 阶段 2：等价封装
- 保留旧逻辑
- 包一层 agent/orchestrator
- skill 先内部调用，不对外自由编排

### 阶段 3：能力抽离
- 文献、文件、云任务、图表逐步 MCP 化
- prompt library 逐步 skill 化

### 阶段 4：可视化控制台
- 展示 trace / 状态 / artifact
- 支持人工重跑某阶段

### 阶段 5：增强能力
- 文献管理器
- 架构图生成
- AIGC / 降重诊断
- 服务器托管体系

---

## 15. 最终结论

PaperForge 的未来升级，不应该是：

> “把当前代码改写成一个全新的 agent 系统”

而应该是：

> “用 Agent 复刻当前状态机，用 Skill 复刻当前单轮动作，用 MCP 替换外部依赖连接，用前端把现有流程可视化。”

也就是：

- **流程控制权仍在 Orchestrator / Agent**
- **Skill 负责原子写作能力**
- **MCP 负责外部世界能力**
- **Frontend 负责可观测和可操作**

只有这样，才能在升级后继续保证：

- phase 一模一样
- writeup 一模一样
- 多轮 citation / refine / latex_fix 一模一样
- 当前声明机制一模一样
- 关键 PDF 工件一模一样

本文档可作为下一步安全升级路线图的设计输入。
