#!/bin/bash
# One command for the whole job-bot pipeline:  jobbot   (or: bash run_jobbot.sh)
# Finds jobs -> tailors resumes -> fills applications -> you review and press ENTER to submit.
# Extra options pass through, e.g.  jobbot --hours 72
cd "$(dirname "$0")"
[ -f ~/.zshrc ] && source ~/.zshrc 2>/dev/null
if [ ! -d venv ]; then
  echo "First run: setting up (takes a few minutes)..."
  bash setup.sh || exit 1
fi
source venv/bin/activate
# fastest free AI first; set LLM_ORDER in ~/.zshrc to change
export LLM_ORDER="${LLM_ORDER:-groq,gemini,cerebras,mistral,github,openrouter}"
python run.py --auto "$@"
