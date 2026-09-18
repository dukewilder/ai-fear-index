"""Federal rules, proposed rules, and presidential documents about AI from the Federal Register API."""
from .common import SINCE, Http, iso, kv_get, kv_set, looks_ai, upsert

BASE = "https://www.federalregister.gov/api/v1/documents.json"
FIELDS = ["document_number", "title", "abstract", "html_url", "publication_date", "type", "subtype",
          "agencies", "executive_order_number", "action"]
TERMS = ["artificial intelligence", "deepfake", "data center"]


def run(db, state, mode):
    http = Http(min_interval=0.5)
    since = SINCE if (mode == "backfill" or not kv_get(db, "fedreg_backfilled")) else kv_get(db, "fedreg_since", SINCE)
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
    kv_set(db, "fedreg_backfilled", True)
    kv_set(db, "fedreg_since", iso()[:10])
    db.commit()
    state["added"] = added
    state["message"] = f"{read} documents read since {since}"


def store(db, d):
    if not looks_ai(d.get("title"), d.get("abstract")):
        return 0  # the term only appears deep in the body
    dtype = d.get("type") or ""
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
        "identifier": ident_label, "title": d.get("title"), "summary": (d.get("abstract") or "")[:6000],
        "status": status, "latest_action": d.get("action") or ident_label,
        "latest_action_date": d.get("publication_date"), "introduced_date": d.get("publication_date"),
        "url": d.get("html_url"), "sponsors": ", ".join(agencies), "source": "Federal Register",
        "updated": d.get("publication_date"), "first_seen": iso(),
    })
