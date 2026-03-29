#!/usr/bin/env bash

# ─── LLM API Keys ──────────────────────────────────────────────
# OpenAI-compatible endpoint (used for idea/code/review models)
export OPENAI_API_KEY='YOUR_OPENAI_API_KEY'
export OPENAI_BASE_URL='https://api.openai.com/v1'

# Optional: dedicated endpoint for writeup model
export OPENAI_WRITEUP_API_KEY='YOUR_OPENAI_WRITEUP_API_KEY'
export OPENAI_WRITEUP_BASE_URL='YOUR_OPENAI_WRITEUP_BASE_URL'

# Anthropic native endpoint (used when --claude-protocol anthropic)
export ANTHROPIC_API_KEY='YOUR_ANTHROPIC_API_KEY'
export ANTHROPIC_BASE_URL='https://api.anthropic.com'

# Optional: Gemini endpoint
# export GEMINI_API_KEY='YOUR_GEMINI_API_KEY'
# export GEMINI_BASE_URL='https://generativelanguage.googleapis.com/v1beta/openai/'

# ─── Literature Search ─────────────────────────────────────────
export OPENALEX_MAIL_ADDRESS='your_email@example.com'
# export S2_API_KEY='YOUR_SEMANTIC_SCHOLAR_API_KEY'

# ─── Writeup Controls ──────────────────────────────────────────
export WRITEUP_CITE_ROUNDS='4'
export WRITEUP_LATEX_FIX_ROUNDS='2'
export WRITEUP_SECOND_REFINEMENT='0'

# ─── Plotting (macOS stability) ────────────────────────────────
export MPLBACKEND='Agg'
export MPLCONFIGDIR='/tmp/mplconfig_paperforge'
export XDG_CACHE_HOME='/tmp'
