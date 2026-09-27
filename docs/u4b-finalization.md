# U4B finalization — 2026-09-27

## 范围与状态

- 分支：`feature/u4b-release-eligibility`，起点 `c628f85`。
- 本次只收尾 U4B；不进入 U4C/U4D，不 push，不修改真实论文，不创建真实 Scientific Evidence。
- 旧 checkout 和 `.paperforge/shadow` 保持不动；影子数据不进入 commit。
- 历史 JSON 最后是 U4B 初版汇报；该汇报不能代替代码与测试。本次发现并修正了只验证报告自身摘要、评估时写库、未绑定 acknowledgement、忽略 build，以及多个直接信任调用方状态的问题。

## F1：build provenance

`ReleaseTarget` 绑定 manuscript/PDF/bibliography 的具体相对路径和 SHA256，以及 `build_manifest_path` / `build_digest`。

`build_digest` 是 manifest 文件实际 bytes 的 SHA256，不是包含自身摘要的 JSON hash。消费的 manifest 契约：

```json
{
  "schema": "paperforge.build-provenance/v1",
  "status": "succeeded",
  "source_hashes": {"main.tex": "sha256", "refs.bib": "sha256"},
  "output_path": "final.pdf",
  "output_sha256": "sha256",
  "command": ["compiler", "main.tex"],
  "toolchain": {"compiler": "version"}
}
```

所有列出的 source hashes 都重验；manifest 必须涵盖目标 manuscript/bibliography，并指向同一 PDF。目标指定 workflow/baseline 时，manifest 必须同样绑定它们。空源文件、缺 build、旧源编译的 PDF、越界路径、glob 和软链接不通过。

这里消费既有构建记录，不运行编译，也不证明编译器或记录签发者可信。生产调用方须提供完整输入清单，不能把手写 manifest 当作构建真实性证明。已有 result baseline 指定时还检查数据库中的 source、APPLIED 状态和有效 approval 绑定；缺失或不一致时阻塞，不自动修复历史记录。

## F2/F3：acknowledgement、授权与发布分离

```text
evaluate_release                 -> eligibility（纯只读）
freeze_assessment                -> 显式保存审计报告、注册 ArtifactStore
record_release_acknowledgement   -> 显式操作者动作
release_authorization           -> 只读授权投影
```

- `ELIGIBLE` 不表示已授权，更不表示已发布。
- ack 绑定 assessment digest、target digest、policy digest、approver、rationale、时间和 decision。
- ack 只能针对当前且 eligible 的报告；不能跨 target/policy/assessment 复用。
- policy 要求 owner ack 但缺记录时：eligibility 可为 `ELIGIBLE`，authorization 为 `NEEDS_HUMAN`。
- 不要求 ack 时 authorization 为 `NOT_REQUIRED`；有效 ack 为 `AUTHORIZED`。
- U4B 不执行发布，授权投影始终报告 `NOT_RELEASED`。
- `human_records` 中任意传入 `owner_ack` 字典不能授权。科学人审消费既有、同 PDF/render 绑定的记录，不能用 owner ack 替代。
- 这是本地操作审计，不是数字签名或身份认证。调用 ack API 的上层必须认证操作者；不能抵抗可任意重写整个本地信任域的攻击者。

## F4：assessment currentness

- digest 排除报告创建时间和 target 创建时间；包含 policy、输入、门禁结果、文件快照与 domain digest。
- `assessment_current` 验证历史摘要，再只读重算当前评估；历史报告不回写。
- source、PDF、bib、build、import sources、review/ledger/matrix/QA、工程记录/log、证据和 approval 数据变化均可使报告失效。
- 保守策略：domain digest 包含相关科学/approval 表的全部行，因此无关证据变化也可能使报告失效，需要重评。
- `assessment_current`、`record_release_acknowledgement`、`release_authorization` 都接收 `current_policy`。上层若已选择新的政策，必须传入；省略时仅判断冻结政策下的现势，不存在隐式“latest policy”。
- 使用只读数据库 facade（`PRAGMA query_only`），旧 verifier 的 schema 创建不能产生副作用。
- 本阶段不声称跨文件/数据库读取具备线性一致性，也不声称完成 SIGKILL/掉电恢复；这些仍属 U4C。

## 补齐的门禁与限制

- Evidence 从 ScientificMemory 读取，不信任调用方伪造的 scientific/current 标签；导入 approval 和源 bytes 重验。
- Ledger 消费已有 verifier；fixture/transcribed 不能满足科学发布输入。
- ScientificReview 检查现势、源文件 hash 和 policy 允许的 review state；caveats 不自动等同 reviewed。
- ReviewerMatrix 只看 required criteria/actions；verified 字符串或回复 prose 不足以通过。
- 当前 release adapter 支持 `hardware_measurement` / `scientific_experiment` 的 bound evidence，以及 `layout` / `manuscript_clarification` 的 target-bound explicit human verification。未知 criterion/binding 类型保守阻塞，不做存在即通过的降级。
- PDF QA 检查同 PDF/build、页覆盖、生产 render/human 记录、critical、target-specific issue 和 warning 白名单。U3B verifier 的 digest 重算含 bundle_id、与 producer 不一致；release adapter 按 producer 的 canonical contract 独立核验，未修改历史 U3B 文件。
- Engineering/environment 必须分别提供 target-bound `paperforge.engineering-check/v1` 记录和 log hash；`passed` 字符串、非零 exit、stale、错误计数、缺记录均不能冒充通过。
- UnifiedRunStatus 增加可选、`authoritative=false` 的 release 摘要展示；不改变 workflow/process 真值，不保存第三份状态。

## 验证

命令（Python 3.11）：

```text
python3.11 -m pytest tests/test_u4b_release.py tests/test_u4b_finalization.py tests/test_u4a_unified_status.py -q
65 passed（46 U4B + 19 U4A）

python3.11 -m pytest tests -q --continue-on-collection-errors --tb=no
559 passed, 6 skipped, 10 failed, 2 collection errors
```

10 failures / 2 collection errors 都在旧 research_partner/agent 导入链，报 `ModuleNotFoundError: No module named 'pymupdf'`。5 个真实计算后端集成测试未启用，1 个 visualization 测试缺 fitz 跳过。不能称完整 suite 全绿。

`python3.11 -m ruff` / `python3.11 -m mypy` 均报模块未安装；本次未安装依赖。merge/push/release 环境门禁仍未关闭。

本地 Python/安全代码复核发现并修正：无 bindings 却 verified、criterion/binding 错配、政策更改未贯穿授权、导入源软链接绕过。最终独立复核未发现剩余 HIGH/CRITICAL；这不是远端自动审查结论。

## 后续纪律

本次独立本地 commit 后停止，不进入 U4C。U4C 的 crash/stress 和 U4D 的 merge readiness 不在本次范围。

用户交接记录说明：自动代码审查开启、每次 push 触发、全面代码审查关闭；自动安全审查开启、每次代码审查触发，自动最低严重级别为高，`@codex` 人工审查最低为中。本次未 push，未把这些配置声称为实时远端核验。

**以后每次 push 后必须立即检查本次自动代码审查和安全审查结果；未出结果不是通过。有阻塞问题先修复，再 push、再复查，不能直接继续下一阶段。**
