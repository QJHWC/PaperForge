---
name: citation-select
description: 文献候选筛选技能。用于从候选搜索结果中选择最适合写入当前论文的一组引用。
---

# Citation Select Skill

## Purpose

在 citation rounds 中，从候选搜索结果中筛选最相关的文献。

## Input

- `papers`
- `draft_context`
- `current_description`

## Output

- `Selected`
- `Description`

## Usage Boundary

该 Skill 只负责：
- 从候选文献中筛选
- 更新描述

该 Skill 不负责：
- 外部搜索
- 把 bibtex 写入 references
- 修改 tex 文件
