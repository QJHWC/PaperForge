---
name: write-section
description: 章节级论文写作技能。用于在既有 scientist / mvp / writeup 流程不变的前提下，生成单个 section 的草稿内容。
---

# Write Section Skill

## Purpose

用于在章节粒度执行论文写作，不接管全局流程，不决定阶段顺序，也不管理轮次。

## Input

- `section_name`
- `notes`
- `idea_metadata`
- `style_policy`
- `existing_tex_context`

## Output

- 单个章节的草稿文本
- 可选的章节内结构建议

## Usage Boundary

该 Skill 只负责：
- 写某一节

该 Skill 不负责：
- 决定是否进入 cite / refine / latex_fix
- 决定下一阶段
- 修改全局 checkpoint
- 写入最终 PDF

## Typical Sections

- Introduction
- Background
- Method
- Experimental Setup
- Results
- Conclusion
