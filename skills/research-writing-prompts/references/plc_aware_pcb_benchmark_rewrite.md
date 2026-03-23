# PLC-Aware PCB Benchmark Rewrite Constraints

This reference file captures the project-level hard constraints for the paper whose theme is:

`统一训练协议下边缘PCB缺陷检测的PLC感知基准与评测框架`

## Scope

Use this file whenever the manuscript is being rewritten, polished, translated, or structurally revised. Treat these rules as higher priority than generic paper-polishing habits.

## Global Framing

1. The manuscript is a benchmark and evaluation-framework paper.
2. `Ours-LW`, `YOLO11n`, and all other routes are peer candidates inside the benchmark.
3. The paper must not be framed as introducing a single new model called `Ours-LW`.

## Hard Constraints

1. Literature mapping must be precise. Any citation used to motivate the introduction must genuinely support the claim that prior work tends to optimize recognition metrics while under-specifying edge deployment constraints or PLC-side decision costs.
2. The deployment-delay formalism, including `\tau = t(s) + c \cdot \Delta t`, must be written in an IEEE TII industrial-informatics register. The paper should explicitly position the framework as a mathematical bridge between CV-side inference and OT-side PLC control logic.
3. The abstract must remain quantitative. Avoid generic claims such as "substantial improvement" or "significant gain" unless exact numbers are provided.
4. The `1350/150` split must be justified from the standpoint of industrial small-sample constraints. The rationale is not convenience; it is fair comparison under scarce high-quality labels.
5. The abstract should follow a tight sequence: industrial pain point -> framework -> unified benchmark evidence -> non-obvious conclusion.
6. The introduction must not read like an iteration diary. Do not narrate the development sequence of P2, BiFPN, CA, or other branch history there.
7. Chinese writing must be formal and clean. Avoid rhetorical filler, over-listed prose, and machine-like transition phrases.
8. The `sim_rpi5` environment must be explained as a methodological bridge between cloud-side training and edge-side deployment estimation.
9. Section titles must follow the benchmark storyline: problem definition -> framework construction -> benchmark experiments -> attribution diagnostics.
10. Cross-dataset validation should not be written as Ours-LW-only evidence if local artifacts do not support that restriction. Prefer symmetric benchmark logic across candidate families.
11. Do not write apologetic language such as "because the previous split was wrong, we reran...". State the final experiment groups directly and academically.
12. The old architecture figure centered on `P2+BiFPN+CA` should not remain the conceptual lead figure of the paper. The primary figure should instead describe the PLC-aware evaluation framework if such an asset exists.
13. Outside model names, metrics, and unavoidable proper nouns, prefer full Chinese exposition in the Chinese manuscript.
14. Table IV must not be left structurally incomplete if local simulated deployment artifacts exist for the compared models.
15. If local formula files exist for Raspberry Pi proxy power, FPS, and latency transfer, those equations belong in the evaluation-framework chapter.
16. Cross-dataset frozen fine-tuning claims for YOLO11n and other baselines must only be stated as completed if the local project actually contains those runs.

## Evidence Gating Rules

These rules prevent the skill from over-writing the paper beyond what the local project supports.

1. Never fabricate simulated latency, FPS, energy, or PLC-aware descriptors for external baselines.
2. Never state that YOLO11n or other baselines were included in PKU-Market-PCB frozen fine-tuning unless the local artifacts confirm it.
3. Never claim that Table IV is complete unless the required values can be computed or recovered from local materials.
4. Never mention a new framework figure as if it already exists unless the figure file has actually been created.
5. If a user requests wording that depends on missing evidence, convert the claim into one of two forms:
   - a verified statement limited to available evidence, or
   - a clearly marked future-work or pending-experiment statement.

## Writing Preferences

### English

- Prefer concise, review-resistant prose over promotional language.
- Replace vague praise with measured statements and explicit values.
- Keep LaTeX commands, citation keys, labels, and equations intact.
- Reduce repetitive benchmark slogans; preserve the benchmark thesis.

### Chinese

- Use disciplined academic Chinese rather than translated-English syntax.
- Avoid empty transitions and stock summary phrases.
- Keep specialized English model names only where translation would reduce precision.

## Local Project Reference Root

Primary project root:

`/Users/qjhwc/Desktop/边缘AI驱动的电气部件缺陷检测系统`

Use the local files under this root as the authoritative evidence base for:
- benchmark protocol definitions,
- deployment formulas,
- tables and figures,
- rerun group definitions,
- and cross-dataset experiment coverage.
