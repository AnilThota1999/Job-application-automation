# Job Bot

It's an automated pipeline that finds new Data Engineer openings every day, tailors a resume to each job description with an LLM, and pre-fills the application so you only review and submit.

## How it works

The pipeline runs in three steps, orchestrated by `run.py`:

1. **Discover jobs** (`step1_discover_jobs.py`) pulls fresh postings from an Apify job feed and roughly 250 company boards on Greenhouse, Ashby, Lever and Workday (listed in `companies.json`), then stores them in a local SQLite database.
2. **Tailor resumes** (`step2_tailor_resume.py`) rewrites your base resume for each job using `prompt.txt`, scores ATS keyword coverage, checks formatting and credibility rules, revises up to three times, and renders the final PDF with WeasyPrint. A report for every job is written to `audits/`.
3. **Apply** (`step3_apply.py`) opens each application in Playwright, fills standard fields, answers screening questions from the rules in `profile.json`, drafts short answers to open-ended questions, and highlights anything it could not answer. Safe Greenhouse and Ashby forms can be auto-submitted; everything else waits for you to review and press ENTER. Jobs it cannot fill are saved to `manual_apply/index.html`.

`llm_client.py` handles the LLM calls with automatic fallback across free providers (Groq, Gemini, Cerebras, Mistral, GitHub Models, OpenRouter) and optionally Claude.

## Project structure

```
job-bot/
├── run.py                    # Orchestrates steps 1 → 3
├── step1_discover_jobs.py    # Job discovery (Apify + ATS boards)
├── step2_tailor_resume.py    # LLM resume tailoring, ATS scoring, PDF rendering
├── step3_apply.py            # Playwright autofill and submission
├── llm_client.py             # Multi-provider LLM client with fallback
├── companies.json            # ATS board tokens to scan
├── config/
│   ├── profile.example.json  # Template for your personal details and answer rules
│   └── prompt.example.txt    # Template for the resume-tailoring prompt
├── run_jobbot.sh             # One-command runner
├── run_jobbot.command        # macOS double-click launcher
├── setup.sh                  # One-time environment setup
├── requirements.txt
└── .env.example              # Environment variables reference
```

## Setup (macOS)

```bash
git clone https://github.com/<your-username>/job-bot.git
cd job-bot
bash setup.sh
```

`setup.sh` creates a virtual environment, installs dependencies and Chromium, and copies the templates to `profile.json` and `prompt.txt`. Then:

1. Fill in every `FILL_ME` value in `profile.json` (rules left as `FILL_ME` are skipped).
2. Edit `prompt.txt` to match your own work history.
3. Place your resume in the project root as `base_resume.pdf`.
4. Add your API keys to `~/.zshrc` using `.env.example` as a reference.

## Usage

```bash
bash run_jobbot.sh                       # full pipeline, auto-submits safe applications
python run.py                            # full pipeline, review every application
python run.py --hours 72                 # look further back for jobs
python step3_apply.py                    # finish applications waiting for review
python step3_apply.py --workday-backlog  # apply to saved Workday jobs
python step2_tailor_resume.py --redo     # re-tailor jobs already processed
python step2_tailor_resume.py --rerender # rebuild PDFs without AI calls
```

Workday passwords and the optional Gmail app password used for verification emails are read from macOS Keychain and are never written to files.

## Privacy

Your profile, prompt, resume, job database, generated resumes, screenshots and browser session are all excluded by `.gitignore`. Only code and templates are committed.

## Disclaimer

This project is intended for personal, noncommercial use. Automated job applications may conflict with the terms of service of some job boards or applicant tracking systems, so check those terms before use. Always review generated resumes and application answers for accuracy before submitting.

## License

This project is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE).

You are welcome to view, run, modify, and share it for personal job searching, learning, research, or other noncommercial purposes. You may not use it, or any modified version of it, for commercial purposes, such as selling it, offering it as a paid service, or building it into a commercial product, without written permission from the author. Copyright remains with the author, and any copy you share must include the LICENSE file.

## Tech stack

Python, Playwright, SQLite, WeasyPrint, pypdf, Requests, and LLM APIs (Gemini, Groq, Cerebras, Mistral, OpenRouter, Anthropic).
