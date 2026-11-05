#!/usr/bin/env python3
"""PubMed Reading List Builder: backend server.

Standard library only (no pip installs). Serves index.html and a small JSON API:
    GET  /api/config   -> is the AI helper enabled?
    GET  /api/search   -> PubMed search (cached), returns article records
    GET  /api/check    -> PubMed query check + MeSH term lookup
    POST /api/suggest  -> AI query helper (Gemini or Claude), keys stay on the server

Run:
    python server.py            then open http://127.0.0.1:8000

Configuration: environment variables, or a .env file next to this script.
    NCBI_API_KEY        optional; raises PubMed limit from 3 to 10 requests/second
    NCBI_EMAIL          optional; contact address sent to NCBI
    AI_PROVIDER         gemini (default), claude, or none
    GEMINI_API_KEY      required when AI_PROVIDER=gemini
    GEMINI_MODEL        default gemini-3.5-flash-lite
    ANTHROPIC_API_KEY   required when AI_PROVIDER=claude
    CLAUDE_MODEL        default claude-haiku-5-5
    AI_RATE_PER_MIN     AI requests per client IP per minute (default 10)
    AI_DAILY_MAX        AI requests per day, whole server (default 200)
    HOST                default 127.0.0.1. Only use 0.0.0.0 behind HTTPS and an access control.
    PORT                default 8000
    NCBI_BASE, GEMINI_BASE, ANTHROPIC_BASE   override upstream URLs (used for testing)

PubMed calls are throttled and retried (up to 3 times) when PubMed is busy or briefly down.
Failed or empty-on-error replies are never cached.

Privacy: search text and AI prompts are never written to the log.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = "pubmed_reading_list_builder"


def load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv(HERE / ".env")
ENV = os.environ.get
NCBI_BASE = ENV("NCBI_BASE", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/")
GEMINI_BASE = ENV("GEMINI_BASE", "https://generativelanguage.googleapis.com")
ANTHROPIC_BASE = ENV("ANTHROPIC_BASE", "https://api.anthropic.com")
NCBI_KEY = ENV("NCBI_API_KEY", "")
NCBI_EMAIL = ENV("NCBI_EMAIL", "")
AI_PROVIDER = ENV("AI_PROVIDER", "gemini").strip().lower()
GEMINI_KEY = ENV("GEMINI_API_KEY", "")
GEMINI_MODEL = ENV("GEMINI_MODEL", "gemini-3.5-flash-lite")
ANTHROPIC_KEY = ENV("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = ENV("CLAUDE_MODEL", "claude-haiku-5-5")
AI_RATE = int(ENV("AI_RATE_PER_MIN", "10"))
AI_DAILY = int(ENV("AI_DAILY_MAX", "200"))
HOST = ENV("HOST", "127.0.0.1")
PORT = int(ENV("PORT", "8000"))

ARTICLE_TYPES = {  # label shown in the UI -> PubMed publication-type tag
    "Systematic review": "Systematic Review[pt]",
    "Meta-analysis": "Meta-Analysis[pt]",
    "Review": "Review[pt]",
    "Randomized trial": "Randomized Controlled Trial[pt]",
    "Clinical trial": "Clinical Trial[pt]",
    "Case report": "Case Reports[pt]",
}

SYSTEM = " ".join([
    "You are an expert medical librarian who writes PubMed search strategies.",
    "Given a research topic, write ONE PubMed query.",
    "Split the topic into concepts. Join concepts with AND and synonyms inside a concept with OR.",
    'For each concept, pair a MeSH heading, written "Heading Name"[MeSH Terms], with free-text synonyms using "phrase"[tiab].',
    "Use quotes for phrases and * truncation sparingly. Wrap each OR group in parentheses.",
    "Only use MeSH headings you are confident exist; the page verifies them. Do not claim you verified anything.",
    "Do not add date or article-type filters unless asked, because the page has filters for those.",
    "Reply with 1 to 3 short sentences explaining the strategy, then the final query alone in a fenced code block tagged pubmed.",
    "If the request is too vague, ask one short clarifying question and include no code block.",
    "Never request patient data. If the user includes personal health information, tell them to remove it and do not repeat it.",
    "Keep the query under 1500 characters.",
])


class ApiError(Exception):
    def __init__(self, status, message, retry=False, retry_after=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry = retry              # worth trying again after a short wait
        self.retry_after = retry_after  # seconds NCBI asked us to wait, if it said


# ---------------------------------------------------------------- PubMed (NCBI)

_ncbi_lock = threading.Lock()
_ncbi_last = [0.0]
RETRY_WAITS = (1.0, 2.5, 5.0)  # seconds to wait before retry 1, 2 and 3
TRANSIENT_HTTP = {429, 500, 502, 503, 504}

NCBI_BUSY = "PubMed is rate limiting requests. Wait a few seconds and try again."
NCBI_DOWN = ("PubMed's search service is having a problem right now. This is on NCBI's side, "
             "not yours. Try again in a few minutes.")
UNEXPECTED_PUBMED = ApiError(
    502, "PubMed returned an unexpected response. A firewall, proxy or security filter on your network "
         "may be replacing it. Try another network, or check with UW-IT.")


def _retry_after(e):
    try:
        return min(float(e.headers.get("Retry-After", "")), 10.0)
    except (TypeError, ValueError):
        return None


def _ncbi_request(endpoint, params):
    """One throttled request to NCBI. Raises ApiError (retry=True when trying again is worthwhile)."""
    # Limits are 3/s without a key and 10/s with one. Stay well under them, because other
    # programs on the same network address (browser tabs, other scripts) share the same limit.
    gap = 0.11 if NCBI_KEY else 0.5
    with _ncbi_lock:
        wait = _ncbi_last[0] + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _ncbi_last[0] = time.monotonic()
    q = {"tool": TOOL, **params}
    if NCBI_EMAIL:
        q["email"] = NCBI_EMAIL
    if NCBI_KEY:
        q["api_key"] = NCBI_KEY
    url = NCBI_BASE + endpoint + "?" + urllib.parse.urlencode(q)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise ApiError(429, NCBI_BUSY, retry=True, retry_after=_retry_after(e))
        if e.code in TRANSIENT_HTTP:
            raise ApiError(503, f"{NCBI_DOWN} (HTTP {e.code})", retry=True, retry_after=_retry_after(e))
        raise ApiError(502, f"PubMed returned an error (HTTP {e.code}).")
    except OSError:
        raise ApiError(502, "Could not reach PubMed. Check the server's internet connection.", retry=True)


def _check_json(raw):
    """Parse an E-utilities JSON reply. NCBI sometimes reports failures inside a normal reply."""
    try:
        data = json.loads(raw)
    except ValueError:
        raise UNEXPECTED_PUBMED
    if not isinstance(data, dict):
        raise UNEXPECTED_PUBMED
    msg = str(data.get("error") or (data.get("esearchresult") or {}).get("ERROR") or "")
    low = msg.lower()
    if "rate limit" in low:
        raise ApiError(429, NCBI_BUSY, retry=True)
    if "temporarily unavailable" in low or "backend failed" in low or re.search(r"status: 5\d\d", low):
        raise ApiError(503, NCBI_DOWN, retry=True)
    if data.get("error") and "esearchresult" not in data:
        raise ApiError(502, "PubMed returned an error: " + msg[:200])
    return data


def ncbi(endpoint, params, as_json=False):
    """Call NCBI, retrying a few times with growing waits when PubMed is busy or briefly down."""
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            raw = _ncbi_request(endpoint, params)
            return _check_json(raw) if as_json else raw
        except ApiError as e:
            if not e.retry or attempt == len(RETRY_WAITS):
                raise
            time.sleep(e.retry_after or RETRY_WAITS[attempt])


def ncbi_json(endpoint, params):
    return ncbi(endpoint, params, as_json=True)


class Cache:
    def __init__(self, ttl, size):
        self.ttl, self.size = ttl, size
        self.data = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            item = self.data.get(key)
            if item is None:
                return None
            expires, value = item
            if expires < time.time():
                del self.data[key]
                return None
            self.data.move_to_end(key)
            return value

    def put(self, key, value):
        with self.lock:
            self.data[key] = (time.time() + self.ttl, value)
            self.data.move_to_end(key)
            while len(self.data) > self.size:
                self.data.popitem(last=False)


search_cache = Cache(ttl=3600, size=300)
mesh_cache = Cache(ttl=86400, size=2000)


def parse_year(value):
    value = (value or "").strip()
    if not value:
        return None
    if not value.isdigit() or not 1900 <= int(value) <= 2100:
        raise ApiError(400, "Years must be between 1900 and 2100.")
    return int(value)


def build_term(query, types, year_from, year_to):
    term = query
    if types:
        term = f"({term}) AND ({' OR '.join(types)})"
    if year_from or year_to:
        term += f' AND ("{year_from or 1900}"[dp] : "{year_to or 3000}"[dp])'
    return term


def read_filters(params):
    query = (params.get("q") or "").strip()
    if not query:
        raise ApiError(400, "Enter a search query.")
    if len(query) > 1000:
        raise ApiError(400, "The query is too long (1000 characters maximum).")
    labels = [t for t in (params.get("types") or "").split(",") if t]
    types = [ARTICLE_TYPES[t] for t in labels if t in ARTICLE_TYPES]
    return build_term(query, types, parse_year(params.get("from")), parse_year(params.get("to")))


def text_of(node, path):
    el = node.find(path)
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_articles(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise UNEXPECTED_PUBMED
    out = []
    for a in root.findall("PubmedArticle"):
        art = "MedlineCitation/Article"
        authors = []
        for au in a.findall(f"{art}/AuthorList/Author"):
            last = text_of(au, "LastName")
            if last:
                authors.append({"last": last, "fore": text_of(au, "ForeName") or text_of(au, "Initials")})
            elif text_of(au, "CollectiveName"):
                authors.append({"last": text_of(au, "CollectiveName"), "fore": ""})
        year = text_of(a, f"{art}/Journal/JournalIssue/PubDate/Year")
        if not year:
            m = re.search(r"\d{4}", text_of(a, f"{art}/Journal/JournalIssue/PubDate/MedlineDate"))
            year = m.group(0) if m else ""
        doi = ""
        for i in a.findall("PubmedData/ArticleIdList/ArticleId"):
            if i.get("IdType") == "doi":
                doi = (i.text or "").strip()
        out.append({
            "pmid": text_of(a, "MedlineCitation/PMID"),
            "title": re.sub(r"\.$", "", text_of(a, f"{art}/ArticleTitle")),
            "authors": authors,
            "journal": text_of(a, f"{art}/Journal/Title"),
            "year": year,
            "volume": text_of(a, f"{art}/Journal/JournalIssue/Volume"),
            "issue": text_of(a, f"{art}/Journal/JournalIssue/Issue"),
            "pages": text_of(a, f"{art}/Pagination/MedlinePgn"),
            "doi": doi,
            "types": [t.text.strip() for t in a.findall(f"{art}/PublicationTypeList/PublicationType") if t.text],
            "abstract": [{"label": t.get("Label", ""), "text": "".join(t.itertext()).strip()}
                         for t in a.findall(f"{art}/Abstract/AbstractText")],
        })
    return out


def api_search(params):
    term = read_filters(params)
    sort = params.get("sort") or "relevance"
    if sort not in ("relevance", "pub_date"):
        raise ApiError(400, "Unknown sort option.")
    try:
        maxn = int(params.get("max") or "50")
    except ValueError:
        maxn = 0
    if maxn not in (20, 50, 100):
        raise ApiError(400, "Max results must be 20, 50 or 100.")

    key = json.dumps([term, sort, maxn])
    cached = search_cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    s = ncbi_json("esearch.fcgi", {"db": "pubmed", "term": term, "retmode": "json",
                                   "retmax": maxn, "sort": sort})
    res = s.get("esearchresult", {})
    if res.get("ERROR") or (res.get("errorlist", {}).get("phrasesnotfound") and not res.get("idlist")):
        raise ApiError(400, "PubMed could not understand that query. Check the syntax and try again.")
    ids = res.get("idlist", [])
    articles = parse_articles(ncbi("efetch.fcgi", {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"})) if ids else []
    if ids and not articles:  # never cache a reply that lost its article details
        raise ApiError(503, NCBI_DOWN)
    result = {"term": term, "total": int(res.get("count") or 0), "articles": articles}
    search_cache.put(key, result)
    return {**result, "cached": False}


MESH_PATTERNS = [
    re.compile(r'"([^"]+?)"\s*\[(?:MeSH Terms|MeSH|mh)(?::noexp)?\]', re.I),
    re.compile(r'(?:^|[\s(])([A-Za-z][A-Za-z\-]*)\s*\[(?:MeSH Terms|MeSH|mh)(?::noexp)?\]', re.I),
]


def mesh_names(term):
    names = []
    for pat in MESH_PATTERNS:
        for m in pat.finditer(term):
            name = m.group(1).strip()
            if name not in names:
                names.append(name)
    return names[:12]


def api_check(params):
    term = read_filters(params)
    s = ncbi_json("esearch.fcgi", {"db": "pubmed", "term": term, "retmode": "json", "retmax": 0})
    r = s.get("esearchresult", {})
    notes = []
    if r.get("ERROR"):
        notes.append("PubMed could not parse this query: " + r["ERROR"])
    el, wl = r.get("errorlist", {}), r.get("warninglist", {})
    notes += [f"Phrase not found in PubMed: {p}" for p in el.get("phrasesnotfound", [])]
    notes += [f"Unknown field tag: {p}" for p in el.get("fieldsnotfound", [])]
    notes += [f"Quoted phrase not found: {p}" for p in wl.get("quotedphrasesnotfound", [])]
    notes += [f"Ignored by PubMed: {p}" for p in wl.get("phrasesignored", [])]
    notes += list(wl.get("outputmessages", []))

    mesh = []
    for name in mesh_names(term):
        ok = mesh_cache.get(name)
        if ok is None:
            m = ncbi_json("esearch.fcgi", {"db": "mesh", "term": f'"{name}"[MeSH Terms]',
                                           "retmode": "json", "retmax": 0})
            ok = int((m.get("esearchresult") or {}).get("count") or 0) > 0
            mesh_cache.put(name, ok)
        mesh.append({"name": name, "found": ok})
    return {"count": int(r.get("count") or 0), "translation": r.get("querytranslation", ""),
            "notes": notes, "mesh": mesh, "hadError": bool(r.get("ERROR"))}


# ------------------------------------------------------------ AI query helper

_ai_lock = threading.Lock()
_ai_hits = defaultdict(deque)
_ai_day = [None, 0]


def ai_enabled():
    return (AI_PROVIDER == "gemini" and bool(GEMINI_KEY)) or (AI_PROVIDER == "claude" and bool(ANTHROPIC_KEY))


def ai_allow(ip):
    now = time.time()
    with _ai_lock:
        hits = _ai_hits[ip]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= AI_RATE:
            return False
        hits.append(now)
        today = time.strftime("%Y-%m-%d")
        if _ai_day[0] != today:
            _ai_day[0], _ai_day[1] = today, 0
        if _ai_day[1] >= AI_DAILY:
            return False
        _ai_day[1] += 1
        return True


def provider_error(code, message):
    low = (message or "").lower()
    if code == 429:
        return ApiError(429, "The AI provider's quota or rate limit is used up for now. Wait, or change the model name in the server's .env file.")
    if code in (400, 401, 403) and ("api key" in low or code in (401, 403)):
        return ApiError(502, "The AI provider rejected the server's API key. Check it in the .env file.")
    if code == 404:
        return ApiError(502, "The AI provider could not find the configured model name. Check GEMINI_MODEL or CLAUDE_MODEL.")
    return ApiError(502, f"The AI provider returned an error (HTTP {code}).")


def http_json(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            message = ""
        raise provider_error(e.code, message)
    except OSError:
        raise ApiError(502, "Could not reach the AI provider. Try again later.")
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise ApiError(502, "The AI provider returned an unexpected response. A firewall or proxy may be interfering.")


def call_gemini(msgs):
    url = f"{GEMINI_BASE}/v1beta/models/{urllib.parse.quote(GEMINI_MODEL)}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "model" if m["role"] == "assistant" else "user",
                      "parts": [{"text": m["content"]}]} for m in msgs],
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.3},
    }
    data = http_json(url, body, {"x-goog-api-key": GEMINI_KEY})
    cands = data.get("candidates") or []
    if not cands:
        if (data.get("promptFeedback") or {}).get("blockReason"):
            raise ApiError(422, "The provider blocked that request. Try rephrasing.")
        raise ApiError(502, "The model returned an empty reply. Try rephrasing.")
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts).strip()


def call_claude(msgs):
    body = {"model": CLAUDE_MODEL, "max_tokens": 1200, "system": SYSTEM, "messages": msgs}
    data = http_json(f"{ANTHROPIC_BASE}/v1/messages", body,
                     {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"})
    return "".join(c.get("text", "") for c in data.get("content") or [] if c.get("type") == "text").strip()


def api_suggest(body, ip):
    if not ai_enabled():
        raise ApiError(503, "The AI helper is not set up on this server. You can still write a query and use Check query.")
    topic = str(body.get("topic", "")).strip()
    if not topic:
        raise ApiError(400, "Describe your topic first.")
    if len(topic) > 1500:
        raise ApiError(400, "Keep the description under 1500 characters.")
    msgs = []
    for m in (body.get("history") or [])[-10:]:
        if not isinstance(m, dict):
            continue
        content = str(m.get("content", ""))[:3000]
        if m.get("role") in ("user", "assistant") and content:
            msgs.append({"role": m["role"], "content": content})
    msgs.append({"role": "user", "content": topic})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if not ai_allow(ip):
        raise ApiError(429, "Too many helper requests. Wait a minute and try again, or write the query yourself.")
    reply = call_gemini(msgs) if AI_PROVIDER == "gemini" else call_claude(msgs)
    if not reply:
        raise ApiError(502, "The model returned an empty reply. Try rephrasing.")
    return {"reply": reply}


# ------------------------------------------------------------------ HTTP layer

class Handler(BaseHTTPRequestHandler):
    server_version = "PubMedReadingList/1.0"

    def log_message(self, fmt, *args):  # never log request paths: they contain search text
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                         "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        try:
            if url.path in ("/", "/index.html"):
                return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/config":
                return self._json(200, {"aiEnabled": ai_enabled(), "provider": AI_PROVIDER,
                                        "model": GEMINI_MODEL if AI_PROVIDER == "gemini" else CLAUDE_MODEL})
            if url.path == "/api/search":
                return self._json(200, api_search(params))
            if url.path == "/api/check":
                return self._json(200, api_check(params))
            raise ApiError(404, "Not found.")
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except Exception as e:
            self._log_unexpected(url.path, e)
            self._json(500, {"error": "Something went wrong on the server."})

    def _log_unexpected(self, path, e):
        # Log the error type and message only. Never log the query string.
        print(f"[server error] {type(e).__name__} on {path}: {str(e)[:200]}", file=sys.stderr, flush=True)

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        try:
            if url.path != "/api/suggest":
                raise ApiError(404, "Not found.")
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                raise ApiError(400, "Bad request.")
            if n > 64 * 1024:
                raise ApiError(413, "Request is too large.")
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ApiError(400, "Bad request.")
            self._json(200, api_suggest(body, self.client_address[0]))
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except ValueError:
            self._json(400, {"error": "Bad request."})
        except Exception as e:
            self._log_unexpected(url.path, e)
            self._json(500, {"error": "Something went wrong on the server."})


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}/"
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print(f"Could not start on port {PORT}: {e}")
        print("Another program may be using this port, or Windows may have reserved it.")
        print("Add a different port to the .env file in this folder (for example PORT=8080), then run again.")
        input("Press Enter to close this window...")
        sys.exit(1)
    if HOST not in ("127.0.0.1", "localhost"):
        print("WARNING: listening beyond this computer. Put HTTPS and an access control in front of it.")
    print(f"PubMed Reading List Builder is running at {url}")
    print(f"AI helper: {'on' if ai_enabled() else 'off'}. Keep this window open while you use the page.")
    print("To stop the server, close this window or press Ctrl+C.")
    if ENV("OPEN_BROWSER", "1") == "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
