"""State bills from the Open States (Plural) v3 API.

Open States searches bill text, so it also finds federal bills whose titles never say AI
and Congress.gov's title match therefore misses. Those land on the same row Congress.gov
would write, and Congress.gov's own record wins wherever both have one.
"""
from .common import (SINCE, Http, HttpError, congress_id, env, iso, kv_get, kv_set, log, retry_at, retry_clear,
                     status_from_action, upsert)

BASE = "https://v3.openstates.org/bills"
QUERIES = ["artificial intelligence", "deepfake", "chatbot", "data center", "automated decision",
           "synthetic media", "digital replica", "algorithmic"]
MAX_REQUESTS = 240  # per run
DAY_BUDGET = 240    # the free tier allows 250 a day, so stop short of it and resume tomorrow
RETRY_HOURS = 3     # a refusal is their rolling day, not ours, so wait it out and go again


def jurisdiction_code(j):
    jid = (j or {}).get("id", "")
    for part in jid.split("/"):
        if part.startswith(("state:", "district:", "territory:")):
            return part.split(":", 1)[1]
    if "country:us" in jid:
        return "us"  # Congress, which Open States files under the country and not a state
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
    requests_used, added, seen, capped = 0, 0, 0, False
    # Start where the last run stopped. Without this the first query spends the whole
    # allowance every day and the last ones are never searched at all.
    offset = kv_get(db, "openstates_offset", 0) % len(QUERIES)
    ordered = QUERIES[offset:] + QUERIES[:offset]
    stopped_at = None
    for i, query in enumerate(ordered):
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
                if "exceeded limit" in str(exc) or exc.status == 429:
                    # the daily allowance is gone; keep what we have and pick up tomorrow
                    capped = True
                    break
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
        if capped:
            stopped_at = (offset + i) % len(QUERIES)
            log("[openstates] allowance refused the run; backing off")
            break
        if requests_used >= budget:
            stopped_at = (offset + i) % len(QUERIES)
            log(f"[openstates] request budget used; resuming '{query}' next run")
            break
        kv_set(db, "openstates_cursors", cursors)
    kv_set(db, "openstates_cursors", cursors)
    kv_set(db, "openstates_spend", {"date": day, "n": used_today + requests_used})
    kv_set(db, "openstates_offset", stopped_at if stopped_at is not None else 0)
    # A refusal that cost nothing is their rolling window, not our budget: the run is worth
    # repeating later the same day rather than writing the day off.
    if capped and not requests_used:
        retry_at(db, "openstates", RETRY_HOURS)
    else:
        retry_clear(db, "openstates")
    db.commit()
    state["added"] = added
    backlog = [q for q, c in cursors.items() if c.get("backfilling")] + [q for q in QUERIES if q not in cursors]
    state["message"] = (f"{seen} bills read in {requests_used} requests"
                        + ("; daily allowance reached" if capped else "")) + (
        f"; still backfilling: {', '.join(backlog)}" if backlog else "")


def store(db, b):
    j = b.get("jurisdiction") or {}
    code = jurisdiction_code(j)
    federal = code == "us" and congress_id(b.get("session"), b.get("identifier"))
    ident = federal or "os-" + (b.get("id", "").rsplit("/", 1)[-1] or f"{code}-{b.get('session')}-{b.get('identifier')}")
    row = db.execute("SELECT updated, source FROM measures WHERE id=?", (ident,)).fetchone()
    if row and row["source"] == "Congress.gov":
        return 0  # the same bill, and Congress.gov carries the summary, the sponsors and the actions
    if row and row["updated"] == b.get("updated_at"):
        return 0
    abstracts = " ".join(a.get("abstract", "") for a in (b.get("abstracts") or []))
    sponsors = [s.get("name") for s in (b.get("sponsorships") or []) if s.get("primary")] or \
               [s.get("name") for s in (b.get("sponsorships") or [])][:3]
    kind = "resolution" if "resolution" in " ".join(b.get("classification") or []) else "bill"
    return upsert(db, "measures", {
        "id": ident, "kind": kind, "jurisdiction": code,
        "jurisdiction_name": "Congress" if code == "us" else (j.get("name") or code.upper()),
        "session": b.get("session"), "identifier": b.get("identifier"), "title": b.get("title"),
        "summary": abstracts[:6000], "status": status_from_action(b.get("latest_action_description")),
        "latest_action": b.get("latest_action_description"), "latest_action_date": (b.get("latest_action_date") or "")[:10],
        "introduced_date": (b.get("first_action_date") or b.get("created_at") or "")[:10],
        "url": b.get("openstates_url"), "sponsors": ", ".join(s for s in sponsors if s),
        "source": "Open States", "updated": b.get("updated_at"), "first_seen": iso(),
    })
