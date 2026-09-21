"""The record held against measures whose outcome is known from outside it.

Every other check in this pipeline asks whether what the site shows is true to its source. None
asked what the site was missing, and that is how it went live saying 86 measures had passed while
New York's frontier AI law, three of Utah's 2025 AI laws and Montana's Right to Compute Act were
on file as pending or not counted at all, and the TAKE IT DOWN Act was not on file. A list of
outcomes confirmed elsewhere, looked up every week, is the cheapest way to see a gap like that
before a reader does. config/known_laws.json is the list; the weekly review prints what fails.

    python -m pipeline.known --db state/index.db
"""
import argparse
import re

from .common import config, connect, in_window, measures_in_scope


def laws():
    return config("known_laws")["laws"]


def squash(identifier):
    return re.sub(r"[\s.]", "", (identifier or "").upper())


def lookup(db, law):
    """The stored measure a known law refers to, or None."""
    rows = db.execute("SELECT * FROM measures WHERE jurisdiction = ?", (law["jurisdiction"],)).fetchall()
    same = [r for r in rows if squash(r["identifier"]) == squash(law["identifier"])]
    exact = [r for r in same if str(r["session"]) == str(law["session"])]
    return (exact or same or [None])[0]


def ids(db):
    """The measure ids of the known laws on file. A person confirmed each is an AI law."""
    return {m["id"] for m in (lookup(db, law) for law in laws()) if m}


def problems(db):
    """Each known law the record gets wrong, and how, as (law, problem)."""
    counted = {m["id"] for m in measures_in_scope(db)}
    out = []
    for law in laws():
        m = lookup(db, law)
        if m is None:
            out.append((law, "not in the record: no collector has found it"))
            continue
        run = db.execute("SELECT ai_related FROM tag_runs WHERE target = ?", (m["id"],)).fetchone()
        if m["id"] not in counted:
            why = ("not read yet" if run is None else
                   "judged not about AI" if not run["ai_related"] else
                   "outside the report's period" if not in_window(m) else
                   "on the suppression list or merged into another record")
            out.append((law, f"in the record but not counted: {why}"))
        if (m["status"] or "") != law["outcome"]:
            out.append((law, f"marked {m['status'] or 'nothing'}, known to be {law['outcome']}; "
                             f"last action on file: {m['latest_action'] or 'none'}"))
    return out


def ocd_jurisdiction(code):
    kind = "district" if code == "dc" else "territory" if code in ("pr", "gu", "vi", "as", "mp") else "state"
    return f"ocd-jurisdiction/country:us/{kind}:{code}/government"


CONGRESS_TYPES = {"H.R.": "HR", "S.": "S", "H.RES.": "HRES", "S.RES.": "SRES", "H.J.RES.": "HJRES",
                  "S.J.RES.": "SJRES", "H.CON.RES.": "HCONRES", "S.CON.RES.": "SCONRES"}


def fetch_missing(db, env_key=None):
    """Look up by number each known law no search has reached, once a day each.

    The searches are keyword scans, and a keyword scan misses a law whose title is an acronym (the
    TAKE IT DOWN Act) or whose term is still waiting its turn in the backfill (California's No Robo
    Bosses Act, under "automated decision"). A known law is fetched directly instead of waiting.
    Returns the number stored. Any failure is logged and left for the next day.
    """
    import datetime as dt
    import os
    from .common import Http, kv_get, kv_set, log
    get_key = env_key or (lambda name: os.environ.get(name, "").strip())
    day = dt.date.today().isoformat()
    tried = kv_get(db, "known:tried", {})
    stored = 0
    for law in laws():
        tag = f"{law['jurisdiction']}|{law['session']}|{law['identifier']}"
        if lookup(db, law) is not None or tried.get(tag) == day:
            continue
        tried[tag] = day
        try:
            if law["jurisdiction"] == "us":
                key = get_key("CONGRESS_API_KEY")
                m = re.match(r"^([A-Za-z.]+)\s*(\d+)$", law["identifier"].strip())
                if not key or not m:
                    continue
                from .collect_congress import store_bill
                btype = CONGRESS_TYPES.get(m.group(1).upper(), m.group(1).upper().replace(".", ""))
                stored += store_bill(db, Http(min_interval=0.8), key, int(law["session"]), btype, int(m.group(2)))
            else:
                key = get_key("OPENSTATES_API_KEY")
                if not key:
                    continue
                from .collect_openstates import BASE, store
                spent = kv_get(db, "openstates_spend", {})
                if spent.get("date") == day and spent.get("n", 0) >= 245:
                    continue  # their daily allowance; tomorrow
                data = Http(min_interval=6.5, headers={"X-API-KEY": key}).json(BASE, params={
                    "jurisdiction": ocd_jurisdiction(law["jurisdiction"]), "session": law["session"],
                    "identifier": law["identifier"], "include": ["abstracts", "sponsorships"]})
                kv_set(db, "openstates_spend", {"date": day, "n": (spent.get("n", 0) if spent.get("date") == day else 0) + 1})
                for bill in data.get("results", []):
                    stored += store(db, bill)
            db.commit()
            found = lookup(db, law) is not None
            log(f"[known] {law['jurisdiction'].upper()} {law['identifier']}: "
                f"{'fetched by number' if found else 'not found by number either'}")
        except Exception as exc:  # one law must not stop the run
            log(f"[known] {law['jurisdiction'].upper()} {law['identifier']}: lookup failed, {str(exc)[:200]}")
    kv_set(db, "known:tried", tried)
    db.commit()
    return stored


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/index.db")
    args = ap.parse_args()
    db = connect(args.db)
    found = problems(db)
    for law, problem in found:
        print(f"{law['jurisdiction'].upper()} {law['identifier']} ({law['name']}): {problem}")
    print(f"{len(laws()) - len({(l['jurisdiction'], l['identifier']) for l, _ in found})} of {len(laws())} known laws are right")


if __name__ == "__main__":
    main()
