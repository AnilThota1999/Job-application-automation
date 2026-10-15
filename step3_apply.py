"""
Step 3 - Autofill applications, answer screening questions, pause for YOUR review, then submit.

How questions are handled:
  1. Standard fields (name, email, phone, LinkedIn, resume) -> filled directly per ATS.
  2. Every other field on the page is scanned: its label is matched against
     "answer_rules" in profile.json (work auth, sponsorship, salary, previous employer, EEO...).
  3. Open-ended text questions ("Why this company?") -> short draft from Claude
     using your tailored resume + the JD (optional, flagged for review).
  4. Anything still unanswered is outlined in RED in the browser and listed in the terminal.
  You fix/confirm everything, then type 'y' to submit.

Setup:
    pip install playwright requests
    playwright install chromium
    python step3_apply.py
"""
import html, json, os, re, sqlite3, subprocess, sys, time
from datetime import datetime
from playwright.sync_api import sync_playwright

PROFILE = json.load(open("profile.json"))
RULES = [r for r in PROFILE.get("answer_rules", []) if not r["answer"].startswith("FILL_ME")]
os.makedirs("screenshots", exist_ok=True)

from llm_client import generate
DRAFT = PROFILE.get("draft_open_questions_with_claude", True)

LABEL_JS = r"""
el => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const isChoice = el.type === 'radio' || el.type === 'checkbox';
  if (!isChoice) {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (clean(l && l.innerText)) return clean(l.innerText); }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = clean(lb.split(/\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' ')); if (t && !/^select one$/i.test(t)) return t; }
    if (el.getAttribute('aria-label') && !/^select one$/i.test(clean(el.getAttribute('aria-label')))) return clean(el.getAttribute('aria-label'));
  }
  const wf = el.closest('[data-automation-id^="formField"]');
  if (wf) {
    const wl = wf.querySelector('label, legend, [data-automation-id="formLabel"]');
    if (clean(wl && wl.innerText)) return clean(wl.innerText).slice(0, 350);
  }
  const fs = el.closest('fieldset');
  if (fs) { const lg = fs.querySelector('legend'); if (clean(lg && lg.innerText)) return clean(lg.innerText); }
  const q = el.closest('.application-question, .custom-question, [data-qa*="question"], [class*="question"], [class*="Question"], [class*="fieldEntry"]');
  if (q) {
    const l = q.querySelector('.application-label, legend, label:not(:has(input)):not(:has(select)), [class*="label"]:not(:has(input)):not(:has(select))');
    if (clean(l && l.innerText)) return clean(l.innerText).slice(0, 350);
  }
  let n = el.parentElement;
  for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
    const c = n.cloneNode(true);
    c.querySelectorAll('button, select, option, input, textarea, ul, ol, [role=listbox], [role=option], [data-automation-id="errorMessage"], label:has(input), [class*="placeholder"], [class*="singleValue"], [class*="single-value"]').forEach(x => x.remove());
    const t = clean(c.textContent);
    if (t.length > 3 && !/^select one$/i.test(t)) return t.slice(0, 350);
  }
  return el.name || el.placeholder || '';
}
"""


OPTION_LABEL_JS = r"""
el => {
  if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText.trim(); }
  const p = el.closest('label'); if (p) return p.innerText.trim();
  const sib = el.nextElementSibling; return sib ? sib.innerText.trim() : (el.value || '');
}
"""

STOP = {"i", "a", "an", "the", "to", "of", "and", "or", "do", "am", "is", "are", "have", "my", "me", "any"}


def norm(t):
    return re.sub(r"\s+", " ", re.sub(r"[’‘`]", "'", t or "")).strip().lower()


SYNONYMS = {
    "male": ["man", "cis-man", "cisgender man", "male"],
    "female": ["woman", "cis-woman", "cisgender woman", "female"],
    "decline": ["decline", "prefer not", "i don't wish", "i do not wish", "choose not", "do not want to answer"],
    "asian": ["asian"],
    "no": ["no"], "yes": ["yes"],
    "he/him": ["he/him", "he / him", "he him"],
}


def best_option(ans, options):
    """Answer may hold fallbacks 'A|B|C' (tried in order). Exact > starts-with > contains > word overlap."""
    if ans and "|" in ans:
        for alt in ans.split("|"):
            hit = best_option(alt.strip(), options)
            if hit:
                return hit
        return None
    a = norm(ans)
    opts = [o for o in options if norm(o) and not norm(o).startswith(("select", "--", "please select", "choose"))]
    short = re.sub(r"^(yes|no)[,.:]?\s+", "", a)       # 'No, I am not a protected veteran' -> 'I am not...'
    if short != a:
        for test in (lambda o: norm(o) == short, lambda o: norm(o).startswith(short), lambda o: short in norm(o)):
            hit = next((o for o in opts if test(o)), None)
            if hit:
                return hit
    for test in (lambda o: norm(o) == a, lambda o: norm(o).startswith(a), lambda o: a in norm(o)):
        hit = next((o for o in opts if test(o)), None)
        if hit:
            return hit
    for key, alts in SYNONYMS.items():                  # e.g. Male -> Man / Cis-man
        if a == key or a.startswith(key + " ") or short == key:
            for alt in alts:
                hit = next((o for o in opts if norm(o) == alt), None) or \
                      next((o for o in opts if norm(o).startswith(alt)), None) or \
                      (next((o for o in opts if alt in norm(o)), None) if len(alt) > 3 else None)
                if hit:
                    return hit
    aw = set(re.findall(r"[a-z]+", a)) - STOP
    if not aw:
        return None
    scored = sorted(((len(aw & set(re.findall(r"[a-z]+", norm(o)))), o) for o in opts), reverse=True)
    if scored and scored[0][0] >= max(1, (len(aw) + 1) // 2):
        if len(scored) == 1 or scored[0][0] > scored[1][0]:   # avoid ties like 'am a' vs 'am not a'
            return scored[0][1]
    return None


# ---------------- helpers ----------------
# Short words that must match as whole words ('state' must not hit 'statement').
WHOLE_WORDS = {"state", "race", "city", "source", "degree", "country", "remote", "gender", "major",
               "school", "college", "hybrid", "onsite", "zip"}


FULL_NAME = f"{PROFILE['first_name']} {PROFILE['last_name']}"
TODAY = datetime.now().strftime("%m/%d/%Y")

# Built-in answers checked BEFORE your profile rules (order matters).
PRONOUN_ANSWER = next((r["answer"] for r in PROFILE.get("answer_rules", [])
                       if "pronoun" in r["match"] and not r["answer"].startswith("FILL_ME")), None)

BUILTIN = [
    (r"pronoun", PRONOUN_ANSWER),                                       # never answered by the gender rule
    (r"preferred( first)? name|nick ?name|goes by|name you (go|prefer)", PROFILE["first_name"]),
    (r"\bfirst name|given name", PROFILE["first_name"]),
    (r"\blast name|surname|family name", PROFILE["last_name"]),
    (r"birth", None),                                                   # never guess date of birth
    (r"mm/dd/yyyy|signaturedate|today'?s date|^date\b", TODAY),           # signature date, not start date
    (r"signature|full name|legal name|^name\b|your name|^name$", FULL_NAME),
    (r"address line 1|street address|^\W*address\W*$|^\W*address \*", 
     None if str(PROFILE.get("address_line1", "FILL_ME")).startswith("FILL_ME") else PROFILE.get("address_line1")),
    (r"^\W*county\b", None if str(PROFILE.get("county", "FILL_ME")).startswith("FILL_ME") else PROFILE.get("county")),
    (r"postal code|zip ?code|^\W*zip\b",
     None if str(PROFILE.get("zip", "FILL_ME")).startswith("FILL_ME") else PROFILE.get("zip")),
    (r"phone (device )?type|type of phone|device type", f"{PROFILE.get('phone_type', 'Mobile')}|Mobile|Cell"),
    (r"country (phone )?code|phone country|dialing code|calling code",
     "United States of America (+1)|United States (+1)|+1|United States"),
    (r"extension|\bext\b", "__SKIP__"),
    (r"e-?mail", PROFILE["email"]),
    (r"(phone|mobile)( number)?\W*$|^\W*(phone|mobile)|cell", PROFILE["phone"]),
    (r"linkedin", PROFILE["linkedin"]),                                  # any field mentioning LinkedIn
    (r"^\W*github\b(?!.*(website|portfolio|profile))", PROFILE.get("github") or PROFILE["linkedin"]),  # GitHub-only field
    (r"profile (url|link)|portfolio|website|personal (site|url)|social|online presence|url",
     PROFILE["linkedin"]),                                                # generic URL fields -> LinkedIn
]


SECTION_JS = r"""
el => {   // the last section heading (Employment / Education / ...) that appears above the field
  const hs = Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, legend, [role=heading]'))
      .filter(h => { const t = (h.innerText || '').trim(); return t && t.length < 60; });
  let best = '';
  for (const h of hs) {
    if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) best = h.innerText.trim();
  }
  return best;
}
"""


def describe(el):
    """Visible question text + the field's name/placeholder + its section heading."""
    label = el.evaluate(LABEL_JS)
    extra = " ".join(filter(None, [el.get_attribute("name"), el.get_attribute("placeholder")]))
    extra = re.sub(r"[\[\]_]", " ", extra)
    try:
        section = el.evaluate(SECTION_JS)
    except Exception:
        section = ""
    return label, f"{label} {extra} [section: {section}]"


SKIP = "__SKIP__"      # leave this field alone (e.g. end date of the current job)
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


def _mm_yyyy(v):
    m = re.match(r"(\d{1,2})/(\d{4})", str(v or ""))
    return (MONTHS[int(m.group(1)) - 1], m.group(2)) if m else (None, None)


def history_answer(l):
    """Employment and Education section fields, answered from profile.json."""
    sec = re.search(r"\[section: ([^\]]*)\]", l)
    sec = sec.group(1).lower() if sec else ""
    q = re.sub(r"\[section:[^\]]*\]", "", l)
    jobs = PROFILE.get("work_history") or []
    if re.search(r"employment|experience|work history|current (job|role|position)", sec) and jobs:
        cur = jobs[0]
        current = str(cur.get("end", "")).lower() == "present"
        if re.search(r"current(ly)? (role|position|job|work here)|i currently work", q):
            return "Yes" if current else SKIP
        if re.search(r"company|employer", q):
            return cur.get("company")
        if re.search(r"\btitle\b|position|role", q):
            return cur.get("title")
        if re.search(r"start", q):
            mon, yr = _mm_yyyy(cur.get("start"))
            return mon if "month" in q else yr if "year" in q else None
        if re.search(r"\bend\b", q):
            if current:
                return SKIP
            mon, yr = _mm_yyyy(cur.get("end"))
            return mon if "month" in q else yr if "year" in q else None
    if re.search(r"education", sec):
        if re.search(r"school|university|college|institution", q):
            return PROFILE.get("school")
        if re.search(r"discipline|major|field of study", q):
            return PROFILE.get("discipline")
        if re.search(r"\bdegree\b", q) and not re.search(r"^\W*(do|are|have|did|will|can) you", q):
            return "Master's Degree|Master's|Masters|Master of Science"
        if re.search(r"start", q):
            mon, yr = _mm_yyyy(PROFILE.get("education_start"))
            return mon if "month" in q else yr if "year" in q else None
        if re.search(r"\bend\b|graduat", q):
            mon, yr = _mm_yyyy(PROFILE.get("graduation"))
            return mon if "month" in q else yr if "year" in q else None
    return None


def match_answer(label):
    l = norm(label)
    hist = history_answer(l)
    if hist:
        return hist
    l = re.sub(r"\[section:[^\]]*\]", "", l)
    if re.search(r"language.*(comfortable|conduct|business|speak|fluent|profici)", l):
        return "English"
    if re.search(r"upcoming time off|planned (time off|leave|vacation)|time off you have planned", l):
        return "None"
    if re.search(r"enrolled student|academic program at|currently a student|previously a student", l):
        return "No"
    if re.search(r"^\W*(do|are|have|did|will|can|would) you\b", l) and re.search(r"\b(degree|school|university|college)\b", l) \
            and not re.search(r"sponsor|authori|relativ|employ|work", l):
        return None
    for pattern, value in BUILTIN:
        if re.search(pattern, l):
            return value
    if re.search(r"\bhear(d)?\b|\breferred\b", l):
        l += " how did you hear"
    for rule in RULES:
        for k in rule["match"]:
            k = k.lower()
            tail = r"(?![a-z])" if k in WHOLE_WORDS else ""
            if re.search(rf"(?<![a-z]){re.escape(k)}{tail}", l):
                return rule["answer"]
    return None


def safe_fill(page, selectors, value):
    if not value:
        return False
    for sel in selectors:
        try:
            loc = page.get_by_label(sel[6:], exact=False) if sel.startswith("label:") else page.locator(sel)
            if loc.count() and loc.first.is_visible() and not is_trap(loc.first):
                loc.first.fill(str(value))
                return True
        except Exception:
            pass
    return False


def upload_resume(page, path, selectors):
    for sel in selectors:
        loc = page.locator(sel)
        if loc.count():
            loc.first.set_input_files(path)
            return True
    return False


def claude_draft(question, job):
    if not DRAFT:
        return None
    resume = ""
    audit = f"audits/{job['job_id']}.md"
    if os.path.exists(audit):
        resume = open(audit, encoding="utf-8").read()[-6000:]
    try:
        return generate(
            "Write a concise, first-person, natural answer (2-4 sentences) to a job application "
            "question. Use only facts from the resume. No fluff, no made-up details.",
            f"Company: {job['company']}\nRole: {job['title']}\n\nJD:\n{job['description'][:4000]}"
            f"\n\nResume:\n{resume}\n\nQuestion: {question}", max_tokens=400).strip()
    except Exception as e:
        print(f"    Claude draft failed: {e}")
        return None


def set_value(el, value):
    """Type the value; if the field is a date picker or read-only, set it directly and fire change events."""
    try:
        el.fill(str(value), timeout=3000)
    except Exception:
        pass
    if (el.input_value() or "").strip() != str(value):
        el.evaluate("""(e, v) => {
            e.removeAttribute('readonly');
            const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(e), 'value').set;
            setter.call(e, v);
            ['input', 'change', 'blur'].forEach(t => e.dispatchEvent(new Event(t, {bubbles: true})));
        }""", str(value))
    try:
        el.press("Escape")          # close any calendar popup
    except Exception:
        pass
    return (el.input_value() or "").strip() == str(value)


def fill_signature(page, filled):
    """EEO disability signature block (Lever and similar): full name + today's date."""
    pairs = [
        ('input[name*="ignature"]:not([name*="Date"]):not([name*="date"]), input[placeholder*="full name" i]', FULL_NAME, "Signature name"),
        ('input[name*="ignatureDate"], input[name*="ignature_date"], input[placeholder="MM/DD/YYYY"], input[placeholder*="mm/dd/yyyy" i]', TODAY, "Signature date"),
    ]
    for sel, value, label in pairs:
        for el in page.locator(sel).all():
            try:
                if el.is_visible() and not (el.input_value() or "").strip():
                    if el.get_attribute("type") == "date":
                        value = datetime.now().strftime("%Y-%m-%d")
                    if set_value(el, value):
                        filled.append((label, value))
            except Exception:
                pass


def mark_red(el):
    try:
        el.evaluate("e => { e.style.outline = '3px solid red'; e.style.outlineOffset = '2px'; }")
    except Exception:
        pass


# ---------------- generic question answering ----------------
PENDING = []     # questions the rules couldn't answer, for the semantic (AI) matcher

HONEYPOT_RE = re.compile(r"robots? only|for robots|do not (enter|fill|type|complete)|leave (this )?(field )?"
                         r"(blank|empty)|if you('| a)re (a )?human|honeypot|bot trap", re.I)
HIDDEN_JS = """e => { const s = getComputedStyle(e); const r = e.getBoundingClientRect();
  return e.tabIndex < 0 || e.getAttribute('aria-hidden') === 'true' || !!e.closest('[aria-hidden="true"]')
      || s.visibility === 'hidden' || s.opacity === '0' || r.width < 2 || r.height < 2
      || r.right < 0 || r.bottom < 0 || r.left > window.innerWidth * 3; }"""


def is_trap(el, label=""):
    """Hidden 'bots only' fields: filling them flags the application as automated."""
    if HONEYPOT_RE.search(label or ""):
        return True
    try:
        return bool(el.evaluate(HIDDEN_JS))
    except Exception:
        return False


def pend(kind, el, label, options=None, items=None):
    PENDING.append({"kind": kind, "el": el, "label": label, "options": options or [], "items": items or []})


def answer_questions(page, job):
    filled, drafted, todo = [], [], []
    PENDING.clear()

    # Text inputs and textareas
    for el in page.locator("input[type=text], input[type=number], input[type=email], input[type=tel], "
                           "input[type=url], input[type=date], input:not([type]), textarea").all():
        try:
            if not el.is_visible() or el.input_value().strip():
                continue
            if el.get_attribute("role") == "combobox" or el.get_attribute("aria-autocomplete"):
                continue  # handled below
            label, match_text = describe(el)
            if is_trap(el, label):
                continue                                   # bot trap: never fill
            ans = match_answer(match_text)
            if ans == SKIP:
                continue
            if ans:
                ans = ans.split("|")[0].strip()
                if el.get_attribute("type") == "date" and ans == TODAY:
                    ans = datetime.now().strftime("%Y-%m-%d")
                set_value(el, ans); filled.append((label, ans)); continue
            is_open = el.evaluate("e => e.tagName") == "TEXTAREA" and \
                not re.search(r"role description|job description|responsibilit|duties", label, re.I)
            if is_open and label:
                draft = claude_draft(label, job)
                if draft:
                    el.fill(draft); mark_red(el); drafted.append((label, draft)); continue
            mark_red(el); todo.append(label or "(unlabeled text field)")
            if label:
                pend("text", el, label)
        except Exception:
            pass

    # Native dropdowns
    for el in page.locator("select").all():
        try:
            if not el.is_visible():
                continue
            current = el.evaluate("e => (e.options[e.selectedIndex] || {}).text || ''")
            if el.evaluate("e => e.selectedIndex > 0") and not norm(current).startswith(("select", "--")):
                continue
            label, match_text = describe(el)
            ans = match_answer(match_text)
            if ans == SKIP:
                continue
            opts = el.evaluate("e => Array.from(e.options).map(o => o.text.trim())")
            pick = best_option(ans, opts) if ans else None
            if pick:
                el.select_option(label=pick); filled.append((label, pick))
            else:
                mark_red(el)
                todo.append(f"{label[:60]}  [your answer: {ans or 'no rule matched'} | options: "
                            f"{' / '.join(o for o in opts if o)[:200]}]")
                pend("select", el, label, [o for o in opts if o and not norm(o).startswith(("select", "--"))])
        except Exception:
            pass

    # Searchable dropdowns (new Greenhouse boards, Ashby)
    for el in page.locator("input[role=combobox]").all():
        try:
            if not el.is_visible():
                continue
            has_value = el.evaluate("""e => {
                const box = e.closest('[class*="control"], [class*="Control"], [class*="select"], [class*="Select"]');
                return !!(box && box.querySelector('[class*="single-value"], [class*="singleValue"], '
                    + '[class*="multi-value"], [class*="multiValue"], [class*="selected"]'));
            }""")
            if has_value or (el.input_value() or "").strip():
                continue
            label, match_text = describe(el)
            ans = match_answer(match_text)
            if ans == SKIP:
                continue
            if not ans:
                el.click(); page.wait_for_timeout(800)
                seen_opts = [o.inner_text().strip() for o in page.locator("[role=option]").all() if o.is_visible()]
                if not seen_opts:
                    page.wait_for_timeout(1200)
                    seen_opts = [o.inner_text().strip() for o in page.locator("[role=option]").all() if o.is_visible()]
                page.keyboard.press("Escape")
                mark_red(el); todo.append(label); pend("combobox", el, label, seen_opts[:60]); continue
            pick, texts = None, []
            el.click(); page.wait_for_timeout(700)

            def visible_options():
                return [o for o in page.locator("[role=option]").all() if o.is_visible()]

            opts = visible_options()
            if not opts:                                    # options still loading: wait and read again
                page.wait_for_timeout(1200)
                opts = visible_options()
            texts = [o.inner_text().strip() for o in opts]
            all_texts = list(texts)                         # full list, kept for the AI matcher
            pick = best_option(ans, texts)
            if not pick:                                    # long lists (countries): type to filter, read again
                for alt in [a.strip() for a in ans.split("|")]:
                    el.fill(alt[:25]); page.wait_for_timeout(800)
                    opts = visible_options()
                    texts = [o.inner_text().strip() for o in opts]
                    pick = best_option(ans, texts)
                    if pick:
                        break
            if pick:
                opts[texts.index(pick)].click()
                page.wait_for_timeout(300)
                page.keyboard.press("Escape")               # close multi-select menus
                filled.append((label, pick))
            else:
                page.keyboard.press("Escape")
                try:
                    el.fill("")
                except Exception:
                    pass
                mark_red(el)
                todo.append(f"{label[:60]}  [your answer: {ans} | options: {' / '.join(all_texts)[:150]}]")
                pend("combobox", el, label, all_texts[:60])
        except Exception:
            pass

    # Checkboxes (privacy acknowledgement, consent, multi-choice lists)
    seen_cb = set()
    AFFIRM = re.compile(r"^(yes|i acknowledge|acknowledge|i agree|agree|i accept|accept|i consent|confirm)", re.I)
    for el in page.locator("input[type=checkbox]").all():
        try:
            name = el.get_attribute("name") or el.get_attribute("id") or ""
            if name in seen_cb:
                continue
            seen_cb.add(name)
            group = page.locator(f'input[type=checkbox][name="{name}"]') if el.get_attribute("name") else el
            boxes = group.all() if el.get_attribute("name") else [el]
            if any(b.is_checked() for b in boxes):
                continue
            label, match_text = describe(el)
            ans = match_answer(match_text)
            if ans == SKIP:
                continue
            texts = [b.evaluate(OPTION_LABEL_JS) for b in boxes]
            if all(t.strip().lower() in ("", "on", "true", "yes") for t in texts) and len(boxes) == 1 \
                    and re.search(r"\?\s*\*?\s*$", label) and not AFFIRM.search((ans or "").split("|")[0]):
                continue                                   # hidden input behind Yes/No buttons (handled below)
            if not ans:
                if "*" in label:                       # required checkbox with no rule
                    mark_red(el); todo.append(label); pend("checkbox", el, label, texts, boxes)
                continue
            pick = best_option(ans, texts) if len(boxes) > 1 else None
            if pick:
                boxes[texts.index(pick)].check(force=True); filled.append((label, pick))
            elif len(boxes) == 1 and AFFIRM.search(ans.split("|")[0].strip()):
                el.check(force=True); filled.append((label, "checked"))
            else:
                mark_red(el); todo.append(f"{label[:60]}  [your answer: {ans} | options: {' / '.join(texts)[:150]}]")
                pend("checkbox", el, label, texts, boxes)
        except Exception:
            pass

    # Yes / No buttons (Ashby and others)
    for yes in page.locator("button:text-is('Yes')").all():
        try:
            box = yes.locator("xpath=..")
            no = box.locator("button:text-is('No')")
            if not yes.is_visible() or not no.count():
                continue
            if box.locator("[aria-pressed=true], [aria-checked=true], [data-selected=true], [class*=active], "
                           "[class*=selected]").count():
                continue                                   # already answered
            label, match_text = describe(box.locator("xpath=..").first)
            ans = match_answer(match_text)
            pick = best_option(ans, ["Yes", "No"]) if ans and ans != SKIP else None
            if pick:
                (yes if pick == "Yes" else no.first).click()
                filled.append((label, pick))
            else:
                mark_red(yes); todo.append(label)
                pend("radio", yes, label, ["Yes", "No"], [yes, no.first])
        except Exception:
            pass

    # Radio groups (Yes/No questions, EEO)
    seen = set()
    for el in page.locator("input[type=radio]").all():
        try:
            name = el.get_attribute("name")
            if not name or name in seen:
                continue
            seen.add(name)
            group = page.locator(f'input[type=radio][name="{name}"]')
            if group.evaluate_all("els => els.some(e => e.checked)"):
                continue
            label, match_text = describe(el)
            ans = match_answer(match_text)
            radios = group.all()
            texts = [r.evaluate(OPTION_LABEL_JS) for r in radios]
            pick = best_option(ans, texts) if ans else None
            if pick:
                radios[texts.index(pick)].check(force=True); filled.append((label, pick))
            else:
                mark_red(el)
                todo.append(f"{label[:60]}  [your answer: {ans or 'no rule matched'} | options: "
                            f"{' / '.join(texts)[:200]}]")
                pend("radio", el, label, texts, radios)
        except Exception:
            pass

    return filled, drafted, todo


# ---------------- semantic matching (AI) ----------------
SEMANTIC_MATCH = True      # let the AI map unanswered questions to your profile facts
NEVER_GUESS = re.compile(r"salary|compensation|pay\b|criminal|convict|felony|date of birth|birth|ssn|"
                         r"social security|start date (month|year)|end date (month|year)|address|street|postal|zip|county", re.I)


def profile_facts():
    p = {k: v for k, v in PROFILE.items() if not k.startswith("_")}
    p["answer_rules"] = [{"question_keywords": r["match"], "answer": r["answer"]}
                         for r in PROFILE.get("answer_rules", []) if not r["answer"].startswith("FILL_ME")]
    return json.dumps(p, indent=1)


def semantic_fill(page, job):
    """One AI call per form: map each unanswered question to the candidate's own stated facts."""
    items = [it for it in PENDING if not NEVER_GUESS.search(it["label"])]
    if not (SEMANTIC_MATCH and items):
        return []
    qs = []
    for i, it in enumerate(items, 1):
        opts = f"\n   OPTIONS (copy one exactly): {json.dumps(it['options'])}" if it["options"] else ""
        qs.append(f"{i}. [{it['kind']}] {it['label'][:300]}{opts}")
    try:
        out = generate(
            "You fill a job application for the candidate using ONLY the CANDIDATE FACTS. "
            "Match each question to the fact that answers it, even if worded differently "
            "(e.g. 'unrestricted right to work in which countries' -> the country they are authorized in; "
            "'gender identity' with options Man/Woman -> the stated gender). "
            "For questions with OPTIONS, answer with one option copied EXACTLY. "
            "If the facts do not clearly determine the answer, answer UNKNOWN. Never invent facts. "
            "Return ONLY JSON like {\"1\": \"answer\", \"2\": \"UNKNOWN\"}.",
            f"CANDIDATE FACTS:\n{profile_facts()}\n\nCOMPANY: {job['company']}\n\nQUESTIONS:\n" + "\n".join(qs),
            max_tokens=1500)
        m = re.search(r"\{.*\}", out, re.S)
        answers = json.loads(m.group(0)) if m else {}
    except Exception as e:
        print(f"    semantic match skipped: {str(e)[:100]}")
        return []

    done = []
    for i, it in enumerate(items, 1):
        ans = str(answers.get(str(i), "UNKNOWN")).strip()
        if not ans or ans.upper() == "UNKNOWN":
            continue
        try:
            el, kind = it["el"], it["kind"]
            if kind == "text":
                if set_value(el, ans):
                    done.append((it["label"], ans))
                continue
            pick = ans if ans in it["options"] else best_option(ans, it["options"])
            if not pick:
                continue
            if kind == "select":
                el.select_option(label=pick)
            elif kind in ("radio", "checkbox"):
                it["items"][it["options"].index(pick)].check(force=True)
            elif kind == "combobox":
                el.click(); page.wait_for_timeout(500)
                opt = [o for o in page.locator("[role=option]").all()
                       if o.is_visible() and o.inner_text().strip() == pick]
                if not opt:
                    el.fill(pick[:25]); page.wait_for_timeout(800)
                    opt = [o for o in page.locator("[role=option]").all()
                           if o.is_visible() and o.inner_text().strip() == pick]
                if not opt:
                    page.keyboard.press("Escape"); continue
                opt[0].click(); page.wait_for_timeout(300); page.keyboard.press("Escape")
            el.evaluate("e => { e.style.outline = ''; }")
            done.append((it["label"], pick))
        except Exception:
            pass
    return done


# ---------------- ATS-specific standard fields ----------------
def fill_greenhouse(page, p, resume):
    safe_fill(page, ["#first_name", "label:First Name"], p["first_name"])
    safe_fill(page, ["#last_name", "label:Last Name"], p["last_name"])
    safe_fill(page, ["#email", "label:Email"], p["email"])
    safe_fill(page, ["#phone", "label:Phone"], p["phone"])
    safe_fill(page, ["label:LinkedIn"], p["linkedin"])
    safe_fill(page, ["label:GitHub"], p.get("github") or p["linkedin"])
    safe_fill(page, ["label:Website", "label:Portfolio"], p["linkedin"])
    upload_resume(page, resume, ["input#resume", "input[type=file][id*=resume]", "input[type=file]"])
    return ["button[type=submit]", "#submit_app", "text=Submit application"]


def fill_lever(page, p, resume):
    upload_resume(page, resume, ["input[name=resume]", "input[type=file]"])
    page.wait_for_timeout(2500)
    safe_fill(page, ["input[name=name]"], f"{p['first_name']} {p['last_name']}")
    safe_fill(page, ["input[name=email]"], p["email"])
    safe_fill(page, ["input[name=phone]"], p["phone"])
    safe_fill(page, ["input[name=org]"], p["current_company"])
    safe_fill(page, ["input[name=location]"], p["location"])
    safe_fill(page, ["input[name='urls[LinkedIn]']"], p["linkedin"])
    safe_fill(page, ["input[name='urls[GitHub]']"], p.get("github"))
    return ["#btn-submit", "button[type=submit]", "text=Submit application"]


def fill_ashby(page, p, resume):
    for sel in ["text=Apply for this Job", "text=Application"]:
        if page.locator(sel).count():
            page.locator(sel).first.click(); break
    upload_resume(page, resume, ["input[type=file]"])
    page.wait_for_timeout(2500)
    safe_fill(page, ["label:Name", "label:Full Name"], f"{p['first_name']} {p['last_name']}")
    safe_fill(page, ["label:Email"], p["email"])
    safe_fill(page, ["label:Phone"], p["phone"])
    safe_fill(page, ["label:LinkedIn"], p["linkedin"])
    safe_fill(page, ["label:Location"], p["location"])
    return ["text=Submit Application", "button[type=submit]"]


def fill_generic(page, p, resume):
    """Simple application forms (SmartRecruiters, Workable, BambooHR, JazzHR, Recruitee, Breezy, Rippling...)."""
    upload_resume(page, resume, ["input[type=file][name*=resume i]", "input[type=file][id*=resume i]",
                                 "input[type=file][name*=cv i]", "input[type=file]"])
    page.wait_for_timeout(2500)
    safe_fill(page, ["input[autocomplete=given-name]", "input[name*=first i]", "label:First name"], p["first_name"])
    safe_fill(page, ["input[autocomplete=family-name]", "input[name*=last i]", "label:Last name"], p["last_name"])
    safe_fill(page, ["input[autocomplete=name]", "label:Full name", "label:Name"], f"{p['first_name']} {p['last_name']}")
    safe_fill(page, ["input[type=email]", "input[autocomplete=email]", "label:Email"], p["email"])
    safe_fill(page, ["input[type=tel]", "input[autocomplete=tel]", "label:Phone"], p["phone"])
    safe_fill(page, ["label:LinkedIn"], p["linkedin"])
    safe_fill(page, ["label:Location", "label:City"], p["location"])
    return ["button[type=submit]", "input[type=submit]", "button:has-text('Submit application')",
            "button:has-text('Submit')", "button:has-text('Apply')"]


GENERIC_BOARDS = {"smartrecruiters", "workable", "bamboohr", "jazzhr", "recruitee", "breezy", "rippling",
                  "jobvite", "personio", "pinpoint", "teamtailor", "dover", "generic"}
APPLY_BOARDS = {"greenhouse", "ashby", "workday"} | GENERIC_BOARDS
FILLERS = {"greenhouse": fill_greenhouse, "lever": fill_lever, "ashby": fill_ashby,
           **{b: fill_generic for b in GENERIC_BOARDS}}


# ---------------- main loop ----------------
# Boards that reject applications from script-controlled browsers.
# For these, the script prepares everything and you apply in your normal Chrome.
ASSIST_BOARDS = {"lever"}


def apply_url(job):
    """Company career pages often wrap the Greenhouse form; go straight to the standard form."""
    if job["board"] == "greenhouse" and job["job_id"].startswith("gh_"):
        # Greenhouse's embeddable form: same application, never redirects to the company's site
        return f"https://boards.greenhouse.io/embed/job_app?for={job['company']}&token={job['job_id'][3:]}"
    return job["job_url"]


FORM_SEL = "input[type=file], #first_name, input[name*='first'], input[name='name'], input[autocomplete='given-name']"
ATS_FRAME_RE = re.compile(r"greenhouse\.io|ashbyhq\.com|lever\.co", re.I)


def ensure_form(page, job):
    """Make sure the application form is on screen: wait, open embedded frames, or click Apply."""
    def has_form(timeout):
        try:
            page.wait_for_selector(FORM_SEL, timeout=timeout, state="attached")
            return True
        except Exception:
            return False

    def open_frame():
        for f in page.frames:
            if f != page.main_frame and ATS_FRAME_RE.search(f.url or ""):
                page.goto(f.url, wait_until="domcontentloaded", timeout=45000)
                return has_form(15000)
        return False

    if has_form(15000) or open_frame():
        return True
    for label in [r"^\s*apply( now| for this job| here)?\s*$", r"apply"]:
        btn = page.get_by_role("link", name=re.compile(label, re.I)).or_(
            page.get_by_role("button", name=re.compile(label, re.I)))
        if btn.count():
            try:
                btn.first.click(timeout=5000)
                page.wait_for_timeout(3000)
                if has_form(10000) or open_frame():
                    return True
            except Exception:
                pass
    return False


# ======================================================================
#  WORKDAY  (account login/creation via macOS Keychain, multi-page wizard)
# ======================================================================
import secrets, string, imaplib, email as email_lib
from email.utils import parsedate_to_datetime


def kc_get(service):
    """Read a password from macOS Keychain (never stored in files)."""
    r = subprocess.run(["security", "find-generic-password", "-s", service, "-a", PROFILE["email"], "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def kc_set(service, password):
    subprocess.run(["security", "add-generic-password", "-U", "-s", service, "-a", PROFILE["email"],
                    "-w", password], capture_output=True)


def new_password():
    """Strong unique password that meets Workday's rules (upper, lower, digit, symbol, 16 chars)."""
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%^&*"]
    chars = [secrets.choice(p) for p in pools] + [secrets.choice("".join(pools)) for _ in range(12)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def WD(aid):
    return f'[data-automation-id="{aid}"]'


def wd_visible(page, sel):
    try:
        loc = page.locator(sel)
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def wd_click(page, sel, label=None):
    """Workday puts an invisible 'click_filter' over buttons; try that first."""
    tries = [f'{WD("click_filter")}[aria-label="{label}"]'] if label else []
    for target in tries + [sel]:
        loc = page.locator(target)
        if not loc.count():
            continue
        for force in (False, True):
            try:
                loc.first.click(timeout=4000, force=force)
                return True
            except Exception:
                pass
    return False


def wd_heading(page):
    for sel in (WD("progressBarActiveStep"), WD("pageHeader"), "h2"):
        try:
            t = page.locator(sel).first.inner_text(timeout=1500).strip()
            if t:
                return t
        except Exception:
            pass
    return ""


def wd_wait_for_human(page, what):
    alert_user()
    input(f"  >>> {what} Press ENTER here when done... ")
    page.wait_for_timeout(2000)


def wd_captcha(page):
    if page.locator("iframe[src*='hcaptcha'], iframe[src*='recaptcha'], iframe[title*='captcha' i]").count():
        wd_wait_for_human(page, "Solve the CAPTCHA in the browser (and click the button if needed).")


def gmail_verify_link(since):
    """Optional: read Workday's verification email with a Gmail APP password stored in Keychain."""
    app = kc_get("jobbot-gmail-app")
    if not app:
        return None
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(PROFILE["email"], app)
        for _ in range(12):                                   # up to ~2 minutes
            M.select("INBOX")
            _, data = M.search(None, '(FROM "workday")')
            for num in reversed(data[0].split()[-6:]):
                _, msg = M.fetch(num, "(RFC822)")
                m = email_lib.message_from_bytes(msg[0][1])
                try:
                    if parsedate_to_datetime(m["Date"]).timestamp() < since - 60:
                        continue
                except Exception:
                    continue
                body = ""
                for part in (m.walk() if m.is_multipart() else [m]):
                    if part.get_content_type() in ("text/html", "text/plain"):
                        body += part.get_payload(decode=True).decode(errors="ignore")
                link = re.search(r'https://[^\s"<>]+?(?:verify|activate|confirm|activation)[^\s"<>]*', body, re.I)
                if link:
                    M.logout()
                    return html.unescape(link.group(0))
            time.sleep(10)
        M.logout()
    except Exception as e:
        print(f"    Gmail check failed: {str(e)[:80]}")
    return None


def wd_verify_email(page, job, since):
    link = gmail_verify_link(since)
    if link:
        print("    opening the verification link from your email")
        tab = page.context.new_page()
        try:
            tab.goto(link, wait_until="domcontentloaded", timeout=45000)
            tab.wait_for_timeout(4000)
        finally:
            tab.close()
    else:
        wd_wait_for_human(page, "Workday emailed you a verification link: click it in your email.")
    page.goto(job["job_url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3000)
    wd_start_application(page)


def wd_start_application(page):
    """Click Apply, or Continue/Resume Application when a saved draft exists."""
    started = wd_click(page, WD("adventureButton"))
    if not started:
        btn = page.get_by_role("button", name=re.compile(
            r"^\s*(apply( now)?|continue application|resume application|continue)\s*$", re.I)).or_(
            page.get_by_role("link", name=re.compile(
                r"^\s*(apply( now)?|continue application|resume application|continue)\s*$", re.I)))
        try:
            if btn.count():
                btn.first.click(timeout=5000)
                started = True
        except Exception:
            pass
    if not started:
        wd_click(page, "a:has-text('Apply'), button:has-text('Apply'), a:has-text('Continue Application'), "
                       "button:has-text('Continue Application')")
    page.wait_for_timeout(2500)
    for opt in ("useMyLastApplication", "autofillWithResume", "applyManually"):
        if wd_visible(page, WD(opt)):
            wd_click(page, WD(opt))
            page.wait_for_timeout(3000)
            break


def wd_passwords(page):
    return [p for p in page.locator("input[type=password]").all() if p.is_visible() and not is_trap(p)]


def wd_on_login(page):
    if wd_passwords(page) or wd_visible(page, WD("signInLink")) or wd_visible(page, WD("createAccountLink")):
        return True
    try:
        return bool(re.search(r"^\s*(create account|sign in)\s*$", page.locator("h1, h2, h3").first.inner_text(timeout=800),
                              re.I | re.M))
    except Exception:
        return False


def wd_email_box(page):
    for sel in (f'input{WD("email")}', "input[type=email]", "input[autocomplete=email]"):
        loc = page.locator(sel)
        if loc.count() and loc.first.is_visible():
            return loc.first
    loc = page.get_by_label(re.compile(r"email", re.I))
    return loc.first if loc.count() else None


def wd_press(page, names):
    """Click the visible button whose text matches (Create Account / Sign In / Submit)."""
    for name in names:
        for loc in (page.locator(f'{WD("click_filter")}[aria-label="{name}"]'),
                    page.get_by_role("button", name=re.compile(rf"^\s*{name}\s*$", re.I))):
            try:
                if loc.count() and loc.last.is_visible():
                    loc.last.click(timeout=4000)
                    return True
            except Exception:
                try:
                    loc.last.click(timeout=4000, force=True)
                    return True
                except Exception:
                    pass
    return False


def wd_consent(page):
    for loc in (page.locator(f'input{WD("createAccountCheckbox")}'),
                page.get_by_label(re.compile(r"consent|agree|terms", re.I))):
        try:
            if loc.count() and not loc.first.is_checked():
                loc.first.check(force=True)
                return
        except Exception:
            pass


def wd_sign_in(page, password):
    if len(wd_passwords(page)) != 1:                       # on the create-account form: switch to sign in
        if not wd_click(page, WD("signInLink")):
            link = page.get_by_role("link", name=re.compile(r"sign in", re.I)).or_(
                page.get_by_role("button", name=re.compile(r"^\s*sign in\s*$", re.I)))
            if link.count():
                link.first.click()
        page.wait_for_timeout(2000)
    box, pw = wd_email_box(page), wd_passwords(page)
    if not box or not pw:
        return False
    set_value(box, PROFILE["email"])
    set_value(pw[0], password)
    wd_captcha(page)
    if not wd_click(page, WD("signInSubmitButton"), "Sign In"):
        wd_press(page, ["Sign In"])
    page.wait_for_timeout(5000)
    wd_captcha(page)
    return not wd_passwords(page)


def wd_account(page, job):
    """Sign in with the saved password for this company, or create an account and save a new one."""
    if not wd_on_login(page):
        return True
    service = f"jobbot-workday-{job['company']}"
    saved = kc_get(service) or kc_get("jobbot-default")
    if saved and wd_sign_in(page, saved):
        print("    signed in to Workday")
        return True
    if saved:
        print("    saved password didn't work for this company; creating an account instead")
    if len(wd_passwords(page)) < 2:                        # open the create-account form
        if not wd_click(page, WD("createAccountLink")):
            link = page.get_by_role("link", name=re.compile(r"create account", re.I)).or_(
                page.get_by_role("button", name=re.compile(r"create account", re.I)))
            if link.count():
                link.first.click()
        page.wait_for_timeout(2000)
    pw_boxes, email_box = wd_passwords(page), wd_email_box(page)
    if len(pw_boxes) < 2 or not email_box:
        print("    couldn't find the create-account fields")
        return False
    password = kc_get("jobbot-default") or new_password()
    kc_set(service, password)                              # saved BEFORE submitting, so it's never lost
    set_value(email_box, PROFILE["email"])
    set_value(pw_boxes[0], password)
    set_value(pw_boxes[1], password)
    wd_consent(page)
    wd_captcha(page)
    since = time.time()
    if not wd_click(page, WD("createAccountSubmitButton"), "Create Account"):
        wd_press(page, ["Create Account"])
    page.wait_for_timeout(5000)
    body = page.inner_text("body")
    if re.search(r"verif|activate|confirm your", body, re.I):
        wd_verify_email(page, job, since)
    if wd_on_login(page):
        if not wd_sign_in(page, password):
            return False
    print("    Workday account ready (password saved in Keychain)")
    return True


def wd_select(page, button, answer):
    """Workday dropdown (button + listbox): open, read options, pick best match."""
    try:
        button.click(timeout=3000)
        page.wait_for_timeout(700)
        opts = [o for o in page.locator("[role=option]").all() if o.is_visible()]
        texts = [o.inner_text().strip() for o in opts]
        pick = best_option(answer, texts)
        if pick:
            opts[texts.index(pick)].click()
            page.wait_for_timeout(400)
            return pick, texts
        page.keyboard.press("Escape")
        return None, texts
    except Exception:
        return None, []


def wd_fill_known(page, filled):
    p = PROFILE
    zip_code = p.get("zip") if p.get("zip") and not str(p.get("zip")).startswith("FILL_ME") else None
    addr = p.get("address_line1") if not str(p.get("address_line1", "")).startswith("FILL_ME") else None
    county = p.get("county") if not str(p.get("county", "")).startswith("FILL_ME") else None
    for aid, val in [("legalNameSection_firstName", p["first_name"]), ("legalNameSection_lastName", p["last_name"]),
                     ("addressSection_addressLine1", addr), ("addressSection_regionSubdivision1", county),
                     ("addressSection_city", p.get("city")), ("addressSection_postalCode", zip_code),
                     ("phone-number", p["phone"]), ("email", p["email"])]:
        loc = page.locator(f'input{WD(aid)}')
        if not loc.count():                                # some companies only label the field
            label_map = {"addressSection_addressLine1": r"address line 1", "addressSection_city": r"^\s*city",
                         "addressSection_postalCode": r"postal code|zip", "addressSection_regionSubdivision1": r"^\s*county"}
            if aid in label_map:
                loc = page.get_by_label(re.compile(label_map[aid], re.I))
        if not (val and loc.count() and loc.first.is_visible()):
            continue
        if aid == "email" and wd_passwords(page):
            continue
        current = (loc.first.input_value() or "").strip()
        overwrite = aid.startswith("addressSection_")      # your profile beats Workday's saved address
        if not current or (overwrite and current.lower() != str(val).lower()):
            set_value(loc.first, val)
            filled.append((aid, val))
    for aid, ans in [("phone-device-type", f"{p.get('phone_type', 'Mobile')}|Mobile|Cell"),
                     ("countryPhoneCode", "United States of America (+1)|United States (+1)|+1"),
                     ("addressSection_countryRegion", "United States of America|United States"),
                     ("addressSection_countryRegion_dropdown", "United States of America|United States"),
                     ("addressSection_region", p.get("state", ""))]:
        btn = page.locator(f'button{WD(aid)}')
        if not (ans and btn.count() and btn.first.is_visible()):
            continue
        current = btn.first.inner_text().strip()
        empty = re.search(r"^\s*(select one)?\s*$", current, re.I)
        wrong_state = aid == "addressSection_region" and current.lower() != str(ans).lower()
        if empty or wrong_state:
            pick, _ = wd_select(page, btn.first, ans)
            if pick:
                filled.append((aid, pick))
    agree = page.locator(f'input{WD("agreementCheckbox")}')
    if agree.count() and not agree.first.is_checked():
        agree.first.check(force=True)
        filled.append(("terms and conditions", "checked"))


def wd_dropdowns(page, job, filled, todo):
    for btn in page.locator('button[aria-haspopup="listbox"]').all():
        try:
            if not btn.is_visible() or not re.search(r"^\s*(select one)?\s*$", btn.inner_text(), re.I):
                continue
            label, match_text = describe(btn)
            ans = match_answer(match_text)
            pick, texts = wd_select(page, btn, ans) if ans else (None, None)
            if pick:
                filled.append((label, pick))
                continue
            if texts is None:
                btn.click(timeout=3000); page.wait_for_timeout(600)
                texts = [o.inner_text().strip() for o in page.locator("[role=option]").all() if o.is_visible()]
                page.keyboard.press("Escape")
            mark_red(btn)
            todo.append(f"{label[:60]}  [your answer: {ans or 'no rule matched'} | options: {' / '.join(texts)[:150]}]")
            pend("combobox", btn, label, texts[:60])
        except Exception:
            pass


def wd_source(page, filled):
    """'How did you hear about us?' multi-select search box (strictly scoped to source field, never Skills/Education)."""
    scope = page.locator('[data-automation-id*="formField-source" i], [data-automation-id*="sourceSection" i], [data-automation-id*="hearAbout" i], [data-automation-id*="HowDidYouHear" i]')
    if scope.count():
        box = scope.locator(f'{WD("multiselectInputContainer")} input, input{WD("searchBox")}, input')
    else:
        box = page.get_by_label(re.compile(r"how did you hear|^\s*source\b", re.I))
    if not box.count() or not box.first.is_visible():
        return
    field_wrap = box.first.locator('xpath=ancestor::*[@data-automation-id^="formField"][1]')
    if field_wrap.count() and field_wrap.locator(f'{WD("selectedItem")}').count():
        return
    rule = match_answer("how did you hear") or "Website"
    for alt in [a.strip() for a in rule.split("|")] + ["LinkedIn", "Website", "Job Board", "Other"]:
        try:
            box.first.fill(alt); page.keyboard.press("Enter"); page.wait_for_timeout(1200)
            opts = [o for o in page.locator(f'{WD("promptOption")}, [role=option]').all() if o.is_visible()]
            texts = [o.inner_text().strip() for o in opts]
            pick = best_option(alt, texts) or (texts[0] if texts else None)
            if pick:
                opts[texts.index(pick)].click(); page.wait_for_timeout(800)
                filled.append(("How did you hear about us?", pick))
                page.keyboard.press("Escape")
                return
        except Exception:
            pass


def wd_month_year(page, scope, prefix, value):
    """value like '09/2024' or 'Present'."""
    m = re.match(r"(\d{1,2})/(\d{4})", value or "")
    if not m:
        return
    for aid, v in (("dateSectionMonth-input", m.group(1).zfill(2)), ("dateSectionYear-input", m.group(2))):
        loc = scope.locator(f'{WD(prefix)} input{WD(aid)}') if prefix else scope.locator(f'input{WD(aid)}')
        if loc.count():
            try:
                loc.first.click(); loc.first.type(v, delay=60)
            except Exception:
                pass


def resume_bullets(job, company):
    """That role's bullets from the tailored resume (plain text) for Workday's Role Description."""
    try:
        md = open(job["resume_path"][:-4] + ".md", encoding="utf-8").read()
    except Exception:
        return ""
    sec = re.search(rf"^###[^\n]*{re.escape(company)}[^\n]*\n(.*?)(?=^###|^## |\Z)", md, re.M | re.S | re.I)
    if not sec:
        return ""
    lines = [re.sub(r"[*_`]", "", l.strip()[2:]).strip() for l in sec.group(1).splitlines()
             if l.strip().startswith(("- ", "* "))]
    return "\n".join(f"• {l}" for l in lines)[:2000]


PANEL_JS = r"""
(e, i) => {   // widest ancestor that still contains only THIS entry's 'Job Title' box
  const isTitle = x => {
    const l = x.id ? document.querySelector(`label[for="${CSS.escape(x.id)}"]`) : null;
    const t = ((l && l.innerText) || x.getAttribute('aria-label') || '').toLowerCase();
    return /job title/.test(t);
  };
  let n = e, best = e.parentElement || e;
  while (n.parentElement && n.parentElement !== document.body) {
    n = n.parentElement;
    const titles = Array.from(n.querySelectorAll('input')).filter(isTitle);
    if (titles.length > 1) break;
    best = n;
  }
  best.setAttribute('data-jb-panel', String(i));
  return true;
}
"""


def wd_entries(page):
    """Visible Work Experience entries, each as (job_title_input, panel)."""
    out = []
    titles = page.get_by_label(re.compile(r"^\s*job title", re.I))
    k = 0
    for i in range(titles.count()):
        t = titles.nth(i)
        try:
            if not t.is_visible():
                continue
            k += 1
            t.evaluate(PANEL_JS, k)
            out.append((t, page.locator(f'[data-jb-panel="{k}"]').first))
        except Exception:
            pass
    return out


def wd_in(panel, label_re, kind="input"):
    loc = panel.get_by_label(re.compile(label_re, re.I))
    for i in range(loc.count()):
        el = loc.nth(i)
        try:
            if el.is_visible() and el.evaluate("e => e.tagName").lower() in (("textarea",) if kind == "textarea"
                                                                            else ("input",)):
                return el
        except Exception:
            pass
    return None


MONTH_SEL = ('[data-automation-id="dateSectionMonth-input"], [data-automation-id="dateSectionMonth-display"], '
             '[role=spinbutton][aria-label*="Month" i], input[aria-label*="Month" i]')
YEAR_SEL = ('[data-automation-id="dateSectionYear-input"], [data-automation-id="dateSectionYear-display"], '
            '[role=spinbutton][aria-label*="Year" i], input[aria-label*="Year" i]')


def wd_dates(panel, which, value):
    """which: 0 = From, 1 = To. value 'MM/YYYY'. Types like a person: click the box, type month then year."""
    m = re.match(r"(\d{1,2})/(\d{4})", value or "")
    if not m:
        return False
    mm, yyyy = m.group(1).zfill(2), m.group(2)
    page = panel.page
    try:
        months = [x for x in panel.locator(MONTH_SEL).all() if x.is_visible()]
        if len(months) > which:
            months[which].click()
            page.keyboard.type(mm, delay=90)
            years = [x for x in panel.locator(YEAR_SEL).all() if x.is_visible()]
            if len(years) > which:
                years[which].click()
            page.keyboard.type(yyyy, delay=90)
            page.keyboard.press("Tab")
            return True
        boxes = [x for x in panel.get_by_text(re.compile(r"^\s*MM\s*/\s*YYYY\s*$")).all() if x.is_visible()]
        if len(boxes) > which:                               # display-only box: click it, then type
            boxes[which].click()
            page.keyboard.type(mm + yyyy, delay=90)
            page.keyboard.press("Tab")
            return True
    except Exception:
        pass
    return False


PANEL_DATE_JS = r"""
([panelEl, which, part, expectedMM, expectedYYYY]) => {
  const re = new RegExp('^\\s*' + which + '\\b', 'i');
  const labels = Array.from(panelEl.querySelectorAll('label, legend, [data-automation-id="formLabel"]'))
    .filter(l => l.offsetParent !== null && re.test((l.innerText || '').trim()));
  const boxes = [];
  for (const l of labels) {
    let b = l.closest('[data-automation-id^="formField"]') || l.closest('fieldset') || l.parentElement;
    for (let k = 0; k < 4 && b && !b.querySelector('input, [role=spinbutton]'); k++) b = b.parentElement;
    if (b && !boxes.includes(b)) boxes.push(b);
  }
  const box = boxes[0];
  if (!box) return {ok: false, why: which + ' box not found in panel'};
  const pick = (sel) => box.querySelector(sel);
  const month = pick('input[data-automation-id*="Month"], input[aria-label*="Month" i], input[id*="Month"], [role=spinbutton][aria-label*="Month" i]');
  const year = pick('input[data-automation-id*="Year"], input[aria-label*="Year" i], input[id*="Year"], [role=spinbutton][aria-label*="Year" i]');
  const any = pick('input:not([type=hidden]):not([type=checkbox])');
  if (part === 'check') {
    const mVal = month ? (month.value || month.getAttribute('aria-valuenow') || month.innerText || '').trim() : '';
    const yVal = year ? (year.value || year.getAttribute('aria-valuenow') || year.innerText || '').trim() : '';
    const disp = Array.from(box.querySelectorAll('[data-automation-id*="display"], input, [role=spinbutton]'))
      .map(x => (x.value || x.getAttribute('aria-valuenow') || x.innerText || '')).join(' ');
    const expM = String(parseInt(expectedMM, 10));
    const mOk = mVal ? (mVal === expectedMM || mVal === expM) : (disp.includes(expectedMM) || disp.includes(expM));
    const yOk = yVal ? (yVal === expectedYYYY) : disp.includes(expectedYYYY);
    return {ok: Boolean(mOk && yOk), disp: disp.slice(0, 60)};
  }
  const target = part === 'year' ? year : (month || any);
  if (!target) return {ok: false, why: part + ' input not found'};
  target.scrollIntoView({block: 'center'});
  target.focus();
  if (target.select) target.select();
  return {ok: true, hasYear: !!year};
}
"""


def wd_set_panel_date(page, panel, which, value, filled, tag):
    """Set and verify exact MM/YYYY inside a specific Work Experience panel (overwrites wrong pre-filled dates)."""
    m = re.match(r"(\d{1,2})/(\d{4})", str(value or ""))
    if not m:
        return
    mm, yyyy = m.group(1).zfill(2), m.group(2)
    for attempt in range(2):
        try:
            el_handle = panel.element_handle(timeout=2000)
            if not el_handle:
                return
            chk = page.evaluate(PANEL_DATE_JS, [el_handle, which, "check", mm, yyyy])
            if chk.get("ok"):
                return
            r = page.evaluate(PANEL_DATE_JS, [el_handle, which, "month", mm, yyyy])
            if not r.get("ok"):
                # Fallback to wd_dates on panel
                wd_dates(panel, 0 if which == "From" else 1, value)
                return
            page.keyboard.press("Meta+A")
            page.keyboard.press("Backspace")
            page.keyboard.type(mm, delay=80)
            if r.get("hasYear"):
                page.evaluate(PANEL_DATE_JS, [el_handle, which, "year", mm, yyyy])
                page.keyboard.press("Meta+A")
                page.keyboard.press("Backspace")
            page.keyboard.type(yyyy, delay=80)
            page.keyboard.press("Tab")
            page.wait_for_timeout(350)
            chk2 = page.evaluate(PANEL_DATE_JS, [el_handle, which, "check", mm, yyyy])
            if chk2.get("ok"):
                filled.append((tag, f"{mm}/{yyyy}"))
                return
        except Exception as e:
            print(f"    panel date ({tag}): {str(e)[:80]}")


def wd_prompt_pick(page, input_loc, search_terms, filled, label_name):
    """Type into a Workday multiselect prompt input, press Enter, and pick the best matching option."""
    try:
        if not input_loc.count() or not input_loc.first.is_visible():
            return False
        for term in search_terms:
            input_loc.first.scroll_into_view_if_needed()
            input_loc.first.click(timeout=2000)
            input_loc.first.fill(term)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1200)
            opts = [o for o in page.locator(f'{WD("promptOption")}, [role=option]').all() if o.is_visible()]
            texts = [o.inner_text().strip() for o in opts if o.inner_text().strip()]
            pick = best_option(term, texts) or (texts[0] if texts else None)
            if pick:
                opts[texts.index(pick)].click()
                page.wait_for_timeout(600)
                page.keyboard.press("Escape")
                filled.append((label_name, pick))
                return True
            page.keyboard.press("Escape")
    except Exception:
        pass
    return False


def wd_fill_education_and_skills(page, filled):
    """Fill School, Degree, Field of Study (Computer Science), and Skills on Workday's My Experience page."""
    school = page.get_by_label(re.compile(r"^\s*school|university", re.I))
    if school.count() and school.first.is_visible() and not (school.first.input_value() or "").strip():
        set_value(school.first, PROFILE["school"])
        filled.append(("School", PROFILE["school"]))

    deg_btn = page.get_by_label(re.compile(r"^\s*degree\b", re.I))
    for i in range(deg_btn.count()):
        b = deg_btn.nth(i)
        try:
            if b.is_visible() and b.evaluate("e => e.tagName.toLowerCase() === 'button'") and re.search(r"select one|^$", b.inner_text(), re.I):
                pick, _ = wd_select(page, b, "Master's Degree|Master of Science|Masters|MS|Master's")
                if pick:
                    filled.append(("Degree", pick))
        except Exception:
            pass

    fos_box = page.locator('[data-automation-id*="formField-field-of-study" i], [data-automation-id*="fieldOfStudy" i]')
    if not fos_box.count():
        fos_input = page.get_by_label(re.compile(r"field of study|major|discipline", re.I))
        if fos_input.count():
            fos_box = fos_input.first.locator('xpath=ancestor::*[@data-automation-id^="formField"][1]')
    if fos_box.count() and fos_box.first.is_visible():
        if not fos_box.first.locator(WD("selectedItem")).count():
            inp = fos_box.first.locator("input")
            wd_prompt_pick(page, inp, [PROFILE.get("discipline", "Computer Science"), "Computer Science", "Computer"], filled, "Field of Study")

    skills_box = page.locator('[data-automation-id*="formField-skills" i], [data-automation-id*="skillsSection" i]')
    skills_inp = page.get_by_label(re.compile(r"type to add skills|^\s*skills\b", re.I))
    target_box = skills_box.first if skills_box.count() else (skills_inp.first.locator('xpath=ancestor::*[@data-automation-id^="formField"][1]') if skills_inp.count() else None)
    if target_box and target_box.count() and target_box.is_visible():
        for chip in target_box.locator(WD("selectedItem")).all():
            try:
                if re.search(r"corporate", chip.inner_text(), re.I):
                    del_btn = chip.locator('[data-automation-id*="DELETE" i], button, svg')
                    if del_btn.count():
                        del_btn.first.click()
                        page.wait_for_timeout(400)
            except Exception:
                pass
        existing_chips = target_box.locator(WD("selectedItem")).count()
        if existing_chips < 3:
            inp = skills_inp if skills_inp.count() else target_box.locator("input")
            for sk in (PROFILE.get("skills") or ["Python", "SQL", "Apache Spark", "Snowflake", "AWS"])[:5]:
                wd_prompt_pick(page, inp, [sk], filled, f"Skill ({sk})")


def wd_experience(page, filled, job=None):
    """Make Work Experience match profile.json exactly: add/delete entries, then fill and verify every field."""
    jobs = [w for w in PROFILE.get("work_history", []) if not str(w.get("title", "")).startswith("FILL_ME")]
    if not jobs:
        return
    add_btn = lambda: page.get_by_role("button", name=re.compile(r"^\s*add( another)?\s*$", re.I)).first
    for _ in range(8):
        if len(wd_entries(page)) >= len(jobs):
            break
        try:
            add_btn().click(); page.wait_for_timeout(1500)
        except Exception:
            break
    for _ in range(8):
        entries = wd_entries(page)
        if len(entries) <= len(jobs):
            break
        _, panel = entries[-1]
        btn = panel.get_by_role("button", name=re.compile(r"delete|remove", re.I))
        if not btn.count():
            btn = page.get_by_role("button", name=re.compile(r"delete|remove", re.I))
        try:
            btn.last.click(); page.wait_for_timeout(1500)
            confirm = page.get_by_role("button", name=re.compile(r"^\s*(ok|yes|delete|confirm)\s*$", re.I))
            if confirm.count() and confirm.last.is_visible():
                confirm.last.click(); page.wait_for_timeout(1000)
            filled.append(("Removed extra work experience entry", "deleted"))
        except Exception:
            break

    entries = wd_entries(page)
    used, assigned = set(), []
    for i, (title_box, panel) in enumerate(entries, 1):
        try:
            company_box = wd_in(panel, r"^\s*company")
            have_company = (company_box.input_value() or "").strip().lower() if company_box else ""
            match = next((k for k, w in enumerate(jobs) if k not in used and have_company and
                          w["company"].lower() in have_company), None)
            if match is None:
                match = next((k for k in range(len(jobs)) if k not in used), None)
            if match is None:
                break
            used.add(match)
            w = jobs[match]
            if (title_box.input_value() or "").strip().lower() != w["title"].lower():
                set_value(title_box, w["title"])
            if company_box and have_company != w["company"].lower():
                set_value(company_box, w["company"])
            loc_box = wd_in(panel, r"^\s*location")
            if loc_box and w.get("location") and not (loc_box.input_value() or "").strip():
                set_value(loc_box, w["location"])
            current = str(w.get("end", "")).lower() == "present"
            cb = panel.get_by_label(re.compile(r"currently work", re.I))
            if cb.count():
                if current and not cb.first.is_checked():
                    cb.first.check(force=True)
                    page.wait_for_timeout(500)
                elif not current and cb.first.is_checked():
                    cb.first.uncheck(force=True)
                    page.wait_for_timeout(500)
            assigned.append(w)
            filled.append((f"Work experience {i}", f"{w['title']} at {w['company']} ({w.get('start')} - {w.get('end')})"))
        except Exception as e:
            print(f"    work experience {i}: {str(e)[:80]}")

    page.wait_for_timeout(600)
    entries_now = wd_entries(page)
    for (_, panel), w in zip(entries_now, assigned):
        current = str(w.get("end", "")).lower() == "present"
        wd_set_panel_date(page, panel, "From", w.get("start"), filled, f"{w['company']} From")
        if not current:
            wd_set_panel_date(page, panel, "To", w.get("end"), filled, f"{w['company']} To")
        desc_box = wd_in(panel, r"role description|^\s*description", kind="textarea")
        text = resume_bullets(job, w.get("company", "")) if job else ""
        if desc_box and text:
            try:
                if not (desc_box.input_value() or "").strip():
                    set_value(desc_box, text)
            except Exception:
                pass
    wd_fill_education_and_skills(page, filled)


def wd_labeled(page, label_re, kind="input"):
    """Visible fields with this label, in page order."""
    out = []
    loc = page.get_by_label(re.compile(label_re, re.I))
    for i in range(loc.count()):
        el = loc.nth(i)
        try:
            tag = el.evaluate("e => e.tagName").lower()
            ok_tag = tag == "textarea" if kind == "textarea" else tag != "textarea"
            if ok_tag and el.is_visible() and not is_trap(el):
                out.append(el)
        except Exception:
            pass
    return out


def wd_type_date(page, box, value):
    """Click the date box (From/To) and type MMYYYY like a person; skip if it already has a value."""
    m = re.match(r"(\d{1,2})/(\d{4})", value or "")
    if not m:
        return
    try:
        current = (box.input_value() or "").strip() if box.evaluate("e => 'value' in e") else ""
        if re.search(r"\d", current):
            return
        box.click()
        page.keyboard.type(m.group(1).zfill(2), delay=70)
        page.keyboard.type(m.group(2), delay=70)
        page.keyboard.press("Tab")
    except Exception:
        try:                                              # fall back to the month/year pieces near it
            wd_dates(box.locator("xpath=ancestor::*[4]"), 0, value)
        except Exception:
            pass


def wd_today(page, filled):
    """Self-identify page signature date."""
    now = datetime.now()
    for aid, v in (("dateSectionMonth-input", f"{now.month:02d}"), ("dateSectionDay-input", f"{now.day:02d}"),
                   ("dateSectionYear-input", str(now.year))):
        loc = page.locator(f'input{WD(aid)}')
        if loc.count() and loc.first.is_visible() and not (loc.first.input_value() or "").strip():
            try:
                loc.first.click(); loc.first.type(v, delay=60)
            except Exception:
                pass
    name = page.locator(f'input{WD("name")}')
    if name.count() and name.first.is_visible() and not (name.first.input_value() or "").strip():
        set_value(name.first, FULL_NAME); filled.append(("Signature name", FULL_NAME))


WD_SUBMIT = [f'button{WD("bottom-navigation-next-button")}:has-text("Submit")',
             f'button{WD("pageFooterNextButton")}:has-text("Submit")', 'button:has-text("Submit")']


def wd_apply(page, job):
    """Walk the Workday wizard up to the Review page. Returns (filled, drafted, todo, submit_selectors)."""
    filled, drafted, todo = [], [], []
    page.goto(job["job_url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3500)
    wd_start_application(page)
    if not wd_account(page, job):
        return filled, drafted, ["Workday sign-in didn't complete: finish it in the browser"], WD_SUBMIT
    page.wait_for_timeout(2500)
    if wd_visible(page, WD("autofillWithResume")) or wd_visible(page, WD("applyManually")):
        wd_start_application(page)
    uploaded, last, same = False, "", 0
    for _ in range(14):
        page.wait_for_timeout(2500)
        wd_captcha(page)
        if wd_on_login(page):
            if not wd_account(page, job):
                return filled, drafted, ["Workday sign-in didn't complete: finish it in the browser"], WD_SUBMIT
            page.wait_for_timeout(2500)
            if wd_visible(page, WD("autofillWithResume")) or wd_visible(page, WD("applyManually")):
                wd_start_application(page)
            continue
        heading = re.sub(r"\s+", " ", wd_heading(page))
        print(f"    Workday page: {heading or '(untitled)'}")
        if re.search(r"\breview\b", heading, re.I):
            return filled, drafted, [], WD_SUBMIT
        same = same + 1 if heading == last else 0
        if same >= 2:
            alert_user()
            print(f"    Workday won't move past '{heading}' (no error shown). A field is probably wrong or empty.")
            ans = input("    Check the page, fix it, click 'Save and Continue' yourself if needed, then press ENTER "
                        "(s = stop here): ").strip().lower()
            if ans == "s":
                break
            same = 0
        last = heading
        file_in = page.locator(f'input{WD("file-upload-input-ref")}, input[type=file]')
        if file_in.count() and not uploaded and not page.locator(WD("file-upload-successful")).count():
            try:
                file_in.first.set_input_files(job["resume_path"]); uploaded = True
                page.wait_for_timeout(4000); filled.append(("Resume", os.path.basename(job["resume_path"])))
            except Exception:
                pass
        wd_fill_known(page, filled)
        wd_source(page, filled)
        if re.search(r"experience", heading, re.I):
            wd_experience(page, filled, job)
        if re.search(r"self.?identif|disabilit", heading, re.I):
            wd_today(page, filled)
        f, d, t = answer_questions(page, job)
        page_todo = list(t)
        wd_dropdowns(page, job, f, page_todo)
        ai = semantic_fill(page, job)
        if ai:
            solved = {q for q, _ in ai}
            page_todo = [x for x in page_todo if not any(x.startswith(q[:60]) for q in solved)]
            f += [(q, f"{a}  (AI-matched)") for q, a in ai]
        filled += f; drafted += d
        for attempt in range(4):
            clicked = wd_click(page, f'button{WD("bottom-navigation-next-button")}', "Save and Continue") or \
                wd_click(page, f'button{WD("pageFooterNextButton")}', "Next") or wd_press(page, ["Save and Continue", "Next"])
            page.wait_for_timeout(3500)
            errs = page.locator(f'{WD("errorMessage")}, {WD("errorBanner")}, [role=alert]')
            msgs = [e.inner_text().strip()[:100] for e in errs.all() if e.is_visible() and e.inner_text().strip()][:6]
            stuck = (not clicked) or (msgs and wd_heading(page) == heading)
            if not stuck:
                break
            alert_user()
            print(f"    Workday needs something on '{heading}':")
            for m in msgs or ["couldn't find the Save and Continue button"]:
                print(f"      - {m}")
            ans = input("    Fix it in the browser, then press ENTER to continue  (s = stop here, finish later): ")
            if ans.strip().lower() == "s":
                return filled, drafted, page_todo + [f"Workday '{heading}': {m}" for m in msgs], WD_SUBMIT
    return filled, drafted, ["Workday stopped before the Review page: continue in the browser"], WD_SUBMIT


def prepare(page, job):
    """Fill the form (never submits). Returns (filled, drafted, todo)."""
    if job["board"] == "workday":
        filled, drafted, todo, sel = wd_apply(page, job)
        drafted_q = [q for q, _ in drafted]
        todo = list(dict.fromkeys(t for t in todo if not any(t.startswith(q[:60]) for q in drafted_q)))
        return list(dict.fromkeys(filled)), drafted, todo, sel
    page.goto(apply_url(job), wait_until="domcontentloaded", timeout=45000)
    if not ensure_form(page, job) and job["job_url"] != apply_url(job):
        page.goto(job["job_url"], wait_until="domcontentloaded", timeout=45000)   # try the company page
        ensure_form(page, job)
    if not page.locator(FORM_SEL).count():
        return [], [], ["application form not found on the page"], ["button[type=submit]"]
    page.wait_for_timeout(1500)
    submit_selectors = FILLERS[job["board"]](page, PROFILE, job["resume_path"])
    page.wait_for_timeout(1000)
    filled, drafted, _ = answer_questions(page, job)
    page.wait_for_timeout(800)
    fill_signature(page, filled)
    page.evaluate("""() => document.querySelectorAll('*').forEach(e => {
        if ((e.style.outline || '').includes('red')) e.style.outline = ''; })""")
    f2, d2, todo = answer_questions(page, job)
    ai = semantic_fill(page, job)
    if ai:
        solved = {q for q, _ in ai}
        todo = [t for t in todo if not any(t.startswith(q[:60]) for q in solved)]
        f2 += [(q, f"{a}  (AI-matched)") for q, a in ai]
    drafted_all = drafted + d2
    todo = list(dict.fromkeys(t for t in todo if not any(t.startswith(q[:60]) for q, _ in drafted_all)))
    seen, merged = set(), []
    for q, a in filled + f2:
        if (q, a) not in seen:
            seen.add((q, a)); merged.append((q, a))
    return merged, drafted + d2, todo, submit_selectors


SUCCESS_RE = re.compile(r"thank you|application (has been )?(submitted|received)|we('ve| have) received|"
                        r"successfully submitted|thanks for applying", re.I)


CODE_RE = re.compile(r"verification code|security code|enter the code|code (was|has been) sent|"
                     r"check your (email|inbox)|confirm your email|one-time (code|passcode)", re.I)


def alert_user():
    try:
        subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], timeout=5)
    except Exception:
        print("\a")


def wait_for_confirmation(page, seconds):
    for _ in range(seconds):
        page.wait_for_timeout(1000)
        try:
            if SUCCESS_RE.search(page.inner_text("body")):
                return True
        except Exception:
            pass
    return False


def handle_code_screen(page):
    """Email security code screen: pause for the user, then confirm."""
    alert_user()
    print("\n  >>> The site emailed you a verification code.")
    print("  >>> Enter the code in the browser and click Submit there.")
    input("  >>> Press ENTER here when done... ")
    if wait_for_confirmation(page, 20):
        return True, "submitted after verification code"
    ok = input("  Did you see a confirmation / thank-you page? [y/n]: ").strip().lower() == "y"
    return ok, "confirmed by you" if ok else "verification not completed"


def submit_and_confirm(page, submit_selectors):
    """Click Submit, wait, and check the page for a confirmation message."""
    for sel in submit_selectors:
        loc = page.locator(sel)
        if loc.count() and loc.first.is_visible():
            loc.first.click()
            break
    else:
        return False, "submit button not found"
    for _ in range(20):                                   # up to ~20s for confirmation or a code screen
        page.wait_for_timeout(1000)
        try:
            body = page.inner_text("body")
        except Exception:
            continue
        if SUCCESS_RE.search(body):
            return True, "confirmation detected"
        if CODE_RE.search(body):
            return handle_code_screen(page)
    return False, "no confirmation message (check the browser: CAPTCHA or a missing required field?)"


def write_answer_sheet(job, filled, drafted, todo):
    os.makedirs("answers", exist_ok=True)
    p = PROFILE
    lines = [f"APPLICATION ANSWER SHEET - {job['company']} | {job['title']}", job["job_url"], "",
             f"RESUME TO UPLOAD: {job['resume_path']}", "",
             "BASIC DETAILS",
             f"  Full name: {FULL_NAME}", f"  First name: {p['first_name']}", f"  Last name: {p['last_name']}",
             f"  Email: {p['email']}", f"  Phone: {p['phone']}", f"  Location: {p['location']}",
             f"  Current company: {p['current_company']}", f"  LinkedIn: {p['linkedin']}",
             f"  GitHub: {p.get('github', '')}", f"  Today's date: {TODAY}", "",
             "QUESTIONS ON THIS FORM"]
    lines += [f"  Q: {q}\n  A: {a}\n" for q, a in filled]
    if drafted:
        lines += ["", "WRITTEN ANSWERS (read and edit before pasting)"]
        lines += [f"  Q: {q}\n  A: {a}\n" for q, a in drafted]
    if todo:
        lines += ["", "YOU NEED TO ANSWER THESE"] + [f"  - {q}" for q in todo]
    path = os.path.abspath(f"answers/{job['job_id']}.txt")
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    return path


def print_summary(filled, drafted, todo):
    print(f"  Prepared {len(filled)} answers.")
    for q, a in filled:
        print(f"    ✓ {q[:70]}  ->  {a}")
    for q, _ in drafted:
        print(f"    ✎ {q[:90]}  (AI draft, review it)")
    for q in todo:
        print(f"    ✗ {q[:300]}")


AUTO_MIN_ATS = 90          # auto-submit only if the resume's ATS check scored at least this
DEFAULT_DAILY_LIMIT = 25   # max auto-submissions per run


def auto_ok(job, todo, drafted):
    """Safety gate for submitting without a human look."""
    if job["board"] in GENERIC_BOARDS or job["board"] == "workday":
        return False, f"{job['board']} application: always reviewed by you"
    notes = job["notes"] or ""
    m = re.search(r"ATS (\d+)%, (\d+) issues", notes)
    if todo:
        return False, f"{len(todo)} unanswered field(s)"
    if drafted:
        return False, "has AI-written answers to read"
    if not m:
        return False, "no ATS report"
    if int(m.group(1)) < AUTO_MIN_ATS or int(m.group(2)) > 0:
        return False, f"resume check: ATS {m.group(1)}%, {m.group(2)} issue(s)"
    return True, ""


def open_browser(pw):
    try:
        return pw.chromium.launch_persistent_context(
            os.path.abspath("browser_profile"), channel="chrome", headless=False, chromium_sandbox=True,
            viewport={"width": 1300, "height": 900}, args=["--hide-crash-restore-bubble"])
    except Exception:
        return pw.chromium.launch_persistent_context(
            os.path.abspath("browser_profile"), headless=False, viewport={"width": 1300, "height": 900},
            args=["--hide-crash-restore-bubble"])


def safe_shot(page, path):
    try:
        page.screenshot(path=path, full_page=True)
    except Exception:
        pass


def live_page(pw, browser, page):
    """Return a working (browser, page); reopen Chrome if the window was closed."""
    try:
        if page is not None and not page.is_closed():
            page.title()
            return browser, page
    except Exception:
        pass
    try:
        browser and browser.close()
    except Exception:
        pass
    print("  (browser window was closed, reopening Chrome)")
    browser = open_browser(pw)
    page = browser.pages[0] if browser.pages else browser.new_page()
    return browser, page


def set_status(db, job, status, note):
    db.execute("UPDATE jobs SET status=?, notes=? WHERE job_id=?",
               (status, f"{note} | {job['notes'] or ''}"[:500], job["job_id"]))
    db.commit()


# Both projects keep their own jobs.db; check the other one so you never apply twice.
OTHER_DBS = [os.path.expanduser(p) for p in
             os.getenv("JOB_DBS", "jobs.db").split(":")]


def applied_elsewhere(job):
    """True if this job (same ID, or same company + title) was applied to from the other project."""
    here = os.path.abspath("jobs.db")
    for path in OTHER_DBS:
        if not os.path.exists(path) or os.path.abspath(path) == here:
            continue
        try:
            other = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            hit = other.execute(
                "SELECT 1 FROM jobs WHERE status='applied' AND (job_id=? OR (lower(trim(company))=lower(trim(?)) "
                "AND lower(trim(title))=lower(trim(?))))", (job["job_id"], job["company"], job["title"])).fetchone()
            other.close()
            if hit:
                return path
        except Exception:
            pass
    return None


def move_to_manual(db, job, why="moved by you"):
    """Save resume + link + answers in manual_apply/ (same as Lever jobs) and mark it manual."""
    folder = ""
    try:
        import step2_tailor_resume as t
        report_path = f"audits/{job['job_id']}_check.md"
        report = open(report_path, encoding="utf-8").read() if os.path.exists(report_path) else "# check report"
        if job["resume_path"] and os.path.exists(job["resume_path"]):
            folder = t.save_manual_package(job, job["resume_path"], report)
    except Exception as e:
        print(f"    (couldn't create the manual-apply folder: {str(e)[:80]})")
    set_status(db, job, "manual", why)
    print(f"  -> SAVED TO MANUAL APPLY{': ' + folder if folder else ''}")


def review_job(db, job, page, submit_selectors, applied, issues=""):
    """Show one filled form to the user and submit on ENTER. Returns 'quit' to stop."""
    try:
        page.bring_to_front()
    except Exception:
        pass
    print(f"\n=== REVIEW: {job['company']} | {job['title']} ===")
    if issues:
        print(f"  needs your attention: {issues}")
    print("  Check the form in the browser (fix anything outlined in red).")
    choice = input("  ENTER = submit  |  a = I already submitted it  |  x = move to manual apply  |  "
                   "s = skip for now  |  q = quit: ").strip().lower() or "y"
    if choice == "q":
        return "quit"
    if choice == "x":
        move_to_manual(db, job)
    elif choice == "s":
        print("  -> SKIPPED (you'll be asked about it next run)")
    elif choice == "a":
        set_status(db, job, "applied", f"applied {datetime.now():%Y-%m-%d %H:%M}")
        applied.append(job)
        print("  -> MARKED SUBMITTED")
    elif choice == "y":
        try:
            done, msg = submit_and_confirm(page, submit_selectors)
        except Exception as e:
            done, msg = False, f"browser problem: {str(e)[:80]}"
        safe_shot(page, f"screenshots/{job['job_id']}_after.png")
        print(f"  {'SUBMITTED' if done else 'Not confirmed'}: {msg}")
        if not done:
            nxt = input("  Finish it in the browser, then: a = submitted  |  x = move to manual apply  |  "
                        "s = skip: ").strip().lower()
            if nxt == "a":
                done = True
            elif nxt == "x":
                move_to_manual(db, job)
                return None
        if done:
            set_status(db, job, "applied", f"applied {datetime.now():%Y-%m-%d %H:%M}")
            applied.append(job)
            print("  -> SUBMITTED")
        else:
            print("  -> SKIPPED (you'll be asked about it next run)")
    return None


def fill_in_tab(browser, job):
    """Open a fresh tab for this job and fill it. Returns (page, filled, drafted, todo, submit_selectors)."""
    blank = [p for p in browser.pages if not p.is_closed() and p.url in ("about:blank", "", "chrome://newtab/")]
    page = blank[0] if blank else browser.new_page()      # use Chrome's empty start tab first
    try:
        page.bring_to_front()                              # always show the tab being filled
    except Exception:
        pass
    print(f"\n=== {job['company']} | {job['title']} ===\n{job['job_url']}")
    try:
        filled, drafted, todo, sel = prepare(page, job)
        print_summary(filled, drafted, todo)
    except Exception as e:
        print(f"  could not fill automatically: {str(e)[:120]}")
        filled, drafted, todo, sel = [], [], ["form could not be filled"], ["button[type=submit]"]
    safe_shot(page, f"screenshots/{job['job_id']}.png")
    try:                                                # opened: next run will ask before refilling
        db = sqlite3.connect("jobs.db")
        db.execute("UPDATE jobs SET status='needs_review' WHERE job_id=? AND status!='applied'", (job["job_id"],))
        db.commit(); db.close()
    except Exception:
        pass
    return page, filled, drafted, todo, sel


def main(job_ids=None, auto=None, review_all=False):
    """job_ids: only these jobs. auto: submit safe ones without asking.
    review_all: fill every job in its own tab first, then review each (nothing submits without you)."""
    if auto is None:
        auto = "--auto" in sys.argv
    if "--workday-backlog" in sys.argv:                       # move saved Workday jobs into the apply queue
        bdb = sqlite3.connect("jobs.db")
        n = bdb.execute("UPDATE jobs SET status='tailored' WHERE board='workday' AND status='manual_ready'").rowcount
        bdb.commit(); bdb.close()
        print(f"{n} saved Workday job(s) moved into the apply queue.")
    limit = DEFAULT_DAILY_LIMIT
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    db = sqlite3.connect("jobs.db")
    db.row_factory = sqlite3.Row
    jobs = db.execute("SELECT * FROM jobs WHERE status IN ('tailored','submit_failed','needs_review') "
                      f"AND board IN ({','.join(repr(b) for b in sorted(APPLY_BOARDS))})").fetchall()
    if job_ids is not None:
        jobs = [j for j in jobs if j["job_id"] in set(job_ids)]
    fresh = []
    for j in jobs:
        where = applied_elsewhere(j)
        if where:
            print(f"  already applied from the other project: {j['company']} - {j['title']}")
            set_status(db, j, "applied", f"applied via {os.path.basename(os.path.dirname(where))}")
            continue
        fresh.append(j)
    jobs = fresh
    print(f"{len(jobs)} application(s) to fill.")
    print("Each one opens in its own Chrome tab. Safe Greenhouse/Ashby ones may submit by themselves;"
          "\neverything else waits for you. You'll get: ENTER = submit | a = already submitted | "
          "x = move to manual apply | s = skip.")
    if not jobs:
        print("Nothing new to fill. Jobs to apply yourself are in manual_apply/index.html")
        return

    applied, waiting = [], []        # waiting = tabs left open, already filled, for your review
    with sync_playwright() as pw:
        browser = open_browser(pw)

        for n, job in enumerate(jobs, 1):
            print(f"\n[{n}/{len(jobs)}] {job['company']} | {job['title']}  ({job['board']})")
            if job["status"] in ("needs_review", "submit_failed"):    # opened in an earlier run
                ans = input("  This form was opened in an earlier run. Did you submit it?\n"
                            "  y = yes, mark submitted  |  n = no, fill it again now  |  x = move to manual apply  |  "
                            "s = skip for now: ").strip().lower()
                if ans == "y":
                    set_status(db, job, "applied", f"confirmed submitted {datetime.now():%Y-%m-%d %H:%M}")
                    applied.append(job)
                    print("  -> MARKED SUBMITTED")
                    continue
                if ans == "x":
                    move_to_manual(db, job)
                    continue
                if ans == "s":
                    print("  -> SKIPPED")
                    continue
                print("  -> filling it again now...")
            try:
                page, filled, drafted, todo, sel = fill_in_tab(browser, job)
            except Exception:                                   # browser was closed: reopen and retry once
                browser, _ = live_page(pw, browser, None)
                page, filled, drafted, todo, sel = fill_in_tab(browser, job)

            if not auto and not review_all:                     # review right away, same filled tab
                if review_job(db, job, page, sel, applied, "; ".join(todo)[:300]) == "quit":
                    break
                try:
                    page.close()
                except Exception:
                    pass
                continue

            ok, why = auto_ok(job, todo, drafted) if auto else (False, "ready for your review")
            if ok and len(applied) >= limit:
                ok, why = False, f"daily limit of {limit} reached"
            if ok:
                try:
                    done, msg = submit_and_confirm(page, sel)
                except Exception as e:
                    done, msg = False, f"browser problem: {str(e)[:80]}"
                safe_shot(page, f"screenshots/{job['job_id']}_after.png")
                if done:
                    print("  -> SUBMITTED")
                    set_status(db, job, "applied", f"auto-applied {datetime.now():%Y-%m-%d %H:%M}")
                    applied.append(job)
                    try:
                        page.close()
                    except Exception:
                        pass
                    continue
                why = msg
            print(f"  -> FILLED, WAITING FOR YOUR REVIEW (tab stays open): {why}")
            set_status(db, job, "needs_review", why)
            detail = why + (" -> " + " | ".join(t[:70] for t in todo) if todo else "") + \
                     (" | read AI drafts: " + " | ".join(q[:50] for q, _ in drafted) if drafted else "")
            waiting.append((job, page, sel, detail))

        # ---------- review the tabs that were kept open (no re-filling) ----------
        if waiting:
            alert_user()
            print(f"\n{len(waiting)} application(s) are filled and waiting in open tabs. Reviewing them now.")
        for job, page, sel, why in waiting:
            if page.is_closed():                                # tab was closed by hand: fill it again
                print(f"\n  (tab for {job['company']} was closed, filling it again)")
                try:
                    page, _, _, todo, sel = fill_in_tab(browser, job)
                except Exception:                               # whole browser was closed
                    browser, _ = live_page(pw, browser, None)
                    page, _, _, todo, sel = fill_in_tab(browser, job)
                why = "; ".join(todo)
            if review_job(db, job, page, sel, applied, why) == "quit":
                break

        try:
            browser.close()
        except Exception:
            pass

    left = db.execute("SELECT COUNT(*) FROM jobs WHERE status='needs_review'").fetchone()[0]
    print(f"\nDONE: {len(applied)} submitted. {left} still waiting for review "
          f"(run 'python step3_apply.py' to finish them later).")


if __name__ == "__main__":
    main()
