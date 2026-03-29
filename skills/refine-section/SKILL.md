---
name: refine-section
description: 章节级精修技能。用于对单个章节做逻辑、表达、结构、紧凑度的 refinement。
---

# Refine Section Skill

## Purpose

在保持全局状态机不变的前提下，对某个章节执行局部 refinement。

## Input

- `section_name`
- `section_text`
- `tips`
- `error_list`
- `style_policy`

## Output

- refined 章节文本
- 可选的修改摘要

## Usage Boundary

该 Skill 只负责：
- 精修单个章节

该 Skill 不负责：
- 决定是否还有下一轮
- 变更 writeup 全局状态
- 更新 checkpoint
- 编译 PDF

## Typical Usage

- init 阶段章节写完后的 refinement
- second refinement 阶段的全稿逐节精修
