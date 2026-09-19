"""Election money from the FEC API for AI-focused committees in the entity list."""
from .common import Entities, Http, env, iso, log, name_key, now, upsert

BASE = "https://api.open.fec.gov/v1"


def cycle_now():
    year = now().year
    return year + (year % 2)


def run(db, state, mode):
    key = env("FEC_API_KEY")
    http = Http(min_interval=0.5)
    ents = Entities()
    cycle = cycle_now()
    added, notes = 0, []
    for ent in ents.items:
        for query in ent.get("fec_search", []):
            found = http.json(f"{BASE}/committees/", params={"api_key": key, "q": query, "per_page": 20,
                                                              "sort": "-last_file_date"}).get("results", [])
            want = name_key(query)
            picks = [c for c in found if name_key(c.get("name")).startswith(want)]
            if not picks:
                notes.append(f"no committee named '{query}'")
                continue
            for c in picks:
                cid = c["committee_id"]
                notes.append(f"{ent['name']} -> {c.get('name')} ({cid})")
                totals = http.json(f"{BASE}/committee/{cid}/totals/", params={"api_key": key, "cycle": cycle}).get("results", [])
                t = totals[0] if totals else {}
                upsert(db, "committees", {
                    "id": cid, "name": c.get("name"), "entity": ent["slug"], "query": query,
                    "committee_type": c.get("committee_type_full") or c.get("committee_type"),
                    "receipts": t.get("receipts"), "disbursements": t.get("disbursements"),
                    "independent_expenditures": t.get("independent_expenditures"), "cycle": cycle, "updated": iso()})
                added += receipts(db, http, key, cid, c.get("name"), cycle)
                added += spending(db, http, key, cid, c.get("name"), cycle)
                db.commit()
    state["added"] = added
    state["message"] = "; ".join(notes)[:700]


def receipts(db, http, key, cid, cname, cycle):
    data = http.json(f"{BASE}/schedules/schedule_a/", params={
        "api_key": key, "committee_id": cid, "two_year_transaction_period": cycle,
        "sort": "-contribution_receipt_amount", "per_page": 100, "is_individual": None})
    added = 0
    for r in data.get("results", []):
        amount = r.get("contribution_receipt_amount") or 0
        if amount <= 0 or r.get("memo_code") == "X":
            continue
        who = r.get("contributor_name") or ""
        ident = r.get("sub_id") or f"{cid}-{who}-{r.get('contribution_receipt_date')}-{amount}"
        added += upsert(db, "fec", {
            "id": f"a-{ident}", "kind": "receipt", "committee_id": cid, "committee_name": cname,
            "counterparty": who, "counterparty_key": name_key(who), "amount": amount,
            "date": (r.get("contribution_receipt_date") or "")[:10],
            "description": r.get("contributor_employer") or r.get("entity_type_desc") or "",
            # a committee's bank interest arrives on the same schedule as its donations
            "receipt_type": r.get("receipt_type_desc") or r.get("receipt_type") or "",
            "support_oppose": None, "candidate": None,
            "url": f"https://www.fec.gov/data/receipts/?committee_id={cid}&two_year_transaction_period={cycle}",
            "first_seen": iso()})
    return added


def spending(db, http, key, cid, cname, cycle):
    data = http.json(f"{BASE}/schedules/schedule_e/", params={
        "api_key": key, "committee_id": cid, "cycle": cycle, "sort": "-expenditure_date", "per_page": 100})
    added = 0
    for r in data.get("results", []):
        amount = r.get("expenditure_amount") or 0
        if amount <= 0:
            continue
        ident = r.get("sub_id") or f"{cid}-{r.get('expenditure_date')}-{amount}-{r.get('candidate_name')}"
        added += upsert(db, "fec", {
            "id": f"e-{ident}", "kind": "independent_expenditure", "committee_id": cid, "committee_name": cname,
            "counterparty": r.get("payee_name") or "", "counterparty_key": name_key(r.get("payee_name")),
            "amount": amount, "date": (r.get("expenditure_date") or "")[:10],
            "description": r.get("expenditure_description") or "",
            "support_oppose": {"S": "supporting", "O": "opposing"}.get(r.get("support_oppose_indicator"), ""),
            "candidate": r.get("candidate_name") or "",
            "url": f"https://www.fec.gov/data/independent-expenditures/?committee_id={cid}&cycle={cycle}",
            "first_seen": iso()})
    return added
