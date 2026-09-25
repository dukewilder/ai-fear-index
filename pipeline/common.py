"""Shared plumbing for every collector: config, database, HTTP, matching, status."""
import calendar
import collections
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
import urllib.parse

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
SINCE = "2025-01-01"  # current legislative sessions
# How much of an official summary is kept, and the tagger and the check read all of what is kept. It
# was 6,000 characters kept and 5,000 checked, which cut short seven summaries, California's SB 53 and
# New York's budget bills among them, and left nine only partly checked.
SUMMARY_MAX = 60000
OLD_SUMMARY_CUT = 6000
# Legislatures that number their sessions rather than naming them by year, and the year each of
# these began. A session that began in 2025 is inside the report even for a bill filed before
# January: Montana numbers its 2025 bills from draft requests made the summer before, and Texas
# and Virginia pre-file in November. Montana's Right to Compute Act was left out that way.
NUMBERED_SESSIONS = {
    ("us", "119"): 2025, ("ak", "34"): 2025, ("az", "57th"): 2025, ("de", "153"): 2025,
    ("il", "104th"): 2025, ("ma", "194th"): 2025, ("me", "132"): 2025, ("nd", "69"): 2025,
    ("ne", "109"): 2025, ("nj", "221"): 2024, ("nj", "222"): 2026, ("nv", "83"): 2025,
    ("oh", "136"): 2025, ("tn", "114"): 2025, ("tx", "89"): 2025,
}


def session_year(jurisdiction, session):
    """The year a legislative session began, or None when it cannot be told from its name."""
    s = str(session or "").strip()
    m = re.match(r"(20\d\d)", s)
    if m:
        return int(m.group(1))
    if (jurisdiction, s) in NUMBERED_SESSIONS:
        return NUMBERED_SESSIONS[(jurisdiction, s)]
    for (j, prefix), year in NUMBERED_SESSIONS.items():
        if j == jurisdiction and s.startswith(prefix):
            return year  # "57th-1st-regular", "89R" and "891" are sessions of the 57th and 89th
    return None


def in_window(m):
    """Inside the report's period: a federal rule or order, a measure filed since it starts, or one
    filed for a legislative session that began then."""
    if (m["kind"] or "") in ("rule", "order") or (m["introduced_date"] or "") >= SINCE:
        return True
    year = session_year(m["jurisdiction"], m["session"])
    return year is not None and year >= int(SINCE[:4])


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


# The vocabulary a sponsor reaches for so a power sounds like a courtesy. It is the tactic this
# report exists to undo, so the site never writes one in its own voice: not in the daily sentence,
# not on the card, not in the copy. A measure's own title and any line quoted from one keep every
# word the legislature chose, because the gap between how it is sold and what it does is the whole
# exhibit, and editing a quote would destroy it.
#
# Grouped by the move each one makes.
EUPHEMISM = re.compile(r"""\b(?:
    # the power is for your own good
      safeguard\w* | guardrail\w* | protections? | protects? | protecting | protective
    | safety | safety[\s-]net | duty[\s-]of[\s-]care | harm[\s-]reduction
    | wellbeing | well[\s-]being | welfare | bad[\s-]actors?
    | mitigat\w* | minimi[sz]\w*[\s-]harm | uphold\w*
    | (?:child(?:ren)?(?:'s)?|kids?(?:'s)?|minors?(?:'s)?|user|public|online|consumer)[\s-]+safety
    | safety[\s-]+(?:audit|review|standard|requirement|measure|protocol|practice|assessment)s?
    | trust[\s-]and[\s-]safety | trustworthy | trusted
    # the power is a virtue
    | responsib\w* | accountab\w* | ethical | ethics | integrity | assurances?
    # the power is just an update
    | modern\w* | future[\s-]proof | streamlin\w* | twenty[\s-]first[\s-]century
    # the power is merely a process
    | framework\w* | oversight | governance | guidance | guidelines
    | best[\s-]practices? | common[\s-]?sense | sensible | balanced?
    | thoughtful\w* | pragmatic | prudent | proportionate | appropriate
    | measured[\s-]+(?:approach|response|steps?) | reasonable[\s-]steps?
    | necessary[\s-]and[\s-]appropriate
    # the power is a conversation
    | stakeholder\w* | public[\s-]private | voluntary[\s-]commitments?
    # the power is a gift
    | empower\w* | ensur\w* | innovation | responsible[\s-]innovation | promote[\s-]innovation
    # the power is fair. Not bare "fair", which is ordinary English for accurate.
    | fairness | equitabl\w*
    # the power is sunlight
    | transparency | visibility[\s-]into
)\b""", re.I | re.X)


# A measure whose title opens this way asks, urges or objects. It imposes nothing on anybody, so
# it carries no control. Two of the five it was giving one to were the inverse of the label: Kansas
# HR 6023 opposes federal preemption and was counted as preemption, and so was a Pennsylvania
# resolution urging Congress to drop the idea.
POSITION = re.compile(
    r"^\s*(?:an?\s+)?(?:\w+\s+)?resolution\s+(?P<a>opposing|urging|supporting|condemning|"
    r"memorializ\w*|recognizing|encouraging|commending|requesting|expressing|congratulating)\b"
    r"|^\s*(?P<b>opposing|urging|supporting|condemning|memorializ\w*|recognizing|encouraging|"
    r"commending|requesting|expressing|congratulating)\b", re.I)


def states_a_position(title):
    """True when the title takes a side rather than doing something."""
    return bool(POSITION.search(title or ""))


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
                last_error = scrub(f"{type(exc).__name__}: {exc}")
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


# Anything that looks like a credential in a query string, and the literal value of every key
# this pipeline is given. The literal pass is the one that matters: a requests exception prints
# the whole URL in its own words, key and all, and a status message is written into a database
# that is published on a public branch. Scrubbing the URL argument alone left that path open.
SECRET_PARAM = re.compile(
    r"((?:api[_-]?key|apikey|key|token|access_token|client_secret|password)=)[^&\s'\"<>:,)]+", re.I)
SECRET_ENVS = ("CONGRESS_API_KEY", "OPENSTATES_API_KEY", "FEC_API_KEY", "LDA_API_KEY",
               "ANTHROPIC_API_KEY", "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN",
               "X_ACCESS_SECRET", "GH_TOKEN", "GITHUB_TOKEN", "CONTACT_EMAIL")


def scrub(text):
    """Take credentials out of anything that is about to be written down or published."""
    text = SECRET_PARAM.sub(r"\1***", str(text if text is not None else ""))
    for name in SECRET_ENVS:
        value = os.environ.get(name, "")
        if len(value) >= 6:
            text = text.replace(value, "***")
    return text


class HttpError(RuntimeError):
    def __init__(self, status, url, body):
        super().__init__(scrub(f"HTTP {status} for {url}: {body}"))
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
CREATE TABLE IF NOT EXISTS checks(
  target TEXT, kind TEXT, value TEXT, evidence TEXT, verdict TEXT, reason TEXT, model TEXT, checked_at TEXT,
  PRIMARY KEY(target, kind, value));
CREATE TABLE IF NOT EXISTS history(date TEXT PRIMARY KEY, snapshot TEXT);
CREATE TABLE IF NOT EXISTS texts(
  target TEXT PRIMARY KEY, url TEXT, text TEXT, fetched TEXT, error TEXT, tries INTEGER DEFAULT 0, asked TEXT);
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
    # measures.source_url: the legislature's own page for a bill, where Open States gives one. NULL
    # until the collector has asked; an empty string once it has and there was none to give.
    for table, column, kind in (("fec", "receipt_type", "TEXT"), ("fec", "line_number", "TEXT"),
                                ("measures", "source_url", "TEXT")):
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
# 5: the first real sentence read "California puts AI auditors under registration" for a law whose
#    registry opens in 2029. A measure that names a later start now has to say the year.
# 6: the next one reverted to the sponsor's grammar, a list of what a company has to do. A duty
#    sentence that hands nobody a power is refused.
# 7: the plate's line read its tense from the whole sentence and could not name an office.
# 8: "under child safety audits" carried the sponsor's reason instead of the act. The audit is of
#    the service, and everyone using it is inside it.
# 9: the edition that went out said California would exempt data centers from environmental review.
#    The measure puts them under the Governor's authority to certify them as exempt, which is an
#    office deciding who is exempt, not a requirement going away. The plate dropped the office.
# 2026-09-21: the launch edition. It was written around midnight by code that counted carried-over
#    bills twice, said offices "hold" powers that bills in committee would hand them, and could
#    lead with a label since taken off by hand. Written again by the code that fixed all three,
#    well before it posts at nine.
#    2: its plate read "Health and Human Services Department, Food and Drug Administration: reports
#    companies must file with the state" for a federal rule. The fallback headline for reporting
#    said the state whoever the measure was, and named a federal rule by its parent department.
# 3: the third reading went in (pipeline.check). Controls and offices are now asked whether the
#    measure does what the label says, and only confirmed ones can lead. The edition is written once
#    more so the post and the page count the same labels at launch.
# 4: the third reading's first pass refused 349 of 749 control and office labels. Its circuit
#    breaker held them; read by hand they were right, so they are applied, and the edition is
#    written once more so the post counts what the page counts.
# 5: that edition led with New York legislators disclosing AI-drafted remarks, after the three
#    measures ahead of it were each refused for one fixable fault. A refused sentence is now written
#    once more with the fault named before its measure is dropped.
# 6: that one led with New Jersey putting data center approvals "under a six-month moratorium and a
#    commission's review". The bill creates a commission and gives it no power the third reading
#    could confirm. A sentence may now name a commission, council or board only if it is a
#    confirmed office of the measure.
# 7: the gates themselves refused good sentences. ", beginning 2027" at the end was read as an
#    explaining clause, and "From 2027, California would put" was read as present tense because the
#    date came first. The rules ask for the year; the gates now let it through.
# 8: the third reading now covers fears too, and the disclosure and reporting definitions were
#    widened to take in real mandates on private parties they had refused. The launch counts change,
#    so the edition is written once more to match them.
# 9: that one led with "California puts companion chatbots under independent auditors". The power in
#    the law is the Attorney General's, to demand the audit reports, and the plate left that out. The
#    lead now says what the office can do, and a firm a company hires cannot stand in for it.
# 10: written again, the California lead dropped "child safety" and then tucked the power into a
#    closing clause ("giving the Attorney General power to demand audit reports"), and was dropped.
#    The edition fell to New York legislators who "would be required to" disclose AI-drafted remarks.
#    The brief now asks the stronger model, gives it three attempts told every fault, names the
#    tucked clause as the fault, and reads the passive as the duty it is.
# 11: the stronger model was given the smaller one's 400 tokens and answered seven candidates in a
#    row with no JSON, so the edition led with the eighth. It now has room, a reminder, and the
#    smaller model behind it when an answer comes back without the JSON.
# 12: that edition led with the Attorney General's power over the audit reports "beginning July 1,
#    2027". The date in the digest belongs to the law's duties; the audit section has none, and its
#    first audits are due by 2029. A start year now has to come from the part of the measure quoted.
# 13: that one was right and said "an operator's audit report" without saying an operator of what,
#    and the plate read "can request operator audit reports for cause". A sentence and its plate
#    now have to say what AI the measure is about, in the measure's words.
# 2026-09-25: written just after midnight, when every measure had been queued to be read again against
#    the seventeen fears and the new control for bans and limits. Its totals (355 measures carrying a
#    control, 68 offices) were counted mid-reading. Written again once the reading is done, before it
#    posts at nine, so the post counts what the page counts.
#    2: written again, it led with Pennsylvania giving its Emergency Management Agency and Attorney
#    General "the power to impose duties on frontier developers". The measure imposes duties on those
#    two offices and sets the developers' standards itself. A duty on an office is now its job, not a
#    power over anyone, in the rules and in the gate.
#    3: the second rewrite gave the Emergency Management Agency "the power to demand reports", which the
#    title does not say. A power now has to be one the text states.
REDO_EDITION = ("2026-09-25", 3)


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


# Bump to clear control tags from measures whose titles only take a position. The tagger refuses
# them now; these are the ones it agreed to before it did.
DROP_POSITION_CONTROLS = 1


def drop_position_controls(db):
    """A resolution opposing preemption was counted among the measures that would preempt."""
    key = f"repair:position-controls:{DROP_POSITION_CONTROLS}"
    if kv_get(db, key):
        return 0
    gone = 0
    for r in db.execute("SELECT DISTINCT m.id, m.title FROM measures m "
                        "JOIN tags t ON t.target = m.id AND t.kind = 'control'").fetchall():
        if states_a_position(r["title"]):
            db.execute("DELETE FROM tags WHERE target=? AND kind='control'", (r["id"],))
            gone += 1
    kv_set(db, key, True)
    db.commit()
    if gone:
        log(f"[repair] {gone} measures that only take a position no longer carry a control")
    return gone


# Bump when a change to config/fears.json needs the whole record read again. Adding a fear does
# not change a single measure's text, and the tagger only re-reads a measure whose text has
# changed, so without this a new fear would only ever be applied to bills filed after it.
# 2: the list grew from ten fears to the highest-scoring of every fear measured on 25 September 2026,
#    and bans and limits on uses of AI became a control, so every measure is read against both.
RETAG_FEARS = 2


def retag_for_fears(db):
    """Queue every AI measure to be read again, because the list of fears it is read against grew.

    The hash is cleared rather than the tags. A measure keeps the labels it has until the moment
    it is read again, so the site never shows a gap while the queue drains. About 1,700 measures,
    which the daily cap clears in a day or two.
    """
    key = f"retag:fears:{RETAG_FEARS}"
    if kv_get(db, key):
        return 0
    n = db.execute("UPDATE tag_runs SET text_hash = NULL WHERE ai_related = 1").rowcount
    kv_set(db, key, True)
    db.commit()
    if n:
        log(f"[repair] {n} measures queued to be read against the fears as they now stand")
    return n


# Bump to read again the Federal Register documents whose title names AI but which the tagger
# judged not about AI. Executive Orders 14179 and 14319 were missing from the record that way.
RETAG_AI_TITLES = 1


def retag_ai_titles(db):
    """Queue those documents once, for a tagger that no longer lets a title naming AI be overruled."""
    key = f"retag:ai-titles:{RETAG_AI_TITLES}"
    if kv_get(db, key):
        return 0
    ids = [r["id"] for r in db.execute(
        "SELECT m.id, m.title FROM measures m JOIN tag_runs t ON t.target = m.id "
        "WHERE m.id LIKE 'fr-%' AND t.ai_related = 0") if AI_TEXT.search(r["title"] or "")]
    for mid in ids:
        db.execute("UPDATE tag_runs SET text_hash = NULL WHERE target = ?", (mid,))
    kv_set(db, key, True)
    db.commit()
    if ids:
        log(f"[repair] {len(ids)} Federal Register documents titled with AI queued to be read again")
    return len(ids)


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
    retag_for_fears(db)
    drop_position_controls(db)
    retag_ai_titles(db)
    retag_ai_bills(db)
    retag_data_centers(db)
    retag_self_driving(db)
    recheck_data_center_labels(db)
    restatus(db)
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
        ok, message = 1, scrub(state["message"] or "ok")
        log(f"[{name}] ok: {state['added']} new or updated, {time.time() - started:.0f}s. {state['message']}")
    except MissingKey as exc:
        ok, message = 0, f"skipped: {exc}"
        annotate("warning", f"{name} skipped", exc)
    except Exception as exc:  # keep going; the site shows the source as stale
        ok, message = 0, scrub(f"{type(exc).__name__}: {exc}")[:800]
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


# A bill that lets "district attorneys, county counsels, city attorneys and city prosecutors" enforce
# it names one class of local official four ways, and counted apart they read as four agencies.
LOCAL_PROSECUTORS = re.compile(r"\b(district|city|county|public|local) (attorneys|prosecutors|counsels)\b|"
                               r"\bprosecuting attorneys\b", re.I)


STATE_NAMES = ("Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
               "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
               "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
               "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
               "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
               "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
               "West Virginia", "Wisconsin", "Wyoming", "District of Columbia", "Puerto Rico")
# The attorney general's office is the attorney general. Pennsylvania's was counted twice, once as
# "Pennsylvania Attorney General" and once as "Pennsylvania Office of Attorney General", and Hawaii's
# as its Attorney General and its "Department of the Attorney General".
AG_OFFICE = re.compile(r"\b(?:Office|Department) of (?:the )?Attorney General\b|\bAttorney General's Office\b")
# A proper name the tagger left in lower case ("Colorado civil rights division"). A plural is a
# class of offices ("California health care professional licensing boards") and keeps its case.
LOWER_OFFICE = re.compile(r"[a-z][a-z ]* (?:division|department|commission|board|office|bureau|agency|authority|"
                          r"council|cabinet)")
SMALL_WORDS = {"of", "and", "the", "for", "on", "in"}


def office_label(name, where=None):
    """The name an office is counted and shown under, or "" when the measure does not name one.

    The tagger names each office with its jurisdiction, which is what makes offices countable, but
    it spells one office several ways: "California Medical Board" and "California Medical Board of
    California" are one board. An office it could only describe, "Minnesota (state agency overseeing
    AI independent verification organizations)", is not a named office and is not counted as one.
    where is the measure's jurisdiction code: a federal office named without its country ("Department
    of Commerce" in a bill before Congress) is given it, as the Department of Labor already has it.
    """
    name = " ".join((name or "").split())
    m = LOCAL_PROSECUTORS.search(name)
    if m:
        prefix = name[:m.start()].strip()
        return f"{prefix} local prosecutors" if prefix else "Local prosecutors"
    bare = re.sub(r"\s*\([^)]*\)", "", name).strip()
    if bare != name and (not bare or bare in STATE_NAMES or bare.lower() in ("state", "federal", "the state")):
        return ""
    name = AG_OFFICE.sub("Attorney General", bare)
    for state in STATE_NAMES:
        if name.startswith(state + " "):
            rest = name[len(state) + 1:]
            rest = re.sub(rf"\s+of (?:the State of )?{state}$", "", rest)
            rest = re.sub(r"^State (?=Department\b)", "", rest)
            if LOWER_OFFICE.fullmatch(rest):
                rest = " ".join(w if w in SMALL_WORDS else w.capitalize() for w in rest.split())
            name = f"{state} {rest}"
            break
    if where in ("us", "us-exec") and re.match(r"(?:Department|Office|Bureau|Agency) of\b", name):
        name = "U.S. " + name
    return name


def name_key(name):
    text = (name or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = SUFFIXES.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- the record the site counts

STATES = frozenset("al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne "
                   "nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy".split())
FEDERAL = frozenset(("us", "us-exec"))
# Places Open States covers that are not states. A count that calls Puerto Rico a state is wrong
# by one in a way anyone can check, so these are counted and named on their own.
TERRITORIES = {"pr": "Puerto Rico", "dc": "the District of Columbia"}


def one_record_per_bill(rows):
    """A bill carried into a legislature's next session, counted once.

    Open States files a carried-over bill again under the new session with a new id: Hawaii's 2025
    bills come back in 2026, Oklahoma's too, and Virginia's 2026 bills in 2027, each with the same
    number, title and date introduced. Taken as they came, 25 bills were counted twice. The record
    from the latest session is the one kept, because it is the one still moving.
    """
    best = {}
    for i, r in enumerate(rows):
        if r["kind"] in ("rule", "order") or not r["identifier"]:
            key = ("id", r["id"])
        else:
            key = (r["jurisdiction"], re.sub(r"\s+", "", r["identifier"].upper()),
                   " ".join((r["title"] or "").lower().split()), r["introduced_date"] or "")
        rank = (str(r["session"] or ""), r["latest_action_date"] or "", r["updated"] or "", r["id"])
        if key not in best or rank > best[key][1]:
            best[key] = (i, rank)
    return [rows[i] for i in sorted(i for i, _ in best.values())]


def measures_in_scope(db):
    """Every measure the site counts, which is every measure the daily card counts.

    Read and judged to be about AI, filed since the report starts or a federal rule or order, not
    on the suppression list, and a carried-over bill once. The front page and the card both count
    over this one list, so they cannot disagree by reading two different sets.
    """
    suppressed = set(config("suppress").get("urls", []))
    rows = [dict(r) for r in db.execute(
        "SELECT m.* FROM measures m JOIN tag_runs tr ON tr.target = m.id WHERE tr.ai_related = 1")]
    return one_record_per_bill([r for r in rows if in_window(r) and r["url"] not in suppressed])


# Open States' own bill pages now redirect into Plural's app, which shows a blank page until its
# script loads and sometimes after. A reader who clicked a bill on this site landed there and took
# the app, with its copied text, for this site's reading of the bill.
OPEN_STATES_HOSTS = ("openstates.org", "pluralpolicy.com", "open.pluralpolicy.com")


def read_link(m):
    """Where a reader goes to read a measure, and the name of the site that is, as (href, where).

    The legislature's own page when Open States gave one, or failing that the bill's text on the
    legislature's site. Congress.gov and the Federal Register records are the official pages
    already. An Open States address is the last resort, and says so.
    """
    href = (m.get("source_url") or "").strip() or (m.get("url") or "")
    host = urllib.parse.urlparse(href).netloc.lower().split(":")[0].removeprefix("www.")
    if not host:
        return href, ""
    if host.endswith(OPEN_STATES_HOSTS):
        return href, "Open States"
    if urllib.parse.urlparse(href).path.lower().endswith(".pdf"):
        return href, f"{host}, PDF"
    return href, host


def where_counted(codes):
    """How many states, which other places, and whether the federal government: "50 states, Puerto
    Rico and the federal government" rather than "51 states". Returns the three parts."""
    codes = set(codes)
    states = len(codes & STATES)
    others = [TERRITORIES[c] for c in sorted(codes & set(TERRITORIES))]
    return states, others, bool(codes & FEDERAL)


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


# Pages on an organization's own site that repost other outlets' coverage of it. AI Now files CNN
# and Guardian stories under /news/press/, and CSET files New York Times and CNN pieces under
# /article/. Shown as the organization's statements, a CNN headline quoting AI Now's director
# doubting doom claims was published as AI Now warning of loss of control.
CLIPPINGS = re.compile(r"(^|\.)ainowinstitute\.org/news/press/|(^|\.)cset\.georgetown\.edu/article/|"
                       r"/in-the-news/|/media-coverage/|/press-coverage/|/news-coverage/", re.I)


def own_post(entity, url, ents=None):
    """True when a feed entry is the organization's own words: a page on its own site that is not
    a repost of somebody else's story about it."""
    ents = ents or Entities()
    ent = ents.by_slug.get(entity) or {}
    parts = urllib.parse.urlparse(url or "")
    host = parts.netloc.lower().split(":")[0].removeprefix("www.")
    if not host or CLIPPINGS.search(host + parts.path):
        return False
    homes = set(d.lower().removeprefix("www.") for d in ent.get("domains", []))
    for page in (ent.get("homepage"), ent.get("rss")):
        if page:
            homes.add(urllib.parse.urlparse(page).netloc.lower().split(":")[0].removeprefix("www."))
    return any(host == d or host.endswith("." + d) for d in homes if d)


def fear_keywords():
    """Each fear's lobbying words, compiled.

    A word, not a run of letters. Matching anywhere in the text found "agi" inside imaging and
    managing, "labor" inside laboratory and collaboration, "teen" inside fifteen, and "minor"
    inside minority: 168 of the 197 filings that matched agi were phantoms. A keyword matches at
    the start of a word and ends at the end of one, unless it is written with a trailing star,
    which is how a stem like discriminat* covers discrimination and discriminatory.
    """
    out = {}
    for f in config("fears"):
        pats = []
        for k in f.get("keywords", []):
            stem = k.lower().rstrip("*")
            pats.append(re.compile(r"\b" + re.escape(stem) + ("" if k.endswith("*") else r"\b"), re.I))
        out[f["slug"]] = pats
    return out


def fears_mentioned(text, keywords=None):
    keywords = keywords or fear_keywords()
    text = text or ""
    return sorted(slug for slug, pats in keywords.items() if any(p.search(text) for p in pats))


def strip_html(text):
    """Tags out, entities decoded. A feed that writes &#8217; means an apostrophe."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


# Vehicles that drive themselves, as statutes and sponsors name them: the owner's decision of 25
# September 2026 counts them with AI whether or not a bill says AI, as data centers are counted. An
# AI is the driver, and a bill on robotaxis or driverless trucks decides who may put one on the road.
# Not a bare "automated vehicle": Washington's "automated vehicle noise enforcement cameras" are
# cameras, and "automated vehicle identification" is a toll reader.
AV_TERMS = (r"autonomous (?:motor )?vehicles?|self[- ]driving|driverless|automated driving(?: systems?)?|"
            r"automated motor vehicles?|(?:highly|fully) automated vehicles?|robo[- ]?taxis?|"
            r"autonomous (?:trucks?|buses|shuttles?)")
AV_TEXT = re.compile(rf"(?i)\b(?:{AV_TERMS})\b")

AI_TEXT = re.compile(
    r"(?i)\b(artificial intelligence|machine learning|algorithm(ic|s)?|automated decision|deep ?fakes?|"
    r"synthetic media|digital replicas?|chat ?bots?|large language models?|foundation models?|"
    r"frontier (ai|models?)|generative|data cent(er|re)s?|autonomous weapons?|facial recognition|"
    r"neural network|computer vision|" + AV_TERMS + r")\b|\bA\.?I\.?\b")


def looks_ai(*texts):
    return any(AI_TEXT.search(t or "") for t in texts)


# A bill's title that names AI settles whether the bill is about AI. The tagger is asked whether a
# text is "substantially about" AI, and given a title and no summary it said no to 180 bills titled
# with it: Utah's "Artificial Intelligence Amendments", Connecticut's "An Act Concerning Artificial
# Intelligence", Congress's "AI Whistleblower Protection Act". Most states publish no summary, so
# this is most of their record. Narrower than AI_TEXT: "AI" alone has to be the acronym, in
# capitals, standing for AI. Data centers have a rule of their own below.
AI_TITLE = re.compile(
    r"(?i)\b(artificial intelligence|machine learning|deep ?fakes?|synthetic media|digital replicas?|"
    r"chat ?bots?|large language models?|foundation models?|frontier (ai|models?)|generative (ai|artificial)|"
    r"automated decision|algorithm(ic|s)?|autonomous weapons?|facial recognition|neural network)\b"
    r"|(?-i:\bA\.?I\.?\b)")


def title_names_ai(title):
    t = re.sub(r"\bAI/AN\b", " ", title or "")  # American Indian and Alaska Native
    if not re.search(r"artificial intelligence\s*\(\s*A\.?I\.?\s*\)", t, re.I):
        t = re.sub(r"\(\s*A\.?I\.?\s*\)", " ", t)  # "Accelerating Innovation (AI) for Kids with Cancer"
    return bool(AI_TITLE.search(t))


# Data center bills are counted whether or not they say AI: the owner's decision of 21 September
# 2026, since the fight over data centers' power, water, land and tax breaks is the fight over the AI
# buildout. Before it the tagger was told "AI data centers" and split them at random, 155 counted
# and 191 just like them left out. A title that names data centers settles it, as a title naming AI
# does, except where "data center" names a data program or an office rather than a building: a
# census State Data Center, a longitudinal data system, an education-to-career data center.
DC_TITLE = re.compile(r"(?i)\b(data ?cent(er|re)s?|hyperscale)\b")
NOT_A_BUILDING = re.compile(r"(?i)\b(state data cent(er|re)|longitudinal|data systems?|education[- ]to[- ]career|"
                            r"career[- ]to[- ]education|water data cent(er|re)|census)\b")
# What sends a measure the tagger turned down back to it under the new rule: data centers named
# anywhere in its text, or the large electricity loads the utilities' own bills call them.
DC_TEXT = re.compile(r"(?i)\b(data ?cent(er|re)s?|hyperscale|large[- ]loads?|high[- ]performance computing)\b")


def title_names_data_centers(title):
    t = title or ""
    return bool(DC_TITLE.search(t)) and not NOT_A_BUILDING.search(t)


def title_names_self_driving(title):
    return bool(AV_TEXT.search(title or ""))


def title_settles(title):
    """A title that decides on its own that a bill is in the report: it names AI, data centers, or
    vehicles that drive themselves."""
    return title_names_ai(title) or title_names_data_centers(title) or title_names_self_driving(title)


# Bump to send the tagger again every measure it turned down that names data centers.
RETAG_DATA_CENTERS = 1


def retag_data_centers(db):
    """Queue once, under the rule that counts data centers, the measures it changes.

    Every measure the tagger turned down that names data centers: a bill whose title names them is
    then counted whatever the tagger says, and the rest are read again by a tagger told they count.
    And every data center measure already counted, because the tagger was asked for offices gaining
    power over AI and so named almost none: 16 offices across 196 data center bills, where a public
    utilities commission handed the approval of a data center's grid connection is gaining exactly
    the power this report counts. An office refused for that reason alone is asked about again.
    """
    key = f"retag:data-centers:{RETAG_DATA_CENTERS}"
    if kv_get(db, key):
        return 0
    marked = {r["target"] for r in db.execute(
        "SELECT target FROM tags WHERE (kind = 'control' AND value = 'data-center-limits') "
        "OR (kind = 'fear' AND value = 'power-bills')")}
    ids = [r["id"] for r in db.execute(
        "SELECT m.id, m.kind, m.jurisdiction, m.session, m.introduced_date, m.title, m.summary, t.ai_related "
        "FROM measures m JOIN tag_runs t ON t.target = m.id")
        if in_window(r) and (
            (r["ai_related"] == 0 and DC_TEXT.search(f"{r['title'] or ''}\n{r['summary'] or ''}"))
            or (r["ai_related"] == 1 and (title_names_data_centers(r["title"]) or r["id"] in marked)))]
    for mid in ids:
        db.execute("UPDATE tag_runs SET text_hash = NULL WHERE target = ?", (mid,))
    # An office refused on a data center measure is asked again under the question that counts
    # power over data centers. Its label is put back on the quote it rested on, so a measure the
    # tagger does not reach first still has it asked; one the tagger does reach starts afresh.
    offices = [r for r in db.execute(
        "SELECT c.target, c.value, c.evidence, m.title, m.summary FROM checks c JOIN measures m ON m.id = c.target "
        "WHERE c.kind = 'agency' AND c.verdict IN ('no', 'hold')")
        if DC_TEXT.search(f"{r['title'] or ''}\n{r['summary'] or ''}")]
    for r in offices:
        db.execute("INSERT OR IGNORE INTO tags(target, kind, value, evidence, model, tagged_at) VALUES(?,?,?,?,?,?)",
                   (r["target"], "agency", r["value"], r["evidence"], "recheck", iso()))
        db.execute("DELETE FROM checks WHERE target = ? AND kind = 'agency' AND value = ?", (r["target"], r["value"]))
    kv_set(db, key, True)
    db.commit()
    if ids or offices:
        log(f"[repair] {len(ids)} data center measures queued to be read again, "
            f"{len(offices)} offices refused on them put back to be asked again")
    return len(ids)


# Bump to send the tagger and the check again every measure that names vehicles that drive themselves.
RETAG_SELF_DRIVING = 1


def retag_self_driving(db):
    """Queue once, under the rule that counts self-driving vehicles, every measure on file naming them.

    Turned down by the tagger or taken out by the check's question about AI, they were read under a
    rule that did not count them; counted already, they were read for power over AI and not over the
    vehicles. Each is read again, and a verdict that took one out is asked again under the new wording.
    """
    key = f"retag:self-driving:{RETAG_SELF_DRIVING}"
    if kv_get(db, key):
        return 0
    ids = [r["id"] for r in db.execute(
        "SELECT m.id, m.kind, m.jurisdiction, m.session, m.introduced_date, m.title, m.summary "
        "FROM measures m JOIN tag_runs t ON t.target = m.id")
        if in_window(r) and AV_TEXT.search(f"{r['title'] or ''}\n{r['summary'] or ''}")]
    for mid in ids:
        db.execute("UPDATE tag_runs SET text_hash = NULL WHERE target = ?", (mid,))
        db.execute("DELETE FROM checks WHERE target = ? AND kind = 'about'", (mid,))
    kv_set(db, key, True)
    db.commit()
    if ids:
        log(f"[repair] {len(ids)} measures naming self-driving vehicles queued to be read again")
    return len(ids)


# Bump to read again the bills whose title names AI that the tagger judged not about AI.
RETAG_AI_BILLS = 1


def retag_ai_bills(db):
    """Queue those bills once, for a tagger that no longer lets a title naming AI be overruled."""
    key = f"retag:ai-bills:{RETAG_AI_BILLS}"
    if kv_get(db, key):
        return 0
    ids = [r["id"] for r in db.execute(
        "SELECT m.id, m.title FROM measures m JOIN tag_runs t ON t.target = m.id "
        "WHERE m.kind = 'bill' AND t.ai_related = 0") if title_names_ai(r["title"])]
    for mid in ids:
        db.execute("UPDATE tag_runs SET text_hash = NULL WHERE target = ?", (mid,))
    kv_set(db, key, True)
    db.commit()
    if ids:
        log(f"[repair] {len(ids)} bills titled with AI queued to be read again")
    return len(ids)


# Bump to ask again about the labels refused on data center measures under wording that counted
# only AI. 1: the agency powers control and the power bills fear were widened to data centers on 21
# September 2026, after the check refused 13 in one pass as "over data centers, not AI".
RECHECK_DATA_CENTER_LABELS = 1
WIDENED_FOR_DATA_CENTERS = (("control", "new-agency-powers"), ("fear", "power-bills"))


def recheck_data_center_labels(db):
    """Put back, once, those labels refused on data center measures, on the quote each rested on."""
    key = f"recheck:data-center-labels:{RECHECK_DATA_CENTER_LABELS}"
    if kv_get(db, key):
        return 0
    rows = [r for r in db.execute(
        "SELECT c.target, c.kind, c.value, c.evidence, m.title, m.summary FROM checks c "
        "JOIN measures m ON m.id = c.target WHERE c.verdict IN ('no', 'hold')")
        if (r["kind"], r["value"]) in WIDENED_FOR_DATA_CENTERS
        and DC_TEXT.search(f"{r['title'] or ''}\n{r['summary'] or ''}")]
    for r in rows:
        db.execute("INSERT OR IGNORE INTO tags(target, kind, value, evidence, model, tagged_at) VALUES(?,?,?,?,?,?)",
                   (r["target"], r["kind"], r["value"], r["evidence"], "recheck", iso()))
        db.execute("DELETE FROM checks WHERE target = ? AND kind = ? AND value = ?",
                   (r["target"], r["kind"], r["value"]))
    kv_set(db, key, True)
    db.commit()
    if rows:
        log(f"[repair] {len(rows)} labels refused on data center measures put back to be asked again")
    return len(rows)


# Bump when the reading of a last action changes, to read every stored one again.
# 3: a one-chamber resolution that chamber adopted is adopted. The Senate agreed to S.Res. 896 and
#    the site called it not passed, along with state resolutions "Read and Adopted".
STATUS_RULES = 3


def restatus(db):
    """Read every stored last action again under the current rules, once per version of them.

    A Federal Register document's status comes from its type, not from words, so it is left alone.
    A Congress.gov bill that the API lists as a law stays passed whatever its last action says.
    """
    key = f"restatus:{STATUS_RULES}"
    if kv_get(db, key):
        return 0
    moved = collections.Counter()
    for r in db.execute("SELECT id, source, status, latest_action, identifier, kind FROM measures "
                        "WHERE source != 'Federal Register'").fetchall():
        kind = r["kind"]
        if r["source"] == "Congress.gov" and CONGRESS_RESOLUTION.match(r["identifier"] or "") and kind != "resolution":
            kind = "resolution"
            db.execute("UPDATE measures SET kind = 'resolution' WHERE id = ?", (r["id"],))
        new = status_from_action(r["latest_action"], r["identifier"], kind)
        if new == r["status"] or (r["source"] == "Congress.gov" and r["status"] == "passed"):
            continue
        db.execute("UPDATE measures SET status = ? WHERE id = ?", (new, r["id"]))
        moved[(r["status"], new)] += 1
    kv_set(db, key, dict((f"{a}>{b}", n) for (a, b), n in moved.items()))
    db.commit()
    if moved:
        log("[repair] status read again: " + ", ".join(f"{n} {a} to {b}" for (a, b), n in moved.items()))
    return sum(moved.values())


# How each legislature writes a bill's last step. The first version of this knew the handful of
# phrasings it was written against, "Signed by Governor" and "Chaptered", and read every other
# state's signing as a bill still waiting: Colorado's "Governor Signed", Illinois's "Public Act",
# New York's "SIGNED CHAP." and "APPROVAL MEMO", Arkansas's "is now Act 927", Montana's "Chapter
# Number Assigned". New York's frontier AI law and Utah's 2025 AI laws were on the site as
# pending. Every phrasing below is one found on file; tests.test_offline holds them.
ENACTED = re.compile(
    r"signed by (the )?gov(ernor|\.)|governor signed|signed by (the )?(governor|gov)\b"
    r"|(approved|signed) by (the )?governor|governor approved|letter of approval from the governor"
    r"|becomes? (public )?law|became (public )?law|without (the )?governor'?s signature"
    r"|chaptered|chapter (no\.?|number)|assigned chapter number|signed chap\b|\bchap\.?\s*\d"
    r"|acts? of assembly chapter|\bacts?,? ch(apter|\.)\s*\d|secretary of state chapter|session law chapter"
    r"|^chapter\s+\d|chapter \d+,? (acts|statutes|laws|\(\d{4} laws\))|chapter \d+ of the acts"
    r"|public act\b|public law|\bp\.\s?l\.\s?\d|approved p\.l\.|now act\b|^act (no\.?\s*)?\d|\bact no\.\s*\d"
    r"|approval memo|signed into law|\benacted\b|^effective\b|effective (date|on|immediately)"
    r"|(filed with|delivered to|presented to|sent to) (the )?secretary of state"
    r"|passed notwithstanding|veto overrid|overrode the veto"
    r"|^signed\.?$|final rule|interim final rule|^executive order", re.I)
# A bill that was stopped. "Amendment failed" is not the bill failing, and a veto that was
# overridden is a law, so those are looked for first.
STOPPED = re.compile(
    r"veto|\bfailed\b|\bdied\b|\bdead\b|withdrawn|postponed? indefinitely|indefinitely postponed"
    r"|passed by indefinitely|inexpedient to legislate|enacting clause stricken|tabled"
    r"|in committee upon adjournment|sine die adjournment|placed in legislative files", re.I)
NOT_THE_BILL = re.compile(r"amendments?\s*(\(s\)\s*)?(no\.?\s*\d+\s*)?failed|motion[^.;]*failed", re.I)
# The last action hands the bill to the one who signs it: the legislature has passed it and nobody
# has acted on it since. "Not passed" is true of it but reads as if the legislature had not, and
# fifteen California bills sat on the governor's desk under that label after the session closed.
AT_SIGNER = re.compile(r"\b(?:presented|sent|delivered|transmitted|forwarded)\s+to\s+(?:the\s+)?"
                       r"(governor|president|mayor)\b", re.I)


def status_label(status, action="", kind=""):
    """What the site and the post may say about where a measure stands, and no more.

    "Pending" and "still live" say a bill can still pass, which the record cannot show: most states
    record nothing when a session ends, so a bill that died in May reads the same as one filed last
    week. "Not passed" is true of both.
    """
    if kind == "rule":
        return "Final" if status == "passed" else "Proposed"
    if kind == "order":
        return "Signed"
    if status == "passed":
        return "Adopted" if kind == "resolution" else "Passed"
    if status == "failed":
        a = (action or "").lower()
        if "veto" in a or "notwithstanding the objections" in a:
            return "Vetoed"
        return "Withdrawn" if "withdrawn" in a else "Did not pass"
    desk = AT_SIGNER.search(action or "")
    if desk and kind != "resolution":
        return f"Sent to the {desk.group(1).lower()}"
    return "Not passed"


def closed_sessions():
    """Sessions whose last day to pass a bill has gone by, each confirmed from a published source."""
    return {(s["jurisdiction"], s["session"]): s["last_day"] for s in config("sessions_closed")["sessions"]}


def can_still_pass(m, today, closed=None):
    """False for a bill left pending in a session known to be over and not with the governor."""
    closed = closed_sessions() if closed is None else closed
    last = closed.get((m["jurisdiction"], m["session"]))
    return not (last and today > last and m["status"] == "pending"
                and not AT_SIGNER.search(m["latest_action"] or ""))


# A resolution of one chamber is done when that chamber adopts it. A concurrent or joint resolution
# still needs the other chamber, and a joint one the signer, so only the one-chamber kind is read
# this way: Congress's H.Res. and S.Res., and a state's HR, SR or R that Open States calls a
# resolution. An amendment, a motion or a report adopted is not the resolution adopted.
CONGRESS_RESOLUTION = re.compile(r"^[HS]\.\s?(?:Con\.\s?)?Res\.\s*\d", re.I)
ONE_CHAMBER = re.compile(r"^(?:[HS]\.\s?Res\.|[HSA]?\s?R(?:es)?\.?)\s*\d", re.I)
ADOPTED = re.compile(r"\b(?:adopted|agreed to)\b", re.I)
NOT_ADOPTION = re.compile(r"\b(?:amendments?|motion|report|committee)\b[^.;]*\b(?:adopted|agreed to)\b"
                          r"|\b(?:adopted|agreed to)\b[^.;]*\bby (?:the )?committee\b"
                          r"|\b(?:died|dead|failed|withdrawn|companion)\b", re.I)


def one_chamber_resolution(identifier, kind):
    ident = (identifier or "").strip()
    if CONGRESS_RESOLUTION.match(ident):
        return "Con." not in ident
    return kind == "resolution" and bool(ONE_CHAMBER.match(ident))


def status_from_action(text, identifier="", kind=""):
    """passed, failed or pending, from the words of the last action on a measure.

    pending means only that no final action is on record. A bill whose session ended without one
    is not pending in any sense a reader would use the word, which is why the site says "not
    passed" rather than "pending" or "still live".
    """
    t = re.sub(r"\s+", " ", (text or "").strip())
    if re.search(r"withdrawn because approved|failed to pass notwithstanding", t, re.I):
        return "failed"  # a companion carried it into law, or the veto held
    if ENACTED.search(t):
        return "passed"
    if one_chamber_resolution(identifier, kind) and ADOPTED.search(t) and not NOT_ADOPTION.search(t):
        return "passed"  # adopted
    if STOPPED.search(NOT_THE_BILL.sub(" ", t)):
        return "failed"
    return "pending"
