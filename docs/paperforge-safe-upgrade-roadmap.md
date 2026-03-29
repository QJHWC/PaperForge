# PaperForge 安全升级路线图

## 1. 文档目的

本文档定义 PaperForge 的 **安全升级路线图**，目标是在 **行为等价** 的前提下，逐步升级到：

- Agent 调度
- Skill 资产化
- MCP 外部能力化
- 可视化前端
- 文献管理器
- 云端/服务器托管增强
- 架构图生成能力
- AIGC / 降重评估能力

本文档默认以前两份文档为前提：

- `docs/paperforge-workflow-equivalence-spec.md`
- `docs/paperforge-agent-skill-mcp-mapping.md`

---

## 2. 总体升级原则

### 2.1 先冻结契约，再等价，后增强
升级顺序必须是：

1. **契约冻结与验收基线建立**
2. **单写者保护层**
3. **行为等价封装**
4. **能力抽象与资产化**
5. **外围能力接入**
6. **产品化与可视化**
7. **高级增强能力**

### 2.2 禁止“一步到位重写”
不允许直接：

- 重写整个 workflow
- 用一个总 prompt 替代当前多阶段流程
- 用自由自治 agent 替代现有状态机
- 在未建立兼容层前废弃环境变量语义
- 在未建立工件兼容层前修改阶段产物命名

### 2.3 每阶段都必须可回退
每个阶段的升级必须支持：

- 可独立验证
- 可快速回滚
- 不破坏既有 CLI
- 不破坏既有文档与工件路径语义

---

## 3. 升级目标分层

### 3.1 核心目标
- 保持 phase 完全一致
- 保持 writeup 内部状态机完全一致
- 保持多轮 citation / refine / latex_fix 完全一致
- 保持许可证合规机制完全一致

### 3.2 增强目标
- Skill 规范化
- MCP 接入
- 前端控制台
- 文献管理器
- 图/架构图生成
- AIGC / 降重评估
- 远程服务器管理增强

---

## 4. 阶段化路线图总览

## P0：冻结当前行为基线与范围

### 目标
先定义“什么不能变”，并把范围定义闭合到：

- `scientist`
- `mvp`
- `writeup`
- 统一入口兼容面
- 隐式 JSON / artifact / notes block 契约

### 产物
- `paperforge-workflow-equivalence-spec.md`
- `paperforge-agent-skill-mcp-mapping.md`
- `paperforge-equivalence-matrix.md`
- `paperforge-json-artifact-contracts.md`（已存在）
- `paperforge-single-writer-locking.md`（已存在）
- 本路线图文档

### 完成标准
- 已明确 scientist / mvp / writeup 的行为边界
- 已明确 CLI 兼容面
- 已形成可执行验收矩阵
- 已在总文档层冻结关键 JSON / artifact 契约边界
- 契约文档与锁策略文档进入紧后续补全文档清单

### 风险
- 如果不先冻结基线，后续升级很容易“看上去更强，实际上流程跑偏”

---

## P1：single-writer 保护层

### 目标
在前端、OpsAgent、MCP 接入前，先补齐当前系统隐含但关键的单写者假设。

### 核心动作
1. 引入 workspace lock / lease
2. 明确锁粒度（建议 workspace 粒度）
3. 定义重入策略
4. 定义 stale lock 恢复策略
5. 明确哪些入口必须先获取锁：
   - mvp phase
   - writeup
   - feedback ingest
   - cloud sync backfill
   - 前端触发动作

### 完成标准
- 同一 workspace 不能被多个写入口同时修改
- 前端 / CLI / OpsAgent 统一走锁门禁
- scientist 的跨 idea 并行继续允许，但不得共享写一个 idea 目录

### 不能做
- 前端绕过 lock 直接写盘
- MCP 直接写 `template.tex` / `workflow_state.json`
- 多入口并发覆盖同一 workspace

### 价值
这是前端、OpsAgent、MCP 接入前的必要门槛。

---

## P2：等价封装层（最重要）

### 目标
在 **不改变外部行为** 的前提下，引入未来架构的壳层。

### 核心动作
1. 引入 `PaperForgeCoordinator`
2. 引入 `ScientistWorkflowAgent`
3. 引入 `MvpWorkflowAgent`
4. 引入 `WriteupAgent`
5. 将现有函数包裹到 Agent 中
6. 保留现有 CLI 入口不变
7. 保留现有 phase / state / artifact 命名不变
8. 保留 `launch_user_entry.py` 的兼容面不变：
   - CLI → ENV → default 优先级
   - 协议路由
   - `dry-run`
   - 配置打印

### 推荐做法
- 旧函数先继续存在
- 新 agent 先只是调用旧函数
- 不急于立刻拆 skill
- 不急于立刻做 MCP 化

### 完成标准
- 用户从 CLI 看，行为等价
- scientist / mvp 入口语义一致
- 统一入口兼容面一致
- 输出工件集合一致
- checkpoint / review / improvement 语义一致

### 不能做
- 改 phase 顺序
- 改 writeup 内部逻辑
- 改 env 变量含义
- 改 checkpoint 结构

### 价值
这是后续一切升级的“稳定壳层”。

---

## P3：Writeup 原子技能化

### 目标
将 `perform_writeup.py` 的内部单步动作逐步 skill 化，但保持 `WriteupAgent` 状态机不变。

### 核心动作
逐步抽离这些 skill：

- `write-title-abstract-skill`
- `refine-abstract-skill`
- `write-section-skill`
- `refine-section-skill`
- `sketch-related-work-skill`
- `citation-gap-skill`
- `citation-select-skill`
- `citation-merge-skill`
- `latex-chktex-fix-skill`
- `latex-sanitize-skill`

### 推荐策略
- 先做“内部 skill”，不直接暴露给用户编排
- `WriteupAgent` 继续严格控制：
  - init
  - cite
  - refine
  - latex_fix
  - done

### 完成标准
- skill 替代旧实现中的部分原子步骤
- 但整体 writeup 产出不变
- checkpoint 仍可恢复
- round 控制仍由 agent 掌握

### 风险
- 如果过早把 round control 下放给 skill，会破坏等价性

---

## P4：Prompt Library 资产化

### 目标
把 `prompt_library.py` 中已有的关键能力，整理为可复用的策略资产。

### 核心动作
建立 `Skill Policy Registry`，优先纳入：

#### 写作/润色类
- SCI论文润色
- 直接润色段落
- 润色英文段落结构和句子逻辑
- 逻辑论证辅助

#### 审查/诊断类
- 论文评审专家
- 语法检查/查找语法错误

#### 降重/自然化类
- 内容降重
- 改写降重
- 同义词替换降重
- 避免连续相同
- 缩写扩写降重
- 关键词汇替换降重
- 句式变换降重
- 逻辑重组
- 综合改写
- 概念解释降重

### 第一阶段要求
- 先资产化，不改主流程
- 先作为 writeup / refine 的 policy 选项
- 不新增破坏现流程的新 phase

### 完成标准
- prompt 资产不再仅仅是平铺字典
- 可以被 skill 选择性引用
- 可按场景配置：academic / reviewer / de-aigc / similarity-reduction

---

## P5：MCP 外部能力接入

### 目标
把“连接外部世界”的能力从本地代码中逐步剥离成标准化工具接口。

---

## P5.1 文献检索 MCP

### 优先级
最高。

### 接入目标
- OpenAlex
- Semantic Scholar
- DOI / arXiv 元数据
- BibTeX 获取

### 第一阶段
- 替换 citation search 的外部能力接入层
- 不改变 cite round 流程

### 完成标准
- citation phase 仍然按原逻辑运行
- 只是搜索与元数据获取改由 MCP 提供

---

## P5.2 文件管理 / 上传下载 MCP

### 接入目标
- 上传文件
- 下载文件
- 目录管理
- 快照归档

### 适配目标
- feedback 上传
- cloud 同步产物
- 远程结果回填

---

## P5.3 远程运行 MCP

### 接入目标
- 上传代码到服务器
- 启动作业
- 拉取日志
- 下载结果
- 作业状态查询

### 与现有模块对应
- `remote_runner.py`
- `run_cloud_pipeline_cycle.py`
- `sync_cloud_results_to_uploads.py`

### 说明
第一阶段只替换能力接入方式，不改变 `cloud` phase 语义。

---

## P5.4 架构图 MCP

### 接入目标
- Mermaid 架构图生成
- 工作流图生成
- 方法流程图生成
- draw.io / SVG 导出

### 说明
这是增强项，可直接作为新工具接入，不会影响现有 phase。

---

## P5.5 文献管理器 MCP

### 接入目标
- Bib 管理
- 引文去重
- 章节-引用映射
- 引用建议
- PDF / Bib 导入

### 注意
第一阶段作为旁路能力，不应替换 cite phase 的决策语义。

---

## P5.6 AIGC / 降重评估 MCP

### 接入目标
- 机器味风险评估
- 文本重复风险评估
- 模板化表达诊断
- 改写前后差异评分

### 注意
第一阶段建议：
- 先做评估
- 后做闭环改写
- 更不能直接替代 writeup/refine 主流程

---

## P6：可视化前端控制台

### 目标
让当前系统的状态、工件、trace 与参数可视化。

### 首批推荐页面

#### 1. Workflow Dashboard
展示：
- 当前 project
- 当前 phase
- 当前 writeup stage
- 当前轮次
- 关键工件
- 状态摘要

#### 2. Writeup Trace Viewer
展示：
- init/cite/refine/latex_fix 执行记录
- 每轮输入输出摘要
- checkpoint 恢复信息

#### 3. Artifact Center
展示：
- PDF 产物
- notes.txt
- upload manifest
- checkpoint snapshots

#### 4. Cloud Console
展示：
- 远程任务状态
- 上传/下载情况
- 同步结果

#### 5. Reference Panel
展示：
- 当前引用列表
- 新增 bibtex
- citation rounds 记录

### 第一阶段要求
- 前端只做观察、触发、审计
- 不接管主流程决策

---

## P7：文献管理器正式融入主流程

### 目标
在不破坏 cite phase 的情况下，把文献管理器真正纳入 PaperForge 主链路。

### 融入点
- bootstrap：初始化文献池
- feedback：融合用户上传文献
- optimize：按实验变化补文献
- refine：检查 claim-evidence 是否缺引文

### 输出
- BibTeX 管理
- citation 缺口列表
- related work 候选文献
- 引用去重报告

---

## P8：AIGC / 降重能力闭环接入

### 目标
将当前已有 prompt 资产产品化，但不破坏现有 writeup 状态机。

### 推荐策略
新增 **旁路闭环**，而不是主线替代：

```text
writeup/refine 输出
→ de-aigc-eval
→ similarity-eval
→ 若启用增强模式，则进入 de-aigc-rewrite skill 链
→ consistency-guard
→ 回写结果
```

### 关键原则
- 不直接替代 writeup 主线
- 不修改核心实验事实
- 不改动引用编号、术语、数据
- 必须保留原始稿和改写稿 diff

### 推荐子链
1. similarity diagnosis
2. sentence-structure rewrite
3. logic regroup
4. humanization pass
5. consistency verification

---

## P9：服务器托管与生产级工作台

### 目标
实现你提到的：
- 自动上传下载服务器
- 与“龙虾”类文件/任务系统结合
- 生产级工作流

### 推荐实现方式
#### 底层
- File Gateway
- Remote Job Gateway

#### 上层
- MCP 暴露标准接口

#### 调度层
- OpsAgent 使用 MCP 调用

### 价值
- 文件流标准化
- 多服务器适配
- 更强的失败恢复能力
- 可视化任务追踪

---

## 5. 每阶段的验收标准

## P0 验收
- scientist / mvp / writeup 范围定义闭合
- 已形成 phase / artifact / state / resume / env precedence 的验收矩阵
- 已冻结关键 JSON / artifact / notes block 契约

## P1 验收
- 同一 workspace 强制 single-writer
- scientist 保持跨 idea 并行，但不允许共享写一个 idea 目录
- 前端 / CLI / OpsAgent / MCP 写入口统一受锁控制

## P2 验收
- CLI 兼容面不变
- scientist / mvp / writeup 行为等价
- 工件集合等价
- checkpoint / review / improvement 语义等价
- 不以“PDF 字节完全相同”作为唯一标准

## P3 验收
- writeup 内部 skill 化
- 轮次与顺序不变
- cite/refine/latex_fix 行为不变

## P4 验收
- prompt 资产可结构化引用
- 关键降重/逻辑增强 prompt 不丢失

## P5 验收
- MCP 接入后不改变阶段决策
- 文献、文件、远程、图表能力可插拔

## P6 验收
- 前端能看清流程、轮次、工件、trace
- 不改变主流程控制权
- 不绕过锁直接写盘

## P7/P8/P9 验收
- 增强能力可选启用
- 默认模式下仍保持当前行为等价

---

## 6. 风险清单

### 风险 1：过早自治化
如果过早引入“自由 agent 决策”，容易破坏当前 phase / round 的严格顺序。

### 风险 2：Skill 过大
如果把多个步骤混成一个 skill，会失去 checkpoint、round control、可视化能力。

### 风险 3：MCP 直接接管主逻辑
MCP 应是工具，不应替代 orchestrator。

### 风险 4：前端越权
前端如果直接修改状态机，容易让行为与 CLI 跑法不一致。

### 风险 5：数据契约漂移
如果在 schema 冻结前就开始 Skill / MCP / Frontend 接入，各模块很容易各自理解一套 `workflow_state.json`、`upload_manifest.json`、`notes.txt` block 语义。

### 风险 6：降重能力过早接主链
如果把 de-aigc / similarity-reduction 直接塞进当前主流程，容易改变现有 writeup 输出语义。

### 风险 7：声明 guard 逻辑丢失
当前声明机制相关逻辑如果在重构中遗漏，属于严重风险。

---

## 7. 技术组织建议

### 推荐新增目录
```text
docs/
agents/
skills/
mcp_servers/
schemas/
services/
frontend/
```

### 推荐模块角色
- `agents/`：状态机、调度
- `skills/`：单轮原子能力
- `mcp_servers/`：外部能力桥接
- `schemas/`：配置、状态、artifact schema
- `services/`：编译、checkpoint、合规、文件服务
- `frontend/`：控制台

---

## 8. 推荐最近两步最优先事项

基于当前阶段，最值得马上进入实现的是：

### 优先事项 1
做 **P0 + P1**
- 按等价验收矩阵执行实施前检查
- 冻结 JSON / artifact / notes 契约
- 建立 single-writer workspace lock / lease

### 优先事项 2
做 **P2 等价封装层**
- 建立 `PaperForgeCoordinator`
- 建立 `ScientistWorkflowAgent`
- 建立 `MvpWorkflowAgent`
- 建立 `WriteupAgent`
- 先包旧逻辑

这两步做完后，整个项目才真正进入“可升级但不跑偏”的状态。

---

## 9. 最终结论

PaperForge 的升级必须走一条非常明确的路线：

> **冻结契约 → single-writer 保护 → 等价封装 → 技能抽离 → 外部能力工具化 → 可视化 → 高级增强**

而不能走：

> **直接重写成一个看起来更现代但流程失真的 Agent 平台**

只要严格按照本文路线图推进，就可以在保留现有完整流程的前提下，逐步实现你想要的目标：

- Agent 主调度
- Skill 资产沉淀
- MCP 扩展外部能力
- 文献管理器
- 自动上传下载服务器
- 架构图生成
- AIGC / 降重持续优化
- 可视化生产控制台

同时仍然做到：

- 与当前流程一模一样
- 与当前轮次控制一模一样
- 与当前 PDF / checkpoint / notes / cloud 逻辑一模一样
- 与当前许可证合规机制一模一样
