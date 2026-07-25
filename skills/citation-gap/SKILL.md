---
name: citation-gap
description: 引文缺口识别技能。用于在当前 draft 中找到最需要补的一条 citation 缺口，并产出 query。
---

# Citation Gap Skill

## Purpose

在 citation rounds 中，识别当前 draft 中最重要的 citation 缺口。

## Input

- `draft`
- `current_round`
- `total_rounds`

## Output

- `Description`
- `Query`

## Usage Boundary

该 Skill 只负责：
- 找缺口
- 产出 query

该 Skill 不负责：
- 真正搜索外部文献
- 决定最终选哪篇文献
- 把引用写回 draft
