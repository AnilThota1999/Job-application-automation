"""
Step 2 - Tailor the base resume to each job, verify it, fix it, and save it as a PDF.

For every job:
  1. Generate the tailored resume with your prompt.txt.
  2. ATS check: extract the JD's keywords and measure how many appear in the resume.
  3. Rule check: summary = 5 plain bullets (no sub-headings), per-role bullet counts from profile.json,
     banned verbs, header + education format, plus an AI review of the judgment rules
     (time-travel, domain bleed, anti-Frankenstein, formula alternation, bolding, BS check).
  4. If anything fails, send the issues back for a revision. Repeats up to 3 times, keeps the best.
  5. Save PDF + a report (audits/<job_id>_check.md) showing the score and anything left.

Usage:
    python step2_tailor_resume.py          # new jobs only
    python step2_tailor_resume.py --redo   # also re-tailor jobs already tailored
"""
import csv, glob, html, json, os, re, shutil, sqlite3, sys
from datetime import datetime
import markdown
from llm_client import generate, QuotaExhausted
from pypdf import PdfReader
from weasyprint import HTML

def _candidate_name():
    """File-name prefix for tailored resumes, taken from profile.json (or CANDIDATE_NAME env var)."""
    if os.getenv("CANDIDATE_NAME"):
        return os.getenv("CANDIDATE_NAME")
    try:
        p = json.load(open("profile.json"))
        return (p.get("first_name", "") + p.get("last_name", "")).replace(" ", "") or "Candidate"
    except (OSError, ValueError):
        return "Candidate"


CANDIDATE_NAME = _candidate_name()
MAX_REVISIONS = 2
os.makedirs("resumes", exist_ok=True)
os.makedirs("audits", exist_ok=True)

GROUNDING = """
SUMMARY FORMAT (strict):
- The Professional Summary is exactly 5 plain bullet points.
- NO sub-headings, NO labels, NO bold lead-ins. Never write things like "Identity & Scope:",
  "Core Tech:", "**Impact:**". Each bullet starts directly with the sentence.

OUTPUT FORMAT:
1. Phase 1 analysis.
2. Then the full resume in Markdown between the exact markers <RESUME> and </RESUME>.
   Use '# Name' for the name, a plain line for contact info (write the full LinkedIn URL, not the word "LinkedIn"), '## Section' for sections
   (PROFESSIONAL SUMMARY, TECHNICAL SKILLS, PROFESSIONAL EXPERIENCE, EDUCATION),
   '### Company – Location (Dates) | Title' for each role, '- ' for every bullet.
3. Finally, every hard-skill ATS keyword from the JD (tools, languages, cloud services, databases,
   frameworks, methodologies; JD's exact wording; no soft skills) as a JSON array between
   <KEYWORDS> and </KEYWORDS>.
"""

CSS = """
@page { size: Letter; margin: 0.5in 0.6in; }
body { font-family: Calibri, Arial, sans-serif; font-size: 10.5pt; line-height: 1.25; color: #111; }
h1 { font-size: 18pt; text-align: center; margin: 0 0 1px; }
p.headline { text-align: center; font-weight: bold; font-size: 11pt; letter-spacing: 1px; margin: 0 0 2px; }
h1 + p, p.headline + p { text-align: center; margin: 0 0 8px; font-size: 9.5pt; }
h2 { font-size: 11.5pt; text-transform: uppercase; border-bottom: 1px solid #333;
     margin: 10px 0 4px; padding-bottom: 1px; }
h3 { font-size: 10.5pt; margin: 6px 0 2px; }
ul { margin: 0 0 4px 16px; padding: 0; } li { margin: 0 0 2px; }
p { margin: 2px 0; }
"""

# Boards you apply to yourself: resume + link + answers saved in manual_apply/<date>/<Company - Title>/
MANUAL_BOARDS = {"lever", "other"}      # Workday is now filled by step3 (with your review)

BANNED_VERBS = ["Directed", "Spearheaded", "Architected"]

# Resume rules live in profile.json -> "resume_rules" so no personal details are hardcoded here.
def _resume_rules():
    try:
        return json.load(open("profile.json")).get("resume_rules", {})
    except (OSError, ValueError):
        return {}


RESUME_RULES = _resume_rules()
ROLES = RESUME_RULES.get("roles", [])                      # newest employer first
ROLE_RULES = {r["name"].lower(): r["bullets"] for r in ROLES if r.get("bullets")}
EDUCATION_LINE = RESUME_RULES.get("education_line", "")
HEADER_LOCATION = RESUME_RULES.get("header_location", "")


# ---------------- helpers ----------------
def load_base_resume():
    if os.path.exists("base_resume.txt"):
        return open("base_resume.txt", encoding="utf-8").read()
    return "\n".join(p.extract_text() for p in PdfReader("base_resume.pdf").pages)


def extract_block(text, tag="RESUME"):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    if not m:
        return text, None
    before = re.sub(r"<KEYWORDS>.*?</KEYWORDS>", "", text[:m.start()], flags=re.S).strip()
    return before, m.group(1).strip()


def parse_json(text, default):
    m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    try:
        return json.loads(m.group(1)) if m else default
    except Exception:
        return default


def sections(md):
    """Return {section_name_lower: text} split on '## '."""
    out, cur = {}, None
    for line in md.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip().lower(); out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def find_section(secs, word):
    return next((v for k, v in secs.items() if word in k), "")


def bullets(text):
    return [l.strip()[2:].strip() for l in text.splitlines() if l.strip().startswith(("- ", "* ", "• "))]


def clean_summary_labels(md):
    """Strip any 'Label:' / '**Label:**' lead-ins from summary bullets (safety net)."""
    lines, in_summary = md.splitlines(), False
    for i, line in enumerate(lines):
        if line.startswith("## "):
            in_summary = "summary" in line.lower(); continue
        if in_summary and line.strip().startswith(("- ", "* ", "• ")):
            body = line.strip()[2:]
            body = re.sub(r"^\*\*[^*]{1,50}?:?\*\*:?\s*", "", body)        # **Label:** or **Label**:
            body = re.sub(r"^[A-Z][\w &/()-]{1,40}:\s+", "", body)           # Label: text
            lines[i] = "- " + body[0].upper() + body[1:] if body else line
    return "\n".join(lines)


def plain(md):
    return re.sub(r"\s+", " ", re.sub(r"[-–/]", " ", re.sub(r"[*_`#]", "", md))).lower()


# ---------------- checks ----------------
def ats_keywords(job):
    out = generate(
        "You are an ATS parser. Extract every hard skill an ATS would match from this job description: "
        "languages, tools, frameworks, cloud services, databases, platforms, methodologies, certifications "
        "and domain terms. Use the JD's exact wording. No soft skills, no generic words like 'data' or "
        "'team'. Return ONLY a JSON array of strings.",
        f"{job['title']}\n\n{job['description'][:12000]}", max_tokens=1500)
    kws = parse_json(out, [])
    return sorted({k.strip() for k in kws if isinstance(k, str) and 1 < len(k.strip()) < 60})


def keyword_present(kw, text):
    kw = re.sub(r"\s+", " ", re.sub(r"[-–/]", " ", kw)).strip()
    variants = {kw.lower()}
    for prefix in ("apache ", "amazon ", "aws ", "google ", "microsoft ", "azure "):
        if kw.lower().startswith(prefix):
            variants.add(kw.lower()[len(prefix):])
    return any(re.search(rf"(?<![a-z0-9]){re.escape(v)}(?![a-z0-9])", text) for v in variants)


def ats_score(md, keywords):
    text = plain(md)
    missing = [k for k in keywords if not keyword_present(k, text)]
    score = 100 if not keywords else round(100 * (len(keywords) - len(missing)) / len(keywords))
    return score, missing


_BASE_ROLES = None
ROLE_ORDER = [r["name"] for r in ROLES]
UNIVERSAL_TOOLS = {"python", "sql", "pyspark", "git", "bash", "shell scripting"}
CONCEPT_RE = re.compile(r"schema|scd|dimensional|model|cdc|change data capture|\betl\b|\belt\b|pipeline|"
                        r"quality|validation|governance|lineage|\bapis?\b|rest|ci/?cd|batch|stream|lakehouse|"
                        r"warehous|medallion|agile|scrum|security|rbac|\biam\b|metadata|semantic|observab|"
                        r"automat|test|documentation|migration|integration|reporting|analytics|dashboard|"
                        r"architecture|framework|optimization|monitoring|alert|catalog|masking|pii|compliance|orchestrat|"
                        r"^\d|%|records|datasets|models|latency|cost|single source", re.I)
HONEST_SYNONYMS = {
    "airflow": ["cloud composer", "airflow"], "apache airflow": ["cloud composer", "airflow"],
    "cloud composer": ["airflow"], "gcp cloud composer": ["cloud composer"],
    "databricks": ["databricks", "delta lake"], "azure databricks": ["databricks"],
    "delta lake": ["delta lake", "databricks"], "adls": ["adls"], "adls gen2": ["adls"],
    "azure data lake": ["adls"], "s3": ["s3"], "emr": ["emr"], "kafka": ["kafka"],
    "spark": ["spark"], "spark structured streaming": ["structured streaming"],
    "github": ["git"], "glue": ["glue"], "synapse": ["synapse"], "azure synapse": ["synapse"],
    "bigquery": ["bigquery"], "gcs": ["gcs", "google cloud storage"], "cloud storage": ["gcs", "google cloud storage"],
}


def base_roles():
    """Lower-cased text of each employer's section in the base resume."""
    global _BASE_ROLES
    if _BASE_ROLES is None:
        text = load_base_resume()
        _BASE_ROLES = {}
        for k, name in enumerate(ROLE_ORDER):
            start = text.find(name + " –") if text.find(name + " –") >= 0 else text.find(name)
            ends = [text.find(n, start + 1) for n in ROLE_ORDER[k + 1:] + ["EDUCATION"]]
            ends = [e for e in ends if e > start]
            _BASE_ROLES[name] = text[start:min(ends) if ends else len(text)].lower() if start >= 0 else ""
    return _BASE_ROLES


def _norm_tool(t):
    t = re.sub(r"\(.*?\)", "", t).strip().lower()
    return re.sub(r"^(amazon|aws|apache|azure|google|gcp|microsoft)\s+", "", t).strip()


def _in(term, text):
    return bool(term) and re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def grounding_issues(md):
    """Tools in an employer's bullets that the base resume doesn't show for that employer."""
    roles, out = base_roles(), []
    secs = re.split(r"^### ", find_section(sections(md), "experience"), flags=re.M)[1:]
    for name in ROLE_ORDER:
        sec = next((x for x in secs if name.lower() in x.splitlines()[0].lower()), "")
        base = roles.get(name, "")
        if not sec or not base:
            continue
        bad = []
        for bold in re.findall(r"\*\*([^*]+)\*\*", sec):
            for part in re.split(r",|\band\b|/|&", bold):
                raw, n = part.strip(), _norm_tool(part)
                if len(n) < 2 or n in UNIVERSAL_TOOLS or CONCEPT_RE.search(n):
                    continue
                if _in(n, base) or _in(raw.lower(), base) or any(_in(x, base) for x in HONEST_SYNONYMS.get(n, [])):
                    continue
                bad.append(raw)
        if bad:
            out.append(f"GROUNDING: {name} bullets mention {', '.join(sorted(set(bad)))}, which the base resume "
                       f"does not show at {name}. Replace with tools the base resume lists for {name} "
                       f"(or honest synonyms); JD-only tools belong in Technical Skills only.")
    return out


def rule_check(md):
    issues = []
    secs = sections(md)
    for need in ["summary", "skills", "experience", "education"]:
        if not find_section(secs, need):
            issues.append(f"Missing section: {need.upper()}")

    summ = find_section(secs, "summary")
    sb = bullets(summ)
    if len(sb) != 5:
        issues.append(f"Professional Summary has {len(sb)} bullets, must be exactly 5.")
    if re.search(r"^#{3,}", summ, re.M):
        issues.append("Professional Summary contains sub-headings; remove them.")
    for b in sb:
        if re.match(r"^(\*\*[^*]+\*\*|[A-Z][\w &/()-]{1,40}:)", b):
            issues.append(f"Summary bullet has a label/sub-heading: '{b[:40]}...'. Remove the label.")

    exp = find_section(secs, "experience")
    roles = re.split(r"^### ", exp, flags=re.M)[1:]
    for company, need in ROLE_RULES.items():
        role = next((r for r in roles if company in r.splitlines()[0].lower()), None)
        if role is None:
            issues.append(f"Role for {company.title()} not found in experience.")
            continue
        header = role.splitlines()[0]
        if not re.search(r"–|-", header) or "(" not in header or "|" not in header:
            issues.append(f"{company.title()} header must be 'Company – Location (Dates) | Title'.")
        n = len(bullets(role))
        if n != need:
            issues.append(f"{company.title()} has {n} bullets, must be exactly {need}.")

    for v in BANNED_VERBS:
        if re.search(rf"\b{v}\b", md):
            issues.append(f"Banned verb used: {v}. Use hands-on verbs (Engineered, Built, Designed...).")

    edu = find_section(secs, "education")
    if EDUCATION_LINE and EDUCATION_LINE.split("|")[0].strip() not in edu:
        issues.append(f"Education must read exactly: '{EDUCATION_LINE}'.")
    if HEADER_LOCATION and HEADER_LOCATION.split("(")[0].strip() not in md.split("## ")[0]:
        issues.append(f"Header contact line must include '{HEADER_LOCATION}'.")
    return issues


def ai_review(md, prompt, base, job):
    legacy = " or ".join(r["name"] for r in ROLES[1:]) or "older roles"
    domains = ", ".join(f"{r['name']} stays {r['domain']}" for r in ROLES if r.get("domain")) or \
        "each past employer stays in its own industry"
    out = generate(
        "You are a strict resume QA reviewer. Check the TAILORED RESUME against the RULES, the JD and the "
        "BASE RESUME. Only check these judgment rules: (1) Time-travel: no post-2022 tools in "
        f"{legacy} bullets. (2) Domain bleed: {domains}; target-industry jargon only in Summary/Skills. (3) Anti-Frankenstein: no overlapping legacy "
        "and JD tools side-by-side in one bullet. (4) Cloud decisiveness and competing-vendor pruning in "
        "Skills. (5) Bullets alternate Formula 1 (metric-first) and Formula 2 (action-led) and match the "
        "required theme per bullet position. (6) JD technologies are bolded inline. (7) BS check: outcomes "
        "logically match actions. Return ONLY JSON: {\"issues\": [\"specific fixable issue\", ...]} "
        "(empty list if everything passes).",
        f"RULES:\n{prompt[:9000]}\n\nJD:\n{job['description'][:6000]}\n\nBASE RESUME:\n{base}\n\n"
        f"TAILORED RESUME:\n{md}", max_tokens=2000)
    return [i for i in parse_json(out, {}).get("issues", []) if isinstance(i, str)]


# ---------------- generate + revise loop ----------------
def build(prompt, base, job):
    system = prompt + "\n" + GROUNDING
    jd_block = (f"TARGET JOB DESCRIPTION\nCompany: {job['company']}\nTitle: {job['title']}\n\n"
                f"{job['description']}\n\nBASE RESUME\n{base}")
    first = generate(system, jd_block, max_tokens=8000)
    analysis, md = extract_block(first)
    if not md:
        raise ValueError("Model output missing <RESUME> block")
    md = clean_summary_labels(md)

    # keywords come back with the first answer (saves an AI call); fall back to a separate call
    _, kw_block = extract_block(first, "KEYWORDS")
    keywords = sorted({k.strip() for k in parse_json(kw_block or "", []) if isinstance(k, str) and 1 < len(k.strip()) < 60})
    if not keywords:
        keywords = ats_keywords(job)

    best, ai_issues = None, None
    for attempt in range(MAX_REVISIONS + 1):
        score, missing = ats_score(md, keywords)
        if ai_issues is None:                       # AI judgment review once; code checks every pass
            ai_issues = ai_review(md, prompt, base, job)
            issues = rule_check(md) + ai_issues
        else:
            issues = rule_check(md)
        print(f"    pass {attempt}: ATS {score}%  |  {len(issues)} rule issues")
        if best is None or (len(issues), -score) < (len(best[2]), -best[1]):
            best = (md, score, issues, missing)
        if score == 100 and not issues:
            break
        if attempt == MAX_REVISIONS:
            break
        fix = (jd_block + "\n\nCURRENT TAILORED RESUME:\n" + md + "\n\nREVISE the resume to fix ALL of these:\n" +
               "\n".join(f"- {i}" for i in issues) +
               ("\n- Add these missing JD keywords using the JD's exact wording, placing them as the RULES "
                "above direct (Technical Skills, Summary, and experience bullets per the Chronological Audit): "
                + ", ".join(missing) if missing else "") +
               "\nKeep everything that already passes. Output ONLY the full revised resume between "
               "<RESUME> and </RESUME>.")
        _, revised = extract_block(generate(system, fix, max_tokens=8000))
        ai_issues = []                              # AI issues were handed to the revision already
        if revised:
            md = clean_summary_labels(revised)
    return analysis, best, keywords


def safe_name(t):
    return re.sub(r'[\\/:*?"<>|]+', "-", t).strip()[:80]


def my_answers_text():
    p = json.load(open("profile.json"))
    lines = ["BASIC DETAILS",
             f"Full name: {p['first_name']} {p['last_name']}", f"Email: {p['email']}", f"Phone: {p['phone']}",
             f"Location: {p['location']}", f"Current company: {p['current_company']}",
             f"Current title: {p.get('current_title', '')}", f"LinkedIn: {p['linkedin']}",
             f"GitHub: {p.get('github', '')}",
             f"Education: {p['degree']} in {p['discipline']}, {p['school']} ({p.get('graduation', '')})",
             f"Today's date: {datetime.now():%m/%d/%Y}", "", "WORK HISTORY"]
    lines += [f"{w['company']} | {w['title']} | {w['start']} - {w['end']} | {w['location']}"
              for w in p.get("work_history", [])]
    lines += ["", "STANDARD ANSWERS (question keyword -> your answer)"]
    lines += [f"{' / '.join(r['match'])}: {r['answer'].split('|')[0]}"
              for r in p.get("answer_rules", []) if not r["answer"].startswith("FILL_ME")]
    return "\n".join(lines)


def save_manual_package(job, pdf_path, report):
    day = datetime.now().strftime("%Y-%m-%d")
    folder = os.path.abspath(os.path.join("manual_apply", day,
                                          safe_name(f"{job['company'].title()} - {job['title']}")))
    os.makedirs(folder, exist_ok=True)
    shutil.copy(pdf_path, folder)
    url = job["job_url"]
    open(os.path.join(folder, "job_link.txt"), "w").write(url + "\n")
    open(os.path.join(folder, "Open job posting.webloc"), "w").write(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict><key>URL</key>'
        f"<string>{html.escape(url)}</string></dict></plist>\n")
    open(os.path.join(folder, "job_description.txt"), "w", encoding="utf-8").write(job["description"])
    open(os.path.join(folder, "ats_report.md"), "w", encoding="utf-8").write(report)
    open(os.path.join(folder, "my_answers.txt"), "w", encoding="utf-8").write(my_answers_text())

    # master list of everything waiting for you
    index_csv = os.path.join("manual_apply", "index.csv")
    new = not os.path.exists(index_csv)
    with open(index_csv, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "company", "title", "board", "job_url", "folder"])
        w.writerow([day, job["company"], job["title"], job["board"], url, folder])
    rows = list(csv.DictReader(open(index_csv, encoding="utf-8")))
    items = "".join(
        f"<tr><td>{r['date']}</td><td>{html.escape(r['company'])}</td><td>{html.escape(r['title'])}</td>"
        f"<td>{r['board']}</td><td><a href='{html.escape(r['job_url'])}' target='_blank'>Apply</a></td>"
        f"<td><a href='file://{html.escape(r['folder'])}'>Folder</a></td></tr>" for r in reversed(rows))
    open(os.path.join("manual_apply", "index.html"), "w", encoding="utf-8").write(
        "<html><body style='font-family:sans-serif'><h2>Jobs to apply manually</h2>"
        "<table border=1 cellpadding=6 style='border-collapse:collapse'><tr><th>Date</th><th>Company</th>"
        f"<th>Title</th><th>Board</th><th>Link</th><th>Resume folder</th></tr>{items}</table></body></html>")
    return folder


def fix_header(md):
    """Put the real, clickable LinkedIn URL in the contact line instead of the word 'LinkedIn'."""
    url = json.load(open("profile.json"))["linkedin"]
    shown = re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
    link = f"[{shown}]({url})"
    # any LinkedIn URL the AI wrote (often invented) is replaced with the real one
    md = re.sub(r"\[[^\]]*linkedin[^\]]*\]\([^)]*\)", link, md, flags=re.I)
    md = re.sub(r"(?<![\[(])(https?://)?(www\.)?linkedin\.com/in/[\w-]+/?", link, md, flags=re.I)
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):                                   # name line; contact line follows
            for j in range(i + 1, min(i + 5, len(lines))):
                if lines[j].strip() and not lines[j].startswith("#") and 'class="headline"' not in lines[j]:
                    if "linkedin.com" in lines[j].lower():
                        return "\n".join(lines)
                    if re.search(r"\bLinkedIn\b", lines[j], re.I):
                        lines[j] = re.sub(r"\[?\bLinkedIn\b\]?(\([^)]*\))?", link, lines[j], count=1, flags=re.I)
                    else:
                        lines[j] = lines[j].rstrip() + f" | {link}"
                    return "\n".join(lines)
            break
    return md


def add_headline(md):
    """Put the headline (default 'DATA ENGINEER') on its own line right under the name."""
    try:
        headline = json.load(open("profile.json")).get("headline", "DATA ENGINEER")
    except Exception:
        headline = "DATA ENGINEER"
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            nxt = re.sub(r"[*_<>/]|p class=\"?headline\"?", "", lines[j]).strip() if j < len(lines) else ""
            if j < len(lines) and (re.fullmatch(r"[A-Za-z &/,-]{3,40}", nxt) and "@" not in nxt):
                lines[j] = f'<p class="headline">{headline}</p>'          # replace an existing title line
            else:
                lines.insert(i + 1, f'<p class="headline">{headline}</p>')
            break
    return "\n".join(lines)


def render_pdf(resume_md, path):
    resume_md = fix_header(add_headline(resume_md))
    body = markdown.markdown(resume_md, extensions=["extra", "sane_lists"])
    HTML(string=f"<html><head><style>{CSS}</style></head><body>{body}</body></html>").write_pdf(path)


def rerender():
    """Rebuild every PDF from its saved text (no AI calls): picks up header/style fixes."""
    db = sqlite3.connect("jobs.db"); db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM jobs WHERE resume_path IS NOT NULL AND status != 'applied'").fetchall()
    for job in rows:
        src = job["resume_path"][:-4] + ".md"
        if os.path.exists(src):
            md = open(src, encoding="utf-8").read()
        elif os.path.exists(f"audits/{job['job_id']}.md"):
            text = open(f"audits/{job['job_id']}.md", encoding="utf-8").read()
            starts = [m.start() for m in re.finditer(r"^# ", text, re.M)]
            if not starts:
                continue
            md = text[starts[-1]:]
        else:
            continue
        render_pdf(md, job["resume_path"])
        for folder in glob.glob(os.path.join("manual_apply", "*", "*")):
            copy = os.path.join(folder, os.path.basename(job["resume_path"]))
            if os.path.exists(copy):
                shutil.copy(job["resume_path"], copy)
        print(f"  rebuilt {os.path.basename(job['resume_path'])}")


def resume_path_for(job, db):
    company = re.sub(r"[^A-Za-z0-9]", "", job["company"].title()) or "Company"
    path = os.path.abspath(f"resumes/{CANDIDATE_NAME}_DataEngineer_{company}.pdf")
    taken = db.execute("SELECT 1 FROM jobs WHERE resume_path=? AND job_id!=?", (path, job["job_id"])).fetchone()
    if taken:                                   # second role at the same company: keep both files
        path = path[:-4] + f"_{re.sub(r'[^A-Za-z0-9]', '', job['job_id'])[-6:]}.pdf"
    return path


def twin_resume(job, db):
    """Same company + same title + near-identical description already tailored -> reuse it."""
    for r in db.execute("SELECT * FROM jobs WHERE job_id!=? AND lower(trim(company))=lower(trim(?)) "
                        "AND lower(trim(title))=lower(trim(?)) AND resume_path IS NOT NULL",
                        (job["job_id"], job["company"], job["title"])):
        a, b = len(r["description"] or ""), len(job["description"] or "")
        src = r["resume_path"][:-4] + ".md"
        if a and b and abs(a - b) / max(a, b) < 0.05 and os.path.exists(src) and \
                os.path.exists(f"audits/{r['job_id']}.md"):
            return r, open(src, encoding="utf-8").read()
    return None, None


def tailor_job(job, prompt, base, db):
    """Tailor, check, render and file one job. Returns the new status. Raises QuotaExhausted."""
    print(f"\n  {job['company']} - {job['title']}")
    twin, twin_md = twin_resume(job, db)
    if twin:
        print(f"    identical to an already-tailored posting ({twin['job_id']}), reusing that resume")
        m = re.search(r"ATS (\d+)%, (\d+) issues", twin["notes"] or "")
        score, n_issues = (int(m.group(1)), int(m.group(2))) if m else (0, 1)
        analysis = open(f"audits/{twin['job_id']}.md", encoding="utf-8").read()
        md, issues, missing, keywords = twin_md, ["(reused)"] * n_issues, [], []
        report = open(f"audits/{twin['job_id']}_check.md", encoding="utf-8").read() \
            if os.path.exists(f"audits/{twin['job_id']}_check.md") else "# reused"
        return _finish_job(job, db, analysis, md, score, issues, missing, keywords, report)
    analysis, (md, score, issues, missing), keywords = build(prompt, base, job)
    return _finish_job(job, db, analysis, md, score, issues, missing, keywords, None)


def _finish_job(job, db, analysis, md, score, issues, missing, keywords, report):
    pdf_path = resume_path_for(job, db)
    render_pdf(md, pdf_path)
    open(pdf_path[:-4] + ".md", "w", encoding="utf-8").write(md)
    open(f"audits/{job['job_id']}.md", "w", encoding="utf-8").write(analysis + "\n\n" + md)
    report = report or (f"# Check report: {job['company']} - {job['title']}\n\n"
              f"ATS keyword match: {score}% ({len(keywords) - len(missing)}/{len(keywords)})\n\n"
              f"## JD keywords\n{', '.join(keywords)}\n\n"
              f"## Missing keywords\n{', '.join(missing) or 'None'}\n\n"
              f"## Remaining rule issues\n" + ("\n".join(f'- {i}' for i in issues) or "None") + "\n")
    open(f"audits/{job['job_id']}_check.md", "w", encoding="utf-8").write(report)
    status = "tailored"
    if job["board"] in MANUAL_BOARDS:
        folder = save_manual_package(job, pdf_path, report)
        status = "manual_ready"
        print(f"      saved for manual apply: {folder}")
    db.execute("UPDATE jobs SET status=?, resume_path=?, notes=? WHERE job_id=?",
               (status, pdf_path, f"ATS {score}%, {len(issues)} issues", job["job_id"]))
    db.commit()
    flag = "OK " if score == 100 and not issues else "CHK"
    print(f"  {flag} ATS {score}%, {len(issues)} issues left -> {pdf_path}")
    if missing:
        print(f"      missing: {', '.join(missing[:12])}")
    return status


def tailor_many(jobs, db):
    prompt = open("prompt.txt", encoding="utf-8").read()
    base = load_base_resume()
    print(f"Tailoring {len(jobs)} resumes...")
    no_ai = 0
    for job in jobs:
        try:
            tailor_job(job, prompt, base, db)
        except QuotaExhausted as e:
            print(f"\n  STOPPED: {e}")
            print("  Jobs not done yet stay in the queue and will be tailored next run.")
            return False
        except Exception as e:
            db.execute("UPDATE jobs SET notes=? WHERE job_id=?", (f"tailor error: {e}", job["job_id"]))
            db.commit()
            print(f"  ERR {job['company']}: {e}")
            if "No AI available" in str(e):
                no_ai += 1
                if no_ai >= 2:
                    print("\n  STOPPED: every free AI is busy or out of quota right now.")
                    print("  The remaining jobs stay in the queue and are tailored on your next run.")
                    return False
            continue
        no_ai = 0
    return True


def main():
    if "--rerender" in sys.argv:
        return rerender()
    redo = "--redo" in sys.argv
    db = sqlite3.connect("jobs.db")
    db.row_factory = sqlite3.Row
    statuses = "('discovered','tailored','submit_failed','needs_review')" if redo else "('discovered')"
    if "--redo-unapplied" in sys.argv:            # every resume not yet submitted, incl. manual-apply ones
        statuses = "('discovered','tailored','submit_failed','needs_review','manual_ready')"
    jobs = db.execute(f"SELECT * FROM jobs WHERE status IN {statuses}").fetchall()
    tailor_many(jobs, db)


if __name__ == "__main__":
    main()
