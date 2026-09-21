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

from .common import AI_TEXT, SINCE, config, env, iso, kv_get, kv_set, log, sha, states_a_position

API = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"
LIMITS = {"hourly": 400, "daily": 1200, "backfill": 3500, "auto": 400}
DAY_CAP = 4000  # items per day: a first backfill clears in a day, then steady state is a trickle
# Whatever the item limit allows, the run gets a quarter of an hour. The queue is picked up again
# on the next pass, and a job that runs out of time saves nothing at all.
SECONDS = 900


class TimedOut(Exception):
    """Not an error to count: the queue simply outlasted the time this run had."""
_lock = threading.Lock()


def call(key, system, user, max_tokens=700):
    last = ""
    for attempt in range(6):
        try:
            r = requests.post(API, timeout=120, headers={
                "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": MODEL, "max_tokens": max_tokens, "system": system,
                      "messages": [{"role": "user", "content": user}]})
        except requests.RequestException as exc:
            last = str(exc)
            time.sleep(5 * 2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504, 529):
            last = f"{r.status_code} {r.text[:200]}"
            time.sleep(min(90, 5 * 2 ** attempt))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Claude API {r.status_code}: {r.text[:300]}")
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        return parse_json(text)
    raise RuntimeError(f"Claude API kept failing: {last}")


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in reply")
    return json.loads(m.group(0))


def norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def definitions(items):
    return "\n".join(f"- {i['slug']}: {i['definition']}" for i in items)


def propose(key, fears, controls, doc, is_measure):
    what = "a U.S. bill, resolution, or federal rule" if is_measure else "a statement or article published by an organization"
    system = (f"You classify {what} about artificial intelligence for a public database. Be literal and use only "
              "what the text says. Reply with a single JSON object and nothing else.")
    control_block = f"\nCONTROLS (what the measure itself would impose):\n{definitions(controls)}\n" if is_measure else ""
    schema = ('{"ai_related": true or false, "fears": [fear slugs], "controls": [control slugs], '
              '"agencies": ["government bodies the measure would give new authority, duties, or enforcement power over AI, '
              'named with their jurisdiction, for example \\"Colorado Attorney General\\""]}') if is_measure else \
        '{"ai_related": true or false, "fears": [fear slugs]}'
    user = (f"FEARS (harms the text may cite):\n{definitions(fears)}\n{control_block}\nTEXT\n{doc}\n\n"
            f"Return JSON: {schema}\nRules: ai_related is true only if the text is substantially about artificial "
            "intelligence, algorithms, automated decisions, synthetic media, or AI data centers. A fear counts only if "
            "the text states or clearly invokes that harm. A control counts only if the measure itself would impose it. "
            "Summaries often recite law already in force before saying what the measure does. Ignore every "
            "sentence that describes existing law, including ones that open with \"Existing law\" or name an "
            "act that already requires something. Only what this measure would newly impose or newly hand to a "
            "government body counts. Use empty lists when nothing applies.")
    return call(key, system, user, max_tokens=500)


def verify(key, fears, controls, doc, proposal):
    fdefs = {f["slug"]: f["definition"] for f in fears}
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
            '"agencies": {"name": "quote or null"}}\nEach value must be a quote copied word for word from the Title '
            'or Summary, between 4 and 30 words, never from the Jurisdiction or Identifier line. Always return the '
            'three keys as objects, never as lists. A sentence describing law already in force does not support a label, so return null when that is the only support.')
    return call(key, system, user, max_tokens=900)


MIN_WORDS = {"fears": 4, "controls": 4, "agencies": 2}
NOT_AGENCIES = {"congress", "us congress", "u s congress", "united states congress", "senate", "house",
                "house of representatives", "state legislature", "legislature", "general assembly",
                "state government", "federal government", "state", "government"}


def agreed(verdict, body_norm, kind, jurisdiction=""):
    """Keep only labels the measure's own words back up, quoted and checked here."""
    found = verdict.get(kind)
    if not isinstance(found, dict):
        return {}  # the model answered with a list or a string; nothing is verified, so nothing counts
    out = {}
    for label, quote in found.items():
        if not isinstance(label, str) or not isinstance(quote, str):
            continue
        q = norm(quote)
        if len(q.split()) < MIN_WORDS.get(kind, 4) or q not in body_norm:
            continue
        if kind == "agencies":
            # a legislature is not an agency gaining power, and neither is the jurisdiction itself
            name = norm(label)
            if name in NOT_AGENCIES or name == norm(jurisdiction) or len(name.split()) < 2:
                continue
        out[label.strip()] = quote.strip()
    return out


def prune_tags(db):
    """Re-apply the current evidence rules to labels already stored, and drop the ones that fail.

    Measures are only re-read when their text changes, so without this a label that a
    later rule would reject keeps appearing on the site forever.
    """
    bodies, gone = {}, []
    for r in db.execute("SELECT id, jurisdiction_name, title, summary FROM measures"):
        bodies[r["id"]] = (norm(f"{r['title'] or ''}\n{r['summary'] or ''}"), r["jurisdiction_name"] or "")
    for r in db.execute("SELECT rowid, target, kind, value, evidence FROM tags"):
        body, jur = bodies.get(r["target"], (None, ""))
        if body is None:
            continue  # a post, whose text is not stored alongside
        kind = {"fear": "fears", "control": "controls", "agency": "agencies"}.get(r["kind"], r["kind"])
        if not agreed({kind: {r["value"]: r["evidence"]}}, body, kind, jur):
            gone.append(r["rowid"])
    for chunk in (gone[i:i + 400] for i in range(0, len(gone), 400)):
        db.execute(f"DELETE FROM tags WHERE rowid IN ({','.join('?' for _ in chunk)})", chunk)
    db.commit()
    return len(gone)


def targets(db, limit):
    rows = db.execute(
        "SELECT m.id, m.jurisdiction_name, m.identifier, m.title, m.summary, t.text_hash FROM measures m "
        "LEFT JOIN tag_runs t ON t.target = m.id "
        "WHERE (m.introduced_date >= ? OR m.kind IN ('rule','order')) "
        "ORDER BY m.introduced_date DESC", (SINCE,)).fetchall()
    out = []
    for r in rows:
        body = f"{r['title'] or ''}\n{r['summary'] or ''}"
        doc = f"Jurisdiction: {r['jurisdiction_name']}\nIdentifier: {r['identifier']}\nTitle: {r['title']}\n" \
              f"Summary: {r['summary'] or '(none)'}"
        h = sha(doc)
        if r["text_hash"] != h:
            out.append(("measure", r["id"], doc[:7000], h, body[:7000]))
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
    key = env("ANTHROPIC_API_KEY")
    fears, controls = config("fears"), config("controls")
    day = iso()[:10]
    spent = kv_get(db, "tag_spend", {})
    used_today = spent.get("n", 0) if spent.get("date") == day else 0
    limit = max(0, min(LIMITS.get(mode, 150), DAY_CAP - used_today))
    if not limit:
        state["message"] = f"daily cap of {DAY_CAP} items reached; resumes tomorrow"
        return
    dropped = prune_tags(db)
    work, backlog = targets(db, limit)
    done, errors = 0, 0
    started = time.time()

    def one(item):
        if time.time() - started > SECONDS:
            raise TimedOut()
        kind, target, doc, h, body = item
        is_measure = kind == "measure"
        proposal = propose(key, fears, controls, doc, is_measure)
        ai = bool(proposal.get("ai_related"))
        # A Federal Register document is only stored when its title or abstract names AI, and a
        # presidential document has no abstract, so the model judges an executive order on its title
        # alone. It judged two orders titled with artificial intelligence, 14179 and 14319, not about
        # AI, and they were missing from the record. A title that names AI settles it.
        title = next((l[7:] for l in doc.split("\n") if l.startswith("Title: ")), "")
        if not ai and target.startswith("fr-") and AI_TEXT.search(title):
            ai = True
        verdict = verify(key, fears, controls, doc, proposal) if ai else {}
        return target, h, ai, verdict, norm(body)

    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(one, w): w for w in work}
        jurisdictions = {w[1]: w[2].split("\n", 1)[0].removeprefix("Jurisdiction: ") for w in work}
        # A measure that asks, urges or objects imposes nothing, so it carries no control. It can
        # still name a fear: what it is worried about is the point of saying it at all.
        positions = {w[1] for w in work
                     if states_a_position(next((l[7:] for l in w[2].split("\n") if l.startswith("Title: ")), ""))}
        for fut in cf.as_completed(futures):
            _, target, _, h, _ = futures[fut]
            try:
                target, h, ai, verdict, doc_norm = fut.result()
                err = None
            except TimedOut:
                continue
            except Exception as exc:
                ai, verdict, doc_norm, err = None, {}, "", str(exc)[:300]
                errors += 1
                if errors <= 3:
                    log(f"[tag] {target}: {err}")
            with _lock:
                if err is None:
                    try:
                        db.execute("DELETE FROM tags WHERE target=?", (target,))
                        for kind in ("fears", "controls", "agencies"):
                            if kind == "controls" and target in positions:
                                continue
                            for label, quote in agreed(verdict, doc_norm, kind, jurisdictions.get(target, "")).items():
                                db.execute("INSERT OR REPLACE INTO tags(target,kind,value,evidence,model,tagged_at) "
                                           "VALUES(?,?,?,?,?,?)",
                                           (target, kind[:-1] if kind != "agencies" else "agency",
                                            label.strip(), quote, MODEL, iso()))
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
    state["message"] = (f"{done} tagged, {errors} errors, {max(0, backlog - done)} still queued, "
                        f"{used_today + done} of {DAY_CAP} today"
                        + (f"; dropped {dropped} unsupported labels" if dropped else "")
                        + (f"; stopped at {SECONDS}s" if ran_out else ""))
    if work and errors == len(work):
        raise RuntimeError(f"every tagging call failed; last error shown in logs")
