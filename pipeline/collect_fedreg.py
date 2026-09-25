"""Federal rules, proposed rules, and presidential documents about AI from the Federal Register API."""
import re

from .common import SINCE, SUMMARY_MAX, Http, iso, kv_get, kv_set, looks_ai, upsert

BASE = "https://www.federalregister.gov/api/v1/documents.json"
FIELDS = ["document_number", "title", "abstract", "html_url", "publication_date", "type", "subtype",
          "agencies", "executive_order_number", "action", "executive_order_notes"]
DOC = "https://www.federalregister.gov/api/v1/documents/{}.json"
# The Register writes on an order that has been revoked, "Revoked by: EO 14318, July 23, 2025". An
# order revoked in part stays, and "Revokes:" is the note on the order doing the revoking.
REVOKED = re.compile(r"\b(?:revoked|superseded)\s+by\b", re.I)
# The executive branch is where the bills send the power, so the search reaches as wide as the
# state search does. Anything whose title and abstract do not mention AI is pruned below.
TERMS = ["artificial intelligence", "deepfake", "data center", "machine learning", "automated decision",
         "synthetic media", "digital replica", "chatbot", "facial recognition", "large language model",
         "automated driving", "autonomous vehicle"]


def run(db, state, mode):
    http = Http(min_interval=0.5)
    # A term added later has to reach back to the start, or it only ever sees today onward.
    fresh = kv_get(db, "fedreg_terms") != TERMS
    since = SINCE if (mode == "backfill" or fresh or not kv_get(db, "fedreg_backfilled")) \
        else kv_get(db, "fedreg_since", SINCE)
    added, read = 0, 0
    for term in TERMS:
        page = 1
        while page <= 40:
            params = [("conditions[term]", term), ("conditions[publication_date][gte]", since),
                      ("order", "newest"), ("per_page", 100), ("page", page)]
            params += [("conditions[type][]", t) for t in ("RULE", "PRORULE", "PRESDOCU")]
            params += [("fields[]", f) for f in FIELDS]
            data = http.json(BASE, params=params)
            results = data.get("results", [])
            for d in results:
                read += 1
                added += store(db, d)
            db.commit()
            if page >= (data.get("total_pages") or 1) or not results:
                break
            page += 1
    dropped = prune(db)
    revoked = revoked_orders(db, http)
    kv_set(db, "fedreg_backfilled", True)
    kv_set(db, "fedreg_terms", TERMS)
    kv_set(db, "fedreg_since", iso()[:10])
    db.commit()
    state["added"] = added
    state["message"] = (f"{read} documents read since {since}" + (f"; dropped {dropped} not about AI" if dropped else "")
                        + (f"; removed {len(revoked)} revoked orders: {', '.join(revoked)}" if revoked else ""))


# The Rules section of the Register also carries notices about rules: a public briefing, a meeting,
# a correction, a comment period reopened. None of them is a rule, and the type field alone stored
# a briefing notice as a final rule that had passed. The action line says what the document is.
NOT_A_RULE = re.compile(r"\b(notification|notice of (a )?(public )?(briefing|meeting|hearing)|public briefing|"
                        r"meeting|correction|extension of (the )?comment period|reopening of (the )?comment period)\b",
                        re.I)


def revoked_orders(db, http):
    """Take off every stored executive order the Register now marks as revoked.

    An order is stored the day it is published and never fetched again, so one revoked months later
    kept counting among the measures that have passed: Executive Order 14141 was revoked in July 2025
    and was still here in September. Once a day each stored order is looked up again.
    """
    if kv_get(db, "fedreg_revoked_checked") == iso()[:10]:
        return []
    gone = []
    for r in db.execute("SELECT id, identifier FROM measures WHERE kind = 'order' AND id LIKE 'fr-%'").fetchall():
        try:
            notes = http.json(DOC.format(r["id"][3:]), params={"fields[]": "executive_order_notes"},
                              tries=2).get("executive_order_notes") or ""
        except Exception:
            continue  # asked again tomorrow
        if REVOKED.search(notes):
            for table in ("tags", "tag_runs", "checks"):
                db.execute(f"DELETE FROM {table} WHERE target=?", (r["id"],))
            db.execute("DELETE FROM measures WHERE id=?", (r["id"],))
            gone.append(r["identifier"])
    kv_set(db, "fedreg_revoked_checked", iso()[:10])
    db.commit()
    return gone


def prune(db):
    """Drop stored documents that only mention AI deep in the body, so they cost nothing to tag, and
    notices that were stored as rules before the action line was read."""
    gone = []
    for r in db.execute("SELECT id, title, summary, latest_action, kind FROM measures WHERE id LIKE 'fr-%'"):
        if not looks_ai(r["title"], r["summary"]) or (r["kind"] == "rule" and NOT_A_RULE.search(r["latest_action"] or "")):
            gone.append(r["id"])
    for chunk in (gone[i:i + 400] for i in range(0, len(gone), 400)):
        marks = ",".join("?" for _ in chunk)
        db.execute(f"DELETE FROM measures WHERE id IN ({marks})", chunk)
        db.execute(f"DELETE FROM tags WHERE target IN ({marks})", chunk)
        db.execute(f"DELETE FROM tag_runs WHERE target IN ({marks})", chunk)
    return len(gone)


def store(db, d):
    if not looks_ai(d.get("title"), d.get("abstract")):
        return 0  # the term only appears deep in the body
    dtype = d.get("type") or ""
    if dtype != "Presidential Document" and NOT_A_RULE.search(d.get("action") or ""):
        return 0  # a notice about a rule, not a rule
    if dtype == "Presidential Document" and REVOKED.search(d.get("executive_order_notes") or ""):
        return 0  # revoked since; it is not in force and does not count as passed
    eo = d.get("executive_order_number")
    if dtype == "Presidential Document":
        kind, ident_label, status = "order", (f"Executive Order {eo}" if eo else (d.get("subtype") or "Presidential document")), "passed"
    elif dtype == "Rule":
        kind, ident_label, status = "rule", "Final rule", "passed"
    else:
        kind, ident_label, status = "rule", "Proposed rule", "pending"
    agencies = [a.get("name") for a in (d.get("agencies") or []) if a.get("name")]
    return upsert(db, "measures", {
        "id": f"fr-{d.get('document_number')}", "kind": kind, "jurisdiction": "us-exec",
        "jurisdiction_name": ", ".join(agencies[:2]) or "Federal government", "session": None,
        "identifier": ident_label, "title": d.get("title"), "summary": (d.get("abstract") or "")[:SUMMARY_MAX],
        "status": status, "latest_action": d.get("action") or ident_label,
        "latest_action_date": d.get("publication_date"), "introduced_date": d.get("publication_date"),
        "url": d.get("html_url"), "sponsors": ", ".join(agencies), "source": "Federal Register",
        "updated": d.get("publication_date"), "first_seen": iso(),
    })
