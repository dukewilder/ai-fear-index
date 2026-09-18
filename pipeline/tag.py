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

from .common import SINCE, config, env, iso, log, sha

API = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"
LIMITS = {"hourly": 60, "daily": 700, "backfill": 3500, "auto": 60}
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
            "Use empty lists when nothing applies.")
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
            '"agencies": {"name": "quote or null"}}\nQuotes must be copied word for word from TEXT, 2 to 30 words.')
    return call(key, system, user, max_tokens=900)


def agreed(verdict, doc_norm, kind):
    out = {}
    for label, quote in (verdict.get(kind) or {}).items():
        q = norm(quote) if isinstance(quote, str) else ""
        if len(q.split()) >= 2 and q in doc_norm:
            out[label] = quote.strip()
    return out


def targets(db, limit):
    rows = db.execute(
        "SELECT m.id, m.jurisdiction_name, m.identifier, m.title, m.summary, t.text_hash FROM measures m "
        "LEFT JOIN tag_runs t ON t.target = m.id "
        "WHERE (m.introduced_date >= ? OR m.kind IN ('rule','order')) "
        "ORDER BY m.introduced_date DESC", (SINCE,)).fetchall()
    out = []
    for r in rows:
        doc = f"Jurisdiction: {r['jurisdiction_name']}\nIdentifier: {r['identifier']}\nTitle: {r['title']}\n" \
              f"Summary: {r['summary'] or '(none)'}"
        h = sha(doc)
        if r["text_hash"] != h:
            out.append(("measure", r["id"], doc[:7000], h))
    posts = db.execute(
        "SELECT p.id, p.title, p.summary, t.text_hash FROM posts p LEFT JOIN tag_runs t ON t.target = 'post:' || p.id "
        "ORDER BY p.published DESC").fetchall()
    for r in posts:
        doc = f"Title: {r['title']}\nText: {r['summary'] or '(none)'}"
        h = sha(doc)
        if r["text_hash"] != h:
            out.append(("post", "post:" + r["id"], doc[:5000], h))
    measures = [t for t in out if t[0] == "measure"]
    posts_ = [t for t in out if t[0] == "post"]
    return (measures[: int(limit * 0.85)] + posts_)[:limit], len(out)


def run(db, state, mode):
    key = env("ANTHROPIC_API_KEY")
    fears, controls = config("fears"), config("controls")
    limit = LIMITS.get(mode, 60)
    work, backlog = targets(db, limit)
    done, errors = 0, 0

    def one(item):
        kind, target, doc, h = item
        is_measure = kind == "measure"
        proposal = propose(key, fears, controls, doc, is_measure)
        ai = bool(proposal.get("ai_related"))
        verdict = verify(key, fears, controls, doc, proposal) if ai else {}
        return target, h, ai, verdict, norm(doc)

    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(one, w): w for w in work}
        for fut in cf.as_completed(futures):
            _, target, _, h = futures[fut]
            try:
                target, h, ai, verdict, doc_norm = fut.result()
                err = None
            except Exception as exc:
                ai, verdict, doc_norm, err = None, {}, "", str(exc)[:300]
                errors += 1
                if errors <= 3:
                    log(f"[tag] {target}: {err}")
            with _lock:
                if err is None:
                    db.execute("DELETE FROM tags WHERE target=?", (target,))
                    for kind in ("fears", "controls", "agencies"):
                        for label, quote in agreed(verdict, doc_norm, kind).items():
                            db.execute("INSERT OR REPLACE INTO tags(target,kind,value,evidence,model,tagged_at) "
                                       "VALUES(?,?,?,?,?,?)", (target, kind[:-1] if kind != "agencies" else "agency",
                                                                label.strip(), quote, MODEL, iso()))
                    db.execute("INSERT OR REPLACE INTO tag_runs(target,text_hash,ai_related,tagged_at,error) "
                               "VALUES(?,?,?,?,NULL)", (target, h, 1 if ai else 0, iso()))
                    done += 1
                    if done % 25 == 0:
                        db.commit()
                        log(f"[tag] {done}/{len(work)} tagged")
    db.commit()
    state["added"] = done
    state["message"] = f"{done} tagged, {errors} errors, {max(0, backlog - done)} still queued"
    if work and errors == len(work):
        raise RuntimeError(f"every tagging call failed; last error shown in logs")
