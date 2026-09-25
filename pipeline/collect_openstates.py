"""State bills from the Open States (Plural) v3 API.

Open States searches bill text, so it also finds federal bills whose titles never say AI
and Congress.gov's title match therefore misses. Those land on the same row Congress.gov
would write, and Congress.gov's own record wins wherever both have one.
"""
import collections
import datetime as dt
import re
import time
import urllib.parse

from .common import (AI_TEXT, OLD_SUMMARY_CUT, SINCE, STATES, SUMMARY_MAX, Http, HttpError, congress_id, env, iso,
                     kv_get, kv_set, log, measures_in_scope, retry_at, retry_clear, status_from_action, upsert)

BASE = "https://v3.openstates.org/bills"
# Every request asks for the bill's sources and versions too. They cost no extra requests, and they
# carry the legislature's own page for the bill, which is where a reader should land (pick_link).
INCLUDE = ["abstracts", "sponsorships", "sources", "versions"]
# "data center" goes last. It matches every bill that mentions one, budget bills included, and its
# backfill had used four days of the allowance at page 203 while "automated decision",
# "algorithmic", "synthetic media" and "digital replica" had not been searched once. California's
# No Robo Bosses Act and Colorado's delay of its AI Act were missing because of it. The stored
# offset points at the fourth query, which in this order is the first of the four never searched.
# Data center bills are counted whether or not they say AI, and the tagger sorts the ones about
# data centers from the ones that mention one in passing. So are bills on vehicles that drive
# themselves, from 25 September 2026, searched by the four names statutes give them; they sit
# before "data center" so that search stays last.
QUERIES = ["artificial intelligence", "deepfake", "chatbot", "automated decision", "algorithmic",
           "synthetic media", "digital replica", "autonomous vehicle", "automated driving", "self-driving",
           "driverless", "data center"]
MAX_REQUESTS = 240  # per run
DAY_BUDGET = 240    # the free tier allows 250 a day, so stop short of it and resume tomorrow
RETRY_HOURS = 3     # a refusal is their rolling day, not ours, so wait it out and go again
# Their rate limit puts six and a half seconds between calls, so a full budget is twenty-six
# minutes of one run's hour spent waiting. The cursors already make this resumable, so it stops
# at the clock and picks up where it left off rather than running the job out of time.
SECONDS = 780
# Bills pre-filed in late 2024 for a session that began in 2025. The searches ask only for records
# created since January, so Texas's, Virginia's and Florida's pre-filed bills were never returned.
# One sweep per search, oldest first action first, stopping at the first bill acted on in 2025.
PREFILE_SINCE = "2024-10-01"
PREFILE_CAP = 30  # requests per run, ahead of the backfills
# A backfill pages through a search newest change first and takes days. A bill changed while it ran
# moved to a page it had already passed, and the backfill used to finish with "since" set to the day
# it finished, so that change was never read: a bill signed mid-backfill kept its old status until
# it changed again. Any pass through a search, a backfill or a day's changes, now resumes where it
# stopped and hands the next pass the day it began. The collector first ran on 18 September 2026;
# a backfill begun before its start was recorded, and every search already caught up, read from
# then, once.
FIRST_RUN = "2026-09-18"
RESWEEP = 1


# Legislatures the search cannot see. Open States' search reads bill text, and some legislatures'
# text is not in it: asked for "artificial intelligence", Indiana's 2026 session of 935 bills
# answered nothing, and so did the DC Council's session that began in 2025, though Indiana had HB
# 1182 on AI-made sexual images and DC has B26-0491, the Artificial Intelligence Literacy in
# Education Act. Neither had a bill on file, and a state map would have shown them as quiet. So
# once a week, where the record holds almost nothing from a legislature in a year, the search is
# asked; if it sees nothing while that legislature filed plenty, the year's bills are read one by
# one instead, title and summary, and those that name AI or data centers go to the tagger like any
# other. A sweep that has read everything once then reads each day what changed.
BLIND_EVERY_DAYS = 7
BLIND_THIN = 5       # bills on file from a legislature in a year, below which the search is doubted
BLIND_FILED = 50     # bills that legislature filed that year, above which a search seeing none is blind
SWEEP_CAP = 60       # requests per run for the legislatures read bill by bill
BLIND_ASK = "artificial intelligence"


def ocd_id(code):
    if code == "us":
        return "ocd-jurisdiction/country:us/government"  # Congress, filed under the country
    kind = "district" if code == "dc" else "territory" if code == "pr" else "state"
    return f"ocd-jurisdiction/country:us/{kind}:{code}/government"


# Found by asking Open States directly on 21 September 2026, before the weekly check existed: the DC
# Council's session that began in 2025 and Indiana's 2026 session answer the search with nothing.
# They start as sweeps so the site can say so today, and the check still runs on the first chance.
KNOWN_BLIND = ("dc:2025-01-01", "in:2026-01-01")


def blind_state(db):
    st = kv_get(db, "openstates_blind")
    if st is None:
        st = {"checked": "", "sweeps": {k: {"page": 1, "started": None, "done": False} for k in KNOWN_BLIND}}
    return st


def find_blind(db, http, cap, today):
    """Once a week, ask the search about the legislatures the record holds almost nothing from.

    Returns the requests used. A legislature and year the search cannot see gets a sweep, keyed by
    the day its bills are read from; one found blind from the earlier year covers the later too.
    """
    st = blind_state(db)
    if st.get("checked") and (dt.date.fromisoformat(today) - dt.date.fromisoformat(st["checked"])).days < BLIND_EVERY_DAYS:
        return 0
    first, current = int(SINCE[:4]), int(today[:4])
    held = collections.Counter((r["jurisdiction"], (r["introduced_date"] or "")[:4]) for r in db.execute(
        "SELECT jurisdiction, introduced_date FROM measures WHERE source = 'Open States'"))
    used = 0

    def total(code, since, q=None):
        nonlocal used
        params = {"jurisdiction": ocd_id(code), "created_since": since, "per_page": 1}
        if q:
            params["q"] = q
        data = http.json(BASE, params=params)
        used += 1
        return int((data.get("pagination") or {}).get("total_items") or 0)

    thin = [(code, held[(code, str(current))] < BLIND_THIN, first < current and held[(code, str(first))] < BLIND_THIN)
            for code in sorted(STATES | {"dc", "pr"})]
    thin = [t for t in thin if t[1] or t[2]]
    if 4 * len(thin) > cap:
        return 0  # not enough left in this run to ask them all; a run with more asks instead
    found = []
    for code, thin_now, thin_before in thin:
        now_since = f"{current}-01-01"
        seen_now = total(code, now_since, BLIND_ASK)
        filed_now = total(code, now_since) if not seen_now else None
        blind_from = now_since if not seen_now and filed_now > BLIND_FILED else None
        if thin_before:
            seen_all = total(code, SINCE, BLIND_ASK)
            if seen_all == seen_now:  # nothing from the earlier year answered the search
                filed_all = total(code, SINCE)
                before = filed_all - (filed_now if filed_now is not None else total(code, now_since))
                if before > BLIND_FILED:
                    blind_from = SINCE
        if blind_from:
            found.append(f"{code}:{blind_from}")
    for key in found:
        code, since = key.split(":", 1)
        mine = [k for k in st["sweeps"] if k.split(":", 1)[0] == code]
        if any(k.split(":", 1)[1] <= since for k in mine):
            continue  # already read from that day or earlier
        for k in mine:
            del st["sweeps"][k]  # a sweep from later in the year is inside this one
        st["sweeps"][key] = {"page": 1, "started": None, "done": False}
    st["checked"] = today
    kv_set(db, "openstates_blind", st)
    db.commit()
    if found:
        log(f"[openstates] the search cannot see: {', '.join(found)}; read bill by bill instead")
    return used


def sweep_blind(db, http, cap, started, today):
    """Read the blind legislatures bill by bill, keeping what names AI or data centers."""
    st = blind_state(db)
    used, stored = 0, 0
    for key in sorted(st["sweeps"]):
        cur = dict(st["sweeps"][key])
        code, since = key.split(":", 1)
        began = cur.get("started") or today
        while used < cap and time.time() - started < SECONDS:
            params = {"jurisdiction": ocd_id(code), "created_since": since, "sort": "updated_desc",
                      "per_page": 20, "page": cur["page"], "include": INCLUDE}
            if cur.get("updated_since"):
                params["updated_since"] = cur["updated_since"]
            try:
                data = http.json(BASE, params=params)
            except HttpError as exc:
                if exc.status == 400 and cur["page"] > 1:
                    cur = {"page": 1, "started": None, "done": True, "updated_since": began}
                    break
                if "exceeded limit" in str(exc) or exc.status == 429:
                    st["sweeps"][key] = cur
                    kv_set(db, "openstates_blind", st)
                    return used, stored
                raise
            used += 1
            results = data.get("results", [])
            for b in results:
                words = f"{b.get('title') or ''} " + " ".join(a.get("abstract", "") for a in (b.get("abstracts") or []))
                if AI_TEXT.search(words):
                    stored += store(db, b)
            db.commit()
            last = (data.get("pagination") or {}).get("max_page") or cur["page"]
            if not results or cur["page"] >= last:
                cur = {"page": 1, "started": None, "done": True, "updated_since": began}
                break
            cur.update(page=cur["page"] + 1, started=began)
        st["sweeps"][key] = cur
    kv_set(db, "openstates_blind", st)
    if used:
        log(f"[openstates] blind legislatures read bill by bill: {used} requests, {stored} bills stored")
    return used, stored


def jurisdiction_code(j):
    jid = (j or {}).get("id", "")
    for part in jid.split("/"):
        if part.startswith(("state:", "district:", "territory:")):
            return part.split(":", 1)[1]
    if "country:us" in jid:
        return "us"  # Congress, which Open States files under the country and not a state
    return jid.rsplit("/", 1)[-1] or "state"


def run(db, state, mode):
    started = time.time()
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
    requests_used, added, seen, capped, ran_out = 0, 0, 0, False, False
    # Start where the last run stopped. Without this the first query spends the whole
    # allowance every day and the last ones are never searched at all.
    offset = kv_get(db, "openstates_offset", 0) % len(QUERIES)
    # The searches already caught up go first, every run: each is a page or two of what changed
    # since yesterday. Put behind a backfill, they waited days, and a bill signed this week read as
    # not passed until the backfill was done. The backfills share what is left, starting where the
    # last run stopped.
    caught_up = [q for q in QUERIES if q in cursors and not cursors[q].get("backfilling", True)]
    behind = [q for q in QUERIES if q not in caught_up]
    if QUERIES[offset] in behind:
        at = behind.index(QUERIES[offset])
        behind = behind[at:] + behind[:at]
    ordered = caught_up + behind
    if kv_get(db, "openstates_resweep") != RESWEEP:
        for c in cursors.values():
            if not c.get("backfilling", True) and (c.get("since") or "") > FIRST_RUN:
                c["since"] = FIRST_RUN
        kv_set(db, "openstates_resweep", RESWEEP)
    try:
        swept, found = prefile_sweep(db, http, min(PREFILE_CAP, budget), started)
        requests_used += swept
        added += found
    except Exception as exc:  # the searches below still run
        log(f"[openstates] pre-filed sweep failed: {str(exc)[:200]}")
    try:
        requests_used += find_blind(db, http, min(80, budget - requests_used), day)
        swept, found = sweep_blind(db, http, min(SWEEP_CAP, budget - requests_used), started, day)
        requests_used += swept
        added += found
    except Exception as exc:  # the searches below still run
        log(f"[openstates] reading blind legislatures failed: {str(exc)[:200]}")
    stopped_at = None
    for i, query in enumerate(ordered):
        cursor = cursors.get(query, {})
        backfilling = cursor.get("backfilling", True)
        page = cursor.get("page", 1)
        since = None if backfilling else cursor.get("since")
        run_started = iso()[:10]
        # The day this pass through the search began. A pass can take several runs, and the next one
        # reads from here, so a bill changed while it ran is read then rather than never.
        began = cursor.get("started") or (run_started if page == 1 else FIRST_RUN)
        while requests_used < budget:
            if time.time() - started > SECONDS:
                ran_out = True
                break
            params = {"q": query, "sort": "updated_desc", "per_page": 20, "page": page,
                      "include": INCLUDE, "created_since": SINCE}
            if since:
                params["updated_since"] = since
            try:
                data = http.json(BASE, params=params)
            except HttpError as exc:
                if exc.status == 400 and page > 1:
                    # Past the last page: the results shrank since the page count was read. That is
                    # the end of this pass, and staying on the page would ask for it every run.
                    cursors[query] = {"backfilling": False, "since": began, "page": 1}
                    break
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
                cursors[query] = {"backfilling": False, "since": began, "page": 1}
                break
            page += 1
            # where to pick up, for a backfill or for a day's changes too long for one run
            cursors[query] = {"backfilling": backfilling, "page": page, "since": cursor.get("since"),
                              "started": began}
        if ran_out:
            stopped_at = QUERIES.index(query)
            log(f"[openstates] out of time at '{query}'; the cursor keeps the place")
            break
        if capped:
            stopped_at = QUERIES.index(query)
            log("[openstates] allowance refused the run; backing off")
            break
        if requests_used >= budget:
            stopped_at = QUERIES.index(query)
            log(f"[openstates] request budget used; resuming '{query}' next run")
            break
        kv_set(db, "openstates_cursors", cursors)
    kv_set(db, "openstates_cursors", cursors)
    # With the day's changes read, what is left of the run goes to the pages of bills stored before
    # the collector asked Open States for them.
    linked = 0
    if not (capped or ran_out) and requests_used < budget:
        try:
            asked, linked, out_of_time = fill_links(db, http, min(LINK_CAP, budget - requests_used), started)
            requests_used += asked
            ran_out = ran_out or out_of_time
        except Exception as exc:  # the searches above are already saved
            log(f"[openstates] finding bills' own pages failed: {str(exc)[:200]}")
    kv_set(db, "openstates_spend", {"date": day, "n": used_today + requests_used})
    kv_set(db, "openstates_offset", stopped_at if stopped_at is not None else 0)
    # A refusal that cost nothing is their rolling window, not our budget: the run is worth
    # repeating later the same day rather than writing the day off. So is a run that stopped at
    # the clock with requests left: at six and a half seconds a request, one run reads 120 of the
    # day's 240, and the other half used to wait for tomorrow.
    if capped and not requests_used:
        retry_at(db, "openstates", RETRY_HOURS)
    elif ran_out and requests_used < budget:
        retry_at(db, "openstates", 1)
    else:
        retry_clear(db, "openstates")
    db.commit()
    state["added"] = added
    backlog = [q for q, c in cursors.items() if c.get("backfilling")] + [q for q in QUERIES if q not in cursors]
    state["message"] = (f"{seen} bills read in {requests_used} requests"
                        + (f"; {linked} bills' own pages found" if linked else "")
                        + ("; daily allowance reached" if capped else "")
                        + (f"; stopped at {SECONDS}s" if ran_out else "")) + (
        f"; still backfilling: {', '.join(backlog)}" if backlog else "")


def measure_id(b):
    """The row a bill from Open States lands on: Congress.gov's id for a federal bill, so one bill is
    one row whoever saw it first, and otherwise Open States' own."""
    code = jurisdiction_code(b.get("jurisdiction") or {})
    federal = code == "us" and congress_id(b.get("session"), b.get("identifier"))
    return federal or "os-" + (b.get("id", "").rsplit("/", 1)[-1] or f"{code}-{b.get('session')}-{b.get('identifier')}")


# Where a reader should land for a bill: its page on the legislature's own site. Open States' own
# pages now redirect into Plural's app, which is a blank page without script and is often blank with
# it, and a reader who got there took its copied text for this site's reading of the bill. Open
# States records the legislature's links as the bill's sources, but a scraper records everything it
# read: data feeds (Texas's FTP, New York's API, Georgia's and Indiana's services), the search or
# listing page it started from (Alabama, Rhode Island, Nebraska, Vermont) and the sponsors' pages
# (Mississippi). So a source counts only as a web page with a number in it, a page naming the bill's
# own number is preferred, and a search or listing page comes last. With no page at all, the bill's
# latest text on the legislature's site will do.
DATA_LINK = re.compile(r"^ftp:|/api/|//api\.|/odata|webservice|\.asmx\b|/bulkdata|leg-databases|"
                       r"\.(?:xml|json|csv|zip|txt)(?:[?#]|$)", re.I)
LISTING = re.compile(r"search|/load|by_date|listing", re.I)


def web_page(url):
    return url.lower().startswith(("http://", "https://")) and not DATA_LINK.search(url)


def pick_link(b):
    """The legislature's own page for this bill, or its latest text there; "" when there is neither."""
    digits = re.findall(r"\d+", b.get("identifier") or "")
    number = digits[-1].lstrip("0") or "0" if digits else ""
    best, best_score = "", None
    for source in b.get("sources") or []:
        url, note = (source.get("url") or "").strip(), (source.get("note") or "").lower()
        if not web_page(url) or "api" in re.findall(r"[a-z]+", note) or "json" in note:
            continue
        if not re.search(r"\d", url.split("//", 1)[-1].split("/", 1)[-1]):
            continue  # a front page or a search form, not a page for this bill
        score = ((4 if number and re.search(rf"(?<!\d)0*{number}(?!\d)", url) else 0)
                 - (5 if LISTING.search(url) else 0) + (1 if url.lower().startswith("https") else 0))
        if best_score is None or score > best_score:
            best, best_score = url, score
    if best:
        return best
    versions = sorted(b.get("versions") or [], key=lambda v: v.get("date") or "")
    for version in reversed(versions):
        links = [l for l in version.get("links") or [] if web_page((l.get("url") or "").strip())]
        for kind in ("text/html", "application/pdf"):
            for l in links:
                if (l.get("media_type") or "").lower() == kind:
                    return l["url"].strip()
        if links:
            return links[0]["url"].strip()
    return ""


def abstract_text(b):
    return " ".join(a.get("abstract", "") for a in (b.get("abstracts") or []))


def store(db, b):
    j = b.get("jurisdiction") or {}
    code = jurisdiction_code(j)
    ident = measure_id(b)
    # Only a record that came with its sources can say where the bill's own page is; one without
    # them (an older reply, a test) leaves whatever is stored.
    link = pick_link(b) if "sources" in b else None
    row = db.execute("SELECT updated, source, source_url FROM measures WHERE id=?", (ident,)).fetchone()
    if row and row["source"] == "Congress.gov":
        return 0  # the same bill, and Congress.gov carries the summary, the sponsors and the actions
    if row and row["updated"] == b.get("updated_at"):
        if link is not None and row["source_url"] != link:
            db.execute("UPDATE measures SET source_url=? WHERE id=?", (link, ident))
        return 0
    abstracts = abstract_text(b)
    sponsors = [s.get("name") for s in (b.get("sponsorships") or []) if s.get("primary")] or \
               [s.get("name") for s in (b.get("sponsorships") or [])][:3]
    kind = "resolution" if "resolution" in " ".join(b.get("classification") or []) else "bill"
    record = {
        "id": ident, "kind": kind, "jurisdiction": code,
        "jurisdiction_name": "Congress" if code == "us" else (j.get("name") or code.upper()),
        "session": b.get("session"), "identifier": b.get("identifier"), "title": b.get("title"),
        "summary": abstracts[:SUMMARY_MAX],
        "status": status_from_action(b.get("latest_action_description"), b.get("identifier"), kind),
        "latest_action": b.get("latest_action_description"), "latest_action_date": (b.get("latest_action_date") or "")[:10],
        "introduced_date": (b.get("first_action_date") or b.get("created_at") or "")[:10],
        "url": b.get("openstates_url"), "sponsors": ", ".join(s for s in sponsors if s),
        "source": "Open States", "updated": b.get("updated_at"), "first_seen": iso(),
    }
    if link is not None:
        record["source_url"] = link
    return upsert(db, "measures", record)


LINK_CAP = 100  # requests per run for the pages of bills stored before the collector asked for them


def fill_links(db, http, cap, started):
    """Find the legislature's own page for the bills on the site that were stored without one.

    Asked twenty at a time, by jurisdiction, session and bill number, the most the API takes in one
    request: the 1,780 bills from Open States on the site at the time were 141 requests. The ones a
    reader is likeliest to open go first: those carrying a label, then the most recently acted on. A
    bill the answer leaves out keeps its Open States address, marked as asked, and is not asked again.
    The same answer carries the summary, which replaces a stored one cut short by the old limit.
    Returns (requests, links found, whether the clock stopped it).
    """
    if cap <= 0:
        return 0, 0, False
    labelled = {r[0] for r in db.execute("SELECT DISTINCT target FROM tags WHERE kind IN ('fear', 'control', 'agency')")}
    todo = [m for m in measures_in_scope(db) if m["source"] == "Open States" and m.get("source_url") is None
            and m["identifier"] and m["session"]]
    todo.sort(key=lambda m: m["latest_action_date"] or "", reverse=True)
    todo.sort(key=lambda m: m["id"] not in labelled)
    groups = collections.OrderedDict()
    for m in todo:
        groups.setdefault((m["jurisdiction"], m["session"]), []).append(m)
    used, found, lengthened, hosts = 0, 0, 0, collections.Counter()
    try:
        for (code, session), ms in groups.items():
            for i in range(0, len(ms), 20):
                if used >= cap:
                    return used, found, False
                if time.time() - started > SECONDS:
                    return used, found, True
                chunk = ms[i:i + 20]
                try:
                    data = http.json(BASE, params={"jurisdiction": ocd_id(code), "session": session,
                                                   "identifier": [m["identifier"] for m in chunk],
                                                   "include": ["sources", "versions", "abstracts"], "per_page": 20})
                except HttpError as exc:
                    if "exceeded limit" in str(exc) or exc.status == 429:
                        return used, found, False
                    if exc.status != 400:
                        raise
                    data = {"results": []}  # a session or number it will not take: asked, and none given
                used += 1
                results = data.get("results") or []
                links = {measure_id(b): pick_link(b) for b in results}
                whole = {measure_id(b): abstract_text(b) for b in results}
                for m in chunk:
                    link = links.get(m["id"], "")
                    db.execute("UPDATE measures SET source_url=? WHERE id=?", (link, m["id"]))
                    found += bool(link)
                    if link:
                        hosts[urllib.parse.urlparse(link).netloc.removeprefix("www.")] += 1
                    # A summary stored when only 6,000 characters were kept is replaced by the whole of
                    # it. The new text is read again, and its labels checked again, like any other.
                    kept, text = m.get("summary") or "", whole.get(m["id"], "")
                    if len(kept) >= OLD_SUMMARY_CUT and len(text) > len(kept):
                        db.execute("UPDATE measures SET summary=? WHERE id=?", (text[:SUMMARY_MAX], m["id"]))
                        lengthened += 1
                db.commit()
        return used, found, False
    finally:
        # Where the links went, in the run's log: a wrong pick shows as a host nobody expects.
        if hosts:
            log("[openstates] bills' own pages found on " + ", ".join(f"{h} ({n})" for h, n in hosts.most_common(12)))
        if lengthened:
            log(f"[openstates] {lengthened} summaries cut at {OLD_SUMMARY_CUT:,} characters replaced by the whole")


def prefile_sweep(db, http, cap, started):
    """Read the pre-filed bills each search has not reached. Returns (requests, rows stored)."""
    state = kv_get(db, "openstates_prefile", {})
    used, stored = 0, 0
    for query in QUERIES:  # data center included, now that data center bills count whatever they say
        cur = state.get(query, {"page": 1, "done": False})
        while not cur["done"] and used < cap and time.time() - started < SECONDS:
            try:
                data = http.json(BASE, params={"q": query, "sort": "first_action_asc", "per_page": 20,
                                               "page": cur["page"], "include": INCLUDE,
                                               "created_since": PREFILE_SINCE})
            except HttpError as exc:
                if "exceeded limit" in str(exc) or exc.status == 429:
                    state[query] = cur
                    kv_set(db, "openstates_prefile", state)
                    return used, stored
                # past the last page, or a request this API refuses: either way, not again
                log(f"[openstates] pre-filed sweep of '{query}' ends: {str(exc)[:160]}")
                cur["done"] = True
                break
            used += 1
            results = data.get("results", [])
            for b in results:
                if (b.get("first_action_date") or "")[:10] >= SINCE:
                    cur["done"] = True  # the rest were filed in the report's own period
                    break
                stored += store(db, b)
            db.commit()
            if not cur["done"]:
                last = (data.get("pagination") or {}).get("max_page") or cur["page"]
                if not results or cur["page"] >= last:
                    cur["done"] = True
                else:
                    cur["page"] += 1
        state[query] = cur
    kv_set(db, "openstates_prefile", state)
    if used:
        log(f"[openstates] pre-filed sweep: {used} requests, {stored} bills stored")
    return used, stored
