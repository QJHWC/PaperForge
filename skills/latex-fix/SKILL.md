---
name: latex-fix
description: LaTeX 修复技能。用于对单轮编译报错或 chktex 诊断做局部修复，不接管 writeup 全局状态机。
---

# LaTeX Fix Skill

## Purpose

在不改变 writeup 阶段顺序的前提下，对当前 LaTeX 文本执行一次局部修复。

## Input

- `latex_text`
- `diagnostics`
- `current_round`

## Output

- 修复后的 `latex_text`
- `fixes_applied`

## Usage Boundary

该 Skill 只负责：
- 单轮 LaTeX 文本修复
- 诊断信息归并

该 Skill 不负责：
- 控制 latex_fix 总轮次
- 更新 checkpoint
- 决定 writeup 是否结束
