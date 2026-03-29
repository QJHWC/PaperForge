# Research Writing Prompts

Codex skill for paper-writing prompt routing, prompt reuse, and direct execution on active manuscripts.

## Install

Copy this folder into your local Codex skills directory:

```bash
mkdir -p ~/.codex/skills
cp -R skills/research-writing-prompts ~/.codex/skills/
```

Then restart Codex.

## Use

Call it explicitly:

```text
$research-writing-prompts
```

Or trigger it with natural language requests such as:

- polish this paragraph
- rewrite this abstract
- remove AI tone
- review this paper like a reviewer
- help me write figure captions

## Contents

- `SKILL.md`: main skill definition
- `references/`: source prompt library and prompt index
- `agents/`: optional agent config
