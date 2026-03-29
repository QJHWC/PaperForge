# PaperForge 行为等价验收矩阵

## 1. 文档目的

本文档将“行为等价”从原则文字转成可执行验收矩阵，用于评估升级后系统是否仍与当前 PaperForge 保持一致。

本文档不要求：

- PDF 字节完全一致
- LLM 输出逐句完全一致
- 检索结果完全一致
- 时间戳或目录名完全一致

本文档要求：

- 状态机一致
- 控制参数语义一致
- 工件集合一致
- checkpoint / resume 语义一致
- scientist / mvp / writeup 主线一致
- 统一入口兼容面一致
- 合规机制一致
- 单写者语义一致

---

## 2. 等价判定方法

每一项验收按以下三类结果判定：

- **PASS**：行为等价成立
- **PARTIAL**：基本成立，但存在可接受差异，需记录
- **FAIL**：行为等价不成立，必须修复

---

## 3. 一级范围矩阵

| 范围 | 当前基线 | 升级后要求 | 验收类型 |
| --- | --- | --- | --- |
| 统一入口 | `launch_user_entry.py` | scientist / mvp 入口语义不变 | 必测 |
| scientist 主线 | `launch_scientist.py` | idea→novelty→experiment→writeup→review→optional improvement→re-review 不变 | 必测 |
| mvp 主线 | `launch_mvp_workflow.py` | bootstrap→feedback→optimize→refine→cloud/all 不变 | 必测 |
| writeup 状态机 | `perform_writeup.py` | init→cite→refine→latex_fix→done 不变 | 必测 |
| JSON / artifact 契约 | 多文件隐式约定 | 字段、路径、更新语义不漂移 | 必测 |
| 合规 | 当前声明机制 | 注入、保护、导出前校验不变 | 必测 |
| 单写者 | 当前隐式假设 | 新系统显式保证 | 必测 |

---

## 4. 统一入口兼容面矩阵

| 编号 | 项目 | 当前行为 | 升级后验收标准 |
| --- | --- | --- | --- |
| E-01 | entry 类型 | 支持 `scientist` / `mvp` | 两种 entry 继续可用 |
| E-02 | 参数优先级 | CLI > ENV > default | 优先级完全一致 |
| E-03 | `--claude-protocol` | 支持 anthropic/openai 路由 | 路由语义一致 |
| E-04 | `--dry-run` | 只打印、不执行 | 行为一致 |
| E-05 | 配置打印 | 打印生效配置与命令 | 打印能力保留 |
| E-06 | stage model flags | idea/code/writeup/review 可独立指定 | 语义一致 |

### 验收说明
- 允许打印格式略有变化
- 不允许丢失字段或改变优先级逻辑

---

## 5. scientist 主线矩阵

| 编号 | 项目 | 当前行为 | 升级后验收标准 |
| --- | --- | --- | --- |
| S-01 | 模板校验 | 先校验模板文件完整性 | 必须保留 |
| S-02 | LaTeX 依赖校验 | writeup=latex 时检查依赖 | 必须保留 |
| S-03 | idea generation | 调用生成 ideas | 必须保留 |
| S-04 | novelty check | 可选执行 novelty check | `--skip-novelty-check` 语义不变 |
| S-05 | novel ideas 过滤 | 按 skip_novelty_check 不同策略过滤 | 过滤逻辑一致 |
| S-06 | 结果目录 | 每个 idea 独立目录 | idea 级目录隔离不变 |
| S-07 | notes 初始化 | 写入标题、实验描述、baseline | notes 初始化结构保留 |
| S-08 | experiments | 先 experiments 再 writeup | 顺序不变 |
| S-09 | writeup | experiments 成功后执行 | 顺序不变 |
| S-10 | review | writeup 后执行 review，产出 `review.txt` | 保留 |
| S-11 | improvement | `--improvement` 开启后执行 improvement | 显式开关语义不变 |
| S-12 | re-review | improvement 后必须再 review | 顺序不变 |
| S-13 | 并行 | 仅跨 idea 并行 | 不允许并发写同一 idea 目录 |

### scientist 工件验收
| 编号 | 工件 | 当前基线 | 升级后要求 |
| --- | --- | --- | --- |
| SA-01 | `ideas.json` | 存在 | 存在语义保留 |
| SA-02 | `<idea_folder>/notes.txt` | 存在 | 存在 |
| SA-03 | `<idea_folder>/<idea_name>.pdf` | 存在 | 存在 |
| SA-04 | `<idea_folder>/review.txt` | review 后存在 | 保留 |
| SA-05 | `<idea_folder>/<idea_name>_improved.pdf` | improvement 后存在 | 保留 |
| SA-06 | `<idea_folder>/review_improved.txt` | re-review 后存在 | 保留 |
| SA-07 | `log.txt` | log_file 模式下存在 | 能力保留 |

---

## 6. mvp 主线矩阵

| 编号 | 项目 | 当前行为 | 升级后验收标准 |
| --- | --- | --- | --- |
| M-01 | bootstrap | 创建 workspace、idea metadata、notes、upload interface、可选 run、plot、writeup | 语义一致 |
| M-02 | feedback | ingest uploads → notes 回填 → writeup | 顺序一致 |
| M-03 | optimize | 多轮 experiment → plot → notes → writeup | 顺序一致 |
| M-04 | refine | profile 控制 writeup 强度 | 语义一致 |
| M-05 | cloud | 调用 cloud cycle、远程运行/同步 | 语义一致 |
| M-06 | all 顺序 | bootstrap → optional cloud → feedback → optimize → refine | 顺序完全一致 |

### mvp 工件验收
| 编号 | 工件 | 当前基线 | 升级后要求 |
| --- | --- | --- | --- |
| MA-01 | `paper_mvp_draft.pdf` | bootstrap 后存在 | 保留 |
| MA-02 | `paper_with_feedback.pdf` | feedback 后存在 | 保留 |
| MA-03 | `paper_after_optimize.pdf` | optimize 后存在 | 保留 |
| MA-04 | `paper_refined.pdf` | refine 后存在 | 保留 |
| MA-05 | `workflow_state.json` | 存在 | 保留 |
| MA-06 | `workflow_idea.json` | 存在 | 保留 |
| MA-07 | `artifacts/upload_manifest.json` | feedback 流程会用到 | 保留 |

---

## 7. writeup 状态机矩阵

| 编号 | 项目 | 当前行为 | 升级后验收标准 |
| --- | --- | --- | --- |
| W-01 | 状态列表 | `start/init/cite/refine/latex_fix/done` | 必须保留 |
| W-02 | init | 逐节写作 + 逐节 refine | 语义一致 |
| W-03 | related work sketch | init 末尾仅 sketch | 保留 |
| W-04 | cite rounds | 多轮 citation augmentation | 语义一致 |
| W-05 | cite early stop | “No more citations needed” 时提前停止 | 语义一致 |
| W-06 | related work refine | cite 后再 refine 一次 | 保留 |
| W-07 | second refinement | 由 `WRITEUP_SECOND_REFINEMENT` 控制 | 保留 |
| W-08 | retitle | second refinement 前重思考 title | 保留 |
| W-09 | latex fix rounds | 多轮 `chktex` 修复 | 保留 |
| W-10 | compile order | `pdflatex → bibtex → pdflatex → pdflatex` | 顺序不变 |
| W-11 | checkpoint resume | 可从中间恢复 | 语义不变 |
| W-12 | done skip | 已 done 且 final pdf 存在时可跳过 | 语义不变 |

---

## 8. 环境变量与控制参数矩阵

| 编号 | 参数 | 当前含义 | 升级后要求 |
| --- | --- | --- | --- |
| C-01 | `WRITEUP_CITE_ROUNDS` | citation 轮数 | 含义不变 |
| C-02 | `WRITEUP_LATEX_FIX_ROUNDS` | latex fix 轮数 | 含义不变 |
| C-03 | `WRITEUP_SECOND_REFINEMENT` | 是否执行 second refinement | 含义不变 |
| C-04 | `WRITEUP_ENABLE_CHECKPOINT` | 是否启用 checkpoint | 含义不变 |
| C-05 | `WRITEUP_RESET_CHECKPOINT` | 是否清理旧 checkpoint | 含义不变 |
| C-06 | `PAPERFORGE_PROMPT_LIBRARY_PATH` | 外部 prompt library 路径 | 含义不变 |
| C-07 | `PAPERFORGE_AIDER_EDIT_FORMAT` | aider edit format | 含义不变 |
| C-08 | `--refine-profile fast/balanced/deep` | 覆盖 writeup profile | 映射语义不变 |
| C-09 | `--improvement` | scientist improvement 开关 | 语义不变 |
| C-10 | `--parallel` / `--gpus` | scientist 并行与 GPU 选择 | 语义不变 |

---

## 9. JSON / artifact 契约矩阵

| 编号 | 文件 | 最小契约 | 升级后要求 |
| --- | --- | --- | --- |
| J-01 | `workflow_idea.json` | `Name` / `Title` / `Experiment` | 字段保留或提供兼容映射 |
| J-02 | `workflow_state.json` | `updated_at` + phase runtime fields | 字段语义不漂移 |
| J-03 | `writeup_checkpoint.json` | `stage/current_round/latest_tex_file/updated_at` | 字段语义不漂移 |
| J-04 | `upload_manifest.json` | `timestamp/code_files/figure_files/paper_figure_files/user_notes` | 字段语义不漂移 |
| J-05 | `notes.txt` blocks | `AUTO:RUN_FEEDBACK` / `AUTO:LITERATURE` / `AUTO:UPLOAD_FEEDBACK` | block 语义保留 |

---

## 10. resume / recovery 矩阵

| 编号 | 场景 | 当前行为 | 升级后验收标准 |
| --- | --- | --- | --- |
| R-01 | writeup 中断后重跑 | 从 checkpoint 恢复 | 行为一致 |
| R-02 | checkpoint reset | 清理旧 checkpoint 后重新开始 | 行为一致 |
| R-03 | latex 阶段失败 | 可继续 resume latex_fix | 行为一致 |
| R-04 | scientist 某个 idea 失败 | 不影响其他 idea 目录 | 语义一致 |
| R-05 | workspace stale lock（新增） | 当前未显式保证 | 升级后必须可恢复 |

---

## 11. 单写者与并发矩阵

| 编号 | 项目 | 当前隐含语义 | 升级后验收标准 |
| --- | --- | --- | --- |
| L-01 | scientist 并行粒度 | 跨 idea 目录 | 保持 |
| L-02 | mvp workspace 写入 | 默认单写者 | 显式锁保证 |
| L-03 | 前端触发写操作 | 当前无前端 | 必须先获取 workspace lock |
| L-04 | OpsAgent 写操作 | 当前未接入 | 必须先获取 workspace lock |
| L-05 | MCP 写操作 | 当前未接入 | 不得绕过 lock |
| L-06 | 重入 | 当前未定义 | 必须有策略 |
| L-07 | stale lock | 当前未定义 | 必须有恢复机制 |

---

## 12. 当前声明机制矩阵

| 编号 | 项目 | 当前行为 | 升级后验收标准 |
| --- | --- | --- | --- |
| G-01 | 声明区块注入 | 缺失时自动补齐 | 保留 |
| G-02 | 声明区块保护 | prompt 中禁止删除 | 保留 |
| G-03 | 导出前检查 | sanitize/ensure 过程中维持存在 | 保留 |
| G-04 | blocked citations 处理 | sanitize 时处理 | 语义保留 |

---

## 13. 推荐验收执行顺序

建议每次重构按以下顺序验收：

1. 统一入口兼容面
2. scientist 主线
3. mvp 主线
4. writeup 状态机
5. JSON / artifact 契约
6. resume / recovery
7. 单写者与并发
8. 合规

---

## 14. 最终判定规则

### 可判定为“行为等价”的条件
必须同时满足：

- scientist / mvp / writeup 主状态机全部 PASS
- JSON / artifact 契约全部 PASS 或有明确兼容映射
- 合规矩阵全部 PASS
- 单写者矩阵全部 PASS
- 统一入口兼容面全部 PASS

### 不可判定为“行为等价”的情况
任一出现即 FAIL：

- scientist 主线缺 review / improvement / re-review
- mvp `all` 顺序改变
- writeup 状态机被扁平化为单轮黑盒流程
- CLI > ENV > default 优先级变化
- checkpoint 语义丢失
- 当前声明机制丢失
- 同一 workspace 可被多入口同时覆盖写

---

## 15. 与路线图的关系

本文档服务于：

- `docs/paperforge-workflow-equivalence-spec.md`
- `docs/paperforge-agent-skill-mcp-mapping.md`
- `docs/paperforge-safe-upgrade-roadmap.md`

它是升级过程中的 **可执行验收基线**，用于替代“PDF 不变”“完全一模一样”这类不可操作的口号式标准。
