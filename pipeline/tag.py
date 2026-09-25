"""Tag bills, rules, and posts with fears, controls, and agencies.

Pass one proposes labels. Pass two must back each label with a quote copied from the
text, and the quote is checked against the text in code. Only labels that survive both
count anywhere on the site.
"""
import concurrent.futures as cf
import json
import re
import threading
import time

import requests

from .common import (AI_TEXT, SUMMARY_MAX, config, env, in_window, iso, kv_get, kv_set, log, sha,
                     states_a_position, title_settles)
from . import known
from .texts import TEXT_MAX, law_texts, reading

API = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"
# A law read from its own text is read by the stronger model the check uses: fifteen thousand
# characters of statute beside seventeen definitions. The smaller model found the RAISE Act's duties
# and its Attorney General's power and, even when told that the law's "critical harm" is the
# catastrophic risk of the loss of control fear, gave it no fear. Should a key not reach this model,
# the reading falls back to the smaller one.
TEXT_MODEL = "claude-sonnet-5"
LIMITS = {"hourly": 400, "daily": 1200, "backfill": 3500, "auto": 400}
DAY_CAP = 4000  # items per day: a first backfill clears in a day, then steady state is a trickle
# Whatever the item limit allows, the run gets a quarter of an hour. The queue is picked up again
# on the next pass, and a job that runs out of time saves nothing at all.
SECONDS = 900


class TimedOut(Exception):
    """Not an error to count: the queue simply outlasted the time this run had."""
_lock = threading.Lock()


def call(key, system, user, max_tokens=700, model=None):
    last = ""
    for attempt in range(6):
        try:
            r = requests.post(API, timeout=120, headers={
                "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": model or MODEL, "max_tokens": max_tokens, "system": system,
                      "messages": [{"role": "user", "content": user}]})
        except requests.RequestException as exc:
            last = str(exc)
            time.sleep(5 * 2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504, 529):
            last = f"{r.status_code} {r.text[:200]}"
            time.sleep(min(90, 5 * 2 ** attempt))
            continue
        if r.status_code in (403, 404) or (r.status_code == 400 and "model" in r.text.lower()):
            if model and model != MODEL:
                log(f"[tag] {model} is not available to this key; reading with {MODEL}")
                model = MODEL
                continue
        if r.status_code != 200:
            raise RuntimeError(f"Claude API {r.status_code}: {r.text[:300]}")
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        return parse_json(text)
    raise RuntimeError(f"Claude API kept failing: {last}")


def parse_json(text):
    """The JSON object a reply opens with. An object followed by a note or a second object is read for
    its first: taken from the first brace to the last, it would not parse, and New York's RAISE Act
    went unread on 25 September for that ("Extra data: line 11 column 4")."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON in reply")
    try:
        found, _ = json.JSONDecoder().raw_decode(text, start)
        if isinstance(found, dict):
            return found
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0))


def norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def definitions(items):
    return "\n".join(f"- {i['slug']}: {i['definition']}" for i in items)


def fear_definition(fear):
    """A fear's definition, with what the third reading has been told it includes (pipeline.check
    FEAR_NOTES), so the first reading and the third read a fear alike. New York's RAISE Act, read
    from its text, was confirmed on its reporting duty and its Attorney General's power and given no
    fear: the law guards against "critical harm", mass casualties or a billion dollars of damage, and
    the tagger was never told that is the catastrophic risk the loss of control fear names."""
    from .check import FEAR_NOTES
    note = FEAR_NOTES.get(fear["slug"])
    return f"{fear['definition']} {note}" if note else fear["definition"]


def propose(key, fears, controls, doc, is_measure, model=None):
    what = "a U.S. bill, resolution, or federal rule" if is_measure else "a statement or article published by an organization"
    system = (f"You classify {what} about artificial intelligence for a public database. Be literal and use only "
              "what the text says. Reply with a single JSON object and nothing else.")
    control_block = f"\nCONTROLS (what the measure itself would impose):\n{definitions(controls)}\n" if is_measure else ""
    schema = ('{"ai_related": true or false, "fears": [fear slugs], "controls": [control slugs], '
              '"agencies": ["government bodies the measure would give new authority, duties, or enforcement power over AI, '
              'data centers, self-driving vehicles, or the people and companies who build or use AI, '
              'named with their jurisdiction, for example \\"Colorado Attorney General\\""]}') if is_measure else \
        '{"ai_related": true or false, "fears": [fear slugs]}'
    fear_block = "\n".join(f"- {f['slug']}: {fear_definition(f)}" for f in fears)
    user = (f"FEARS (harms the text may cite):\n{fear_block}\n{control_block}\nTEXT\n{doc}\n\n"
            f"Return JSON: {schema}\nRules: ai_related is true only if the text is substantially about artificial "
            "intelligence, algorithms, automated decisions, synthetic media, data centers, or vehicles that drive "
            "themselves. Data centers count with their building, power, water, land, siting, rates or taxes, and the "
            "large electricity loads they bring, whether or not the text says AI (not a data program or office that "
            "happens to be called a data center). Vehicles that drive themselves count whether or not the text says "
            "AI: autonomous vehicles, automated driving systems, robotaxis and driverless trucks, and their testing, "
            "permits, operation, insurance and liability. A fear counts only if "
            "the text states or clearly invokes that harm. Rules a measure sets for government bodies' own use of "
            "AI, such as inventories, assessments, purchasing rules or policies for the systems agencies, schools or "
            "police use, bind the government itself, not the people who build, sell or use AI: they are not controls, "
            "and an office that only oversees them is not an agency gaining power. "
            "Summaries often recite law already in force before saying what the measure does. Ignore every "
            "sentence that describes existing law, including ones that open with \"Existing law\" or name an "
            "act that already requires something. Only what this measure would newly impose or newly hand to a "
            "government body counts. When the text of the law is given, read it for what the law does and the harms "
            "it is written against, which its definitions and purpose often name; a section it only reprints from law "
            "already in force is not what it imposes. Use empty lists when nothing applies.")
    return call(key, system, user, max_tokens=500, model=model)


def verify(key, fears, controls, doc, proposal, model=None):
    fdefs = {f["slug"]: fear_definition(f) for f in fears}
    cdefs = {c["slug"]: c["definition"] for c in controls}
    labels = {
        "fears": {s: fdefs[s] for s in proposal.get("fears", []) if s in fdefs},
        "controls": {s: cdefs[s] for s in proposal.get("controls", []) if s in cdefs},
        "agencies": [a for a in proposal.get("agencies", []) if isinstance(a, str) and a.strip()][:8],
    }
    if not any(labels.values()):
        return {}
    system = ("You check another reviewer's labels against a text. For each label, copy an exact quote from the "
              "text that supports it, or null if the text does not support it. Reply with one JSON object only.")
    user = (f"TEXT\n{doc}\n\nLABELS TO CHECK\n{json.dumps(labels, indent=1)}\n\n"
            'Return JSON: {"fears": {"slug": "quote or null"}, "controls": {"slug": "quote or null"}, '
            '"agencies": {"name": "quote or null"}}\nEach value must be a quote copied word for word from the Title, '
            'the Summary or the Text of the law, between 4 and 30 words, never from the Jurisdiction or Identifier '
            'line. Always return the '
            'three keys as objects, never as lists. A sentence describing law already in force does not support a label, so return null when that is the only support.')
    return call(key, system, user, max_tokens=900, model=model)


MIN_WORDS = {"fears": 4, "controls": 4, "agencies": 2}
NOT_AGENCIES = {"congress", "us congress", "u s congress", "united states congress", "senate", "house",
                "house of representatives", "state legislature", "legislature", "general assembly",
                "state government", "federal government", "state", "government"}
# A legislature is not an agency gaining power, whatever it is called. Matching whole names let
# through "Kansas Legislature (via task force)", a legislative reference bureau, a House committee
# and a joint commission of the Virginia General Assembly.
LEGISLATIVE = re.compile(r"\b(legislature|legislative|general assembly|senate|house of representatives|"
                         r"house of delegates|state assembly|assembly committee|house committee|city council|"
                         r"joint commission on technology and science)\b", re.I)
# A summary often recites the law already in force before it says what the measure does. A
# sentence that opens "Existing law" starts the recital, and it runs until one opens "This bill".
RECITAL_START = re.compile(r"^\W*(?:under\s+)?(?:existing|current)\s+(?:federal\s+|state\s+)?law\b", re.I)
BILL_START = re.compile(r"^\W*(?:this|the)\s+(?:bill|act|measure|resolution|joint resolution)\b", re.I)
# Mandatory reporting is a developer or deployer reporting to government. An agency sending its
# own study to the legislature is government reporting to itself.
GOV_REPORT = re.compile(r"\breport\w*\s+(?:\w+\s+){0,6}?to\s+(?:the\s+)?(?:\w+\s+)?"
                        r"(?:legislature|general assembly|congress|governor)\b", re.I)
REPORTER = re.compile(r"\b(developer|deployer|compan|operator|provider|business|manufacturer|platform|"
                      r"employer|insurer|vendor|licensee)", re.I)


def words(quote):
    """The words a reader would count. An enumerator like (a)(1)(A) is not four of them."""
    return len(re.findall(r"[A-Za-z]+", re.sub(r"\([^)]{0,4}\)", " ", quote or "")))


def recital_only(quote_norm, raw):
    """True when every sentence the quote sits in describes law already in force."""
    hits, reciting = [], False
    for sentence in re.split(r"(?<=[.;:])\s+(?=[A-Z(])", raw or ""):
        if RECITAL_START.search(sentence):
            reciting = True
        elif BILL_START.search(sentence):
            reciting = False
        if f" {quote_norm} " in f" {norm(sentence)} ":
            hits.append(reciting)
    return bool(hits) and all(hits)


def agreed(verdict, body_norm, kind, jurisdiction="", raw="", code=""):
    """Keep only labels the measure's own words back up, quoted and checked here.

    raw is the title and summary as written, for telling a recital of existing law from what the
    measure does; code is the jurisdiction code, for the one control that only a federal measure
    can carry. Both are optional so a caller without them still gets every other check.
    """
    found = verdict.get(kind)
    if not isinstance(found, dict):
        return {}  # the model answered with a list or a string; nothing is verified, so nothing counts
    out = {}
    for label, quote in found.items():
        if not isinstance(label, str) or not isinstance(quote, str):
            continue
        q = norm(quote)
        # Whole words. As a run of characters, "drones and ai" was found in "drones and aircraft".
        if not q or words(quote) < MIN_WORDS.get(kind, 4) or f" {q} " not in f" {body_norm} ":
            continue
        if raw and recital_only(q, raw):
            continue  # the law already in force, not what this measure would do
        if kind == "agencies":
            # a legislature is not an agency gaining power, and neither is the jurisdiction itself
            name = norm(label)
            if name in NOT_AGENCIES or name == norm(jurisdiction) or len(name.split()) < 2 \
                    or LEGISLATIVE.search(label):
                continue
        if kind == "controls":
            if label == "preemption" and code and code not in ("us", "us-exec"):
                continue  # state laws overridden by Washington: a state's own measure cannot be that
            if label == "mandatory-reporting" and GOV_REPORT.search(quote) and not REPORTER.search(quote):
                continue
        out[label.strip()] = quote.strip()
    return out


def pulled_labels():
    """Labels taken off by hand, as (url, kind, value): read, and found to say the opposite of
    what the measure does. Each carries its reason in config/suppress.json."""
    return {(x["url"], x["kind"], x["value"]) for x in config("suppress").get("labels", [])}


KINDS = {"fear": "fears", "control": "controls", "agency": "agencies"}


def prune_tags(db):
    """Re-apply the current evidence rules to labels already stored: take off the ones that fail,
    and put back the confirmed ones that pass. Returns (taken off, put back).

    Measures are only re-read when their text changes, so without this a label that a
    later rule would reject keeps appearing on the site forever.

    The other way round: a label the third reading confirmed (pipeline.check) stays while its quote
    still stands in the measure and passes these rules. A measure is read afresh whenever its text
    changes, and a fresh reading does not always propose every label the last one found. On 25
    September, 179 labels all three readings had agreed on were off the site for that reason alone,
    among them the criminal offenses Texas created for AI-made sexual images of children.
    """
    bodies, posts, gone = {}, {}, []
    texts = law_texts(db)
    for r in db.execute("SELECT id, jurisdiction, jurisdiction_name, title, summary, status, url FROM measures"):
        law = reading(dict(r), texts)
        raw = f"{r['title'] or ''}\n{r['summary'] or ''}" + (f"\n{law}" if law else "")
        bodies[r["id"]] = (norm(raw), r["jurisdiction_name"] or "", raw, r["jurisdiction"] or "",
                           states_a_position(r["title"]), r["url"])
    for r in db.execute("SELECT id, title, summary FROM posts"):
        raw = f"{r['title'] or ''}\n{r['summary'] or ''}"
        posts["post:" + r["id"]] = (norm(raw), raw)
    pulled = pulled_labels()

    def holds(target, kind, value, evidence):
        label = {KINDS.get(kind, kind): {value: evidence}}
        if target.startswith("post:"):
            # A statement names fears and nothing else, and it has to still exist. Labels on posts
            # were never checked here, so ones on deleted posts stayed in the counts.
            p = posts.get(target)
            return p is not None and kind == "fear" and bool(agreed(label, p[0], "fears", raw=p[1]))
        b = bodies.get(target)
        if b is None:
            return False  # the measure it was on is gone
        body, jur, raw, code, position, url = b
        return not ((position and kind in ("control", "agency")) or (url, kind, value) in pulled) \
            and bool(agreed(label, body, KINDS.get(kind, kind), jur, raw=raw, code=code))
    for r in db.execute("SELECT rowid, target, kind, value, evidence FROM tags"):
        if not holds(r["target"], r["kind"], r["value"], r["evidence"]):
            gone.append(r["rowid"])
    # A label the third reading refused on this very quote stays off, even on a run where the
    # reading itself cannot happen (no key, the day's cap reached, the model unreachable).
    gone += [r["rowid"] for r in db.execute(
        "SELECT t.rowid FROM tags t JOIN checks c ON c.target = t.target AND c.kind = t.kind "
        "AND c.value = t.value AND c.evidence = t.evidence AND c.verdict = 'no'")]
    gone = sorted(set(gone))
    for chunk in (gone[i:i + 400] for i in range(0, len(gone), 400)):
        db.execute(f"DELETE FROM tags WHERE rowid IN ({','.join('?' for _ in chunk)})", chunk)
    # Confirmed, still quoted, and missing: put back on the quote the check confirmed, dated when it
    # did. Only for a measure still read as about AI, and only under a fear or control still defined.
    known_slugs = {"fear": {f["slug"] for f in config("fears")}, "control": {c["slug"] for c in config("controls")}}
    back = 0
    for r in db.execute(
            "SELECT c.target, c.kind, c.value, c.evidence, c.checked_at FROM checks c "
            "JOIN tag_runs tr ON tr.target = c.target AND tr.ai_related = 1 "
            "LEFT JOIN tags t ON t.target = c.target AND t.kind = c.kind AND t.value = c.value "
            "WHERE c.verdict = 'yes' AND c.kind IN ('fear', 'control', 'agency') AND t.target IS NULL").fetchall():
        if r["kind"] in known_slugs and r["value"] not in known_slugs[r["kind"]]:
            continue
        if holds(r["target"], r["kind"], r["value"], r["evidence"]):
            db.execute("INSERT OR IGNORE INTO tags(target, kind, value, evidence, model, tagged_at) VALUES(?,?,?,?,?,?)",
                       (r["target"], r["kind"], r["value"], r["evidence"], "confirmed", r["checked_at"]))
            back += 1
    db.commit()
    return len(gone), back


def check_labels(db, key):
    """The third reading (pipeline.check), which never takes the tagging run down with it."""
    from . import check
    try:
        return check.run(db, key)
    except Exception as exc:  # the labels stay as they are and are checked on the next run
        log(f"[check] {exc}")
        return f"label check failed: {str(exc)[:160]}"


def targets(db, limit):
    # Every measure inside the report's period, which includes a bill filed before January for a
    # session that began then. Sixty-three of Montana's 2025 bills were never read at all.
    rows = [r for r in db.execute(
        "SELECT m.id, m.kind, m.jurisdiction, m.session, m.introduced_date, m.jurisdiction_name, m.identifier, "
        "m.title, m.summary, m.status, t.text_hash FROM measures m LEFT JOIN tag_runs t ON t.target = m.id "
        "ORDER BY m.introduced_date DESC").fetchall() if in_window(r)]
    texts = law_texts(db)
    out = []
    for r in rows:
        # A law whose summary is missing or a line is read with its enacted text (pipeline.texts).
        law = reading(dict(r), texts)
        body = f"{r['title'] or ''}\n{r['summary'] or ''}" + (f"\n{law}" if law else "")
        doc = f"Jurisdiction: {r['jurisdiction_name']}\nIdentifier: {r['identifier']}\nTitle: {r['title']}\n" \
              f"Summary: {r['summary'] or '(none)'}" + (f"\nText of the law: {law}" if law else "")
        h = sha(doc)
        if r["text_hash"] != h:
            # the whole of what is kept: the title, the summary up to SUMMARY_MAX, the law's text
            out.append(("measure", r["id"], doc[:SUMMARY_MAX + 2000 + TEXT_MAX], h,
                        body[:SUMMARY_MAX + 2000 + TEXT_MAX]))
    posts = db.execute(
        "SELECT p.id, p.title, p.summary, t.text_hash FROM posts p LEFT JOIN tag_runs t ON t.target = 'post:' || p.id "
        "ORDER BY p.published DESC").fetchall()
    for r in posts:
        doc = f"Title: {r['title']}\nText: {r['summary'] or '(none)'}"
        h = sha(doc)
        if r["text_hash"] != h:
            out.append(("post", "post:" + r["id"], doc[:5000], h, doc[:5000]))
    measures = [t for t in out if t[0] == "measure"]
    posts_ = [t for t in out if t[0] == "post"]
    return (measures[: int(limit * 0.85)] + posts_)[:limit], len(out)


def run(db, state, mode):
    # The re-check first. It needs no model, so neither a missing key nor the daily cap should
    # leave a label the current rules reject on the site until tomorrow.
    dropped, restored = prune_tags(db)
    key = env("ANTHROPIC_API_KEY")
    fears, controls = config("fears"), config("controls")
    day = iso()[:10]
    spent = kv_get(db, "tag_spend", {})
    used_today = spent.get("n", 0) if spent.get("date") == day else 0
    limit = max(0, min(LIMITS.get(mode, 150), DAY_CAP - used_today))
    if not limit:
        state["message"] = (f"daily cap of {DAY_CAP} items reached; resumes tomorrow"
                            + (f"; dropped {dropped} unsupported labels" if dropped else "")
                            + (f"; put back {restored} confirmed labels" if restored else "")
                            + f"; {check_labels(db, key)}")
        return
    # The laws a person has confirmed are about AI are read as about AI whatever their text shows.
    # Illinois's ban on AI therapy is titled "Therapy Resources Oversight" and has no summary on
    # file, so nothing else here could have known.
    known_ai = known.ids(db)
    if known_ai:
        marks = ",".join("?" * len(known_ai))
        db.execute(f"UPDATE tag_runs SET text_hash = NULL WHERE ai_related = 0 AND target IN ({marks})", list(known_ai))
        db.commit()
    work, backlog = targets(db, limit)
    resolutions = {r["id"] for r in db.execute("SELECT id FROM measures WHERE kind = 'resolution'")}
    done, errors = 0, 0
    started = time.time()

    def one(item):
        if time.time() - started > SECONDS:
            raise TimedOut()
        kind, target, doc, h, body = item
        is_measure = kind == "measure"
        model = TEXT_MODEL if "\nText of the law: " in doc else None
        proposal = propose(key, fears, controls, doc, is_measure, model=model)
        ai = bool(proposal.get("ai_related"))
        # A Federal Register document is only stored when its title or abstract names AI, and a
        # presidential document has no abstract, so the model judges an executive order on its title
        # alone. It judged two orders titled with artificial intelligence, 14179 and 14319, not about
        # AI, and they were missing from the record. A title that names AI settles it.
        title = next((l[7:] for l in doc.split("\n") if l.startswith("Title: ")), "")
        if not ai and target.startswith("fr-") and AI_TEXT.search(title):
            ai = True
        # The same for a bill, and for the same reason: most states publish no summary, and the
        # model given a bare title said "Artificial Intelligence Amendments" was not about AI. A
        # title naming data centers or self-driving vehicles settles it the same way, as the report
        # counts them with AI.
        # Not a resolution, which can name AI in honouring a school team.
        if not ai and target not in resolutions and not target.startswith("fr-") and title_settles(title):
            ai = True
        if not ai and target in known_ai:
            ai = True
        verdict = verify(key, fears, controls, doc, proposal, model=model) if ai else {}
        return target, h, ai, verdict, norm(body), body, proposal, model or MODEL

    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(one, w): w for w in work}
        jurisdictions = {w[1]: w[2].split("\n", 1)[0].removeprefix("Jurisdiction: ") for w in work}
        about = {r["id"]: (r["jurisdiction"] or "", r["url"]) for r in db.execute("SELECT id, jurisdiction, url FROM measures")}
        pulled = pulled_labels()
        # A measure that asks, urges or objects imposes nothing, so it carries no control. It can
        # still name a fear: what it is worried about is the point of saying it at all.
        positions = {w[1] for w in work
                     if states_a_position(next((l[7:] for l in w[2].split("\n") if l.startswith("Title: ")), ""))}
        for fut in cf.as_completed(futures):
            _, target, _, h, _ = futures[fut]
            try:
                target, h, ai, verdict, doc_norm, raw, proposal, used = fut.result()
                err = None
            except TimedOut:
                continue
            except Exception as exc:
                ai, verdict, doc_norm, raw, proposal, used, err = None, {}, "", "", {}, MODEL, str(exc)[:300]
                errors += 1
                if errors <= 3:
                    log(f"[tag] {target}: {err}")
            with _lock:
                if err is None:
                    try:
                        before = db.execute("SELECT kind, value, evidence, model, tagged_at FROM tags WHERE target=?",
                                            (target,)).fetchall()
                        refused = {(r["kind"], r["value"], r["evidence"]) for r in db.execute(
                            "SELECT kind, value, evidence FROM checks WHERE target=? AND verdict='no'", (target,))}
                        db.execute("DELETE FROM tags WHERE target=?", (target,))
                        code, url = about.get(target, ("", ""))
                        for kind in ("fears", "controls", "agencies"):
                            # A measure that only takes a position imposes nothing and hands no
                            # office anything, so it carries neither a control nor an office.
                            if kind in ("controls", "agencies") and target in positions:
                                continue
                            # Two passes that agree: the second can only confirm what the first
                            # proposed. It used to be able to add a label nobody had proposed.
                            asked = {norm(x) for x in (proposal.get(kind) or []) if isinstance(x, str)}
                            for label, quote in agreed(verdict, doc_norm, kind, jurisdictions.get(target, ""),
                                                       raw=raw, code=code).items():
                                if norm(label) not in asked:
                                    continue
                                if (url, kind[:-1] if kind != "agencies" else "agency", label.strip()) in pulled:
                                    continue
                                db.execute("INSERT OR REPLACE INTO tags(target,kind,value,evidence,model,tagged_at) "
                                           "VALUES(?,?,?,?,?,?)",
                                           (target, kind[:-1] if kind != "agencies" else "agency",
                                            label.strip(), quote, used, iso()))
                        # What the last reading found stays while its quote still stands in the text
                        # and passes the same rules; this reading adds to it (prune_tags says why).
                        for o in before if ai else []:
                            kind = KINDS.get(o["kind"])
                            if not kind or (kind in ("controls", "agencies") and target in positions) \
                                    or (url, o["kind"], o["value"]) in pulled \
                                    or (o["kind"], o["value"], o["evidence"]) in refused:
                                continue
                            if agreed({kind: {o["value"]: o["evidence"]}}, doc_norm, kind,
                                      jurisdictions.get(target, ""), raw=raw, code=code):
                                db.execute("INSERT OR IGNORE INTO tags(target,kind,value,evidence,model,tagged_at) "
                                           "VALUES(?,?,?,?,?,?)", (target, o["kind"], o["value"], o["evidence"],
                                                                   o["model"], o["tagged_at"]))
                        db.execute("INSERT OR REPLACE INTO tag_runs(target,text_hash,ai_related,tagged_at,error) "
                                   "VALUES(?,?,?,?,NULL)", (target, h, 1 if ai else 0, iso()))
                        done += 1
                    except Exception as exc:  # a single odd answer must not end the run
                        errors += 1
                        if errors <= 3:
                            log(f"[tag] storing {target}: {exc}")
                    if done % 25 == 0:
                        db.commit()
                        log(f"[tag] {done}/{len(work)} tagged")
    kv_set(db, "tag_spend", {"date": day, "n": used_today + done})
    db.commit()
    state["added"] = done
    ran_out = time.time() - started > SECONDS
    checked = check_labels(db, key)
    state["message"] = (f"{done} tagged, {errors} errors, {max(0, backlog - done)} still queued, "
                        f"{used_today + done} of {DAY_CAP} today"
                        + (f"; dropped {dropped} unsupported labels" if dropped else "")
                        + (f"; put back {restored} confirmed labels" if restored else "")
                        + (f"; stopped at {SECONDS}s" if ran_out else "")
                        + f"; {checked}")
    if work and errors == len(work):
        raise RuntimeError(f"every tagging call failed; last error shown in logs")
