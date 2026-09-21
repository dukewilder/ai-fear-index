"""Federal bills from the Congress.gov API."""
import datetime as dt

from .common import Http, env, iso, kv_get, kv_set, log, looks_ai, now, status_from_action, strip_html, upsert

BASE = "https://api.congress.gov/v3"
SUMMARY_CAP = 150  # new bills found by summary per run, at two requests each
TYPE_LABEL = {"HR": "H.R.", "S": "S.", "HRES": "H.Res.", "SRES": "S.Res.", "HJRES": "H.J.Res.",
              "SJRES": "S.J.Res.", "HCONRES": "H.Con.Res.", "SCONRES": "S.Con.Res."}
TYPE_SLUG = {"HR": "house-bill", "S": "senate-bill", "HRES": "house-resolution", "SRES": "senate-resolution",
             "HJRES": "house-joint-resolution", "SJRES": "senate-joint-resolution",
             "HCONRES": "house-concurrent-resolution", "SCONRES": "senate-concurrent-resolution"}


def current_congress(today=None):
    year = (today or now().date()).year
    return (year - 1789) // 2 + 1


def ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def run(db, state, mode):
    key = env("CONGRESS_API_KEY")
    http = Http(min_interval=0.8)
    congress = current_congress()
    since = kv_get(db, "congress_since")
    started = iso()
    params = {"api_key": key, "format": "json", "limit": 250}
    if since and mode != "backfill":
        params["fromDateTime"] = since[:19] + "Z"
    matched, offset, pages = [], 0, 0
    # A bill already on file is followed whatever its title, so one found by its summary still has
    # its status kept up to date when it moves.
    stored = {r["id"] for r in db.execute("SELECT id FROM measures WHERE jurisdiction = 'us' AND source = 'Congress.gov'")}
    while True:
        url = f"{BASE}/bill/{congress}?sort=updateDate+desc"
        data = http.json(url, params={**params, "offset": offset})
        bills = data.get("bills", [])
        pages += 1
        for b in bills:
            ident = f"us-{congress}-{(b.get('type') or '').lower()}-{b.get('number')}"
            if looks_ai(b.get("title")) or ident in stored:
                matched.append(b)
        if len(bills) < params["limit"] or pages > 120:
            break
        offset += params["limit"]
    log(f"[congress] scanned {pages} pages, {len(matched)} AI-titled bills")
    added = 0
    for b in matched:
        added += store_bill(db, http, key, congress, (b.get("type") or "").upper(), b.get("number"), b)
    # A title scan cannot see a bill whose title is an acronym: the TAKE IT DOWN Act, the DEFIANCE
    # Act, the NO FAKES Act. The Congressional Research Service's summaries say what each bill
    # does in plain words, so they are scanned too, from the same point in time as the titles.
    by_summary, capped = 0, False
    try:
        # The first scan reads every summary this Congress has; after that, what changed since.
        full = not kv_get(db, f"congress_summaries:{congress}")
        seen = {(b.get("type") or "").upper() + str(b.get("number")) for b in matched}
        for kind, number in summary_matches(http, key, congress, None if full else params.get("fromDateTime")):
            if kind + str(number) in seen or f"us-{congress}-{kind.lower()}-{number}" in stored:
                continue
            if by_summary >= SUMMARY_CAP:
                capped = True  # the rest next run
                break
            seen.add(kind + str(number))
            by_summary += 1
            added += store_bill(db, http, key, congress, kind, number)
            db.commit()
        if full and not capped:
            kv_set(db, f"congress_summaries:{congress}", iso())
    except Exception as exc:  # the title scan still stands
        log(f"[congress] summary scan failed: {str(exc)[:200]}")
    kv_set(db, "congress_since", started)
    db.commit()
    state["added"] = added
    state["message"] = f"{len(matched)} AI-titled bills checked, and {by_summary} more found by their summaries"


def summary_matches(http, key, congress, since=None):
    """(type, number) of every bill whose CRS summary names AI, updated since the given time."""
    params = {"api_key": key, "format": "json", "limit": 250}
    if since:
        params["fromDateTime"] = since
    offset, pages, out = 0, 0, []
    while pages < 80:
        data = http.json(f"{BASE}/summaries/{congress}", params={**params, "offset": offset, "sort": "updateDate+desc"})
        items = data.get("summaries", [])
        pages += 1
        for item in items:
            bill = item.get("bill") or {}
            kind, number = (bill.get("type") or "").upper(), bill.get("number")
            if kind in TYPE_LABEL and number and looks_ai(strip_html(item.get("text") or "")):
                out.append((kind, int(number)))
        if len(items) < params["limit"]:
            break
        offset += params["limit"]
    return out


def store_bill(db, http, key, congress, btype, number, listing=None):
    """Fetch one bill's record, summary and status and store it. Returns 1 if the row changed.

    listing is the bill as the list endpoint gave it, when there is one; a bill looked up by its
    number alone (a known law the title scan missed) has none, and its detail record serves.
    """
    listing = listing or {}
    ident = f"us-{congress}-{btype.lower()}-{number}"
    row = db.execute("SELECT updated FROM measures WHERE id=?", (ident,)).fetchone()
    if listing and row and row["updated"] == listing.get("updateDate"):
        return 0
    detail = http.json(f"{BASE}/bill/{congress}/{btype.lower()}/{number}", params={"api_key": key, "format": "json"})
    bill = detail.get("bill", {})
    summary = ""
    try:
        sums = http.json(f"{BASE}/bill/{congress}/{btype.lower()}/{number}/summaries",
                         params={"api_key": key, "format": "json"}).get("summaries", [])
        if sums:
            summary = strip_html(sorted(sums, key=lambda s: s.get("updateDate", ""))[-1].get("text", ""))
    except Exception as exc:  # summaries are optional
        log(f"[congress] no summary for {ident}: {exc}")
    latest = bill.get("latestAction") or listing.get("latestAction") or {}
    status = "passed" if bill.get("laws") else status_from_action(latest.get("text"))
    sponsors = [s.get("fullName") for s in bill.get("sponsors", []) if s.get("fullName")]
    return upsert(db, "measures", {
        "id": ident, "kind": "bill", "jurisdiction": "us", "jurisdiction_name": "Congress",
        "session": str(congress), "identifier": f"{TYPE_LABEL.get(btype, btype)} {number}",
        "title": listing.get("title") or bill.get("title"), "summary": summary[:6000], "status": status,
        "latest_action": latest.get("text"), "latest_action_date": latest.get("actionDate"),
        "introduced_date": bill.get("introducedDate"),
        "url": f"https://www.congress.gov/bill/{ordinal(congress)}-congress/{TYPE_SLUG.get(btype, 'bill')}/{number}",
        "sponsors": ", ".join(sponsors), "source": "Congress.gov",
        "updated": listing.get("updateDate") or bill.get("updateDate"), "first_seen": iso(),
    })
