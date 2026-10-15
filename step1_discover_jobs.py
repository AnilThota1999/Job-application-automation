"""
Step 1 - Discover Data Engineer jobs posted recently on Greenhouse, Ashby, Lever and Workday.

    python step1_discover_jobs.py              # last 24 hours
    python step1_discover_jobs.py --hours 72   # wider window

Usage:
    pip install requests --break-system-packages   # or in a venv
    export SERPER_API_KEY=...        # optional: auto-discovers new companies via Google
    python step1_discover_jobs.py

Outputs:
    companies.json   - growing list of ATS board tokens (seed it by hand, discovery adds more)
    jobs.db          - SQLite tracker (dedupe + status for later steps)
    new_jobs.json    - today's new matching jobs, input for Step 2 (tailoring)
"""
import html, json, os, re, sqlite3, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import requests

TITLE_RE = re.compile(r"\b(data engineer|data engineering|data platform engineer|big data engineer|"
                      r"analytics engineer|etl (engineer|developer)|data pipeline engineer|"
                      r"data infrastructure engineer|cloud data engineer)", re.I)
EXCLUDE_RE = re.compile(r"\b(manager|director|principal|staff|intern|head of)\b", re.I)
US_RE = re.compile(r"(united states|\bUS\b|USA|remote|, [A-Z]{2}\b|new york|san francisco)", re.I)
HOURS = int(sys.argv[sys.argv.index("--hours") + 1]) if "--hours" in sys.argv else 24
MAX_AGE = timedelta(hours=HOURS)

# Job boards to search. Greenhouse + Ashby are auto-applied in Step 3.
# Lever + Workday jobs get a tailored resume saved in manual_apply/ (Step 2) for you to submit.
ENABLED_BOARDS = {"greenhouse", "ashby", "lever", "workday"}
HEADERS = {"User-Agent": "Mozilla/5.0 (job-discovery script)"}

# Seed list: add companies you care about. Token = the slug in the board URL.
DEFAULT_COMPANIES = {
    "greenhouse": [
        "databricks", "stripe", "airbnb", "robinhood", "coinbase", "datadog", "doordashusa",
        "instacart", "lyft", "pinterest", "reddit", "dropbox", "figma", "gitlab", "cloudflare",
        "mongodb", "elastic", "twilio", "affirm", "chime", "brex", "discord", "samsara", "okta",
        "asana", "squarespace", "duolingo", "grammarly", "toast", "hubspotjobs", "gusto",
        "flexport", "nuro", "waymo", "scaleai", "anthropic", "fivetran", "dbtlabsinc",
        "confluent", "starburst", "cockroachlabs", "airtable", "betterment", "marqeta",
        "sofi", "carvana", "peloton", "etsy", "warbyparker", "oscar", "zocdoc", "headway",
        "lendingclub", "nerdwallet", "upstart", "mercury", "plaid", "benchling", "tempus",
    ],
    "lever": [
        "palantir", "spotify", "netlify", "attentive", "kraken", "binance", "zoox",
        "wealthfront", "outreach", "highspot", "veeva", "ro", "included",
    ],
    "workday": [
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
        "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site",
        "https://adobe.wd5.myworkdayjobs.com/external_experienced",
        "https://capitalone.wd12.myworkdayjobs.com/Capital_One",
        "https://wellsfargo.wd1.myworkdayjobs.com/WellsFargoJobs",
        "https://mastercard.wd1.myworkdayjobs.com/CorporateCareers",
        "https://walmart.wd5.myworkdayjobs.com/WalmartExternal",
        "https://target.wd5.myworkdayjobs.com/targetcareers",
        "https://lowes.wd5.myworkdayjobs.com/LWS_External_CS",
    ],
    "ashby": [
        "ramp", "notion", "openai", "linear", "vanta", "deel", "replit", "cohere",
        "plaid", "snowflake", "watershed", "hex", "modal", "retool",
    ],
}


# ---------- company discovery (optional, via Serper.dev Google search) ----------
EXTRA_COMPANIES = {
    "greenhouse": [
        "amplitude",
        "anduril",
        "appliedintuition",
        "bitgo",
        "calendly",
        "carta",
        "coursera",
        "cruise",
        "dataiku",
        "doximity",
        "faire",
        "fanatics",
        "gemini",
        "hellofresh",
        "hims",
        "imc",
        "jumpcloud",
        "justworks",
        "klaviyo",
        "lattice",
        "lucidmotors",
        "mixpanel",
        "mozilla",
        "nextdoor",
        "niantic",
        "postman",
        "roblox",
        "seatgeek",
        "spacex",
        "stackadapt",
        "sumologic",
        "thumbtack",
        "twitch",
        "udemy",
        "webflow",
        "whoop",
        "zipline",
        "zillow",
        "andurilindustries",
        "relativity",
        "grafanalabs",
        "newrelic",
        "pagerduty",
        "hashicorp",
        "sentry",
        "segment",
        "braze",
        "contentful",
        "algolia",
        "abnormalsecurity",
        "verkada",
        "rubrik",
        "netskope",
        "sigmacomputing",
        "clari",
        "gong",
        "outschool",
        "chegg",
        "wealthsimple",
        "addepar",
        "alloy",
        "checkr",
        "earnin",
        "dave",
        "current",
        "stash",
        "acorns",
        "oportun",
        "greenlight",
        "point72",
        "twosigma",
        "janestreet",
        "citadel",
        "hudsonrivertrading",
        "jumptrading",
        "optiver",
        "virtu",
        "nuna",
        "color",
        "cerebral",
        "springhealth",
        "lyrahealth",
        "headspace",
        "noom",
        "calm",
        "modernhealth",
        "flatiron",
        "grail",
        "recursionpharmaceuticals",
        "insitro",
        "komodohealth",
        "cityblock",
        "devoted",
        "clover"
    ],
    "lever": [
        "shieldai",
        "voleon",
        "matchgroup",
        "nium",
        "sonder",
        "quantcast",
        "eventbrite",
        "yelp",
        "lucid",
        "mindbody",
        "brightedge",
        "fetchrewards",
        "hive",
        "tala",
        "aircall",
        "leadiq",
        "pattern",
        "degreed",
        "nerdy"
    ],
    "ashby": [
        "airbyte",
        "dagsterlabs",
        "posthog",
        "supabase",
        "clickhouse",
        "motherduck",
        "perplexity",
        "anyscale",
        "astronomer",
        "sigmacomputing",
        "harvey",
        "ironclad",
        "rippling",
        "mercury",
        "brex",
        "benchling",
        "pinecone",
        "weaviate",
        "tecton",
        "prefect",
        "hightouch",
        "census",
        "fivetran",
        "dbtlabs",
        "materialize",
        "neon",
        "temporal",
        "baseten",
        "together",
        "fireworks",
        "runway",
        "elevenlabs",
        "character",
        "scale",
        "writer",
        "glean",
        "sierra"
    ]
}
for _ats, _toks in EXTRA_COMPANIES.items():
    DEFAULT_COMPANIES.setdefault(_ats, [])
    DEFAULT_COMPANIES[_ats] += [t for t in _toks if t not in DEFAULT_COMPANIES[_ats]]


def discover_companies(companies):
    key = os.getenv("SERPER_API_KEY")
    if not key:
        return companies
    patterns = {
        "greenhouse": (r"(?:boards|job-boards)\.greenhouse\.io/([\w-]+)",
                       'site:boards.greenhouse.io OR site:job-boards.greenhouse.io "data engineer"'),
        "lever": (r"jobs\.lever\.co/([\w-]+)", 'site:jobs.lever.co "data engineer"'),
        "ashby": (r"jobs\.ashbyhq\.com/([\w-]+)", 'site:jobs.ashbyhq.com "data engineer"'),
        "workday": (r"(https://[\w-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[\w-]+)",
                    'site:myworkdayjobs.com "data engineer"'),
    }
    for ats, (rx, q) in patterns.items():
        try:
            r = requests.post("https://google.serper.dev/search",
                              headers={"X-API-KEY": key, "Content-Type": "application/json"},
                              json={"q": q, "tbs": "qdr:d", "num": 100}, timeout=30)
            for item in r.json().get("organic", []):
                m = re.search(rx, item.get("link", ""))
                if m:
                    token = re.sub(r"/[a-z]{2}-[A-Z]{2}/", "/", m.group(1))
                    companies.setdefault(ats, [])
                    if token not in companies[ats]:
                        companies[ats].append(token)
        except Exception as e:
            print(f"[discover] {ats} failed: {e}")
    return companies


# ---------- ATS fetchers (all public, no login) ----------
def fetch_greenhouse(token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:         # company not on Greenhouse
        return
    for j in r.json().get("jobs", []):
        yield {
            "job_id": f"gh_{j['id']}", "board": "greenhouse", "company": token,
            "title": j["title"], "location": (j.get("location") or {}).get("name", ""),
            "job_url": j["absolute_url"],
            "posted_at": j.get("first_published") or j.get("updated_at"),
            "description": re.sub(r"<[^>]+>", " ", html.unescape(j.get("content", ""))),
        }

def fetch_lever(company):
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    data = requests.get(url, headers=HEADERS, timeout=30).json()
    if not isinstance(data, list):   # company not on Lever (moved/renamed)
        return
    for j in data:
        lists = "\n".join(f"{l['text']}\n{re.sub(r'<[^>]+>', ' ', l['content'])}" for l in j.get("lists", []))
        yield {
            "job_id": f"lv_{j['id']}", "board": "lever", "company": company,
            "title": j["text"], "location": j.get("categories", {}).get("location", ""),
            "job_url": j.get("applyUrl") or j["hostedUrl"],
            "posted_at": datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).isoformat(),
            "description": f"{j.get('descriptionPlain', '')}\n{lists}\n{j.get('additionalPlain', '')}",
        }

def fetch_ashby(board):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:         # company not on Ashby
        return
    for j in r.json().get("jobs", []):
        yield {
            "job_id": f"ab_{j['id']}", "board": "ashby", "company": board,
            "title": j["title"], "location": j.get("location", ""),
            "job_url": j.get("applyUrl") or j["jobUrl"],
            "posted_at": j.get("publishedAt"),
            "description": j.get("descriptionPlain", ""),
        }

def fetch_workday(site_url):
    """Workday career sites expose a JSON search API used by their own pages."""
    m = re.match(r"https://(([\w-]+)\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", site_url)
    if not m:
        return
    host, tenant, site = m.groups()
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    hdrs = {**HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
    r = requests.post(f"{api}/jobs", headers=hdrs, timeout=30,
                      json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "Data Engineer"})
    if r.status_code != 200:
        return
    for p in r.json().get("jobPostings", []):
        posted = (p.get("postedOn") or "").lower()
        if "today" in posted:
            days = 0
        elif "yesterday" in posted:
            days = 1
        else:
            d = re.search(r"(\d+)\+? days", posted)
            days = int(d.group(1)) if d else 99
        if days > 1 or not TITLE_RE.search(p.get("title", "")):
            continue
        detail = requests.get(f"{api}{p['externalPath']}", headers=hdrs, timeout=30)
        info = detail.json().get("jobPostingInfo", {}) if detail.status_code == 200 else {}
        yield {
            "job_id": f"wd_{tenant}_{info.get('jobReqId') or p['externalPath'].split('_')[-1]}",
            "board": "workday", "company": tenant,
            "title": p["title"], "location": p.get("locationsText") or info.get("location", ""),
            "job_url": info.get("externalUrl") or f"https://{host}/{site}{p['externalPath']}",
            "posted_at": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
            "description": re.sub(r"<[^>]+>", " ", html.unescape(info.get("jobDescription", ""))),
        }


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby, "workday": fetch_workday}


# ---------- filtering + storage ----------
def is_recent(ts):
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if len(ts.strip()) <= 10:                      # date only: give it the whole day
        dt = dt + timedelta(hours=24)
    return datetime.now(timezone.utc) - dt <= MAX_AGE

def matches(job):
    return (TITLE_RE.search(job["title"]) and not EXCLUDE_RE.search(job["title"])
            and US_RE.search(job["location"] or "remote"))

def init_db():
    db = sqlite3.connect("jobs.db")
    db.execute("""CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY, board TEXT, company TEXT, title TEXT, location TEXT,
        job_url TEXT, posted_at TEXT, description TEXT, first_seen TEXT,
        status TEXT DEFAULT 'discovered', resume_path TEXT, notes TEXT)""")
    return db


# ---------------- Apify: jobs from 275k+ company career sites ----------------
APIFY_ACTOR = "fantastic-jobs~career-site-job-listing-api"
APIFY_LIMIT = os.getenv("APIFY_LIMIT", "auto")           # "auto" = spread remaining monthly credit evenly
APIFY_JOB_COST, APIFY_RUN_COST = 0.012, 0.01            # free-tier prices of the actor (USD)
APIFY_TITLES = ["Data Engineer", "Senior Data Engineer", "Analytics Engineer", "Data Platform Engineer",
                "Big Data Engineer", "ETL Developer", "Cloud Data Engineer"]
APIFY_EXCLUDE = ["Manager", "Director", "Principal", "Staff", "Intern", "Head"]
SOURCE_TO_BOARD = {"greenhouse": "greenhouse", "ashby": "ashby", "lever.co": "lever", "workday": "workday"}


def apify_job_id(board, url, fallback_id):
    """Use the same IDs as the direct ATS fetchers so jobs are never duplicated."""
    m = (re.search(r"[?&]gh_jid=(\d+)", url) or re.search(r"greenhouse\.io/[\w-]+/jobs/(\d+)", url))
    if board == "greenhouse" and m:
        return f"gh_{m.group(1)}"
    m = re.search(r"jobs\.lever\.co/[\w.-]+/([0-9a-f-]{36})", url)
    if board == "lever" and m:
        return f"lv_{m.group(1)}"
    m = re.search(r"jobs\.ashbyhq\.com/[\w.-]+/([0-9a-f-]{36})", url)
    if board == "ashby" and m:
        return f"ab_{m.group(1)}"
    return f"ap_{fallback_id}"


def apify_company_token(board, url, org):
    for rx in (r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)", r"jobs\.lever\.co/([\w.-]+)",
               r"jobs\.ashbyhq\.com/([\w.-]+)", r"//([\w-]+)\.wd\d+\.myworkdayjobs"):
        m = re.search(rx, url)
        if m:
            return m.group(1)
    return re.sub(r"[^a-z0-9]", "", (org or "company").lower())


def apify_daily_limit(token):
    """Today's share of the remaining monthly Apify credit, as a number of jobs."""
    if APIFY_LIMIT != "auto":
        return int(APIFY_LIMIT)
    try:
        r = requests.get("https://api.apify.com/v2/users/me/limits", params={"token": token}, timeout=30)
        d = r.json()["data"]
        used = float(d["current"]["monthlyUsageUsd"])
        cap = float(d["limits"]["maxMonthlyUsageUsd"])
        end = datetime.fromisoformat(d["monthlyUsageCycle"]["endAt"].replace("Z", "+00:00"))
        days_left = max(1, (end - datetime.now(timezone.utc)).days + 1)
        today = (cap - used) / days_left
        jobs = int((today - APIFY_RUN_COST) / APIFY_JOB_COST)
        print(f"Apify credit: ${cap - used:.2f} left of ${cap:.2f}, {days_left} days in cycle "
              f"-> today's budget ${today:.2f} = {max(jobs, 0)} jobs")
        return jobs
    except Exception as e:
        print(f"  couldn't read Apify usage ({str(e)[:80]}), using 15 jobs")
        return 15


def fetch_apify(hours):
    token = os.getenv("APIFY_TOKEN")
    if not token:
        return []
    limit = apify_daily_limit(token)
    if limit < 10:                                   # the actor's minimum per run
        print("  Apify: today's budget is under 10 jobs, saving credit for later days")
        return []
    time_range = "24h" if hours <= 24 else "7d"
    body = {"timeRange": time_range, "limit": min(limit, 5000), "titleSearch": APIFY_TITLES,
            "titleExclusionSearch": APIFY_EXCLUDE, "locationSearch": ["United States"],
            "ats": list(SOURCE_TO_BOARD), "descriptionType": "text", "removeAgency": True}
    print(f"Asking Apify for up to {body['limit']} jobs ({time_range}) from company career sites...")
    try:
        r = requests.post(f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
                          params={"token": token}, json=body, timeout=600)
        r.raise_for_status()
        items = r.json()
    except Exception as e:
        print(f"  Apify request failed: {str(e)[:150]}")
        return []
    jobs = []
    for it in items if isinstance(items, list) else []:
        board = SOURCE_TO_BOARD.get((it.get("source") or "").lower())
        url = it.get("url") or ""
        if not board or not url:
            continue
        jobs.append({
            "job_id": apify_job_id(board, url, it.get("id")),
            "board": board,
            "company": apify_company_token(board, url, it.get("organization")),
            "title": it.get("title", ""),
            "location": "; ".join(it.get("locations_derived") or []) or "United States",
            "job_url": url,
            "posted_at": it.get("date_posted") or datetime.now(timezone.utc).isoformat(),
            "description": it.get("description_text") or "",
        })
    print(f"  Apify returned {len(items) if isinstance(items, list) else 0} jobs, {len(jobs)} on supported job boards")
    return jobs


def fetch_board(ats, token):
    try:
        return ats, token, list(FETCHERS[ats](token)), None
    except Exception as e:
        return ats, token, [], str(e)[:80]


def main():
    companies = json.load(open("companies.json")) if os.path.exists("companies.json") else {}
    for ats, tokens in DEFAULT_COMPANIES.items():
        companies.setdefault(ats, [])
        companies[ats] += [t for t in tokens if t not in companies[ats]]
    companies = discover_companies(companies)
    json.dump(companies, open("companies.json", "w"), indent=2)

    tasks = [(ats, t) for ats, toks in companies.items() if ats in ENABLED_BOARDS for t in toks]
    print(f"Checking {len(tasks)} company job boards (last {HOURS}h)...")
    stats = {b: {"boards": 0, "responding": 0, "postings": 0, "title_match": 0, "recent": 0, "new": 0}
             for b in ENABLED_BOARDS}
    db, new_jobs = init_db(), []

    results = []
    apify_jobs = fetch_apify(HOURS)
    if apify_jobs:
        stats["apify"] = {"boards": 0, "responding": 0, "postings": 0, "title_match": 0, "recent": 0, "new": 0}
        results.append(("apify", "apify", apify_jobs, None))

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_board, ats, t) for ats, t in tasks]
        for f in as_completed(futures):
            results.append(f.result())

    for ats, token, jobs, err in results:
        if True:
            st = stats[ats]
            st["boards"] += 1
            if jobs:
                st["responding"] += 1
            st["postings"] += len(jobs)
            for job in jobs:
                if not matches(job):
                    continue
                st["title_match"] += 1
                if ats != "apify" and not is_recent(job["posted_at"]):   # Apify already filtered by date
                    continue
                st["recent"] += 1
                if db.execute("SELECT 1 FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone():
                    continue
                job["first_seen"] = datetime.now(timezone.utc).isoformat()
                db.execute("INSERT INTO jobs (job_id,board,company,title,location,job_url,posted_at,"
                           "description,first_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                           tuple(job[k] for k in ["job_id", "board", "company", "title", "location",
                                                  "job_url", "posted_at", "description", "first_seen"]))
                st["new"] += 1
                new_jobs.append(job)
    db.commit()

    print(f"\n{'Board':<12}{'boards':>7}{'responding':>12}{'postings':>10}{'title-match':>13}{'recent':>8}{'NEW':>5}")
    for b in sorted(stats):
        st = stats[b]
        print(f"{b:<12}{st['boards']:>7}{st['responding']:>12}{st['postings']:>10}{st['title_match']:>13}"
              f"{st['recent']:>8}{st['new']:>5}")
    print("(Workday 'postings' = its Data Engineer search results only)")

    json.dump(new_jobs, open("new_jobs.json", "w"), indent=2)
    print(f"\n{len(new_jobs)} new jobs in the last {HOURS}h:")
    for j in new_jobs:
        print(f"  [{j['board']}] {j['company']} - {j['title']} ({j['location']})\n      {j['job_url']}")
    if not new_jobs:
        print("  Tip: try --hours 72, or add a free SERPER_API_KEY so new companies are discovered daily.")


if __name__ == "__main__":
    main()
