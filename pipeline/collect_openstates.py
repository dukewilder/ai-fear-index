"""State bills from the Open States (Plural) v3 API."""
from .common import SINCE, Http, HttpError, env, iso, kv_get, kv_set, log, status_from_action, upsert

BASE = "https://v3.openstates.org/bills"
QUERIES = ["artificial intelligence", "deepfake", "chatbot", "data center", "automated decision",
           "synthetic media", "digital replica", "algorithmic"]
MAX_REQUESTS = 420  # per run
DAY_BUDGET = 420    # and per day, so a long backfill cannot exhaust the free tier


def jurisdiction_code(j):
    jid = (j or {}).get("id", "")
    for part in jid.split("/"):
        if part.startswith(("state:", "district:", "territory:")):
            return part.split(":", 1)[1]
    return jid.rsplit("/", 1)[-1] or "state"


def run(db, state, mode):
    key = env("OPENSTATES_API_KEY")
    http = Http(min_interval=6.5, headers={"X-API-KEY": key})
    cursors = kv_get(db, "openstates_cursors", {})
    day = iso()[:10]
    spent = kv_get(db, "openstates_spend", {})
    used_today = spent.get("n", 0) if spent.get("date") == day else 0
    budget = max(0, min(MAX_REQUESTS, DAY_BUDGET - used_today))
    if not budget:
        state["message"] = f"daily budget of {DAY_BUDGET} requests used; resumes tomorrow"
        return
    requests_used, added, seen = 0, 0, 0
    for query in QUERIES:
        cursor = cursors.get(query, {})
        page = cursor.get("page", 1) if cursor.get("backfilling", True) else 1
        since = None if cursor.get("backfilling", True) else cursor.get("since")
        run_started = iso()[:10]
        while requests_used < budget:
            params = {"q": query, "sort": "updated_desc", "per_page": 20, "page": page,
                      "include": ["abstracts", "sponsorships"], "created_since": SINCE}
            if since:
                params["updated_since"] = since
            try:
                data = http.json(BASE, params=params)
            except HttpError as exc:
                if exc.status == 400 and page > 1:
                    break  # past the last page
                raise
            requests_used += 1
            results = data.get("results", [])
            for b in results:
                seen += 1
                added += store(db, b)
            db.commit()
            pag = data.get("pagination", {})
            max_page = pag.get("max_page") or page
            if not results or page >= max_page:
                cursors[query] = {"backfilling": False, "since": run_started, "page": 1}
                break
            page += 1
            cursors[query] = {"backfilling": True, "page": page} if cursor.get("backfilling", True) else cursor
        else:
            log(f"[openstates] request budget used; resuming '{query}' next run")
            break
        kv_set(db, "openstates_cursors", cursors)
    kv_set(db, "openstates_cursors", cursors)
    kv_set(db, "openstates_spend", {"date": day, "n": used_today + requests_used})
    db.commit()
    state["added"] = added
    backlog = [q for q, c in cursors.items() if c.get("backfilling")] + [q for q in QUERIES if q not in cursors]
    state["message"] = f"{seen} bills read in {requests_used} requests" + (
        f"; still backfilling: {', '.join(backlog)}" if backlog else "")


def store(db, b):
    j = b.get("jurisdiction") or {}
    code = jurisdiction_code(j)
    ident = "os-" + (b.get("id", "").rsplit("/", 1)[-1] or f"{code}-{b.get('session')}-{b.get('identifier')}")
    row = db.execute("SELECT updated FROM measures WHERE id=?", (ident,)).fetchone()
    if row and row["updated"] == b.get("updated_at"):
        return 0
    abstracts = " ".join(a.get("abstract", "") for a in (b.get("abstracts") or []))
    sponsors = [s.get("name") for s in (b.get("sponsorships") or []) if s.get("primary")] or \
               [s.get("name") for s in (b.get("sponsorships") or [])][:3]
    kind = "resolution" if "resolution" in " ".join(b.get("classification") or []) else "bill"
    return upsert(db, "measures", {
        "id": ident, "kind": kind, "jurisdiction": code, "jurisdiction_name": j.get("name") or code.upper(),
        "session": b.get("session"), "identifier": b.get("identifier"), "title": b.get("title"),
        "summary": abstracts[:6000], "status": status_from_action(b.get("latest_action_description")),
        "latest_action": b.get("latest_action_description"), "latest_action_date": (b.get("latest_action_date") or "")[:10],
        "introduced_date": (b.get("first_action_date") or b.get("created_at") or "")[:10],
        "url": b.get("openstates_url"), "sponsors": ", ".join(s for s in sponsors if s),
        "source": "Open States", "updated": b.get("updated_at"), "first_seen": iso(),
    })
