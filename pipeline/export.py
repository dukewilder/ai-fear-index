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

from .common import (REPO_URL, SITE_URL, SINCE, Entities, config, fear_keywords, fears_mentioned, iso, kv_get, kv_set,
                     name_key, now, sha)

SMALL = {"of", "and", "for", "the", "in", "on", "to", "a", "an", "at", "by"}
ACRONYMS = {"AI", "US", "USA", "UK", "EU", "PAC", "TV", "IT", "AG", "DC", "PC", "ML", "IP", "HR"}
STATUS_LABEL = {"passed": "Passed", "pending": "Pending", "failed": "Failed"}
KIND_LABEL = {"bill": "Bill", "resolution": "Resolution", "rule": "Rule", "order": "Executive order"}
# Organizations whose stated purpose is the fear, as opposed to companies and trade groups
# whose lobbying touches it among everything else they work on.
MISSION_TYPES = {"Advocacy group", "Foundation", "Pollster"}


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


def slugify(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").lower())).strip("-")[:60] or "org"


def quarter_of(date_str):
    d = dt.date.fromisoformat(date_str[:10])
    return d.year, (d.month - 1) // 3 + 1


def recent_quarters(n=4):
    y, q = now().year, (now().month - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.append((y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return set(out)


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
    measures = [dict(r) for r in db.execute(
        "SELECT m.* FROM measures m JOIN tag_runs tr ON tr.target = m.id WHERE tr.ai_related = 1 "
        "AND (m.introduced_date >= ? OR m.kind IN ('rule','order'))", (SINCE,))]
    for m in measures:
        tg = tags.get(m["id"], {"fear": set(), "control": set(), "agency": set()})
        m["fears"] = sorted(s for s in tg["fear"] if s in fear_by)
        m["controls"] = sorted(s for s in tg["control"] if s in control_by)
        m["agencies"] = sorted(tg["agency"])
    suppressed = set(config("suppress").get("urls", []))
    measures = [m for m in measures if m["url"] not in suppressed]
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
        o["lobbying"] += r["amount"] or 0
        o["n"] = o.get("n", 0) + 1
        o["fears"].update(r["fears"])
        o["type"] = o["type"] or "Lobbying client"
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
    raised = collections.Counter()
    for r in receipts:
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
    page_slugs = ({o["slug"] for o in top} | {o["slug"] for o in industry_top}
                  | {o["slug"] for o in com_ranked[:30]})

    # ---------------- agencies that would gain authority
    agency_count, agency_name = collections.Counter(), {}
    agency_controls = collections.defaultdict(collections.Counter)
    for m in measures:
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
                         "gains": (control_by[top_c[0][0]]["chip"].lower() if top_c else "new authority"),
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
    post_fears = collections.Counter(t["value"] for t in db.execute(
        "SELECT value FROM tags WHERE kind='fear' AND target LIKE 'post:%'"))
    filings_by_fear, advocacy_by_fear = collections.Counter(), collections.Counter()
    for r in lob_recent:
        mission = bool(r["entity"]) and ents.by_slug[r["entity"]]["type"] in MISSION_TYPES
        for slug in r["fears"]:
            filings_by_fear[slug] += 1
            if mission:
                advocacy_by_fear[slug] += r["amount"] or 0
    fear_stats = {}
    for f in fears:
        slug = f["slug"]
        ms = [m for m in measures if slug in m["fears"]]
        fear_stats[slug] = {
            "measures": ms, "controls": sum(len(m["controls"]) for m in ms),
            "with_controls": sum(1 for m in ms if m["controls"]),
            "passed": sum(1 for m in ms if m["status"] == "passed"),
            "states": sorted({m["jurisdiction"] for m in ms if m["jurisdiction"] not in ("us", "us-exec")}),
            "congress": sum(1 for m in ms if m["jurisdiction"] in ("us", "us-exec")),
            "federal": any(m["jurisdiction"] in ("us", "us-exec") for m in ms),
            "new_week": sum(1 for m in ms if (m["introduced_date"] or "") >= week_ago),
            "filings": filings_by_fear[slug], "advocacy": advocacy_by_fear[slug], "statements": post_fears[slug],
            "news30": int(news30.get(slug, 0)), "wiki30": int(wiki30.get(slug, 0)),
            "has_wiki": bool(f.get("wikipedia")),
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
    n_states = len([j for j in jurisdictions if j not in ("us", "us-exec")])
    has_fed = any(j in ("us", "us-exec") for j in jurisdictions)
    index = {"value": f"{len(measures):,}", "suffix": "",
             "text": "bills, rules and orders about AI since January 2025",
             "second": f"{len(controlled):,}",
             "second_text": "of them would put AI, the people building it or the people using it under new government control",
             "where": (f"{n_states} states" if n_states else "") + (" and Congress" if has_fed and n_states else "Congress" if has_fed else ""),
             "change": f"▲ {new_week:,} new this week" if new_week else None, "trend": trend, "series": series,
             "series_label": "Bills, rules and orders about AI, running total over the last 90 days",
             "total_measures": len(measures), "controlled": len(controlled)}
    index["chain"] = [c for c in [
        [compact(sum(int(v) for v in wiki30.values())), "Wikipedia views on the nine fears, last 30 days"]
        if wiki30 else None,
        [f"{len(measures):,}", "bills, rules and orders about AI since January 2025"] if measures else None,
        [f"{len(controlled):,}", "of them put AI under new government control"] if controlled else None,
        [f"{len(agency_count):,}", "agencies and officials handed new power"] if agency_count else None,
    ] if c]
    polls = [p for p in config("polls") if p.get("figure") and p.get("url")]
    site = config("site")

    # ---------------- the wall of numbers
    filings_naming = sum(1 for r in lob_recent if r["fears"])
    advocacy_total = sum(o["lobbying"] for o in ranked)
    election_total = sum(o["election"] for o in com_ranked)
    news_total = sum(int(v) for v in news30.values())
    all_filings = db.execute("SELECT COUNT(*) c FROM lobbying").fetchone()["c"]
    all_measures = db.execute("SELECT COUNT(*) c FROM measures").fetchone()["c"]
    # the money leads, largest first: it is the number that says most in one glance
    spent = sorted(((election_total, "raised and spent by AI super PACs this cycle"),
                    (advocacy_total, "spent lobbying on the fears by advocacy groups, past year")),
                   reverse=True)
    numbers = [n for n in [
        [money(spent[0][0]), spent[0][1]] if spent[0][0] else None,
        [money(spent[1][0]), spent[1][1]] if spent[1][0] else None,
        [f"{sum(len(m['controls']) for m in measures):,}",
         "new government controls written into the bills"] if controlled else None,
        [f"{n_states}", ("states, plus Congress, " if has_fed else "states ") + "with AI measures on the books or in motion"]
        if n_states else None,
        [f"{filings_naming:,}", "federal lobbying filings naming one of the nine fears, past year"] if filings_naming else None,
        [f"{news_total:,}", "news articles on the nine fears in the last 30 days"] if news_total else None,
        [f"{all_measures:,}", "bills collected and read so far"] if all_measures else None,
        [f"{all_filings:,}", "lobbying filings in the database"] if all_filings else None,
    ] if n]

    # ---------------- in their own words: the quotes the labels rest on
    quotes = own_words(db, measures, fear_by, control_by, suppressed)

    # ---------------- already law
    passed = sorted((m for m in controlled if m["status"] == "passed"),
                    key=lambda m: (-len(m["controls"]), m["latest_action_date"] or ""))
    already_law = [{"name": f"{m['identifier'] or 'Measure'}, {m['jurisdiction_name']}", "title": cut(m["title"] or "", 140),
                    "url": m["url"], "date": law_date(m, today),
                    "controls": [control_by[c]["chip"] for c in m["controls"]],
                    "fears": [fear_by[fs]["name"] for fs in m["fears"]]} for m in passed[:8]]
    law_total = len(passed)

    # ---------------- where it is happening: every jurisdiction, ranked
    by_state = collections.defaultdict(list)
    for m in measures:
        by_state[m["jurisdiction"]].append(m)
    state_rows = []
    for j, ms in by_state.items():
        fear_mix = collections.Counter(fs for m in ms for fs in m["fears"])
        state_rows.append({"code": j, "name": ms[0]["jurisdiction_name"], "n": len(ms),
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
            f"{len(st['measures']):,} bills since January 2025" + (f" in {len(st['states'])} states." if st["states"] else ".") if st["measures"] else "",
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
    source_rows = []
    for key, what, where in SOURCES:
        s = status.get(key)
        source_rows.append([what, where, (s or {}).get("last_ok"), bool(s and s["ok"])])
    ok_count = sum(1 for s in status.values() if s["ok"])

    data = {
        "built_at": iso(), "sources_count": str(ok_count), "period": "past year", "site_url": SITE_URL,
        "funders_total": f"{len(ranked):,}", "beneficiaries_total": f"{len(agency_count):,}",
        "election_total": f"{len(com_ranked):,}", "election": election,
        "industry_total": f"{len(industry_ranked):,}", "industry": industry,
        "totals": {"funders": len(ranked), "industry": len(industry_ranked), "election": len(com_ranked),
                   "beneficiaries": len(agency_count)},
        "links": {"data": f"{REPO_URL}/tree/data", "code": REPO_URL,
                  "report": f"{REPO_URL}/issues/new?template=error.yml"},
        "analytics": {"goatcounter": "dukewilder"},
        "exhibit": exhibit, "index": index, "grid": grid, "polls": polls, "numbers": numbers, "site": site,
        "quotes": quotes[:5], "already_law": already_law, "law_total": law_total,
        "states": state_rows, "states_total": len(state_rows), "runs_today": runs_today,
        "feed_today": sum(1 for i in feed if (i.get("time_iso") or "") >= (today - dt.timedelta(days=1)).isoformat()),
        "tracked": {"measures": all_measures, "filings": all_filings, "orgs": len(ents.items)},
        "fears_tracked": [f["name"] if f["name"].startswith(("AI", "China")) else f["name"][0].lower() + f["name"][1:]
                          for f in fears],
        "fears": fear_rows, "feed_types": FEED_TYPES, "feed": feed[:80], "funders": funders,
        "beneficiaries": beneficiaries, "controls": control_rows, "sources": source_rows,
        "schedule": SCHEDULE, "fear_pages": fear_pages, "org_pages": org_pages,
    }
    (out / "site_data.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    (out / "status.json").write_text(json.dumps(status, indent=1, default=str))
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
    ("gdelt", "News coverage", "GDELT"),
    ("wikipedia", "Public attention", "Wikipedia pageviews"),
    ("rss", "Organization statements", "Newsroom feeds of tracked organizations"),
    ("tag", "Fear and control labels", "Claude, two passes that must agree on a quoted line"),
]
SCHEDULE = [
    ["Hourly", "News, statements, federal rules, and the feed"],
    ["Daily", "Bills, lobbying, donations, labels, and every ranking"],
    ["Quarterly", "New lobbying reports, filed 20 days after each quarter ends"],
]
FEED_TYPES = [["all", "All"], ["bill", "Bills"], ["rule", "Rules and orders"], ["lobbying", "Lobbying"],
              ["donation", "Donations"], ["spending", "Election spending"], ["statement", "Statements"]]


def window_30(db, prefix, today):
    """Thirty days of a daily series, ending at the last day the series actually has.

    A source that stops answering must not quietly shorten the window and leave every
    fear looking quieter than it is. The count stays a true thirty days and lags instead.
    """
    last = db.execute("SELECT MAX(date) FROM series WHERE series LIKE ?", (prefix + ":%",)).fetchone()[0]
    end = min(last, today.isoformat()) if last else today.isoformat()
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=29)).isoformat()
    rows = db.execute("SELECT series s, SUM(value) v FROM series WHERE series LIKE ? AND date BETWEEN ? AND ? "
                      "GROUP BY series", (prefix + ":%", start, end)).fetchall()
    return {r["s"][len(prefix) + 1:]: r["v"] or 0 for r in rows}, end


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


GRID_CHANNELS = [("bills", "Bills"), ("states", "States"), ("congress", "Congress"), ("filings", "Lobbying filings"),
                 ("statements", "Statements"), ("news30", "News, 30 days"), ("wiki30", "Wikipedia, 30 days")]


def build_grid(order, stats, ends=None, today=None):
    """The same nine fears down the side, every channel across the top, every cell a count."""
    tops = {key: max((channel_value(stats[f["slug"]], key) for f in order), default=0) for key, _ in GRID_CHANNELS}
    rows = []
    for f in order:
        st = stats[f["slug"]]
        cells = []
        for key, _ in GRID_CHANNELS:
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
        parts.append(f"{len(st['measures']):,} bills")
    if st["states"]:
        parts.append(f"{len(st['states'])} states")
    if st["filings"]:
        parts.append(f"{st['filings']:,} lobbying filings")
    if st["wiki30"] and len(parts) < 3:
        parts.append(f"{compact(st['wiki30'])} Wikipedia views this month")
    if st["news30"] and len(parts) < 3:
        parts.append(f"{st['news30']:,} news articles this month")
    return " · ".join(parts[:3])


def law_date(m, today):
    """'Passed' with its date, or 'Effective' when the date on file is still ahead of us."""
    d = (m["latest_action_date"] or m["introduced_date"] or "")[:10]
    if not d:
        return "Passed"
    return f"Effective {d}" if d > today.isoformat() else f"Passed · {d}"


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
        who = [a.strip() for a in m["agencies"] if name_key(a)]
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
    for m in measures:
        when = m["latest_action_date"] or m["introduced_date"] or ""
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
                          "text": f"{nice_name(r['client']) or 'A lobbying client'} reported {money(r['amount'])} in lobbying that mentions AI{refs}",
                          "url": r["url"], "time_iso": r["posted"][:19] + "+00:00", "time": r["posted"][:10],
                          "fears": r["fears"], "org": r["entity"] or slugify(nice_name(r["client"]))})
    for r in receipts:
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
        ent = ents.by_slug.get(p["entity"], {})
        items.append({"type": "statement", "label": "Statement", "text": f"{ent.get('name', p['entity'])}: {cut(p['title'] or '', 160)}",
                      "url": p["url"], "time_iso": p["published"], "time": p["published"][:10],
                      "fears": sorted(post_tags.get(p["id"], [])), "org": p["entity"]})
    items.sort(key=lambda i: i["time_iso"] or "", reverse=True)
    return items


def bill_headline(title):
    """The name a legislature gave a bill, without the drafting boilerplate after it."""
    head = re.split(r"\s*;", (title or "").strip(), 1)[0]
    head = re.sub(r"^(an?\s+act\s+(relating to|providing for|concerning|to)\s+)", "", head, flags=re.I)
    return head.strip().strip('"').strip("\u201c\u201d").strip()


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
    if arts:
        words = [set(w for w in re.findall(r"[a-z]{4,}", a["title"].lower())) for a in arts]

        def score(i):
            return sum(len(words[i] & w) / (len(words[i] | w) or 1) for j, w in enumerate(words) if j != i)
        pick = arts[max(range(len(arts)), key=score)]
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
            bits.append(f"{len(st['measures']):,} bills")
        if st["states"]:
            bits.append(f"in {len(st['states'])} states")
        if st["filings"]:
            bits.append(f"{st['filings']:,} lobbying filings")
        line = f"{best['name']}: " + (", ".join(bits) if bits else f"index {st['index']}")
    rows = [["Index", f"{st['index']} of 100, rank 1 of {len(order)}"]]
    if st["wiki30"] or st["news30"]:
        att = []
        if st["wiki30"]:
            att.append(f"{st['wiki30']:,} Wikipedia views")
        if st["news30"]:
            att.append(f"{st['news30']:,} news articles")
        rows.append(["Attention", ", ".join(att) + " this month"])
    if st["measures"]:
        where = f", in {len(st['states'])} states" if st["states"] else ""
        rows.append(["Bills", f"{len(st['measures']):,} since January 2025{where}" + (", plus Congress" if st["federal"] else "")])
    if st["controls"]:
        rows.append(["Controls", f"{st['controls']:,} written into those bills"])
    gains, gain_name = collections.Counter(), {}
    for m in st["measures"]:
        for a in m["agencies"]:
            k = name_key(a)
            if k:
                gains[k] += 1
                gain_name.setdefault(k, a.strip())
    if gains:
        k, n = gains.most_common(1)[0]
        rows.append(["Goes to", f"{cut(gain_name[k], 58)}, in {n} of them"])
    if st["filings"]:
        rows.append(["Lobbying", f"{st['filings']:,} filings name it, past year"])
    if st["advocacy"]:
        rows.append(["Funding", f"{money(st['advocacy'])} from advocacy groups, past year"])
    if source:
        rows.append([source_label, source])
    return {"chyron": f"Loudest fear right now: {best['name']}", "line": line, "line_url": line_url,
            "rows": rows, "fear_slug": best["slug"]}


def quarter_axis():
    y = now().year
    return [(yy, q) for yy in (y - 2, y - 1, y) for q in (1, 2, 3, 4)]


def fear_page(f, rank, of, fear_stats, lob, feed, today, db, control_by, page_slugs, ents):
    st = fear_stats[f["slug"]]
    axis = quarter_axis()
    idx = {k: i for i, k in enumerate(axis)}
    money_q = collections.Counter()
    for r in lob:
        if f["slug"] in r["fears"] and r["quarter"]:
            k = (r["year"], int(r["quarter"][1]))
            if k in idx:
                money_q[idx[k]] += r["amount"] or 0
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
    lanes = ["Lobbying filings naming it", "Bills citing it", "Wikipedia views"]
    quarters = [f"Q{q} {y}" for y, q in axis]
    timeline = {"aria": f"Lobbying money, bills, and public attention for {f['name']} by quarter",
                "years": [str(axis[0][0]), str(axis[4][0]), str(axis[8][0])], "funding": funding,
                "messages": dots, "concern": concern, "lanes": lanes, "quarters": quarters,
                "funding_raw": {str(q): money(v) for q, v in money_q.items() if v > 0},
                "bills_raw": {str(q): int(v) for q, v in bills_q.items() if v},
                "wiki_raw": {str(q): f"{int(v):,}" for q, v in wiki_q.items()},
                "first_label": compact(wiki_q[min(wiki_q)]) if wiki_q else "",
                "last_label": compact(wiki_q[max(wiki_q)]) if wiki_q else "", "events": []}
    window = recent_quarters(4)
    pushers = collections.defaultdict(lambda: {"amount": 0.0, "n": 0, "name": "", "entity": None})
    for r in lob:
        if f["slug"] in r["fears"] and r["quarter"] and (r["year"], int(r["quarter"][1])) in window:
            key = r["entity"] or r["client_key"]
            p = pushers[key]
            p["amount"] += r["amount"] or 0
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
    agencies = collections.Counter()
    names = {}
    for m in st["measures"]:
        for a in m["agencies"]:
            k = name_key(a)
            agencies[k] += 1
            names.setdefault(k, a)
    ben_rows = [{"rank": i, "name": names[k], "slug": None, "type": "Attorney general" if "attorney general" in k else "Agency",
                 "gains": "new authority", "score": str(n), "unit": "bills"} for i, (k, n) in enumerate(agencies.most_common(5), 1)]
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
        evidence.append([f"{st['news30']:,}", "news articles about it in the last 30 days"])
    if st["measures"]:
        evidence.append([f"{len(st['measures']):,}", "bills, rules and orders cite it since January 2025"
                         + (f", in {len(st['states'])} states" if st["states"] else "")
                         + (" and Congress" if st["federal"] and st["states"] else ", in Congress" if st["federal"] else "")])
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
                      {"name": "Attention", "cls": "k3", "pct": (st.get("parts") or {}).get("attention", 0),
                       "value": (compact(st["wiki30"] + st["news30"]) if (st["wiki30"] or st["news30"]) else "0")}],
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
    fear_amounts = collections.Counter()
    window = recent_quarters(4)
    for r in filings:
        if r["quarter"] and (r["year"], int(r["quarter"][1])) in window:
            for s in r["fears"]:
                fear_amounts[s] += r["amount"] or 0
    top = max(fear_amounts.values() or [1])
    fears = [{"name": s.replace("-", " ").capitalize(), "slug": s, "amount": money(v), "bar": max(3, round(100 * v / top))}
             for s, v in fear_amounts.most_common(6)]
    fear_names = {f["slug"]: f["name"] for f in config("fears")}
    for row in fears:
        row["name"] = fear_names.get(row["slug"], row["name"])
    lobbying = []
    for r in filings[:8]:
        chips = sorted({control_by[c]["chip"] for ref in (r["bill_refs"] or "").split(", ") if ref in by_ident
                        for c in by_ident[ref]["controls"]})
        lobbying.append({"bill": r["bill_refs"] or "No bill numbers listed", "amount": money(r["amount"]),
                         "period": f"{r['quarter']} {r['year']}", "controls": chips, "url": r["url"]})
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
    parts = []
    if o["lobbying"]:
        parts.append(f"{money(o['lobbying'])} in lobbying that mentions AI")
    if o["election"]:
        parts.append(f"{money(o['election'])} in election money")
    if len(parts) == 1:  # the big number above already says how much
        parts = ["lobbying that mentions AI" if o["lobbying"] else "election money"]
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
