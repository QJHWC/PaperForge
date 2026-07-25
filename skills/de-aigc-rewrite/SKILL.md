---
name: de-aigc-rewrite
description: 去机器味改写技能。用于在不改变论证结构的前提下，压缩模板化表达和明显 AI 腔调。
---

# De-AIGC Rewrite Skill

## Purpose

对单段或单节文本执行一次风格去机械化改写，保持事实和结构不变。

## Input

- `text`
- `style_goal`

## Output

- `rewritten_text`
- `style_notes`

## Usage Boundary

该 Skill 只负责：
- 表达压缩
- 减少模板化开场和填充语

该 Skill 不负责：
- 改实验结论
- 发明新引用
- 接管 writeup 流程
