"""Federal lobbying filings from the LDA.gov API."""
import re

from .common import Http, env, iso, kv_get, kv_set, log, name_key, now, upsert

BASE = "https://lda.gov/api/v1/filings/"
TERMS = ["artificial intelligence", "deepfake", "machine learning", "data center"]
PERIODS = {"first_quarter": "Q1", "second_quarter": "Q2", "third_quarter": "Q3", "fourth_quarter": "Q4",
           "mid_year": "Q2", "year_end": "Q4"}
BILL_REF = re.compile(r"\b(H\.?\s?R\.?|S\.|H\.?\s?Res\.?|S\.?\s?Res\.?|H\.?\s?J\.?\s?Res\.?|S\.?\s?J\.?\s?Res\.?)\s?(\d{1,5})\b")


def norm_ref(prefix, num):
    p = re.sub(r"[\s.]", "", prefix).upper()
    label = {"HR": "H.R.", "S": "S.", "HRES": "H.Res.", "SRES": "S.Res.", "HJRES": "H.J.Res.", "SJRES": "S.J.Res."}.get(p, p)
    return f"{label} {int(num)}"


def run(db, state, mode):
    key = env("LDA_API_KEY", required=False)
    headers = {"Authorization": f"Token {key}"} if key else {}
    http = Http(min_interval=0.6 if key else 4.2, headers=headers)
    year_now = now().year
    years = list(range(year_now - 3, year_now + 1)) if (mode == "backfill" or not kv_get(db, "lda_backfilled")) \
        else [year_now - 1, year_now] if now().month <= 2 else [year_now]
    added, read = 0, 0
    for term in TERMS:
        for year in years:
            url, params = BASE, {"filing_specific_lobbying_issues": term, "filing_year": year,
                                 "page_size": 25, "ordering": "-dt_posted"}
            pages = 0
            while url:
                data = http.json(url, params=params)
                params = None  # "next" already carries the query
                pages += 1
                if pages == 1 and (data.get("count") or 0) > 40000:
                    raise RuntimeError(f"filter looks unsupported: {data.get('count')} filings for '{term}' in {year}")
                fresh = 0
                for f in data.get("results", []):
                    read += 1
                    fresh += store(db, f, term)
                added += fresh
                db.commit()
                url = data.get("next")
                if mode != "backfill" and kv_get(db, "lda_backfilled") and fresh == 0 and pages >= 2:
                    break  # caught up with what we already have
            log(f"[lda] '{term}' {year}: {pages} pages")
    kv_set(db, "lda_backfilled", True)
    db.commit()
    state["added"] = added
    state["message"] = f"{read} filings read for {years[0]}-{years[-1]}"


def store(db, f, term):
    ident = f.get("filing_uuid")
    if not ident:
        return 0
    acts = f.get("lobbying_activities") or []
    relevant = [a for a in acts if term.lower() in (a.get("description") or "").lower()] or acts
    issues = " | ".join((a.get("description") or "").strip() for a in relevant if a.get("description"))
    existing = db.execute("SELECT issues FROM lobbying WHERE id=?", (ident,)).fetchone()
    if existing and existing["issues"] and issues and issues in existing["issues"]:
        return 0
    if existing and existing["issues"]:
        merged = existing["issues"] if issues in existing["issues"] else existing["issues"] + " | " + issues
        issues = merged
    refs = sorted({norm_ref(p, n) for p, n in BILL_REF.findall(issues)})
    entities = sorted({(e.get("name") or "").strip() for a in relevant for e in (a.get("government_entities") or [])
                       if e.get("name")})
    client = (f.get("client") or {}).get("name") or ""
    amount = f.get("income") if f.get("income") is not None else f.get("expenses")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    return upsert(db, "lobbying", {
        "id": ident, "client": client, "client_key": name_key(client),
        "registrant": (f.get("registrant") or {}).get("name") or "", "year": f.get("filing_year"),
        "period": f.get("filing_period"), "quarter": PERIODS.get(f.get("filing_period") or "", ""),
        "amount": amount, "issues": issues[:8000], "bill_refs": ", ".join(refs),
        "gov_entities": ", ".join(entities)[:1000], "url": f.get("filing_document_url") or "",
        "posted": (f.get("dt_posted") or "")[:19], "filing_type": f.get("filing_type") or "",
        "first_seen": iso(),
    })
