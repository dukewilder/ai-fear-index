"""Federal bills from the Congress.gov API."""
import datetime as dt

from .common import Http, env, iso, kv_get, kv_set, log, looks_ai, now, status_from_action, strip_html, upsert

BASE = "https://api.congress.gov/v3"
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
    while True:
        url = f"{BASE}/bill/{congress}?sort=updateDate+desc"
        data = http.json(url, params={**params, "offset": offset})
        bills = data.get("bills", [])
        pages += 1
        for b in bills:
            if looks_ai(b.get("title")):
                matched.append(b)
        if len(bills) < params["limit"] or pages > 120:
            break
        offset += params["limit"]
    log(f"[congress] scanned {pages} pages, {len(matched)} AI-titled bills")
    added = 0
    for b in matched:
        btype = (b.get("type") or "").upper()
        number = b.get("number")
        ident = f"us-{congress}-{btype.lower()}-{number}"
        row = db.execute("SELECT updated FROM measures WHERE id=?", (ident,)).fetchone()
        if row and row["updated"] == b.get("updateDate"):
            continue
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
        latest = bill.get("latestAction") or b.get("latestAction") or {}
        status = "passed" if bill.get("laws") else status_from_action(latest.get("text"))
        sponsors = [s.get("fullName") for s in bill.get("sponsors", []) if s.get("fullName")]
        added += upsert(db, "measures", {
            "id": ident, "kind": "bill", "jurisdiction": "us", "jurisdiction_name": "Congress",
            "session": str(congress), "identifier": f"{TYPE_LABEL.get(btype, btype)} {number}",
            "title": b.get("title") or bill.get("title"), "summary": summary[:6000], "status": status,
            "latest_action": latest.get("text"), "latest_action_date": latest.get("actionDate"),
            "introduced_date": bill.get("introducedDate"),
            "url": f"https://www.congress.gov/bill/{ordinal(congress)}-congress/{TYPE_SLUG.get(btype, 'bill')}/{number}",
            "sponsors": ", ".join(sponsors), "source": "Congress.gov", "updated": b.get("updateDate"),
            "first_seen": iso(),
        })
    kv_set(db, "congress_since", started)
    db.commit()
    state["added"] = added
    state["message"] = f"{len(matched)} AI-titled bills checked"
