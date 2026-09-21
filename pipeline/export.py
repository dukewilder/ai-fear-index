"""Turn the database into site_data.json (the only thing the site reads) and public CSVs.

Every number here is computed from stored records. When a section has no data yet it is
left empty, and the page hides it instead of showing a guess.
"""
import collections
import csv
import datetime as dt
import json
import math
import pathlib
import re
import urllib.parse

from .common import (FEDERAL, REPO_URL, SITE_URL, STATES, Entities, config, fear_keywords, fears_mentioned, iso,
                     kv_get, kv_set, measures_in_scope, name_key, now, own_post, sha, spell, tidy_headline,
                     where_counted)

SMALL = {"of", "and", "for", "the", "in", "on", "to", "a", "an", "at", "by"}
ACRONYMS = {"AI", "US", "USA", "UK", "EU", "PAC", "TV", "IT", "AG", "DC", "PC", "ML", "IP", "HR"}
STATUS_LABEL = {"passed": "Passed", "pending": "Pending", "failed": "Failed"}
KIND_LABEL = {"bill": "Bill", "resolution": "Resolution", "rule": "Rule", "order": "Executive order"}
# Organizations whose stated purpose is the fear, as opposed to companies and trade groups
# whose lobbying touches it among everything else they work on.
MISSION_TYPES = {"Advocacy group", "Foundation", "Pollster"}


def count(n, one, many=None):
    """A number and its noun, in the number's own grammar."""
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def money(v):
    v = float(v or 0)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= size:
            s = f"{v / size:.1f}".rstrip("0").rstrip(".")
            return f"${s}{suffix}"
    return f"${v:,.0f}"


def compact(v):
    v = float(v or 0)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= size:
            return f"{v / size:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{v:,.0f}"


def cut(text, n):
    """Shorten to n characters on a word boundary, with a single ellipsis."""
    text = (text or "").strip()
    if len(text) <= n:
        return text
    head = text[:n].rsplit(" ", 1)[0].rstrip(",;:")
    return head + "…"


def short_type(committee_type):
    t = (committee_type or "").lower()
    if "super pac" in t or "independent expenditure" in t:
        return "Super PAC"
    if "pac" in t:
        return "PAC"
    return committee_type or "Committee"


def gave(line_number, known=None):
    """FEC Form 3X, Schedule A, by the line the filing puts a receipt on.

    Lines 11 and 12 are money given to the committee. Line 17, other receipts, is mostly
    the committee's own bank interest, so it counts only when the payer is an organization
    this report already tracks: a group moving its own money into a super PAC is a source,
    a bank paying that PAC interest on its balance is not.
    """
    code = (line_number or "").strip().upper()
    return code.startswith(("11", "12")) or (code.startswith("17") and bool(known))


def donor_name(name):
    """FEC files people surname first. Say it the way a person would."""
    raw = (name or "").strip()
    if raw.count(",") == 1:
        last, first = [part.strip() for part in raw.split(",")]
        if first and last and not re.search(r"\b(INC|LLC|CORP|CO|LTD|LP|LLP|PBC|FUND|PAC|COMMITTEE)\b", first, re.I):
            raw = f"{first} {last}"
    return nice_name(raw)


def when(day):
    try:
        return dt.date.fromisoformat(day).strftime("%b %Y")
    except (TypeError, ValueError):
        return ""


def nice_name(name):
    name = re.sub(r",?\s+(INC|LLC|L\.L\.C\.|CORP|CORPORATION|CO|LTD|LP|LLP|PBC)\.?$", "", (name or "").strip(), flags=re.I)
    if not name.isupper():
        return name
    words = []
    for i, w in enumerate(name.split()):
        if w.strip(".,") in ACRONYMS or "&" in w or (len(w) <= 3 and not re.search(r"[AEIOU]", w)):
            words.append(w)
        elif i and w.lower() in SMALL:
            words.append(w.lower())
        else:
            words.append(w.capitalize())
    return " ".join(words)


def and_list(parts):
    """a, b and c"""
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def federal_word(codes):
    """Congress when every federal measure in the set is a bill, the federal government when an
    agency rule or an executive order is among them. Neither when there are none."""
    fed = set(codes) & FEDERAL
    return "" if not fed else "Congress" if fed == {"us"} else "the federal government"


def where_phrase(codes):
    """Where a set of measures was filed, with states counted as states.

    "51 states" was 50 states and Puerto Rico, and "plus Congress" covered executive orders. States
    are counted, any other place is named, and the federal government is only called Congress when
    Congress is all of it.
    """
    states, others, _ = where_counted(codes)
    return and_list([count(states, "state") if states else ""] + others + [federal_word(codes)])


def slugify(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").lower())).strip("-")[:60] or "org"


def quarter_of(date_str):
    d = dt.date.fromisoformat(date_str[:10])
    return d.year, (d.month - 1) // 3 + 1


def recent_quarters(n=4, today=None):
    """The last n quarters whose reports are due, which is what "past year" means for lobbying.

    A quarter's reports are due twenty days after it ends. Counting the quarter in progress, which
    holds a handful of early filings, made the year three quarters of filings and a stub, and the
    most recent finished quarter is left out until its deadline has passed.
    """
    today = today or now().date()
    y, q = today.year, (today.month - 1) // 3 + 1
    out = []
    while len(out) < n:
        q -= 1
        if q == 0:
            y, q = y - 1, 4
        end = dt.date(y, 12, 31) if q == 4 else dt.date(y, 3 * q + 1, 1) - dt.timedelta(days=1)
        if today >= end + dt.timedelta(days=21):
            out.append((y, q))
    return set(out)


def reported(filings):
    """Dollars reported on a set of filings, without counting a client's money twice.

    A client that lobbies in-house files its own report, and the Lobbying Disclosure Act has it
    include what it paid outside firms that quarter. Those firms file too, each reporting the same
    money as income. Where a client's own report is in the set, its firms' reports for that quarter
    add nothing to the total. They are still filings, and still counted as filings.
    """
    own = {(r["client_key"], r["year"], r["quarter"]) for r in filings if r.get("self")}
    return sum(r["amount"] or 0 for r in filings
               if r.get("self") or (r["client_key"], r["year"], r["quarter"]) not in own)


def export(db, out_dir, base=""):
    out = pathlib.Path(out_dir)
    (out / "public").mkdir(parents=True, exist_ok=True)
    ents = Entities()
    fears, controls = config("fears"), config("controls")
    fear_by, control_by = {f["slug"]: f for f in fears}, {c["slug"]: c for c in controls}
    kw = fear_keywords()
    today = now().date()
    week_ago = (today - dt.timedelta(days=7)).isoformat()

    # ---------------- measures and their agreed tags
    tags = collections.defaultdict(lambda: {"fear": set(), "control": set(), "agency": set()})
    for t in db.execute("SELECT target, kind, value FROM tags"):
        if t["kind"] in ("fear", "control", "agency"):
            tags[t["target"]][t["kind"]].add(t["value"])
    # The same list the daily card counts over: suppressions applied, a carried-over bill once.
    measures = measures_in_scope(db)
    for m in measures:
        tg = tags.get(m["id"], {"fear": set(), "control": set(), "agency": set()})
        m["fears"] = sorted(s for s in tg["fear"] if s in fear_by)
        m["controls"] = sorted(s for s in tg["control"] if s in control_by)
        m["agencies"] = sorted(tg["agency"])
    suppressed = set(config("suppress").get("urls", []))
    controlled = [m for m in measures if m["controls"]]

    # ---------------- lobbying, de-duplicated to the latest filing per registrant, client, quarter
    latest = {}
    for r in db.execute("SELECT * FROM lobbying WHERE amount IS NOT NULL ORDER BY posted"):
        latest[(r["registrant"], r["client_key"], r["year"], r["quarter"])] = dict(r)
    lob = [r for r in latest.values() if r["url"] not in suppressed]
    window = recent_quarters(4)
    lob_recent = [r for r in lob if (r["year"], int(r["quarter"][1]) if r["quarter"] else 0) in window]
    for r in lob:
        r["fears"] = fears_mentioned(r["issues"], kw)
        r["entity"] = ents.match_name(r["client"])
        r["self"] = bool(r["client_key"]) and name_key(r["registrant"]) == r["client_key"]

    # ---------------- funders: AI lobbying plus election money
    orgs = {}

    def org(key, display, entity=None):
        o = orgs.get(key)
        if not o:
            ent = ents.by_slug.get(entity) if entity else None
            o = orgs[key] = {"key": key, "name": ent["name"] if ent else nice_name(display), "entity": entity,
                             "type": ent["type"] if ent else None, "flag": (ent or {}).get("flag"),
                             "lobbying": 0.0, "election": 0.0, "fears": set(), "filings": []}
        return o

    # Only filings whose issue text mentions a tracked fear count here. A filing that
    # mentions AI without naming a fear stays on the org's own page and in the CSV.
    for r in lob_recent:
        if not r["fears"] or not (r["entity"] or r["client_key"]):
            continue
        o = org(r["entity"] or r["client_key"], r["client"], r["entity"])
        o["filings"].append(r)
        o["n"] = o.get("n", 0) + 1
        o["fears"].update(r["fears"])
        o["type"] = o["type"] or "Lobbying client"
    for o in orgs.values():
        o["lobbying"] = reported(o["filings"])
    # Two different things get called lobbying. An advocacy group's filing is money spent
    # on the fear itself; a trade group's filing covers everything it works on that quarter
    # and merely mentions one. Ranking them together buries the first under the second.
    spending = sorted((o for o in orgs.values() if o["lobbying"] > 0), key=lambda o: -o["lobbying"])
    for o in spending:
        o["total"] = o["lobbying"]
        o["slug"] = o["entity"] or slugify(o["name"])
    ranked = [o for o in spending if o["type"] in MISSION_TYPES]
    industry_ranked = sorted((o for o in spending if o["type"] not in MISSION_TYPES),
                             key=lambda o: (-o.get("n", 0), -o["lobbying"]))
    for group in (ranked, industry_ranked):
        for i, o in enumerate(group, 1):
            o["rank"] = i
    top, industry_top = ranked[:30], industry_ranked[:30]

    def money_row(o):
        return {"rank": o["rank"], "name": o["name"], "slug": o["slug"], "type": o["type"],
                "fears": str(len(o["fears"])), "amount": money(o["total"]), "flag": o["flag"]}

    funders = [money_row(o) for o in top]
    industry = [{**money_row(o), "amount": f"{o.get('n', 0)} filing{'s' if o.get('n', 0) != 1 else ''}"}
                for o in industry_top]

    # ---------------- election money, kept separate and counted by committee
    committees = [dict(c) for c in db.execute("SELECT * FROM committees")]
    receipts = [dict(r) for r in db.execute("SELECT * FROM fec WHERE kind='receipt'")]
    # A committee's own bank interest arrives on Schedule A beside the donations. It is money the
    # committee has, but nobody raised it, so the same line rule that names the donors below counts
    # it here too, and the headline figure means what the words under it say.
    raised = collections.Counter()
    for r in receipts:
        if gave(r.get("line_number"), ents.match_name(r["counterparty"] or "")):
            raised[r["committee_id"]] += r["amount"] or 0
    com_orgs = {}
    for c in committees:
        key = c["entity"] or name_key(c["name"])
        ent = ents.by_slug.get(c["entity"]) if c["entity"] else None
        o = com_orgs.setdefault(key, {
            "key": key, "name": ent["name"] if ent else nice_name(c["name"]), "entity": c["entity"],
            "type": short_type(c["committee_type"]), "flag": (ent or {}).get("flag"),
            "lobbying": 0.0, "election": 0.0, "fears": set(), "committee_ids": []})
        o["election"] += max(c["independent_expenditures"] or 0, raised.get(c["id"], 0))
        o["committee_ids"].append(c["id"])
    com_ranked = sorted((o for o in com_orgs.values() if o["election"] > 0), key=lambda o: -o["election"])
    for i, o in enumerate(com_ranked, 1):
        o["rank"], o["total"] = i, o["election"]
        o["slug"] = o["entity"] or slugify(o["name"])
    election = [{"rank": o["rank"], "name": o["name"], "slug": o["slug"], "type": o["type"],
                 "fears": "0", "amount": money(o["total"]), "flag": o["flag"]} for o in com_ranked[:30]]

    # ---------------- and the names behind it: a super PAC is only as anonymous as its receipts
    side = {c["id"]: ((ents.by_slug.get(c["entity"]) or {}).get("name") or nice_name(c["name"]))
            for c in committees}
    com_entity = {c["id"]: c["entity"] for c in committees}
    # A committee banks its money, so its own bank interest arrives on Schedule A beside the
    # donations. Once the filings say which line each sits on, only what was given is listed.
    filed = any((r.get("line_number") or "") for r in receipts)
    pairs = {}
    for r in receipts:
        known = ents.match_name(r["counterparty"] or "")
        if filed and not gave(r.get("line_number"), known):
            continue
        if known and known == com_entity.get(r["committee_id"]):
            known = None  # one arm of a network paying another: say which arm, not the network
        who = ents.by_slug[known]["name"] if known else donor_name(r["counterparty"])
        if not who or not (r["amount"] or 0) > 0:
            continue
        to = side.get(r["committee_id"]) or nice_name(r["committee_name"] or "")
        cell = pairs.setdefault((who, to), {"amount": 0.0, "date": "", "url": r["url"]})
        cell["amount"] += r["amount"]
        cell["date"] = max(cell["date"], r["date"] or "")
    donors = sorted(({"name": who, "committee": to, "amount": money(cell["amount"]), "when": when(cell["date"]),
                      "url": cell["url"], "n": cell["amount"]}
                     for (who, to), cell in pairs.items()), key=lambda x: -x["n"])
    for i, giver in enumerate(donors, 1):
        giver["rank"] = i
    page_slugs = ({o["slug"] for o in top} | {o["slug"] for o in industry_top}
                  | {o["slug"] for o in com_ranked[:30]})

    # ---------------- agencies that would gain authority
    # Counted over the measures that carry a control. An office named in a measure that imposes
    # nothing is not holding a new power, and the front page says these offices would hold one.
    agency_count, agency_name = collections.Counter(), {}
    agency_controls = collections.defaultdict(collections.Counter)
    for m in controlled:
        for a in m["agencies"]:
            k = name_key(a)
            if not k:
                continue
            agency_count[k] += 1
            agency_name.setdefault(k, a.strip())
            for c in m["controls"]:
                agency_controls[k][c] += 1

    def agency_rows(counter, limit):
        rows = []
        for i, (k, n) in enumerate(counter.most_common(limit), 1):
            name = agency_name[k]
            top_c = agency_controls[k].most_common(1)
            rows.append({"rank": i, "name": name, "slug": None,
                         "type": "Attorney general" if "attorney general" in k else "Agency",
                         "gains": control_by[top_c[0][0]]["chip"] if top_c else "",
                         "score": str(n), "unit": "bills"})
        return rows

    beneficiaries = agency_rows(agency_count, 30)

    # ---------------- controls ranking
    control_rows = []
    for c in controls:
        ms = [m for m in measures if c["slug"] in m["controls"]]
        fear_mix = collections.Counter(fs for m in ms for fs in m["fears"])
        control_rows.append({"slug": c["slug"], "name": c["name"], "bills": str(len(ms)),
                             "definition": c["definition"],
                             "fears": [fear_by[fs]["name"] for fs, _ in fear_mix.most_common(3)],
                             "passed": str(sum(m["status"] == "passed" for m in ms)),
                             "pending": str(sum(m["status"] == "pending" for m in ms)), "n": len(ms)})
    control_rows.sort(key=lambda r: -r["n"])
    for i, r in enumerate(control_rows, 1):
        r["rank"] = i

    # ---------------- fears: every channel counted, then scored
    news30, news_end = window_30(db, "news", today)
    wiki30, wiki_end = window_30(db, "wiki", today)
    news_stop = (stalled({"news30": news_end}, "news30", today) or "").removeprefix("to ")
    # How the news count is dated wherever it appears: the last 30 days, or the 30 days to the last
    # day GDELT has counted when it has fallen behind.
    counted = days_counted(db, "news", news_end)
    base = f"the 30 days to {news_stop}" if news_stop else "the last 30 days"
    news_span = f"in {base}" if counted >= 30 else f"from the {counted} of {base} that GDELT has counted"
    # Statements that still exist, were judged about AI, and are the organization's own words. The
    # count read every label on a post, including labels on posts long since deleted.
    post_fears = collections.Counter(t["value"] for t in db.execute(
        "SELECT t.value, p.entity, p.url FROM tags t JOIN posts p ON t.target = 'post:' || p.id "
        "JOIN tag_runs tr ON tr.target = t.target WHERE t.kind='fear' AND tr.ai_related = 1")
        if own_post(t["entity"], t["url"], ents))
    filings_by_fear, mission_filings = collections.Counter(), collections.defaultdict(list)
    for r in lob_recent:
        mission = bool(r["entity"]) and ents.by_slug[r["entity"]]["type"] in MISSION_TYPES
        for slug in r["fears"]:
            filings_by_fear[slug] += 1
            if mission:
                mission_filings[slug].append(r)
    advocacy_by_fear = {slug: reported(rs) for slug, rs in mission_filings.items()}
    fear_stats = {}
    for f in fears:
        slug = f["slug"]
        ms = [m for m in measures if slug in m["fears"]]
        fear_stats[slug] = {
            "measures": ms, "controls": sum(len(m["controls"]) for m in ms),
            "with_controls": sum(1 for m in ms if m["controls"]),
            "passed": sum(1 for m in ms if m["status"] == "passed"),
            "states": sorted({m["jurisdiction"] for m in ms if m["jurisdiction"] in STATES}),
            "codes": sorted({m["jurisdiction"] for m in ms}),
            "congress": sum(1 for m in ms if m["jurisdiction"] in FEDERAL),
            "federal": any(m["jurisdiction"] in FEDERAL for m in ms),
            "new_week": sum(1 for m in ms if (m["introduced_date"] or "") >= week_ago),
            "filings": filings_by_fear[slug], "advocacy": advocacy_by_fear.get(slug, 0), "statements": post_fears[slug],
            "news30": int(news30.get(slug, 0)), "wiki30": int(wiki30.get(slug, 0)),
            # An article on file and no readership yet are different things. A fear added today has
            # both a Wikipedia article and nothing collected from it until the next daily pass, and
            # a zero there would be a claim nobody has checked rather than a count.
            "has_wiki": bool(f.get("wikipedia")) and slug in wiki30,
            "wiki_article": bool(f.get("wikipedia")),
            "news_span": news_span,
        }
    for slug, score in index_scores(fears, fear_stats).items():
        fear_stats[slug]["index"] = score
    order = sorted(fears, key=lambda f: (-fear_stats[f["slug"]]["index"], -len(fear_stats[f["slug"]]["measures"])))
    prev = previous_snapshot(db, today)
    fear_rows = []
    for i, f in enumerate(order, 1):
        st = fear_stats[f["slug"]]
        move = None
        if prev and f["slug"] in prev.get("fear_rank", {}):
            delta = prev["fear_rank"][f["slug"]] - i
            move = {"dir": "up", "text": str(delta)} if delta > 0 else {"dir": "down", "text": str(-delta)} if delta < 0 else None
        fear_rows.append({"rank": i, "name": f["name"], "slug": f["slug"], "score": str(st["index"]),
                          "bar": max(3, st["index"]), "move": move, "line": fear_line(st),
                          "new_week": st["new_week"], "segments": segments(st)})
    grid = build_grid(order, fear_stats, {"news30": news_end, "wiki30": wiki_end}, today)

    # ---------------- feed
    feed = [i for i in build_feed(db, ents, measures, lob, receipts, today) if i["url"] not in suppressed]

    kinds = collections.Counter(i["type"] for i in feed)
    feed_types = [[key, label, f"{(len(feed) if key == 'all' else kinds[key]):,}"]
                  for key, label in FEED_TYPES if key == "all" or kinds[key]]

    # ---------------- exhibit: the loudest fear right now, always on
    exhibit = build_exhibit(db, order, fear_stats, today, suppressed)

    # ---------------- headline
    cutoffs = [today - dt.timedelta(days=7 * k) for k in range(12, -1, -1)]
    cum = [sum(1 for m in measures if (m["introduced_date"] or "9999") <= c.isoformat()) for c in cutoffs]
    lo, hi = min(cum), max(cum)
    trend = [50 if hi == lo else round(15 + 70 * (v - lo) / (hi - lo)) for v in cum]
    series = [[c.isoformat(), v] for c, v in zip(cutoffs, cum)]
    runs_key = f"runs:{today.isoformat()}"
    runs_today = (kv_get(db, runs_key, 0) or 0) + 1
    kv_set(db, runs_key, runs_today)
    new_week = sum(1 for m in measures if (m["introduced_date"] or "") >= week_ago)
    jurisdictions = {m["jurisdiction"] for m in measures}
    n_states, other_places, _ = where_counted(jurisdictions)
    beyond = and_list(other_places + [federal_word(jurisdictions)])
    index = {"value": f"{len(measures):,}", "suffix": "",
             "text": "bills, rules and orders about AI since January 2025",
             "second": f"{len(controlled):,}",
             "second_text": "of them would put AI, the people building it or the people using it under new government control",
             "where": where_phrase(jurisdictions),
             "change": f"▲ {new_week:,} new this week" if new_week else None, "trend": trend, "series": series,
             "series_label": "Bills, rules and orders about AI, running total over the last 90 days",
             "total_measures": len(measures), "controlled": len(controlled)}
    index["chain"] = [c for c in [
        [compact(sum(int(v) for v in wiki30.values())), f"Wikipedia views on the {spell(len(fears))} fears, last 30 days"]
        if wiki30 else None,
        [f"{len(measures):,}", "bills, rules and orders about AI since January 2025"] if measures else None,
        [f"{len(controlled):,}", "of them would put AI under new government control"] if controlled else None,
        [f"{len(agency_count):,}", "agencies and officials they would hand new power"] if agency_count else None,
    ] if c]
    polls = live_polls(config("polls"), today)
    site = config("site")

    # ---------------- the wall of numbers
    filings_naming = sum(1 for r in lob_recent if r["fears"])
    advocacy_total = sum(o["lobbying"] for o in ranked)
    election_total = sum(o["election"] for o in com_ranked)
    election_spent = sum(c["independent_expenditures"] or 0 for c in committees)
    news_total = sum(int(v) for v in news30.values())
    all_filings = db.execute("SELECT COUNT(*) c FROM lobbying").fetchone()["c"]
    all_measures = db.execute("SELECT COUNT(*) c FROM measures").fetchone()["c"]
    # The money leads, largest first: it is the number that says most in one glance. What the
    # committees hold is money raised, and the line says spent only once one of them has spent.
    # Two of them are super PACs and the rest are ordinary and hybrid PACs, so the line says
    # committees: a number that is right to the dollar is still wrong if its label is.
    election_words = ("raised by political committees working on AI policy this cycle"
                      if not election_spent else
                      "raised by political committees working on AI policy this cycle, "
                      f"{money(election_spent)} of it spent")
    spent = sorted(((election_total, election_words),
                    (advocacy_total,
                     "reported by advocacy groups on lobbying filings that name a fear, past year")),
                   reverse=True)
    numbers = [n for n in [
        [money(spent[0][0]), spent[0][1]] if spent[0][0] else None,
        [money(spent[1][0]), spent[1][1]] if spent[1][0] else None,
        [f"{sum(len(m['controls']) for m in measures):,}",
         f"separate government controls across the {len(controlled):,} measures that carry one"] if controlled else None,
        [f"{n_states}", "states" + (f", plus {beyond}," if beyond else "") + " with AI measures on the books or in motion"]
        if n_states else None,
        [f"{filings_naming:,}", f"federal lobbying filings naming one of the {spell(len(fears))} fears, past year"] if filings_naming else None,
        [f"{news_total:,}", f"news articles on the {spell(len(fears))} fears {news_span}"] if news_total else None,
    ] if n]

    # ---------------- in their own words: the quotes the labels rest on
    quotes = own_words(db, measures, fear_by, control_by, suppressed)

    # ---------------- already law
    passed = sorted((m for m in controlled if m["status"] == "passed"),
                    key=lambda m: (-len(m["controls"]), m["latest_action_date"] or ""))
    already_law = [{"name": f"{m['identifier'] or 'Measure'}, {m['jurisdiction_name']}", "title": cut(m["title"] or "", 140),
                    "url": m["url"], "date": law_date(m, today),
                    "controls": [control_by[c]["chip"] for c in m["controls"]],
                    "fears": [fear_by[fs]["name"] for fs in m["fears"]]} for m in passed[:40]]
    law_total = len(passed)
    # How many measures passed in all, so the section's own number reads as the share it is. A
    # reader who sees "34 passed" takes it for every AI measure that has passed; it is the ones
    # carrying a control, out of a larger number that did not.
    law_passed = sum(1 for m in measures if m["status"] == "passed")

    # ---------------- where it is happening: every jurisdiction, ranked
    by_state = collections.defaultdict(list)
    for m in measures:
        by_state[m["jurisdiction"]].append(m)
    state_rows = []
    for j, ms in by_state.items():
        fear_mix = collections.Counter(fs for m in ms for fs in m["fears"])
        # Rules and orders come from several offices under one code, and the row took the name of
        # whichever came first: a list of executive orders labelled Food and Drug Administration.
        name = "Federal agencies and the President" if j == "us-exec" else ms[0]["jurisdiction_name"]
        state_rows.append({"code": j, "name": name, "n": len(ms),
                           "bills": f"{len(ms):,}", "controlled": str(sum(1 for m in ms if m["controls"])),
                           "passed": str(sum(1 for m in ms if m["status"] == "passed")),
                           "top_fear": fear_by[fear_mix.most_common(1)[0][0]]["name"] if fear_mix else None})
    state_rows.sort(key=lambda r: -r["n"])
    for i, r in enumerate(state_rows, 1):
        r["rank"] = i

    # ---------------- fear pages and org pages
    fear_pages = [fear_page(f, i + 1, len(fears), fear_stats, lob, feed, today, db, control_by, page_slugs, ents)
                  for i, f in enumerate(order)]
    for fp in fear_pages:
        st = fear_stats[fp["slug"]]
        fp["quotes"] = own_words(db, st["measures"], fear_by, control_by, suppressed, per_fear=fp["slug"], limit=5)
        fp["receipt"] = " ".join(x for x in [
            f"{fp['name']}: {fp['score']} of 100 on the Fear Index.",
            count(len(st["measures"]), "bill") + " since January 2025"
            + (f" in {where_phrase(st['codes'])}." if st["codes"] else ".") if st["measures"] else "",
            f"{st['controls']:,} new government controls written into them." if st["controls"] else "",
            f"{st['filings']:,} federal lobbying filings name it." if st["filings"] else "",
            f"{st['wiki30']:,} Wikipedia views this month." if st["wiki30"] else "",
            f"{SITE_URL}/fear/{fp['slug']}/"] if x)
    org_pages = [org_page(o, len(ranked), lob, receipts, committees, feed, measures, control_by) for o in top]
    org_pages += [org_page(o, len(industry_ranked), lob, receipts, committees, feed, measures, control_by, "industry")
                  for o in industry_top]
    org_pages += [org_page(o, len(com_ranked), lob, receipts, committees, feed, measures, control_by, "election")
                  for o in com_ranked[:30]]

    # ---------------- sources and status
    status = {r["source"]: dict(r) for r in db.execute("SELECT * FROM status")}
    feeds = ", ".join(sorted(src["name"] for src in config("news")))
    source_rows = []
    for key, what, where in SOURCES:
        s = status.get(key)
        # The headline row names the publishers rather than describing them, because the rule for
        # the front page headline says it comes from one of these and a reader should see which.
        if where == "NEWS_FEEDS":
            where = f"Read in full: {feeds}"
        source_rows.append([what, where, (s or {}).get("last_ok"), bool(s and s["ok"])])
    ok_count = sum(1 for s in status.values() if s["ok"])
    # A collector that knows it has not finished says so in its own message. The method page
    # claimed every bill; the record is a search still running, and a page about how the numbers
    # work should say which searches have not caught up. Empty once they have.
    filling = next((m.split("still backfilling:")[1].split(";")[0].strip()
                    for m in (str((s or {}).get("message") or "") for s in status.values())
                    if "still backfilling:" in m), "")

    data = {
        "built_at": iso(), "sources_count": str(ok_count), "period": "past year", "site_url": SITE_URL,
        "filling": filling,
        "election_spent": money(election_spent) if election_spent else "",
        "funders_total": f"{len(ranked):,}", "beneficiaries_total": f"{len(agency_count):,}",
        "election_total": f"{len(com_ranked):,}", "election": election,
        "donors": donors[:80], "donors_total": len(donors),
        "industry_total": f"{len(industry_ranked):,}", "industry": industry,
        "totals": {"funders": len(ranked), "industry": len(industry_ranked), "election": len(com_ranked),
                   "beneficiaries": len(agency_count)},
        "links": {"data": f"{REPO_URL}/tree/data", "code": REPO_URL,
                  # the raw file, for anything that wants the table rather than a page about it
                  "csv": f"{REPO_URL.replace('github.com', 'raw.githubusercontent.com')}"
                         f"/data/public/measures.csv",
                  "report": f"{REPO_URL}/issues/new?template=error.yml"},
        "analytics": {"goatcounter": "dukewilder"},
        # The latest card, so the page can show one rather than describe one. Written by
        # pipeline.brief, which also drops the plate itself into the published folder. A pass that
        # has not written today's edition yet leaves yesterday's here, which is a card either way.
        "brief": brief_latest(db),
        "exhibit": exhibit, "index": index, "grid": grid, "polls": polls, "numbers": numbers, "site": site,
        "quotes": quotes[:5], "already_law": already_law, "law_total": law_total,
        "law_passed": law_passed,
        "states": state_rows, "states_total": len(state_rows), "runs_today": runs_today,
        "feed_today": sum(1 for i in feed if (i.get("time_iso") or "") >= (today - dt.timedelta(days=1)).isoformat()),
        "tracked": {"measures": all_measures, "filings": all_filings, "orgs": len(ents.items)},
        "fears_tracked": [f["name"] if f["name"].startswith(("AI", "China")) else f["name"][0].lower() + f["name"][1:]
                          for f in fears],
        "fears": fear_rows, "feed_types": feed_types, "feed": feed[:80], "funders": funders,
        "beneficiaries": beneficiaries, "controls": control_rows, "sources": source_rows,
        "publishers": feeds,
        "schedule": SCHEDULE, "fear_pages": fear_pages, "org_pages": org_pages,
        "fear_word": spell(len(fears)),
    }
    (out / "site_data.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    (out / "status.json").write_text(json.dumps(status, indent=1, default=str))
    # The blind-spot list rides the data branch rather than the site: it is a note to whoever
    # maintains the fear list, not something a reader of the report needs.
    spots = kv_get(db, "blindspots:report")
    if spots:
        (out / "blindspots.json").write_text(json.dumps(spots, indent=1, default=str))
    write_csvs(out / "public", measures, lob_recent, ranked + industry_ranked, com_ranked)
    save_snapshot(db, today, {"fear_rank": {r["slug"]: r["rank"] for r in fear_rows},
                              "fear_index": {r["slug"]: int(r["score"]) for r in fear_rows},
                              "controlled": len(controlled), "measures": len(measures)})
    return data


SOURCES = [
    ("congress", "Federal bills", "Congress.gov"),
    ("openstates", "State bills", "Open States, all 50 states plus DC and Puerto Rico"),
    ("fedreg", "Federal rules and executive orders", "Federal Register"),
    ("lda", "Lobbying", "LDA.gov federal lobbying disclosures"),
    ("fec", "Donations and election spending", "Federal Election Commission"),
    ("gdelt", "News volume", "GDELT, daily article counts per fear"),
    ("news", "Headlines", "NEWS_FEEDS"),   # filled in from config/news.json, so the page cannot drift from it
    ("wikipedia", "Public attention", "Wikipedia pageviews"),
    ("rss", "Organization statements", "Newsroom feeds of tracked organizations"),
    ("tag", "Fear and control labels", "Claude, each label resting on a quote that code finds word for word in the measure"),
]
SCHEDULE = [
    ["Every 20 minutes", "News, statements, federal rules and the feed"],
    ["Daily", "Bills, lobbying, donations, labels and every ranking"],
    ["Quarterly", "New lobbying reports, filed 20 days after each quarter ends"],
]
FEED_TYPES = [["all", "All"], ["bill", "Bills"], ["rule", "Rules and orders"], ["lobbying", "Lobbying"],
              ["donation", "Donations"], ["spending", "Election spending"], ["statement", "Statements"]]


def live_polls(polls, today, months=18):
    """Newest first, and nothing old enough to be describing a different year.

    The figures are the one thing here kept by hand, so they are the one thing that can
    quietly rot. A poll past its shelf life leaves rather than sits at the top of the page.
    """
    floor = (today - dt.timedelta(days=int(months * 30.44))).isoformat()
    good = [p for p in polls if p.get("figure") and p.get("url") and (p.get("date") or "") >= floor]
    return sorted(good, key=lambda p: p["date"], reverse=True)


def window_30(db, prefix, today):
    """Thirty days of a daily series, ending at the last day the series actually has.

    A source that stops answering must not quietly shorten the window and leave every
    fear looking quieter than it is. The count stays a true thirty days and lags instead.
    """
    # The last day with anything in it. GDELT answers for days it has not counted yet with zeros,
    # so the latest date on file was today at a count of nought while the real coverage stopped a
    # week earlier, and "last 30 days" summed 23 days of news and a week of nothing.
    last = db.execute("SELECT MAX(date) FROM series WHERE series LIKE ? AND value > 0", (prefix + ":%",)).fetchone()[0]
    end = min(last, today.isoformat()) if last else today.isoformat()
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=29)).isoformat()
    rows = db.execute("SELECT series s, SUM(value) v FROM series WHERE series LIKE ? AND date BETWEEN ? AND ? "
                      "GROUP BY series", (prefix + ":%", start, end)).fetchall()
    return {r["s"][len(prefix) + 1:]: r["v"] or 0 for r in rows}, end


def days_counted(db, prefix, end):
    """How many of the thirty days a window covers have any count on file. GDELT goes quiet for
    days at a time, and a sum over the days it did count is not thirty days of coverage."""
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=29)).isoformat()
    return db.execute("SELECT COUNT(DISTINCT date) FROM series WHERE series LIKE ? AND date BETWEEN ? AND ?",
                      (prefix + ":%", start, end)).fetchone()[0]


# ---------------------------------------------------------------- the index
INDEX_WEIGHTS = {"bills": 0.4, "filings": 0.3, "news30": 0.15, "wiki30": 0.15}


def index_scores(fears, stats):
    """Score each fear 0 to 100 against the loudest fear on each channel.

    Legislation counts 40%, lobbying filings 30%, public attention 30% (news and
    Wikipedia, 15% each). Each channel is log scaled to its leader, so a fear with
    a tenth of the leader's bills still registers, and a fear with no Wikipedia
    article configured is scored on the channels it has.
    """
    tops = {}
    for key in INDEX_WEIGHTS:
        tops[key] = max((channel_value(stats[f["slug"]], key) for f in fears), default=0)
    out = {}
    for f in fears:
        st = stats[f["slug"]]
        parts, weight = {}, 0.0
        for key, w in INDEX_WEIGHTS.items():
            if key == "wiki30" and not st["has_wiki"]:
                continue
            if not tops[key]:
                continue
            parts[key] = w * math.log1p(channel_value(st, key)) / math.log1p(tops[key])
            weight += w
        score = round(100 * sum(parts.values()) / weight) if weight else 0
        # how much of the score each channel supplies, so the bar can show it
        contrib = {"bills": parts.get("bills", 0), "filings": parts.get("filings", 0),
                   "attention": parts.get("news30", 0) + parts.get("wiki30", 0)}
        scale = (score / (sum(contrib.values()) or 1)) if weight else 0
        st["parts"] = {k: round(v * scale, 1) for k, v in contrib.items()}
        out[f["slug"]] = score
    return out


def channel_value(st, key):
    if key == "bills":
        return len(st["measures"])
    if key == "states":
        return len(st["states"])
    return st.get(key, 0) or 0


# "Federal" rather than "Congress": the column counts executive orders and agency rules as well.
GRID_CHANNELS = [("bills", "Bills"), ("states", "States"), ("congress", "Federal"), ("filings", "Lobbying filings"),
                 ("statements", "Statements"), ("news30", "News, 30 days"), ("wiki30", "Wikipedia, 30 days")]


def build_grid(order, stats, ends=None, today=None):
    """The same fears down the side, every channel across the top, every cell a count."""
    tops = {key: max((channel_value(stats[f["slug"]], key) for f in order), default=0) for key, _ in GRID_CHANNELS}
    rows = []
    for f in order:
        st = stats[f["slug"]]
        cells = []
        for key, _ in GRID_CHANNELS:
            if key == "wiki30" and not st["has_wiki"]:
                # Nothing to count, which is not the same as nobody reading. Either there is no
                # article on this fear, or there is one and its readership has not been read yet.
                cells.append({"n": "\u2013", "level": 0,
                              "note": ("Readership not collected yet" if st["wiki_article"]
                                       else "No Wikipedia article on this fear")})
                continue
            v = channel_value(st, key)
            level = 0 if not v or not tops[key] else min(4, 1 + int(3.99 * math.log1p(v) / math.log1p(tops[key])))
            if v < 3:
                level = min(level, v)  # a lone 1 or 2 should never glow like a column leader
            cells.append({"n": compact(v) if v >= 10000 else f"{v:,}", "level": level})
        rows.append({"name": f.get("short") or f["name"], "slug": f["slug"], "cells": cells, "index": str(st["index"])})
    return {"channels": [{"label": label, "note": stalled(ends, key, today)} for key, label in GRID_CHANNELS],
            "rows": rows}


def stalled(ends, key, today, days=3):
    """The last day a window covers, shown only once a source is far enough behind to notice."""
    end = (ends or {}).get(key)
    if not end or not today:
        return None
    last = dt.date.fromisoformat(end)
    if (today - last).days < days:
        return None
    return f"to {last:%b} {last.day}"


def segments(st):
    p = st.get("parts") or {}
    return [{"cls": "k1", "pct": p.get("bills", 0)}, {"cls": "k2", "pct": p.get("filings", 0)},
            {"cls": "k3", "pct": p.get("attention", 0)}]


def fear_line(st):
    """One line of counts under a fear's name: the biggest three things true about it."""
    parts = []
    if st["measures"]:
        parts.append(count(len(st["measures"]), "bill"))
    if st["states"]:
        parts.append(count(len(st["states"]), "state"))
    if st["filings"]:
        parts.append(count(st["filings"], "lobbying filing"))
    if st["wiki30"] and len(parts) < 3:
        parts.append(f"{compact(st['wiki30'])} Wikipedia views this month")
    if st["news30"] and len(parts) < 3:
        parts.append(count(st["news30"], "news article") + " " + st.get("news_span", "in the last 30 days"))
    return " · ".join(parts[:3])


def law_date(m, today):
    """'Passed' with its date, or 'Effective' when the date on file is still ahead of us."""
    d = (m["latest_action_date"] or m["introduced_date"] or "")[:10]
    if not d:
        return "Passed"
    return f"Effective · {d}" if d > today.isoformat() else f"Passed · {d}"


def own_words(db, measures, fear_by, control_by, suppressed=(), per_fear=None, limit=40):
    """The exact words each fear label rests on, next to the control the same bill
    would create and the office it would go to."""
    ev = {}
    for t in db.execute("SELECT target, value, evidence FROM tags WHERE kind='fear' AND target NOT LIKE 'post:%'"):
        ev.setdefault(t["target"], []).append((t["value"], t["evidence"]))
    out = []
    for m in measures:
        if m["url"] in suppressed:
            continue
        chips = [control_by[c]["chip"] for c in m["controls"] if c in control_by]
        # One office named, and the card names it. Several, and it says how many: the arrow pointed at
        # whichever came first in the alphabet, which is not the office holding the power.
        offices = sorted({name_key(a): a.strip() for a in m["agencies"] if name_key(a)}.values())
        who = offices if len(offices) == 1 else [f"{len(offices)} offices"] if offices else []
        for slug, quote in ev.get(m["id"], []):
            if slug not in fear_by or (per_fear and slug != per_fear):
                continue
            quote = (quote or "").strip().strip('"')
            words = len(quote.split())
            letters = [c for c in quote if c.isalpha()]
            # a shouted bill title is a header, not language anyone wrote to persuade
            shouted = letters and sum(c.isupper() for c in letters) / len(letters) > 0.6
            if words < 6 or shouted:
                continue
            out.append({"fear": fear_by[slug]["name"], "slug": slug, "quote": quote,
                        "bill": f"{m['identifier'] or 'Measure'}, {m['jurisdiction_name']}", "url": m["url"],
                        "status": STATUS_LABEL.get(m["status"], "Pending"), "words": words,
                        "controls": chips, "agency": cut(who[0], 58) if who else "",
                        "when": m["introduced_date"] or ""})
    # the heaviest bills first: the most controls, an office named, already passed,
    # and language that does the controlling itself. Then newest, then spread across
    # fears so one loud topic does not take every slot.
    out.sort(key=lambda q: (damning(q), q["when"], q["words"]), reverse=True)
    spread, seen, used = [], collections.Counter(), set()
    for q in out:
        # one card per bill, and a companion bill with the same words counts as the same bill
        key = " ".join(q["quote"].lower().split())[:120]
        if q["url"] in used or key in used:
            continue
        if seen[q["slug"]] < (2 if per_fear is None else limit):
            spread.append(q)
            seen[q["slug"]] += 1
            used.update((q["url"], key))
        if len(spread) >= limit:
            break
    return spread


POWER_WORDS = re.compile(r"\b(requir|prohibit|mandat|authoriz|licens|regist|report|penalt|enforc|ban|"
                         r"moratori|permit|shall|designat|establish|impos|restrict|certif|approv)", re.I)


def damning(q):
    """How much power the quoted bill hands out, as far as the record shows."""
    return (3 * len(q["controls"]) + (2 if q["agency"] else 0) + (2 if q["status"] == "Passed" else 0)
            + min(3, len(POWER_WORDS.findall(q["quote"]))) + (1 if 8 <= q["words"] <= 30 else 0))


def build_feed(db, ents, measures, lob, receipts, today):
    items = []
    horizon = (today - dt.timedelta(days=21)).isoformat()
    stamp = today.isoformat()
    for m in measures:
        when = m["latest_action_date"] or m["introduced_date"] or ""
        if when > stamp:  # an effective date years out says nothing about when this moved
            when = (m["updated"] or m["first_seen"] or stamp)[:10]
        if when >= horizon:
            label = KIND_LABEL.get(m["kind"], "Bill")
            action = f" ({cut(m['latest_action'], 90)})" if m["latest_action"] and m["kind"] not in ("rule", "order") else ""
            items.append({"type": "rule" if m["kind"] in ("rule", "order") else "bill", "label": label,
                          "text": f"{m['jurisdiction_name']} {m['identifier']}: {cut(m['title'] or 'Untitled', 150)}{action}",
                          "url": m["url"], "time_iso": when[:10] + "T12:00:00+00:00", "time": when[:10],
                          "fears": m["fears"], "org": None})
    for r in lob:
        if (r["posted"] or "") >= horizon:
            refs = f", naming {r['bill_refs']}" if r["bill_refs"] else ""
            items.append({"type": "lobbying", "label": "Lobbying filing",
                          # The amount is the filing's total for the quarter across every issue on
                          # it, not the part spent on AI, which no filing breaks out.
                          "text": f"{nice_name(r['client']) or 'A lobbying client'} filed a lobbying report that mentions AI, "
                                  f"{money(r['amount'])} for the quarter across all its issues{refs}",
                          "url": r["url"], "time_iso": r["posted"][:19] + "+00:00", "time": r["posted"][:10],
                          "fears": r["fears"], "org": r["entity"] or slugify(nice_name(r["client"]))})
    for r in receipts:
        # The same line rule as the totals: a committee's bank paying it interest is not a donor.
        if r.get("line_number") and not gave(r["line_number"], ents.match_name(r["counterparty"] or "")):
            continue
        if (r["date"] or "") >= horizon:
            items.append({"type": "donation", "label": "Donation",
                          "text": f"{nice_name(r['counterparty'])} gave {money(r['amount'])} to {nice_name(r['committee_name'])}",
                          "url": r["url"], "time_iso": r["date"] + "T12:00:00+00:00", "time": r["date"],
                          "fears": [],
                          "org": ents.match_name(r["committee_name"]) or slugify(nice_name(r["committee_name"]))})
    for r in db.execute("SELECT * FROM fec WHERE kind='independent_expenditure' AND date >= ?", (horizon,)):
        who = f" {r['support_oppose']} {nice_name(r['candidate'])}" if r["candidate"] else ""
        items.append({"type": "spending", "label": "Election spending",
                      "text": f"{nice_name(r['committee_name'])} spent {money(r['amount'])}{who}",
                      "url": r["url"], "time_iso": r["date"] + "T12:00:00+00:00", "time": r["date"],
                      "fears": [], "org": None})
    post_tags = collections.defaultdict(set)
    for t in db.execute("SELECT target, value FROM tags WHERE kind='fear' AND target LIKE 'post:%'"):
        post_tags[t["target"][5:]].add(t["value"])
    for p in db.execute("SELECT p.* FROM posts p JOIN tag_runs tr ON tr.target = 'post:' || p.id "
                        "WHERE tr.ai_related = 1 AND p.published >= ?", (horizon,)):
        named = sorted(post_tags.get(p["id"], []))
        if not named:
            continue  # AI-related is not the bar here; naming one of the fears on file is
        if not own_post(p["entity"], p["url"], ents):
            continue  # another outlet's story reposted on the organization's site
        ent = ents.by_slug.get(p["entity"], {})
        items.append({"type": "statement", "label": "Statement", "text": f"{ent.get('name', p['entity'])}: {cut(p['title'] or '', 160)}",
                      "url": p["url"], "time_iso": p["published"], "time": p["published"][:10],
                      "fears": named, "org": p["entity"]})
    items.sort(key=lambda i: i["time_iso"] or "", reverse=True)
    return items


def bill_headline(title):
    """The name a legislature gave a bill, without the drafting boilerplate after it."""
    head = re.split(r"\s*;", (title or "").strip(), 1)[0]
    head = re.sub(r"^(an?\s+act\s+(relating to|providing for|concerning|to)\s+)", "", head, flags=re.I)
    return head.strip().strip('"').strip("\u201c\u201d").strip()


FEED_HOST = re.compile(r"^(?:www|rss|feeds?|api)\.")
# A feed served from one domain for articles published on another.
FEED_ALIASES = {"bbci.co.uk": ("bbc.com", "bbc.co.uk")}


def read_directly():
    """The publishers this report subscribes to, by the domain their articles carry.

    GDELT sweeps the whole web, which is what makes it a fair measure of volume and a poor one of
    who to quote: for a fear with a dozen stories in a week, the most typical headline can land on
    any site that ran the wire copy, and it moved between three of them in an hour. These sixteen
    are the ones config/news.json already reads in full, and the method page already says the
    headlines come from them.
    """
    hosts = set()
    for src in config("news"):
        host = FEED_HOST.sub("", urllib.parse.urlparse(src["url"]).netloc.lower())
        hosts.add(host)
        hosts.update(FEED_ALIASES.get(host, ()))
    return hosts


def our_domain(domain, hosts):
    return FEED_HOST.sub("", (domain or "").lower()) in hosts


def brief_latest(db):
    """The edition the site should show, or nothing at all.

    Nothing is the normal state on a fresh database and on any run before the first edition is
    written, so the section that shows the card has to be able to not exist. The date is the
    plate's own filename, published under /brief by the pass that builds the site.
    """
    latest = kv_get(db, "brief:latest") or {}
    day, head = latest.get("edition"), latest.get("headline")
    if not day:
        # Editions written before this key existed, and any run where brief.py has not gone yet
        # today. The table knows which days were published even when it does not know what they
        # said, and the plate is named for its day, so the card is still reachable. The headline
        # arrives with the next edition and the alt text stops being generic then.
        row = db.execute("SELECT MAX(edition) FROM brief").fetchone()
        day = row[0] if row else None
        head = None
    if not day:
        return None
    return {"date": day, "headline": head or "", "image": f"/brief/{day}.png",
            "alt": latest.get("alt") or head
            or f"The AI Fear Report card for {day}, posted on X."}


def sentence_name(fear):
    """The fear's name as it reads inside a sentence rather than as a label.

    The names are written as headings, so they carry a capital that is wrong mid-sentence: "cite
    Loss of control" reads as the title of something. Which ones keep the capital is not something
    a rule can work out from the letters, because "China" and "Deepfakes" are the same shape and
    only one of them is a place, so every fear carries its own sentence form in the config and
    check_fears_config makes sure it is there. The same discipline "because" already follows in
    that file: a fragment written to be dropped into a sentence, capitalised only where the word
    would be capitalised anywhere.
    """
    return fear["sentence"]


def build_exhibit(db, order, fear_stats, today, suppressed=()):
    """The top of the front page: the fear leading the index, its loudest headline, the receipt.

    Always renders. If no fresh headline is stored for the leader, the line is built
    from the leader's own numbers instead of leaving the space empty.
    """
    if not order:
        return None
    d7 = (today - dt.timedelta(days=7)).isoformat()
    best = order[0]
    st = fear_stats[best["slug"]]
    arts = [dict(a) for a in db.execute(
        "SELECT * FROM articles WHERE fear=? AND seen > ? AND length(title) > 25 ORDER BY seen DESC LIMIT 120",
        (best["slug"], d7)) if a["url"] not in suppressed]
    line, line_url, source, source_label = None, None, None, "Headline from"
    ours = [a for a in arts if our_domain(a["domain"], read_directly())]
    arts = ours or arts   # nothing from them on this fear this week: take what there is, and say so
    if arts:
        for a in arts:
            a["title"] = tidy_headline(a["title"], a["domain"])
        words = [set(w for w in re.findall(r"[a-z]{4,}", a["title"].lower())) for a in arts]

        def score(i):
            return sum(len(words[i] & w) / (len(words[i] | w) or 1) for j, w in enumerate(words) if j != i)
        # Two questions, answered separately, because blending them into one number made the
        # answer turn on a margin of two percent and flip between outlets as articles arrived.
        #
        # First, which story is the coverage converging on: the headline sharing most words with
        # the rest of them. Second, who to quote it from. Outlets word the same event differently,
        # so the ones running it cluster at a similarity well clear of the ones running something
        # else, and inside that cluster every candidate says the same thing. The choice there goes
        # to the outlet that writes about most of the fears at all, because a newsroom covering
        # the whole subject speaks for the coverage better than one that touched it once.
        breadth = collections.Counter()
        for r in db.execute("SELECT DISTINCT domain, fear FROM articles"):
            breadth[r["domain"]] += 1
        seed = max(range(len(arts)), key=score)

        def alike(i):
            return len(words[seed] & words[i]) / (len(words[seed] | words[i]) or 1)
        closest = max((alike(i) for i in range(len(arts)) if i != seed), default=0)
        story = [i for i in range(len(arts)) if i == seed or alike(i) >= closest * 0.6]
        pick = arts[max(story, key=lambda i: (breadth[arts[i]["domain"]], score(i)))]
        line, line_url, source = cut(pick["title"], 110), pick["url"], pick["domain"]
    if not line:
        # no fresh headline on file for the leader: its newest bill speaks for it.
        # Legislatures put the bill's name first and the boilerplate after a semicolon.
        newest = sorted(st["measures"], key=lambda m: (m["introduced_date"] or "", m["id"]), reverse=True)
        named = [(m, bill_headline(m["title"])) for m in newest]
        named = [(m, h) for m, h in named if len(h) > 24]
        bill = next((mh for mh in named if len(mh[1]) <= 70), named[0] if named else None)
        if bill:
            line, line_url = cut(bill[1], 70), bill[0]["url"]
            source = f"{bill[0]['identifier'] or 'Measure'}, {bill[0]['jurisdiction_name']}"
            source_label = "Newest bill"
    if not line:
        bits = []
        if st["measures"]:
            bits.append(count(len(st["measures"]), "bill"))
        if st["codes"]:
            bits.append("in " + where_phrase(st["codes"]))
        if st["filings"]:
            bits.append(f"{st['filings']:,} lobbying filings")
        line = f"{best['name']}: " + (", ".join(bits) if bits else f"index {st['index']}")
    rows = [["Index", f"{st['index']} of 100, rank 1 of {len(order)}"]]
    if st["wiki30"] or st["news30"]:
        att = []
        if st["wiki30"]:
            att.append(f"{st['wiki30']:,} Wikipedia views in the last 30 days")
        if st["news30"]:
            att.append(f"{st['news30']:,} news articles {st.get('news_span', 'in the last 30 days')}")
        rows.append(["Attention", ", ".join(att)])
    if st["measures"]:
        where = where_phrase(st["codes"])
        rows.append(["Bills", f"{len(st['measures']):,} since January 2025" + (f", in {where}" if where else "")])
    if st["controls"]:
        rows.append(["Controls", f"{st['controls']:,} written into those bills"])
    gains, gain_name = collections.Counter(), {}
    for m in st["measures"]:
        if not m["controls"]:
            continue  # the same rule as the front page count: an office holds a power only if there is one
        for a in m["agencies"]:
            k = name_key(a)
            if k:
                gains[k] += 1
                gain_name.setdefault(k, a.strip())
    if gains:
        k, n = gains.most_common(1)[0]
        rows.append(["Goes to", f"{cut(gain_name[k], 58)}, in {n} of them"])
    if st["filings"]:
        rows.append(["Lobbying", count(st["filings"], "filing") + " name it, past year"])
    if st["advocacy"]:
        rows.append(["Funding", f"{money(st['advocacy'])} from advocacy groups, past year"])
    # What the fear bought, in the same breath as the fear. A headline alone at the top of the page
    # reads as the report's own statement, and as good news. It is evidence that the fear is loud,
    # which is half of what this site is about; these counts are the other half, and they go in the
    # larger type. Which office gains what stays in the table below, where there is room for it.
    bought = []
    if st["measures"]:
        where = where_phrase(st["codes"])
        where = f" in {where}" if where else ""
        # Named, not "it". This line is the one that gets read on its own and screenshotted on
        # its own, and an "it" pointing back at a chyron above it does not survive either.
        bought.append(f"{count(len(st['measures']), 'bill')}{where} cite {sentence_name(best)}")
    if st["controls"]:
        # Who ends up holding them. The bills and the controls say the fear turned into law; this
        # says it turned into somebody's authority, which is the whole of what this report is for.
        # It sat in the table below in small type, against a single office named in three of them.
        # Would, not held: most of these measures have not passed, and nobody holds a power a
        # measure in committee would hand them. Counted over the measures that carry a control.
        held = {name_key(a) for m in st["measures"] if m["controls"] for a in m["agencies"] if name_key(a)}
        bought.append(f"They carry {count(st['controls'], 'new government control')}"
                      + (f" and would hand new power to {count(len(held), 'office')}" if held else ""))
    # The quote up there carries its own attribution, so the table only names the source when there
    # is no quote. What gets quoted is a publisher's headline where there is one and the leading
    # measure's own title where there is not, so a quiet news week still leads with the counts.
    if source and not bought:
        rows.append([source_label, source])
    return {"chyron": f"Loudest fear right now: {best['name']}", "line": line, "line_url": line_url,
            "said": line if source else "", "said_from": source or "",
            "bought": ". ".join(bought) + "." if bought else "",
            "rows": rows, "fear_slug": best["slug"]}


def quarter_axis():
    y = now().year
    return [(yy, q) for yy in (y - 2, y - 1, y) for q in (1, 2, 3, 4)]


def fear_page(f, rank, of, fear_stats, lob, feed, today, db, control_by, page_slugs, ents):
    st = fear_stats[f["slug"]]
    axis = quarter_axis()
    idx = {k: i for i, k in enumerate(axis)}
    by_q = collections.defaultdict(list)
    for r in lob:
        if f["slug"] in r["fears"] and r["quarter"]:
            k = (r["year"], int(r["quarter"][1]))
            if k in idx:
                by_q[idx[k]].append(r)
    money_q = collections.Counter({q: reported(rs) for q, rs in by_q.items()})
    top_m = max(money_q.values() or [0])
    funding = [[q, max(2, round(40 * v / top_m))] for q, v in sorted(money_q.items()) if v > 0] if top_m else []
    bills_q = collections.Counter()
    for m in st["measures"]:
        if m["introduced_date"]:
            k = quarter_of(m["introduced_date"])
            if k in idx:
                bills_q[idx[k]] += 1
    top_b = max(bills_q.values() or [0])
    unit = max(1, math.ceil(top_b / 5))
    dots = [min(5, math.ceil(bills_q.get(i, 0) / unit)) for i in range(12)]
    wiki_q = collections.Counter()
    for r in db.execute("SELECT date, value FROM series WHERE series=?", (f"wiki:{f['slug']}",)):
        k = quarter_of(r["date"])
        if k in idx:
            wiki_q[idx[k]] += r["value"]
    top_w = max(wiki_q.values() or [0])
    concern = [[q, round(100 * v / top_w)] for q, v in sorted(wiki_q.items())] if top_w else []
    lanes = ["Money on filings naming it", "Bills citing it", "Wikipedia views"]
    quarters = [f"Q{q} {y}" for y, q in axis]
    timeline = {"aria": f"Lobbying money, bills and public attention for {f['name']} by quarter",
                "years": [str(axis[0][0]), str(axis[4][0]), str(axis[8][0])], "funding": funding,
                "messages": dots, "concern": concern, "lanes": lanes, "quarters": quarters,
                "funding_raw": {str(q): money(v) for q, v in money_q.items() if v > 0},
                "bills_raw": {str(q): int(v) for q, v in bills_q.items() if v},
                "wiki_raw": {str(q): f"{int(v):,}" for q, v in wiki_q.items()},
                "first_label": compact(wiki_q[min(wiki_q)]) if wiki_q else "",
                "last_label": compact(wiki_q[max(wiki_q)]) if wiki_q else "", "events": []}
    window = recent_quarters(4)
    pushers = collections.defaultdict(lambda: {"amount": 0.0, "n": 0, "name": "", "entity": None, "filings": []})
    for r in lob:
        if f["slug"] in r["fears"] and r["quarter"] and (r["year"], int(r["quarter"][1])) in window:
            key = r["entity"] or r["client_key"]
            p = pushers[key]
            p["filings"].append(r)
            p["amount"] = reported(p["filings"])
            p["n"] += 1
            p["name"] = ents.by_slug[r["entity"]]["name"] if r["entity"] else nice_name(r["client"])
            p["entity"] = r["entity"]
    push_rows, industry_rows = [], []
    for key, p in sorted(pushers.items(), key=lambda kv: (-kv[1]["n"], -kv[1]["amount"])):
        slug = p["entity"] or slugify(p["name"])
        ent = ents.by_slug.get(p["entity"]) if p["entity"] else None
        row = {"name": p["name"], "slug": slug if slug in page_slugs else None,
               "type": ent["type"] if ent else "Lobbying client", "messages": f"{p['n']}",
               "flag": (ent or {}).get("flag")}
        if ent and ent["type"] in MISSION_TYPES:
            push_rows.append({**row, "amount": money(p["amount"])})
        else:
            industry_rows.append({**row, "messages": "", "fears": "0",
                                  "amount": f"{p['n']} filing{'s' if p['n'] != 1 else ''}"})
    push_rows.sort(key=lambda r: -float(r["amount"].strip("$KMB").replace(",", "") or 0) * {"K": 1e3, "M": 1e6, "B": 1e9}.get(r["amount"][-1], 1))
    for i, r in enumerate(push_rows, 1):
        r["rank"] = i
    for i, r in enumerate(industry_rows, 1):
        r["rank"] = i
    push_rows, industry_rows = push_rows[:6], industry_rows[:6]
    lob_year = sum(p["amount"] for p in pushers.values())
    # The front page's rule: offices named in measures that carry a control, one office however
    # it is spelled, and the control it is named beside most often.
    agencies, names = collections.Counter(), {}
    beside = collections.defaultdict(collections.Counter)
    for m in st["measures"]:
        if not m["controls"]:
            continue
        for a in m["agencies"]:
            k = name_key(a)
            if not k:
                continue
            agencies[k] += 1
            names.setdefault(k, a.strip())
            beside[k].update(m["controls"])
    ben_rows = [{"rank": i, "name": names[k], "slug": None, "type": "Attorney general" if "attorney general" in k else "Agency",
                 "gains": control_by[beside[k].most_common(1)[0][0]]["chip"] if beside[k] else "",
                 "score": str(n), "unit": "bills"} for i, (k, n) in enumerate(agencies.most_common(5), 1)]
    bills = sorted(st["measures"], key=lambda m: (len(m["controls"]), m["introduced_date"] or ""), reverse=True)[:10]
    bill_rows = [{"name": f"{m['identifier'] or 'Measure'}, {m['jurisdiction_name']}", "title": cut(m["title"] or "", 160),
                  "status": STATUS_LABEL.get(m["status"], "Pending"), "url": m["url"],
                  "controls": [control_by[c]["chip"] for c in m["controls"]]} for m in bills]
    buying = collections.Counter(c for m in st["measures"] for c in m["controls"])
    buying_rows = [{"rank": i, "name": control_by[c]["name"], "chip": control_by[c]["chip"], "bills": str(n),
                    "passed": str(sum(1 for m in st["measures"] if c in m["controls"] and m["status"] == "passed"))}
                   for i, (c, n) in enumerate(buying.most_common(), 1)]
    evidence = []
    if st["wiki30"]:
        evidence.append([f"{st['wiki30']:,}", "Wikipedia views on this fear in the last 30 days"])
    if st["news30"]:
        evidence.append([f"{st['news30']:,}", f"news articles about it {st.get('news_span', 'in the last 30 days')}"])
    if st["measures"]:
        evidence.append([f"{len(st['measures']):,}", "bills, rules and orders cite it since January 2025"
                         + (f", in {where_phrase(st['codes'])}" if st["codes"] else "")])
    if st["controls"]:
        evidence.append([f"{st['controls']:,}", "new government controls written into those bills"])
    if st["filings"]:
        evidence.append([f"{st['filings']:,}", "federal lobbying filings name it, past year"])
    if st["advocacy"]:
        evidence.append([money(st["advocacy"]), "spent lobbying on it by advocacy groups, past year"])
    latest = [i for i in feed if f["slug"] in (i.get("fears") or [])][:6]
    return {"slug": f["slug"], "name": f["name"], "rank": str(rank), "of": str(of), "score": str(st["index"]),
            "score_text": "on the Fear Index",
            "parts": [{"name": "Bills", "cls": "k1", "pct": (st.get("parts") or {}).get("bills", 0), "value": f"{len(st['measures']):,}"},
                      {"name": "Lobbying filings", "cls": "k2", "pct": (st.get("parts") or {}).get("filings", 0), "value": f"{st['filings']:,}"},
                      # A nought here says nobody is reading about it. What is true is that
                      # nothing has been counted: a fear added today has no readership until the
                      # next daily reading, and the grid already says so with the same mark.
                      {"name": "Attention", "cls": "k3", "pct": (st.get("parts") or {}).get("attention", 0),
                       "value": (compact(st["wiki30"] + st["news30"])
                                 if (st["wiki30"] or st["news30"]) else "\u2013")}],
            "line": fear_line(st), "buying": buying_rows, "industry": industry_rows,
            "change": f"▲ {st['new_week']} new bills this week" if st["new_week"] else None, "dir": "up",
            "evidence": evidence, "timeline": timeline,
            "timeline_note": (f"Each dot is {unit} bills." if unit > 1 else "Each dot is one bill."),
            "timeline_sources": "LDA.gov lobbying filings that mention this fear, bills tagged with it, and Wikipedia pageviews for its topic.",
            "events": [], "latest": latest, "pushers": push_rows, "beneficiaries": ben_rows, "bills": bill_rows,
            "sentence": None, "reach": None}


def org_page(o, of, lob, receipts, committees, feed, measures, control_by, group="funders"):
    by_ident = {m["identifier"]: m for m in measures if m["jurisdiction"] == "us"}
    filings = [r for r in lob if (r["entity"] or r["client_key"]) == o["key"]]
    filings.sort(key=lambda r: r["posted"] or "", reverse=True)
    per_fear = collections.defaultdict(list)
    window = recent_quarters(4)
    for r in filings:
        if r["quarter"] and (r["year"], int(r["quarter"][1])) in window:
            for s in r["fears"]:
                per_fear[s].append(r)
    fear_amounts = collections.Counter({s: reported(rs) for s, rs in per_fear.items()})
    top = max(fear_amounts.values() or [1])
    fear_names = {f["slug"]: f["name"] for f in config("fears")}
    fears = [{"slug": s, "name": fear_names.get(s, s.replace("-", " ").capitalize()),
              "amount": money(v), "bar": max(3, round(100 * v / top))}
             for s, v in fear_amounts.most_common(6)]
    lobbying = []
    for r in filings[:8]:
        chips = sorted({control_by[c]["chip"] for ref in (r["bill_refs"] or "").split(", ") if ref in by_ident
                        for c in by_ident[ref]["controls"]})
        names = [fear_names.get(f, f) for f in (r["fears"] or [])]
        lobbying.append({"bill": r["bill_refs"] or "No bill numbers listed", "amount": money(r["amount"]),
                         "period": f"{r['quarter']} {r['year']}", "controls": chips, "url": r["url"],
                         "fears": names})
    money_out = [{"to": nice_name(r["committee_name"]), "amount": money(r["amount"]), "year": (r["date"] or "")[:4],
                  "source": "FEC", "url": r["url"]}
                 for r in receipts if (o["entity"] and o["entity"] == Entities_cache().match_name(r["counterparty"]))
                 or name_key(r["counterparty"]) == o["key"]][:8]
    own = [c for c in committees if (c["entity"] or name_key(c["name"])) == o["key"]]
    ids = {c["id"] for c in own}
    givers = {}
    ents_ = Entities_cache()
    for r in receipts:
        if r["committee_id"] not in ids or not (r["amount"] or 0):
            continue
        ent = ents_.match_name(r["counterparty"])
        # The same line rule the total above this list uses. Without it a committee's own bank
        # appeared among its donors for the interest it paid, and the names under the figure added
        # up to more than the figure.
        if not gave(r["line_number"], ent):
            continue
        key = ent or r["counterparty_key"] or nice_name(r["counterparty"])
        label = ents_.by_slug[ent]["name"] if ent else nice_name(r["counterparty"])
        if ent and ent == o["entity"]:
            label += " (affiliated committee)"  # a transfer between two committees of the same group
        g = givers.setdefault(key, {"from": label, "total": 0.0, "years": set(), "url": r["url"], "n": 0})
        g["total"] += r["amount"]
        g["n"] += 1
        if r["date"]:
            g["years"].add(r["date"][:4])
    money_in = [{"from": g["from"], "amount": money(g["total"]),
                 "year": "-".join(sorted(g["years"])[::len(g["years"]) - 1 or 1]) if g["years"] else "",
                 "source": f"FEC, {g['n']} filings" if g["n"] > 1 else "FEC", "url": g["url"]}
                for g in sorted(givers.values(), key=lambda g: -g["total"])[:12]]
    latest = [i for i in feed if i.get("org") == o["slug"]][:6]
    # Each half carries its own period. Lobbying is the last four quarters; election money is
    # everything the committee has raised this cycle, and the receipts behind this one start in
    # August 2025. One "past year" under both of them was wrong about the second.
    parts = []
    if o["lobbying"]:
        parts.append(f"{money(o['lobbying'])} reported on filings that name a fear, past year")
    if o["election"]:
        parts.append(f"{money(o['election'])} raised in election money this cycle")
    if len(parts) == 1:  # the big number above already says how much
        parts = ["reported on lobbying filings that name a fear, past year" if o["lobbying"] else
                 "raised in election money this cycle"]
    return {"slug": o["slug"], "name": o["name"], "type": o["type"], "flag": o["flag"], "rank": str(o["rank"]),
            "of": f"{of:,}", "group": group, "spent": money(o["total"]), "period": "past year",
            "breakdown": " and ".join(parts),
            "fears": fears, "gains": None, "money_out": money_out, "money_in": money_in, "lobbying": lobbying,
            "latest": latest}


_ENTS = None


def Entities_cache():
    global _ENTS
    if _ENTS is None:
        _ENTS = Entities()
    return _ENTS


def previous_snapshot(db, today):
    row = db.execute("SELECT snapshot FROM history WHERE date <= ? ORDER BY date DESC LIMIT 1",
                     ((today - dt.timedelta(days=7)).isoformat(),)).fetchone()
    return json.loads(row["snapshot"]) if row else None


def save_snapshot(db, today, snap):
    db.execute("INSERT INTO history(date, snapshot) VALUES(?, ?) ON CONFLICT(date) DO UPDATE SET snapshot=excluded.snapshot",
               (today.isoformat(), json.dumps(snap)))
    db.commit()


def write_csvs(folder, measures, lob_recent, ranked, com_ranked):
    with open(folder / "measures.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "kind", "jurisdiction", "identifier", "title", "status", "introduced", "url", "fears", "controls", "agencies"])
        for m in sorted(measures, key=lambda m: m["introduced_date"] or "", reverse=True):
            w.writerow([m["id"], m["kind"], m["jurisdiction_name"], m["identifier"], m["title"], m["status"],
                        m["introduced_date"], m["url"], "; ".join(m["fears"]), "; ".join(m["controls"]), "; ".join(m["agencies"])])
    with open(folder / "lobbying.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["client", "registrant", "year", "quarter", "amount", "bills", "fears mentioned", "url"])
        for r in sorted(lob_recent, key=lambda r: (r["year"], r["quarter"]), reverse=True):
            w.writerow([r["client"], r["registrant"], r["year"], r["quarter"], r["amount"], r["bill_refs"],
                        "; ".join(r["fears"]), r["url"]])
    with open(folder / "funders.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "name", "type", "lobbying on filings mentioning a tracked fear", "fears mentioned"])
        for o in ranked:
            w.writerow([o["rank"], o["name"], o["type"], round(o["lobbying"]), "; ".join(sorted(o["fears"]))])
    with open(folder / "election.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "committee", "type", "election money", "committee ids"])
        for o in com_ranked:
            w.writerow([o["rank"], o["name"], o["type"], round(o["election"]), "; ".join(o.get("committee_ids", []))])
