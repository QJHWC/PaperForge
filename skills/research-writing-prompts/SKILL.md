---
name: research-writing-prompts
description: Use when the user wants paper-writing prompt templates or wants Codex to automatically choose and apply the best prompt logic for tasks such as 翻译, 润色, 改写, 缩写, 扩写, 逻辑检查, reviewer审稿, 去AI味, 实验分析, 图表标题生成, 模型选择, or converting a strong external prompt collection into a reusable paper-writing workflow. Includes a project-specific PLC-aware PCB benchmark rewrite mode.
---

# Research Writing Prompts

## Overview

This skill turns the prompt library from `QJHWC/awesome-ai-research-writing` into a Codex-usable workflow. Use it when the user wants either:

1. The raw prompt template itself, ready to copy or adapt.
2. A customized prompt rewritten for the user's current paper/project.
3. Direct execution of the prompt logic, where Codex performs the writing task instead of only returning the prompt.

## Quick Start

1. Identify the task type first:
   - translation
   - rewriting / polishing
   - shortening / expansion
   - logic checking
   - reviewer-style critique
   - experiment analysis
   - figure / table naming
   - model selection
2. Read [references/prompt-index.md](references/prompt-index.md) to find the right section.
3. Load only the needed section from [references/awesome-ai-research-writing.md](references/awesome-ai-research-writing.md).
4. Choose one of three output modes:
   - `prompt-only`: return the best prompt template.
   - `prompt-plus-adaptation`: return the source prompt plus a version customized for the user's project.
   - `direct-execution`: apply the prompt logic internally and return the finished writing/result.

## Project-Specific Mode: PLC-Aware PCB Benchmark Rewrite

When the task concerns the PCB manuscript centered on a PLC-aware benchmark under unified training protocols, load [references/plc_aware_pcb_benchmark_rewrite.md](references/plc_aware_pcb_benchmark_rewrite.md) before using the generic prompt library.

Use this mode when the user mentions:
- PLC-aware benchmark
- unified training protocol
- edge PCB defect detection
- DeepPCB 1350/150
- Ours-LW, YOLO11n, or `sim_rpi5`
- TII manuscript rewrite for the benchmark storyline

This mode overrides generic polishing behavior in four ways:
1. The paper is a benchmark/framework paper, not a single-model novelty paper.
2. Claims that require new evidence, new figures, or new experiments must be gated by local artifacts.
3. English and Chinese polishing must remove formulaic AI-style narration without changing measured values, citations, or claim boundaries.
4. Section titles, figure logic, and framework exposition must follow the benchmark storyline: problem definition -> framework construction -> benchmark experiments -> attribution diagnostics.

## Intent Router

Use this routing table before loading any detailed prompt section.

- If the user says `翻译`, `中翻英`, `英翻中`, `把这段转成英文论文`, or equivalent:
  route to translation prompts.
- If the user says `润色`, `重写`, `改写`, `优化表达`, `学术化`, or equivalent:
  route to polish / rewrite prompts.
- If the user says `缩短`, `压缩`, `扩写`, `补充展开`:
  route to shortening / expansion prompts.
- If the user says `逻辑不顺`, `帮我检查逻辑`, `有没有跳跃`, `flow`:
  route to logic-check prompts.
- If the user says `像 reviewer 一样看`, `审稿`, `挑刺`, `review my paper`:
  route to reviewer-style critique prompts.
- If the user says `去AI味`, `更自然`, `降低机器感`:
  route to de-AI-tone prompts, but keep claims faithful to the evidence.
- If the user says `实验分析`, `帮我分析结果`, `写实验部分`:
  route to experiment-analysis prompts.
- If the user says `图标题`, `表标题`, `画图建议`, `架构图`:
  route to figure / table / diagram prompts.
- If the user says `模型选择`, `哪个模型更好`, `帮我选路线`:
  route to model-selection prompts.

If multiple intents appear together, use this priority:
1. reviewer / logic diagnosis
2. translation
3. rewrite / polish
4. shorten / expand
5. experiment / figure / table support
6. model selection

When the user already provides manuscript text, prefer `direct-execution` after routing instead of returning a long prompt block.

## Task Map

- Translation:
  Use `中转英`, `英转中`, or `中转中`.
- Rewrite / polish:
  Use `缩写`, `扩写`, `表达润色（英文论文）`, `表达润色（中文论文）`, or the two `去 AI 味` sections as needed.
- Review / diagnosis:
  Use `逻辑检查` or `论文整体以 Reviewer 视角进行审视`.
- Experiment writing:
  Use `实验分析`, `论文架构图`, `实验绘图推荐`, `生成图的标题`, or `生成表的标题`.
- Research decision support:
  Use `模型选择`.
- Skill onboarding / examples:
  Use `Skills 的配置`, `Skills 总览`, and `使用场景与示例 Prompt` only when the user is asking how to use the repo itself.

## Usage Rules

1. Prefer direct execution when the user has already supplied text, figures, tables, or a LaTeX section to edit.
2. Prefer `prompt-plus-adaptation` when the user says a prompt is useful and wants a reusable project-specific version.
3. Prefer `prompt-only` when the user explicitly asks for the template itself.
4. Preserve the user's target format:
   - LaTeX for paper source editing.
   - plain text for Word-oriented Chinese writing.
   - bilingual output only when the chosen prompt requires it.
5. If the user already has a fixed paper template or storyline, adapt the prompt to that context instead of returning a generic version unchanged.

## For Ongoing Paper Projects

When the user is working on an active paper rather than experimenting with prompts:

1. Extract the useful prompt logic from the library.
2. Translate that logic into direct editing choices on the current manuscript.
3. Keep claims aligned with the user's actual evidence, tables, and figures.
4. Avoid returning large prompt blocks unless the user explicitly wants them.

For benchmark-style papers, prefer prompt logic that emphasizes:
- protocol control,
- claim-evidence alignment,
- clear writing boundaries,
- reviewer-facing justification,
- and distinction between recognition ranking and deployment ranking.

For the PLC-aware PCB benchmark project, additionally enforce:
- industrial-informatics framing that connects computer vision with OT/PLC decision logic,
- justification of the 1350/150 split from the small-sample industrial setting,
- no iterative lab-notebook narration,
- no post hoc excuses about reruns or split mistakes,
- no unsupported completion claims for Table IV, cross-dataset all-baseline transfer, or simulated edge metrics.

## References

- Use [references/prompt-index.md](references/prompt-index.md) as the navigation layer.
- Use [references/awesome-ai-research-writing.md](references/awesome-ai-research-writing.md) as the source prompt library.
- Use [references/plc_aware_pcb_benchmark_rewrite.md](references/plc_aware_pcb_benchmark_rewrite.md) as the project-level hard constraint set for the current PCB benchmark paper.
- Do not load the entire prompt library unless the user is explicitly asking for a broad survey of all prompts.
