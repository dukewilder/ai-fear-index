"""Shared plumbing for every collector: config, database, HTTP, matching, status."""
import calendar
import contextlib
import datetime as dt
import hashlib
import html
import json
import os
import pathlib
import random
import re
import sqlite3
import sys
import time
import traceback

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
SINCE = "2025-01-01"  # current legislative sessions
REPO_URL = "https://github.com/dukewilder/ai-fear-report"
SITE_URL = "https://aifearreport.com"


def now():
    return dt.datetime.now(dt.timezone.utc)


def today():
    return now().date()


def iso(value=None):
    return (value or now()).isoformat(timespec="seconds")


def env(name, required=True):
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise MissingKey(f"{name} is not set")
    return value


class MissingKey(RuntimeError):
    pass


def config(name):
    return json.loads((CONFIG / f"{name}.json").read_text())


def user_agent():
    contact = os.environ.get("CONTACT_EMAIL", "").strip()
    return f"AIFearReport/1.0 (+{REPO_URL}{'; ' + contact if contact else ''})"


TLD = r"(?:com|org|net|gov|edu|io|co|uk|ca|au|news|tv)"
SPACED_TLD = re.compile(r"\s+\.\s+(" + TLD + r")\b", re.I)
SPACED_PUNCT = re.compile(r"\s+([,;:.!?%](?:\s|$))")
INITIALS = re.compile(r"\b([A-Z])\.\s+(?=[A-Z]\.)")
SPACED_BRACKET = re.compile(r"\(\s+|\s+\)")
CONTRACTION = re.compile(r"\s+'\s*(s|t|re|ve|ll|d|m)\b")
CREDIT = re.compile(r"\s*[\u2013\u2014|-]\s*([^|\u2013\u2014-]{2,40})\s*$")


def domain_key(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def tidy_headline(title, domain=""):
    """Undo what a headline feed does to punctuation, and drop the publisher stamped on the end.

    GDELT stores a tokenized title, so every comma and full stop arrives with a space in front of
    it and the site would print the space. The publisher's own name is often appended as well, and
    the site already shows that beside the headline. Only a tail matching the domain the headline
    came from is cut, so a headline that genuinely ends in a dash and a few words keeps them. No
    word is ever changed: this moves punctuation and removes a repeat of the source, nothing else.
    """
    t = " ".join((title or "").split())
    t = SPACED_TLD.sub(r".\1", t)
    t = SPACED_PUNCT.sub(r"\1", t)
    t = INITIALS.sub(r"\1.", t)
    t = SPACED_BRACKET.sub(lambda m: "(" if m.group(0).startswith("(") else ")", t)
    t = CONTRACTION.sub(r"'\1", t)
    host = domain_key(re.sub(r"^www\.", "", domain or "").split("/")[0])
    if host:
        m = CREDIT.search(t)
        if m:
            tail = domain_key(m.group(1))
            bare = re.sub(r"(com|org|net|gov|edu|io|co|news|tv)$", "", host)
            if tail and (tail == host or tail == bare or host.startswith(tail) or tail.startswith(host)):
                t = t[:m.start()].rstrip()
    return t.strip()


WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve"]


def spell(n):
    """A small number in words, so the copy can say how many fears there are rather than assume."""
    return WORDS[n] if 0 <= n < len(WORDS) else f"{n:,}"


def annotate(level, title, message):
    """Surface a message in the GitHub Actions UI (error, warning, notice)."""
    msg = str(message).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level} title={title}::{msg}", flush=True)


def log(*parts):
    print(*parts, flush=True)


def iso_from_struct(stamp):
    """A feedparser time tuple, which is UTC, as an ISO timestamp."""
    return dt.datetime.fromtimestamp(calendar.timegm(stamp), tz=dt.timezone.utc).isoformat(timespec="seconds")


def sha(*parts):
    return hashlib.sha1("\u241f".join(str(p) for p in parts).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- HTTP

class Http:
    """requests.Session with spacing between calls and retries on 429/5xx."""

    def __init__(self, min_interval=0.0, headers=None, timeout=60):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent()
        if headers:
            self.session.headers.update(headers)
        self.min_interval = min_interval
        self.timeout = timeout
        self._last = 0.0
        self.calls = 0

    def get(self, url, params=None, headers=None, tries=5, ok_statuses=(200,)):
        last_error = ""
        for attempt in range(tries):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(min(60, 3 * 2 ** attempt + random.random()))
                continue
            if resp.status_code in ok_statuses:
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else min(120, 5 * 2 ** attempt)
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                time.sleep(delay)
                continue
            raise HttpError(resp.status_code, url, resp.text[:500])
        raise HttpError(0, url, last_error or "gave up")

    def json(self, url, params=None, headers=None, **kw):
        resp = self.get(url, params=params, headers=headers, **kw)
        try:
            return resp.json()
        except ValueError:
            raise HttpError(resp.status_code, url, "not JSON: " + resp.text[:300])


class HttpError(RuntimeError):
    def __init__(self, status, url, body):
        clean = re.sub(r"(api_key|apikey|key)=[^&\s]+", r"\1=***", url)
        super().__init__(f"HTTP {status} for {clean}: {body}")
        self.status = status


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS measures(
  id TEXT PRIMARY KEY, kind TEXT, jurisdiction TEXT, jurisdiction_name TEXT, session TEXT,
  identifier TEXT, title TEXT, summary TEXT, status TEXT, latest_action TEXT, latest_action_date TEXT,
  introduced_date TEXT, url TEXT, sponsors TEXT, source TEXT, updated TEXT, first_seen TEXT);
CREATE TABLE IF NOT EXISTS tags(
  target TEXT, kind TEXT, value TEXT, evidence TEXT, model TEXT, tagged_at TEXT,
  PRIMARY KEY(target, kind, value));
CREATE TABLE IF NOT EXISTS tag_runs(
  target TEXT PRIMARY KEY, text_hash TEXT, ai_related INTEGER, tagged_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS lobbying(
  id TEXT PRIMARY KEY, client TEXT, client_key TEXT, registrant TEXT, year INTEGER, period TEXT,
  quarter TEXT, amount REAL, issues TEXT, bill_refs TEXT, gov_entities TEXT, url TEXT, posted TEXT,
  filing_type TEXT, first_seen TEXT);
CREATE TABLE IF NOT EXISTS committees(
  id TEXT PRIMARY KEY, name TEXT, entity TEXT, query TEXT, committee_type TEXT, receipts REAL,
  disbursements REAL, independent_expenditures REAL, cycle INTEGER, updated TEXT);
CREATE TABLE IF NOT EXISTS fec(
  id TEXT PRIMARY KEY, kind TEXT, committee_id TEXT, committee_name TEXT, counterparty TEXT,
  counterparty_key TEXT, amount REAL, date TEXT, description TEXT, support_oppose TEXT,
  candidate TEXT, url TEXT, first_seen TEXT, receipt_type TEXT, line_number TEXT);
CREATE TABLE IF NOT EXISTS articles(
  id TEXT PRIMARY KEY, fear TEXT, title TEXT, url TEXT, domain TEXT, seen TEXT, entity TEXT);
CREATE TABLE IF NOT EXISTS posts(
  id TEXT PRIMARY KEY, entity TEXT, title TEXT, summary TEXT, url TEXT, published TEXT,
  feed TEXT, first_seen TEXT);
CREATE TABLE IF NOT EXISTS series(series TEXT, date TEXT, value REAL, PRIMARY KEY(series, date));
CREATE TABLE IF NOT EXISTS brief(
  target TEXT PRIMARY KEY, edition TEXT, sentence TEXT, evidence TEXT, office TEXT, controls TEXT,
  written_at TEXT);
CREATE INDEX IF NOT EXISTS brief_edition ON brief(edition);
CREATE TABLE IF NOT EXISTS status(
  source TEXT PRIMARY KEY, last_run TEXT, ok INTEGER, added INTEGER, message TEXT, last_ok TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS history(date TEXT PRIMARY KEY, snapshot TEXT);
CREATE INDEX IF NOT EXISTS idx_articles_seen ON articles(seen);
CREATE INDEX IF NOT EXISTS idx_measures_intro ON measures(introduced_date);
CREATE INDEX IF NOT EXISTS idx_lobbying_year ON lobbying(year, quarter);
"""


BILL_NUMBER = re.compile(r"([A-Za-z]+)\s*0*(\d+)$")


def congress_id(session, identifier):
    """The id Congress.gov gives a federal bill, so one bill is one row whoever saw it first."""
    m = BILL_NUMBER.match((identifier or "").replace(".", "").strip())
    if not m or not str(session or "").isdigit():
        return None
    return f"us-{session}-{m.group(1).lower()}-{m.group(2)}"


def add_columns(db):
    """Columns added to a table after a database already exists. CREATE TABLE IF NOT EXISTS
    will not add them, so they are added here once, in place."""
    for table, column, kind in (("fec", "receipt_type", "TEXT"), ("fec", "line_number", "TEXT")):
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            db.commit()


def repair(db):
    """Correct rows an earlier version of a collector stored wrongly. Idempotent, and cheap
    once there is nothing left to correct.

    Open States files Congress under country:us rather than a state, which an earlier reading
    turned into a jurisdiction called "government": a phantom state in every count, and a
    federal bill that counted as neither. Each one moves onto the Congress row it belongs to.
    """
    rows = db.execute("SELECT id, session, identifier FROM measures WHERE jurisdiction='government'").fetchall()
    for r in rows:
        ident = congress_id(r["session"], r["identifier"])
        if ident and db.execute("SELECT 1 FROM measures WHERE id=?", (ident,)).fetchone():
            for table in ("tags", "tag_runs"):
                db.execute(f"DELETE FROM {table} WHERE target=?", (r["id"],))
            db.execute("DELETE FROM measures WHERE id=?", (r["id"],))
        elif ident:
            for table in ("tags", "tag_runs"):
                db.execute(f"DELETE FROM {table} WHERE target=?", (ident,))
                db.execute(f"UPDATE {table} SET target=? WHERE target=?", (ident, r["id"]))
            db.execute("UPDATE measures SET id=?, jurisdiction='us', jurisdiction_name='Congress' WHERE id=?",
                       (ident, r["id"]))
        else:
            db.execute("UPDATE measures SET jurisdiction='us', jurisdiction_name='Congress' WHERE id=?", (r["id"],))
    if rows:
        db.commit()
        log(f"[repair] {len(rows)} federal bills moved out of the phantom jurisdiction")
    return len(rows)


RECITAL = ("SELECT id FROM measures WHERE summary LIKE '%This bill would%' "
           "AND (summary LIKE '%Existing law%' OR summary LIKE '%Act requires%' OR summary LIKE '%Act authorizes%')")


def retag_recitals(db):
    """A state summary often recites the law already in force before saying what the bill does.

    The first tagger read both as the bill's own text, so a handful of measures carried a control
    the bill would not impose. The prompt now draws that line; these are the rows tagged before it
    did, queued once so the next pass reads them again.
    """
    if kv_get(db, "retag:recitals"):
        return 0
    ids = [r["id"] for r in db.execute(RECITAL)]
    if ids:
        marks = ",".join("?" * len(ids))
        db.execute(f"DELETE FROM tag_runs WHERE target IN ({marks})", ids)
        db.execute(f"DELETE FROM tags WHERE target IN ({marks})", ids)
    kv_set(db, "retag:recitals", True)
    db.commit()
    log(f"[repair] {len(ids)} summaries that recite existing law queued for a second reading")
    return len(ids)


def redo_briefs(db):
    """Throw away the editions written before the tense was checked.

    The first edition called a bill sitting in committee a duty that data centers "must" meet. It
    was published but never posted, and the entries it used are marked as spent, so without this
    they would never come round again. Clearing the table puts them back in the pool to be written
    properly.
    """
    if kv_get(db, "redo:briefs"):
        return 0
    n = db.execute("SELECT COUNT(*) FROM brief").fetchone()[0]
    db.execute("DELETE FROM brief")
    kv_set(db, "redo:briefs", True)
    db.commit()
    if n:
        log(f"[repair] {n} brief entries cleared, to be written again with the tense checked")
    return n


# Bump this when an edition already written needs writing again, and say why in the comment.
# 2: the first rewrite fell through to a pattern because every candidate sentence was rejected,
#    and the rejections did not say which gate did it. They do now.
# 3: it fell through again, and the log that would have said why is not reachable from here. The
#    reasons are now written into the database, where they ride the data branch.
# 4: those reasons named it. Every sentence had been dying of a NameError since the rules were
#    rewritten, caught by a row-by-row except and reported as the model not answering.
REDO_EDITION = ("2026-09-20", 4)


def redo_today(db):
    """Write one edition again, because the one on file was written by code since changed.

    The entries an edition used are marked spent so they never come round twice. Clearing the rows
    puts them back in the pool, and clearing the record of the post lets the day go out once more.
    """
    day, version = REDO_EDITION
    key = f"redo:{day}:{version}" if version > 1 else f"redo:{day}"
    if kv_get(db, key):
        return 0
    n = db.execute("SELECT COUNT(*) FROM brief WHERE edition=?", (day,)).fetchone()[0]
    db.execute("DELETE FROM brief WHERE edition=?", (day,))
    db.execute("DELETE FROM kv WHERE key=?", (f"posted:{day}",))
    kv_set(db, key, True)
    db.commit()
    if n:
        log(f"[repair] the {day} edition is cleared again, to be written by the code that replaced it")
    return n


def connect(path):
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=60)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    add_columns(db)
    repair(db)
    retag_recitals(db)
    redo_briefs(db)
    redo_today(db)
    return db


def kv_get(db, key, default=None):
    row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(db, key, value):
    db.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))


def retry_at(db, name, hours):
    """Ask for another run of one source later today, when a refusal cost nothing."""
    kv_set(db, f"retry:{name}", (now() + dt.timedelta(hours=hours)).isoformat(timespec="seconds"))


def retry_clear(db, name):
    db.execute("DELETE FROM kv WHERE key=?", (f"retry:{name}",))


def retry_due(db, name):
    when = kv_get(db, f"retry:{name}")
    return bool(when) and now().isoformat(timespec="seconds") >= when


def upsert(db, table, row, key="id"):
    cols = list(row)
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in (key, "first_seen"))
    sql = (f"INSERT INTO {table}({','.join(cols)}) VALUES({placeholders}) "
           f"ON CONFLICT({key}) DO UPDATE SET {updates}")
    cur = db.execute(sql, [row[c] for c in cols])
    return cur.rowcount


def exists(db, table, ident):
    return db.execute(f"SELECT 1 FROM {table} WHERE id=?", (ident,)).fetchone() is not None


@contextlib.contextmanager
def source_run(db, name):
    """Record success or failure for one collector without stopping the others."""
    state = {"added": 0, "message": ""}
    started = time.time()
    try:
        yield state
        ok, message = 1, state["message"] or "ok"
        log(f"[{name}] ok: {state['added']} new or updated, {time.time() - started:.0f}s. {state['message']}")
    except MissingKey as exc:
        ok, message = 0, f"skipped: {exc}"
        annotate("warning", f"{name} skipped", exc)
    except Exception as exc:  # keep going; the site shows the source as stale
        ok, message = 0, f"{type(exc).__name__}: {exc}"[:800]
        annotate("error", f"{name} failed", message)
        traceback.print_exc(file=sys.stdout)
    prev = db.execute("SELECT last_ok FROM status WHERE source=?", (name,)).fetchone()
    last_ok = iso() if ok else (prev["last_ok"] if prev else None)
    db.execute(
        "INSERT INTO status(source,last_run,ok,added,message,last_ok) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET last_run=excluded.last_run, ok=excluded.ok, "
        "added=excluded.added, message=excluded.message, last_ok=excluded.last_ok",
        (name, iso(), ok, state["added"], message, last_ok))
    db.commit()


# ---------------------------------------------------------------- matching

SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l l c|corp|corporation|co|company|ltd|limited|lp|llp|plc|pbc|opco|"
    r"holdings|group|the|na|n a|us|usa)\b")


def name_key(name):
    text = (name or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = SUFFIXES.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


class Entities:
    def __init__(self):
        self.items = config("entities")
        self.by_slug = {e["slug"]: e for e in self.items}
        self.alias = {}
        for e in self.items:
            for a in [e["name"], *e.get("aliases", [])]:
                self.alias[name_key(a)] = e["slug"]
        self.domain = {}
        for e in self.items:
            for d in e.get("domains", []):
                self.domain[d.lower()] = e["slug"]

    def match_name(self, name):
        key = name_key(name)
        if not key:
            return None
        if key in self.alias:
            return self.alias[key]
        for alias, slug in self.alias.items():
            if len(alias) >= 6 and (key.startswith(alias + " ") or key == alias):
                return slug
        return None

    def match_domain(self, domain):
        d = (domain or "").lower().removeprefix("www.")
        while d:
            if d in self.domain:
                return self.domain[d]
            if "." not in d:
                return None
            d = d.split(".", 1)[1]
        return None


def fear_keywords():
    out = {}
    for f in config("fears"):
        out[f["slug"]] = [k.lower() for k in f.get("keywords", [])]
    return out


def fears_mentioned(text, keywords=None):
    keywords = keywords or fear_keywords()
    low = (text or "").lower()
    return sorted(slug for slug, words in keywords.items() if any(w in low for w in words))


def strip_html(text):
    """Tags out, entities decoded. A feed that writes &#8217; means an apostrophe."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


AI_TEXT = re.compile(
    r"(?i)\b(artificial intelligence|machine learning|algorithm(ic|s)?|automated decision|deep ?fakes?|"
    r"synthetic media|digital replicas?|chat ?bots?|large language models?|foundation models?|"
    r"frontier (ai|models?)|generative|data cent(er|re)s?|autonomous weapons?|facial recognition|"
    r"neural network|computer vision)\b|\bA\.?I\.?\b")


def looks_ai(*texts):
    return any(AI_TEXT.search(t or "") for t in texts)


def status_from_action(text):
    t = (text or "").lower()
    if re.search(r"signed by (the )?governor|chaptered|became (public )?law|enacted|approved by (the )?governor|"
                 r"effective|public law|act no\.|signed into law|final rule", t):
        return "passed"
    if re.search(r"veto|failed|died|withdrawn|indefinitely postponed|tabled|dead", t):
        return "failed"
    return "pending"
