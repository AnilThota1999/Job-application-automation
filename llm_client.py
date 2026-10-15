"""
Free LLM helper used by Step 2 (resume tailoring) and Step 3 (open-question drafts).
"""
import json, os, re, sys, time
import requests

PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
FALLBACK = os.getenv("LLM_FALLBACK", "").lower()
BASE = "https://generativelanguage.googleapis.com/v1beta"
PREFERRED = [
    "gemini-3.5-flash",
    "gemini-3-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
]

MAX_CALL_SECONDS = int(os.getenv("LLM_TIMEOUT", "45"))
HTTP_TIMEOUT = (10, 35)

_models = None
_dead_backups = set()

OPENAI_COMPAT = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY", "openai/gpt-oss-120b"),
    "cerebras": ("https://api.cerebras.ai/v1/chat/completions", "CEREBRAS_API_KEY", "gpt-oss-120b"),
    "mistral": ("https://api.mistral.ai/v1/chat/completions", "MISTRAL_API_KEY", "mistral-small-latest"),
    "github": ("https://models.github.ai/inference/chat/completions", "GITHUB_TOKEN", "openai/gpt-4.1-mini"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY",
                   "meta-llama/llama-3.3-70b-instruct:free"),
}
PREFERRED_MODELS = {
    "groq": [
        "openai/gpt-oss-120b",
        "llama-3.3-70b-versatile",
        "meta-llama/llama-4-maverick-17b-128e-instruct",
        "qwen/qwen3-32b",
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "openai/gpt-oss-20b",
        "llama-3.1-8b-instant",
    ],
    "cerebras": ["gpt-oss-120b", "llama-3.3-70b", "qwen-3-235b-a22b-instruct-2507", "qwen-3-32b", "llama3.1-8b"],
    "mistral": ["mistral-medium-latest", "mistral-small-latest", "open-mistral-nemo"],
}
_dead_models = set()
_announced = set()
_exhausted = set()
_busy_until = {}
MAX_GEMINI_TRIES = 5
_last_good = [None]
_last_call = [0.0]
_skip_remaining_tailoring = False


class QuotaExhausted(RuntimeError):
    pass


def _handle_interrupt():
    global _skip_remaining_tailoring
    print("\n\n    [PAUSED] Choose an action:", flush=True)
    print("      [s] Skip this slow job for now (keep in queue & move to next job)", flush=True)
    print("      [a] Stop tailoring remaining links & jump straight to Apply for finished resumes", flush=True)
    print("      [c] Continue waiting on current job", flush=True)
    print("      [q] Quit now (all unsubmitted jobs stay in queue for later)", flush=True)
    try:
        choice = input("    Select [s/a/c/q] (default: s): ").strip().lower() or "s"
    except (KeyboardInterrupt, EOFError):
        choice = "q"

    if choice == "c":
        print("    -> Resuming current job...", flush=True)
        return
    if choice == "a":
        _skip_remaining_tailoring = True
        raise QuotaExhausted("Paused tailoring by user — jumping to Apply with resumes finished so far.")
    if choice == "q":
        print("    -> Exiting cleanly. Unfinished jobs remain in your queue for later.", flush=True)
        sys.exit(0)
    raise RuntimeError("Skipped by user -> kept in queue for later")


def _check_deadline(deadline, quick=False):
    if not quick and time.time() > deadline:
        raise RuntimeError(f"Timed out after {MAX_CALL_SECONDS}s -> kept in queue for later")


def provider_order():
    order = os.getenv("LLM_ORDER")
    if order:
        return [p.strip().lower() for p in order.split(",") if p.strip()]
    order = [PROVIDER] + [f.strip().lower() for f in FALLBACK.split(",") if f.strip() and f.strip().lower() != PROVIDER]
    for extra in ("groq", "cerebras", "mistral", "github", "openrouter"):
        if extra not in order and os.getenv(OPENAI_COMPAT[extra][1]):
            order.append(extra)
    return order


def generate(system, user, max_tokens=8000, quick=False):
    global _skip_remaining_tailoring
    if _skip_remaining_tailoring and not quick and max_tokens >= 2000:
        raise QuotaExhausted("Skipped — jumping to Apply with already-tailored resumes.")

    deadline = time.time() + (15 if quick else MAX_CALL_SECONDS)
    max_rounds = 1 if quick else 2

    for round_idx in range(max_rounds):
        errors, quota_only = [], True
        gemini_was_busy = False
        for name in provider_order():
            if name in _dead_backups:
                continue
            try:
                _check_deadline(deadline, quick)
                if name == "gemini":
                    return _gemini(system, user, max_tokens, quick, deadline)
                if name == "anthropic":
                    import anthropic
                    msg = anthropic.Anthropic().messages.create(
                        model="claude-sonnet-5-5", max_tokens=max_tokens, system=system,
                        messages=[{"role": "user", "content": user}])
                    return "".join(b.text for b in msg.content if b.type == "text")
                if name == "ollama":
                    return _ollama(system, user)
                if name in OPENAI_COMPAT:
                    return _openai_compat(name, system, user, max_tokens, deadline)
            except KeyboardInterrupt:
                _handle_interrupt()
            except QuotaExhausted:
                if _skip_remaining_tailoring:
                    raise
                errors.append(f"{name}: quota used up")
            except Exception as e:
                msg_str = str(e)
                if "Skipped by user" in msg_str or "Timed out after" in msg_str:
                    raise
                if "not set" in msg_str:
                    _dead_backups.add(name)
                    continue
                if name == "gemini" and "busy" in msg_str.lower():
                    gemini_was_busy = True
                quota_only = False
                errors.append(f"{name}: {msg_str[:80]}")
                if "daily limit" in msg_str:
                    _dead_backups.add(name)
            if not quick and len(provider_order()) > 1 and errors:
                print(f"    {errors[-1]} -> trying the next AI", flush=True)

        if gemini_was_busy and round_idx + 1 < max_rounds and time.time() + 10 < deadline:
            try:
                print("    All providers busy; waiting 8s for Gemini to recover (press Ctrl+C to skip job)...", flush=True)
                time.sleep(8)
            except KeyboardInterrupt:
                _handle_interrupt()
            _busy_until.clear()
            continue
        break

    if quota_only and errors:
        raise QuotaExhausted("All free AI quotas are used up for today (Gemini resets at midnight Pacific). "
                             "Run again later; unfinished jobs stay in the queue.")
    raise RuntimeError("No AI available right now: " + "; ".join(errors))


def _generate_old(system, user, max_tokens=8000, quick=False):
    return generate(system, user, max_tokens=max_tokens, quick=quick)


def _key():
    k = os.getenv("GEMINI_API_KEY")
    if not k:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return k


def _available_models():
    global _models
    if _models is not None:
        return _models
    if os.getenv("GEMINI_MODEL"):
        _models = [os.getenv("GEMINI_MODEL")]
        return _models
    names = []
    try:
        r = requests.get(f"{BASE}/models", headers={"x-goog-api-key": _key()},
                         params={"pageSize": 200}, timeout=15)
        for m in r.json().get("models", []):
            n = m["name"].split("/")[-1]
            if (
                "generateContent" in m.get("supportedGenerationMethods", [])
                and "flash" in n
                and not re.search(
                    r"gemini-2\.5-flash-lite|gemini-(1\.|2\.0)|image|tts|audio|live|vision|embed|thinking|exp|preview|omni|latest",
                    n,
                )
            ):
                names.append(n)
    except Exception:
        pass
    ordered = [p for p in PREFERRED if p in names] + sorted((n for n in names if n not in PREFERRED), reverse=True)
    _models = ordered or PREFERRED
    return _models


def _throttle(min_gap=5.0):
    wait = _last_call[0] + min_gap - time.time()
    if wait > 0:
        time.sleep(wait)


def _retry_delay(err):
    for d in err.get("details", []):
        if "retryDelay" in d:
            m = re.match(r"(\d+)", str(d["retryDelay"]))
            if m:
                return int(m.group(1)) + 2
    return None


def _gemini(system, user, max_tokens, quick=False, deadline=None):
    deadline = deadline or (time.time() + MAX_CALL_SECONDS)
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens + 4000, "temperature": 0.4},
    }
    last_err = "no models available"
    models = _available_models()
    if _last_good[0] in models:
        models = [_last_good[0]] + [m for m in models if m != _last_good[0]]

    active_candidates = [m for m in models if m not in _exhausted]
    if active_candidates and all(_busy_until.get(m, 0) > time.time() for m in active_candidates):
        _busy_until.clear()

    tried = 0
    switched_from_busy = False
    for model in models:
        _check_deadline(deadline, quick)
        if model in _exhausted or _busy_until.get(model, 0) > time.time():
            continue
        if tried >= MAX_GEMINI_TRIES:
            last_err = f"{tried} Gemini models busy"
            break
        tried += 1
        if switched_from_busy and not quick:
            print(f"    -> trying {model} (press Ctrl+C to skip/pause)...", flush=True)

        for attempt in range(1 if quick else 2):
            _check_deadline(deadline, quick)
            _throttle(1.0 if quick else 5.0)
            remaining = max(5, int(deadline - time.time()))
            req_timeout = (8, min(15 if quick else 30, remaining))
            try:
                r = requests.post(
                    f"{BASE}/models/{model}:generateContent",
                    headers={"x-goog-api-key": _key()},
                    json=body,
                    timeout=req_timeout,
                )
            except requests.RequestException as e:
                last_err = str(e)
                if not quick:
                    print(f"    {model}: took too long, switching model", flush=True)
                switched_from_busy = True
                break

            if r.status_code == 429:
                _last_call[0] = time.time()
                err = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("error", {})
                text = json.dumps(err).lower()
                if "per day" in text or "perday" in text or "daily" in text or "requestsperday" in text:
                    if not quick:
                        print(f"    {model}: daily free quota used up, switching model", flush=True)
                    _exhausted.add(model)
                    switched_from_busy = True
                    last_err = f"{model} daily quota"
                    break
                if quick:
                    last_err = f"{model} busy"
                    break
                wait = min(_retry_delay(err) or 10, 15)
                if time.time() + wait >= deadline:
                    switched_from_busy = True
                    last_err = f"{model} rate-limited"
                    break
                print(f"    {model}: per-minute limit, waiting {wait}s (Ctrl+C to skip)", flush=True)
                time.sleep(wait)
                continue

            if r.status_code in (500, 502, 503, 504):
                _busy_until[model] = time.time() + 25
                if not quick:
                    print(f"    {model}: Google busy, trying another", flush=True)
                switched_from_busy = True
                last_err = f"{model} busy"
                break

            if r.status_code in (400, 403, 404):
                _exhausted.add(model)
                last_err = f"{model} unavailable ({r.status_code})"
                break

            r.raise_for_status()
            _last_call[0] = time.time()
            cands = r.json().get("candidates") or [{}]
            parts = cands[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if text.strip():
                _last_good[0] = model
                return text
            last_err = "empty response"
            break
        else:
            last_err = f"{model} kept hitting limits"

    if last_err == "no models available" and _busy_until:
        last_err = "all Gemini models busy"
    if all(m in _exhausted for m in _available_models()):
        raise QuotaExhausted(
            "Gemini free quota is used up for today (resets at midnight Pacific time). "
            "Run again tomorrow, or set LLM_FALLBACK=ollama to continue with a free local model.")
    raise RuntimeError(f"Gemini failed: {last_err}")


_compat_model = {}


def _pick_model(name, key, refresh=False):
    env = os.getenv(f"{name.upper()}_MODEL")
    if env and (name, env) not in _dead_models:
        return env
    if name in _compat_model and not refresh and (name, _compat_model[name]) not in _dead_models:
        return _compat_model[name]
    ids = []
    if name != "github":
        base = OPENAI_COMPAT[name][0].rsplit("/chat/completions", 1)[0]
        try:
            r = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=15)
            ids = [m.get("id", "") for m in r.json().get("data", [])]
        except Exception:
            ids = []
    alive = lambda m: (name, m) not in _dead_models
    pick = next((m for m in PREFERRED_MODELS.get(name, []) if (not ids or m in ids) and alive(m)), None)
    if not pick and ids:
        if name == "openrouter":
            pick = next((m for m in ids if m.endswith(":free") and alive(m) and
                         re.search(r"70b|llama|qwen|gpt-oss|deepseek", m)), None)
        else:
            pick = next((
                m for m in ids
                if alive(m) and not re.search(r"allam|guard|whisper|tts|embed|vision|playai|arabic|compound", m, re.I)
            ), None)
    if not pick and alive(OPENAI_COMPAT[name][2]):
        pick = OPENAI_COMPAT[name][2]
    if not pick:
        raise RuntimeError("daily limit reached on all models")
    _compat_model[name] = pick
    return pick


def _openai_compat(name, system, user, max_tokens, deadline=None):
    deadline = deadline or (time.time() + MAX_CALL_SECONDS)
    url, key_env, _default = OPENAI_COMPAT[name]
    key = os.getenv(key_env)
    if not key:
        raise RuntimeError(f"{key_env} is not set")
    model = _pick_model(name, key)
    if name not in _announced:
        print(f"    using free backup: {name} ({model})", flush=True)
        _announced.add(name)
    if name == "groq" and len(system) + len(user) > 28000:
        user = user[: max(8000, 28000 - len(system))]
    body = {"model": model, "temperature": 0.4, "max_tokens": min(max_tokens, 4000 if name == "groq" else 8000),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    for attempt in range(6):
        _check_deadline(deadline)
        remaining = max(5, int(deadline - time.time()))
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json=body,
                timeout=(8, min(30, remaining)),
            )
        except requests.RequestException:
            _dead_models.add((name, model))
            model = _pick_model(name, key, refresh=True)
            body["model"] = model
            print(f"    {name}: request timed out, switching to {model}", flush=True)
            continue

        if r.status_code == 429:
            err_lower = r.text.lower()
            wait = int(float(r.headers.get("retry-after", 0) or 0)) or 12 * (attempt + 1)
            is_daily = (
                wait > 180
                or "tokens per day" in err_lower
                or "requests per day" in err_lower
                or "(tpd)" in err_lower
                or "(rpd)" in err_lower
            )
            if is_daily or time.time() + wait >= deadline:
                _dead_models.add((name, model))
                model = _pick_model(name, key, refresh=True)
                body["model"] = model
                print(f"    {name}: limit on that model, switching to {model}", flush=True)
                continue
            wait = min(wait, 20)
            print(f"    {name} ({model}): rate limit, waiting {wait}s (Ctrl+C to skip)", flush=True)
            time.sleep(wait)
            continue
        if r.status_code in (400, 404, 413):
            _dead_models.add((name, model))
            model = _pick_model(name, key, refresh=True)
            body["model"] = model
            print(f"    {name}: model rejected request ({r.status_code}), switching to {model}", flush=True)
            continue
        r.raise_for_status()
        text = r.json()["choices"][0]["message"].get("content") or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        if text:
            return text
    raise RuntimeError("no response")


def _ollama(system, user):
    model = os.getenv("OLLAMA_MODEL", "qwen3:8b")
    try:
        r = requests.post("http://localhost:11434/api/chat", timeout=HTTP_TIMEOUT, json={
            "model": model, "stream": False, "think": False,
            "options": {"num_ctx": 16384, "temperature": 0.4},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        })
    except requests.ConnectionError:
        raise RuntimeError("Ollama is not running. Install it from ollama.com and run: ollama pull " + model)
    r.raise_for_status()
    return re.sub(r"<think>.*?</think>", "", r.json()["message"]["content"], flags=re.S).strip()
