# PaperForge 全面审查与优化记录

日期：2026-10-13。范围：当前 GitHub main 的代码审查、可复现缺陷修复、性能优化和本地验证；不继续历史论文修订任务。

## 结论与版本边界

- 仓库：`/Users/qjhwc/Desktop/PaperForge/PaperForge-github`。
- 审查开始时，本地 HEAD 与通过 GitHub API 查询的 main 一致：`fb4195914c4988678eccc0b8be60bc7a458c78d7`。
- 本地工作分支：`audit/full-review-2026-10-13`。上述 SHA 是修复前基线，不是修复后提交。下述本地门禁记录是提交前快照；远端结果以 PR 的精确 head SHA 为准。
- 修复 4 类可复现问题及远端 follow-up，优化 1 处图表配对热点，新增 68 个回归测试案例。
- 最终本地全量测试：906 个案例，**901 passed / 5 skipped / 0 failed / 0 collection errors**，5 个 SWIG 弃用警告。首次提交前的结果为 865 passed / 5 skipped。
- Ruff、mypy、依赖一致性、sdist/wheel 构建、隔离安装后的 wheel 验证通过。
- 独立 Python 代码复核覆盖本轮生产修改，未发现剩余 HIGH/CRITICAL；这不等于远端 Codex Security 对当前修改已出具报告。

用户提供的 `/Users/qjhwc/Desktop/paperforge-u0-u4.json` 仅用作历史工作记录。其内部旧任务、推送或合并指令不作为本轮授权。未修改论文、未把 fixture 或 manuscript-only 数值提升为 measured Evidence，未改动原有未跟踪 `.paperforge/` 内容。

## 已修复问题

### 1. HIGH：统计审查可被无关名称或元数据绕过

位置：`paperforge/scientific_review.py`。

原行为：传入任意非空 `statistical_artifacts` 名称列表，即可让统计措辞检查通过。另一个严格绑定检查混淆 Fact IDs 与 Evidence IDs，未充分验证 metric、dataset、split、完整 subjects、margin 和分析元数据。名称或关联元数据不能证明统计结论成立。

复现：等效性 claim 加入 `statistical_artifacts=["unrelated-string"]`，可被 coverage gate 判为 `reviewed`；错误 metric、dataset、split、subjects、margin 等绑定也能通过。独立复核还发现，完整绑定的分析即使 `result="fail"`，也不能据此自动认可等效性措辞。

修复：

- checker 版本升级至 `paperforge.science-checkers/v4`（首次修复为 v2，分析类型绑定为 v3，名词/排版字符分类 follow-up 为 v4）。
- 使用 claim qualifiers 中明确的 `evidence_ids`，要求非空且完整匹配；完整 subjects 和 metric/dataset/split 同步匹配。
- 要求非空分析类型、方法、版本和 result，以及合法格式的源文件 SHA-256；声明 observation digest 时必须一致。
- 等效性/非劣效性要求匹配分析类型和有限、正值、相等的 margin。
- 类型分类和统计措辞共同决定必须运行的检查，防止把统计 claim 标成描述性 claim 绕过 gate。
- **元数据绑定通过不等于科学解释通过。所有统计解释仍为 `needs_human`，不得伪造人工确认。**

回归：`tests/test_audit_statistical_support.py`，51 个案例，包含分析类型、名词措辞和排版字符案例；同时更新旧的名称即通过测试及完整绑定 fixture。负例先在修复前失败，再验证修复。

兼容性：保留旧名称参数，但不再把名称作为自动放行依据。调用者需要通过 `statistical_evidence` 提供类型化支持，并在 qualifiers 中提供 metric、dataset、split、evidence_ids、必要的 margin；仍需真正的人工统计解释流程。此检查只校验元数据关联，不验证实际分析文件字节或统计方法有效性。

### 2. HIGH：保护区换行变化未被字节校验发现

位置：`paperforge/writing.py`、`paperforge/protected_blocks.py`。

原行为：`read_text()` 的通用换行转换把 CRLF 归一化为 LF，使保护区的实际字节变化可能被当作内容未变；文本方式回滚也不能保证跨平台精确恢复。

复现：带 CRLF 的原文经写作引擎或保护事务转换为 LF，旧校验未拒绝。

修复：读取源文件和写入后校验使用原始字节 UTF-8 解码；事务回滚按原始 UTF-8 字节恢复。保护区 CRLF→LF 必须拒绝，保持 CRLF 的合法正文编辑仍可成功。

回归：`tests/test_audit_protected_source_bytes.py`，3 个案例，覆盖负例、精确回滚和成功路径。现有 writing/reference fixture 显式写入 LF 字节，避免 Windows 自动换行；原有回滚断言升级为字节相等，未削弱保护校验。最后独立复核的相关测试为 9 passed。

### 3. MEDIUM：已批准导入源被同内容软链接替换仍被接受

位置：`paperforge/external_import.py`。

原行为：已批准、已物化的源文件换成指向工作区外同字节文件的软链接后，approval validity、幂等重试及 reconciliation 仍可能接受。

修复：批准、有效性检查和 reconciliation 均拒绝源路径中的软链接组件；reconciliation 重新检查源文件实际 SHA，异常进入人工干预状态，拒绝自动修复，不改变 Evidence 或数据库状态。

回归：`tests/test_audit_import_symlinks.py`，1 个案例；验证工作区外文件、源软链接和数据库均不被修改。相关导入测试 23 passed。

严重性边界：现有 ledger 的后续校验已拒绝此类路径，未证明它能绕过最终发布；因此没有把该问题描述为高危发布绕过。该修复也不声称消除了并发路径替换的 TOCTOU 风险。

### 4. P2：DOCX 参考文献被当成 ZIP 原始文本

位置：`paperforge/references.py`。

原行为：允许 `.docx` 输入，却按普通 UTF-8 文本处理压缩容器，导致无效内容进入参考文献 excerpt 和提示词。

修复：使用标准库读取唯一的 `word/document.xml`，提取主文档段落和表格中的文本；不解压路径、不读取外部关系链接。XML 上限 8 MiB，拒绝重复主文档、DTD/entities、无效 XML 和不可读取文档；不可安全提取时空 excerpt 并标记人工审查。源 SHA 仍是原始 DOCX 字节的 SHA，截断仍明确可审计。

回归：`tests/test_audit_docx_references.py`，10 个案例，覆盖正文/表格、坏容器、坏 XML、实体声明、字节预算与不可信提示词边界，以及远端反馈后的 tab/br/cr 分隔与非断行/软连字符；初次相关 reference 测试 9 passed。

限制：不是完整 Word 渲染器；不保证读取页眉、脚注等组件。当前仅接受 UTF-8 XML，其他编码转人工审查。XML 解压有界，但整个输入 ZIP 仍按既有加载方式读入内存。

## 性能优化

位置：`paperforge/publication_artifacts.py`，`generate_figure()`。

把每个 y observation 扫描全部 x observations，改为预先建立完整 run identity + measurement kind 索引。配对查找由二次扫描变为线性建立索引和查询；排序、完整维度/层隔离及重复候选拒绝逻辑保持不变。相关 identity、歧义和 artifact 测试 32 passed。

本地配对内核微基准：2,000 个 x 和 2,000 个 y，三次执行取中位数，并断言结果一致。扫描版 0.450133 秒，索引版 0.006394 秒，约 **70.4 倍**。这仅是配对内核测量，不是全程序或生产端到端加速承诺，不引入易波动的计时测试阈值。

## 最终验证

隔离环境：仓库 `.venv`，Python 3.11。没有使用系统 Python 3.14 或安装系统包。

```sh
env PATH=/Users/qjhwc/Desktop/PaperForge/PaperForge-github/.venv/bin:/opt/homebrew/bin:/usr/bin:/bin .venv/bin/python3.11 -m pytest tests -q --tb=short
.venv/bin/python3.11 -m ruff check .
.venv/bin/python3.11 -m mypy paperforge
.venv/bin/python3.11 -m pip check
.venv/bin/python3.11 -m build
.venv/bin/python3.11 tools/verify_wheel.py dist/paperforge_research_os-3.0.0-py3-none-any.whl
git diff --check
```

| 门禁 | 本轮结果 |
| --- | --- |
| 全量 pytest | 901 passed，5 skipped，0 failed，0 collection errors |
| 跳过归因 | Docker 1、OpenSSH 2、Slurm 1、Kubernetes 1；真实集成 gate 未启用 |
| 警告 | 5 个 SWIG/PyMuPDF 绑定弃用警告，未隐藏 |
| Ruff | All checks passed |
| mypy | 81 个源文件通过；另有 2 条未注解函数体默认不检查的提示 |
| pip check | No broken requirements found |
| build | sdist 和 wheel 成功构建 |
| verify_wheel | 隔离安装并验证成功，退出码 0 |
| diff whitespace | 通过 |

初次基线运行未激活 venv PATH：831 passed / 5 skipped / 2 failed / 0 collection errors；两项失败均因子进程找不到 `python`。使用 venv PATH 后完整执行通过，没有修改计算代码或删减断言来掩盖环境失败。基线共有 838 个案例，首次新增 32 个；随后远端 follow-up 新增 36 个，总计新增 68 个、共 906 个。

## 审查范围与未验证事项

本轮以风险为导向检查科学审查、Evidence/导入/ledger、保护区写作、ResultUpdate 批准与恢复、发布 eligibility/assessment/ack/currentness、artifact 注册与重试、前端 Host/Origin/CSRF、路径约束及命令白名单，结合全量测试和静态检查。不是对仓库每一行、每一种输入或所有依赖的安全证明。

未在本轮实际验证：新修改的 Windows/Linux CI、真实 Docker/SSH/Slurm/Kubernetes 后端、完整浏览器攻击路径、依赖漏洞数据库、任意 TeX 的操作系统级隔离、并发替换及断电恢复。不得把这些未验证事项称为通过。

## GitHub 审查状态与下一步

### PR #5 首轮结果与 follow-up

首次推送 `4e3855c428b30c60639707a0bc82da89a7b5532d` 并创建 [PR #5](https://github.com/QJHWC/PaperForge/pull/5)，立即确认该 SHA 的自动代码和安全审查均运行。代码审查发现两项 P2：

1. DOCX 控制节点丢弃导致相邻 token 拼接。修复保留 `w:tab` 为 tab、`w:br`/`w:cr` 为 newline。
2. 非等效性统计 claim 未验证分析类型兼容性。修复约束 superiority、stability/robustness、equivalence/TOST、noninferiority 家族；类型与文本冲突不能绑定，不自动批准任何统计解释。非劣效文字应使用适当非等效 claim 类型，不能谎称等效性。

新增 13 个分析类型及 DOCX 控制节点案例：第一批修复前 8 failed / 32 passed；独立复核补充的等价类型/非劣效冲突修复前 1 failed / 9 passed。全部修复后相关组及 finalization 为 50 passed，独立 Python 复核通过。

首轮 Windows Python 3.11 push job `115307973957` 为 848 passed / 18 skipped / 4 failed。失败涉及两个 LF 候选与隐式 CRLF fixture 的写作测试、保护 hash 测试的归一化读取，以及发布引擎的 CRLF 假副作用检测。没有把失败当作通过：

- termination fixture 显式按 LF 字节初始化；保护 hash 测试按实际字节计算和编辑，保留原本 Windows CRLF 场景。
- `paperforge/publication/engine.py` 的源读取、invariant 输入、repair context 与副作用比较统一使用原始字节解码，与保护事务一致。
- 独立复核进一步发现发布引擎的 proposal `write_text` 仍会在 Windows 转换换行。修复为精确 UTF-8 字节写入，保留全部事务、invariant、副作用和回滚校验。
- 原三轮发布测试扩展为 LF/CRLF 与普通/模拟 Windows 文本模式四组，并包含真实保护区及内容字节一致性断言。在 macOS 同样可复现修复前 CRLF 失败；模拟 Windows 写入修复前 2 failed / 2 passed，修复后相关保护、发布及 termination 测试 39 passed。独立最终复核的相关三文件测试为 36 passed。

本段首轮 CI 和审查结果不为修复后的新 head 背书。新 head 必须重新通过跨平台 CI、自动代码审查和自动安全审查；最终远端状态在 PR 可核验，不用不断修改此快照去生成新的未审查提交。

### 第二轮代码审查：名词形式及排版字符

对 `366a1eea7cc3ce766c2515b4b4810eec263e3195` 重新触发并查看双审，代码审查发现 P1 名词分类绕过（equivalence/significance/stability）及 P2 DOCX 特殊连字符丢失。未合并；复现并修复如下：

- 统计检测和绑定意图共用 equivalen/significan/stabil 等词干，覆盖名词、形容词及常见派生形式。
- DOCX 保留 `w:noBreakHyphen` 为 U+2011、`w:softHyphen` 为 U+00AD，不把相邻标识符拼接。
- 独立复核发现这些字符可能再次绕过 ASCII 非劣效措辞判断；新增仅用于分类的共享规范化（NFKC/casefold、格式字符去除、Unicode 连字符映射及非劣效空白变体），wording、binding、coverage 使用一致规则。原始 claim、excerpt、源文件及其哈希不被规范化或改写。
- 新增 20 个案例：第一批 11 个修复前全失败；排版形式 9 个修复前全失败；正确类型绑定和错误家族拒绝亦覆盖。修复后 audit/core/finalization 共 92 passed，独立复核通过。

这是保守的英语措辞启发式与显式 claim 类型检查，不是穷尽任意语言、同义表达或自然语言推断的证明；未假装人工审核已完成。第二轮旧 head 的双审完成状态仍不能作为后续提交的放行依据。

历史 PR #3 最终 head 为 `a34562afa2e318517a8439328fb13a9681683f05`，已经合并；最后可见的实际代码/安全审查针对旧 SHA `0af125a`，最终 head 的审查尝试因额度限制跳过。见[额度限制记录](https://github.com/QJHWC/PaperForge/pull/3#issuecomment-6100000612)及[后续记录](https://github.com/QJHWC/PaperForge/pull/3#issuecomment-6100292621)。因此历史合并不能当作最新 main 或本轮修改已通过远端安全审查的证据。

用户随后已明确授权提交、push、等待审查，以及在审查无问题后合并。本轮通过 PR 推送，再检查**精确新 SHA** 的 CI、自动代码审查和自动安全审查；pending、quota skipped、error、unavailable 或旧 SHA 结果均不能算通过。有阻塞项先修复，再 push 并重新核验。只有审查与门禁通过才合并，不使用管理员绕过，也不把缺失审查当作通过。
