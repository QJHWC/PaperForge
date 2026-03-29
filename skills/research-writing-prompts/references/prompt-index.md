# Prompt Index

Use this file first. It maps common writing tasks to the corresponding sections in `prompt-library.md`.

## Core Writing Tasks

| Task | Section Heading | Approx. line |
|---|---|---:|
| Chinese draft to English paper text | `中转英` | 57 |
| English LaTeX to Chinese explanation | `英转中` | 100 |
| Chinese draft to polished Chinese academic text | `中转中` | 130 |
| Shorten English LaTeX text | `缩写` | 172 |
| Expand English LaTeX text | `扩写` | 215 |
| Polish English paper wording | `表达润色（英文论文）` | 259 |
| Polish Chinese paper wording | `表达润色（中文论文）` | 301 |
| Check paragraph / section logic | `逻辑检查` | 349 |
| Reduce AI-sounding tone in English LaTeX | `去 AI 味（LaTeX 英文）` | 379 |
| Reduce AI-sounding tone in Chinese Word text | `去 AI 味（Word 中文）` | 437 |

## Figures, Tables, and Experiments

| Task | Section Heading | Approx. line |
|---|---|---:|
| Draft architecture-diagram prompt | `论文架构图` | 483 |
| Ask for plotting recommendations | `实验绘图推荐` | 548 |
| Generate figure titles | `生成图的标题` | 617 |
| Generate table titles | `生成表的标题` | 647 |
| Analyze experiment results | `实验分析` | 677 |

## Review and Decision Support

| Task | Section Heading | Approx. line |
|---|---|---:|
| Review whole paper as reviewer | `论文整体以 Reviewer 视角进行审视` | 716 |
| Choose a model / route | `模型选择` | 762 |

## Repo / Skill Meta Sections

| Task | Section Heading | Approx. line |
|---|---|---:|
| Learn how the original repo configures skills | `Skills 的配置` | 775 |
| Understand its skill catalog | `Skills 总览` | 829 |
| See example prompts for repo usage | `使用场景与示例 Prompt` | 839 |

## Selection Guidance

- If the user already supplied paper text, prefer direct execution of the chosen prompt logic.
- If the user wants a reusable prompt for later, return the original prompt plus a customized task-specific version.
- If the user is unsure which prompt family to use, inspect the task intent first, then choose the narrowest matching section.
