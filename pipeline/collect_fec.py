"""Election money from the FEC API: the committees on the entity list, and the register itself."""
import re

from .common import Entities, Http, HttpError, env, iso, log, name_key, now, upsert

BASE = "https://api.open.fec.gov/v1"


def cycle_now():
    year = now().year
    return year + (year % 2)


# Committees nobody has put on the entity list yet. Searching the register by name is the only way
# to find a PAC that formed last week, and the match has to be tight: "AI" appears inside hundreds
# of ordinary committee names, so it counts only as a word of its own.
SWEEP = ["artificial intelligence", "AI policy", "AI PAC", "AI super PAC",
         "machine learning", "data center", "deepfake", "tech policy"]
AI_NAME = re.compile(r"(?:\bA\.?I\.?\b|artificial intelligence|machine learning|deepfake|"
                     r"algorithm|data cent(?:er|re))", re.I)
# The FEC appends a bracket to tell two committees of similar name apart, and it is not what the
# committee is about: "APPRAISAL INSTITUTE PAC (AI PAC)" is a real-estate appraisers' committee
# and the two letters that matched are in the disambiguator. It comes off before the name is read.
BRACKET = re.compile(r"\s*\([^)]*\)\s*$")


def about_ai(name):
    return bool(AI_NAME.search(BRACKET.sub("", name or "")))
POLITICAL = ("O", "U", "V", "W", "N", "Q", "I")  # super PAC, hybrid, electioneering, and the PAC types


def totals_for(http, key, cid, cycle):
    """This cycle's totals for one committee, or None where the register has no filing for it.

    The register lists committees the totals endpoint answers 404 for: registered, never filed, or
    filed under a different cycle. Sweeping the register turned that into a source-wide failure,
    which is a great deal louder than the fact deserves.
    """
    try:
        rows = http.json(f"{BASE}/committee/{cid}/totals/",
                         params={"api_key": key, "cycle": cycle}).get("results", [])
    except HttpError as exc:
        if exc.status == 404:
            return None
        raise
    return rows[0] if rows else None


def sweep(db, http, key, ents, cycle, notes):
    """Search the register itself, so a committee that formed this month is not missed.

    Anything found here is stored without an entity attached: it is a committee the report knows
    about because the FEC lists it, not because somebody added it to a file.
    """
    # Anything this sweep put here before is re-read against the test as it now stands. A committee
    # it should not have taken is dropped rather than living on because it was added once.
    dropped = []
    for r in db.execute("SELECT id, name FROM committees WHERE entity IS NULL AND query LIKE 'sweep:%'"):
        if not about_ai(r["name"]):
            db.execute("DELETE FROM committees WHERE id=?", (r["id"],))
            db.execute("DELETE FROM fec WHERE committee_id=?", (r["id"],))
            dropped.append(r["name"])
    if dropped:
        db.commit()
        notes.append(f"dropped {len(dropped)} the sweep should not have taken: {', '.join(dropped[:3])}")
        log(f"[fec] dropped from the register sweep: {', '.join(dropped)}")
    known = {r["id"] for r in db.execute("SELECT id FROM committees")}
    added, new = 0, []
    for query in SWEEP:
        try:
            found = http.json(f"{BASE}/committees/", params={
                "api_key": key, "q": query, "per_page": 30, "sort": "-last_file_date"}).get("results", [])
        except Exception as exc:
            notes.append(f"sweep '{query}': {str(exc)[:60]}")
            continue
        for c in found:
            cid, cname = c.get("committee_id"), c.get("name") or ""
            if not cid or cid in known or not about_ai(cname):
                continue
            if (c.get("committee_type") or "") not in POLITICAL:
                continue
            t = totals_for(http, key, cid, cycle)
            if t is None:
                continue  # the register lists it and the totals endpoint does not; not ours to fix
            if not (t.get("receipts") or t.get("independent_expenditures")):
                continue  # registered but has raised and spent nothing; it is not money yet
            known.add(cid)
            new.append(f"{cname} ({cid})")
            upsert(db, "committees", {
                "id": cid, "name": cname, "entity": None, "query": f"sweep:{query}",
                "committee_type": c.get("committee_type_full") or c.get("committee_type"),
                "receipts": t.get("receipts"), "disbursements": t.get("disbursements"),
                "independent_expenditures": t.get("independent_expenditures"),
                "cycle": cycle, "updated": iso()})
            added += 1
            added += receipts(db, http, key, cid, cname, cycle)
            added += spending(db, http, key, cid, cname, cycle)
            db.commit()
    notes.append(f"swept the register: {len(new)} not on the list" + (f" ({'; '.join(new[:4])})" if new else ""))
    return added


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
                # A committee on the entity list stays on the site whether or not the totals
                # endpoint answers for this cycle. Its receipts are read separately and they are
                # what the figures rest on; dropping the row would take it off the page.
                t = totals_for(http, key, cid, cycle) or {}
                upsert(db, "committees", {
                    "id": cid, "name": c.get("name"), "entity": ent["slug"], "query": query,
                    "committee_type": c.get("committee_type_full") or c.get("committee_type"),
                    "receipts": t.get("receipts"), "disbursements": t.get("disbursements"),
                    "independent_expenditures": t.get("independent_expenditures"), "cycle": cycle, "updated": iso()})
                added += receipts(db, http, key, cid, c.get("name"), cycle)
                added += spending(db, http, key, cid, c.get("name"), cycle)
                db.commit()
    added += sweep(db, http, key, ents, cycle, notes)
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
            # a committee's bank interest arrives on the same schedule as its donations,
            # and only the line it is filed on tells them apart
            "receipt_type": r.get("receipt_type_desc") or r.get("receipt_type") or "",
            "line_number": r.get("line_number") or "",
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
