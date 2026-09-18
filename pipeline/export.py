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

from .common import (REPO_URL, SINCE, Entities, config, fear_keywords, fears_mentioned, iso, name_key, now, sha)

SMALL = {"of", "and", "for", "the", "in", "on", "to", "a", "an", "at", "by"}
STATUS_LABEL = {"passed": "Passed", "pending": "Pending", "failed": "Failed"}
KIND_LABEL = {"bill": "Bill", "resolution": "Resolution", "rule": "Rule", "order": "Executive order"}


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


def nice_name(name):
    name = re.sub(r",?\s+(INC|LLC|L\.L\.C\.|CORP|CORPORATION|CO|LTD|LP|LLP|PBC)\.?$", "", (name or "").strip(), flags=re.I)
    if not name.isupper():
        return name
    words = []
    for i, w in enumerate(name.split()):
        if len(w) <= 3 and not re.search(r"[AEIOU]", w):
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

    for r in lob_recent:
        if not (r["entity"] or r["client_key"]):
            continue  # a filing with no client name cannot be attributed to anyone
        o = org(r["entity"] or r["client_key"], r["client"], r["entity"])
        o["lobbying"] += r["amount"] or 0
        o["fears"].update(r["fears"])
        o["type"] = o["type"] or "Lobbying client"
    committees = [dict(c) for c in db.execute("SELECT * FROM committees")]
    for c in committees:
        o = org(c["entity"] or name_key(c["name"]), c["name"], c["entity"])
        o["election"] += c["independent_expenditures"] or 0
        o["type"] = o["type"] or "Super PAC"
    receipts = [dict(r) for r in db.execute("SELECT * FROM fec WHERE kind='receipt'")]
    for r in receipts:
        ent = ents.match_name(r["counterparty"])
        if not (ent or r["counterparty_key"]):
            continue
        o = org(ent or r["counterparty_key"], r["counterparty"], ent)
        o["election"] += r["amount"] or 0
        o["type"] = o["type"] or "Donor"
    ranked = sorted((o for o in orgs.values() if o["lobbying"] + o["election"] > 0),
                    key=lambda o: -(o["lobbying"] + o["election"]))
    for i, o in enumerate(ranked, 1):
        o["rank"], o["total"] = i, o["lobbying"] + o["election"]
        o["slug"] = o["entity"] or slugify(o["name"])
    top = ranked[:30]
    page_slugs = {o["slug"] for o in top}
    funders = [{"rank": o["rank"], "name": o["name"], "slug": o["slug"], "type": o["type"],
                "fears": str(len(o["fears"])), "amount": money(o["total"]), "flag": o["flag"]} for o in top]

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
        control_rows.append({"slug": c["slug"], "name": c["name"], "bills": str(len(ms)),
                             "passed": str(sum(m["status"] == "passed" for m in ms)),
                             "pending": str(sum(m["status"] == "pending" for m in ms)), "n": len(ms)})
    control_rows.sort(key=lambda r: -r["n"])
    for i, r in enumerate(control_rows, 1):
        r["rank"] = i

    # ---------------- fears ranking
    fear_stats = {}
    for f in fears:
        ms = [m for m in measures if f["slug"] in m["fears"]]
        fear_stats[f["slug"]] = {
            "measures": ms, "controls": sum(len(m["controls"]) for m in ms),
            "with_controls": sum(1 for m in ms if m["controls"]),
            "states": sorted({m["jurisdiction"] for m in ms if m["jurisdiction"] not in ("us", "us-exec")}),
            "federal": any(m["jurisdiction"] in ("us", "us-exec") for m in ms),
            "new_week": sum(1 for m in ms if m["controls"] and (m["introduced_date"] or "") >= week_ago)}
    order = sorted(fears, key=lambda f: (-fear_stats[f["slug"]]["controls"], -len(fear_stats[f["slug"]]["measures"])))
    top_controls = max([fear_stats[f["slug"]]["controls"] for f in fears] + [1])
    prev = previous_snapshot(db, today)
    fear_rows = []
    for i, f in enumerate(order, 1):
        st = fear_stats[f["slug"]]
        move = None
        if prev and f["slug"] in prev.get("fear_rank", {}):
            delta = prev["fear_rank"][f["slug"]] - i
            move = {"dir": "up", "text": str(delta)} if delta > 0 else {"dir": "down", "text": str(-delta)} if delta < 0 else None
        fear_rows.append({"rank": i, "name": f["name"], "slug": f["slug"], "score": f"{st['controls']:,}",
                          "bar": max(3, round(100 * st["controls"] / top_controls)) if st["controls"] else 3,
                          "move": move})

    # ---------------- feed
    feed = [i for i in build_feed(db, ents, measures, lob, receipts, today) if i["url"] not in suppressed]

    # ---------------- exhibit: the loudest fear this week
    exhibit = build_exhibit(db, fears, fear_stats, today, suppressed)

    # ---------------- index (headline)
    cutoffs = [today - dt.timedelta(days=7 * k) for k in range(12, -1, -1)]
    cum = [sum(1 for m in controlled if (m["introduced_date"] or "9999") <= c.isoformat()) for c in cutoffs]
    lo, hi = min(cum), max(cum)
    trend = [50 if hi == lo else round(15 + 70 * (v - lo) / (hi - lo)) for v in cum]
    new_week = sum(1 for m in controlled if (m["introduced_date"] or "") >= week_ago)
    index = {"value": f"{len(controlled):,}", "suffix": "",
             "text": "bills, rules, and orders introduced since January 2025 would add new controls over AI, "
                     "the companies building it, or the people using it",
             "change": f"▲ {new_week:,} new this week" if new_week else None, "trend": trend,
             "total_measures": len(measures)}

    # ---------------- fear pages and org pages
    fear_pages = [fear_page(f, i + 1, len(fears), fear_stats, lob, feed, today, db, control_by, page_slugs, ents)
                  for i, f in enumerate(order)]
    org_pages = [org_page(o, len(ranked), lob, receipts, committees, feed, measures, control_by) for o in top]

    # ---------------- sources and status
    status = {r["source"]: dict(r) for r in db.execute("SELECT * FROM status")}
    source_rows = []
    for key, what, where in SOURCES:
        s = status.get(key)
        source_rows.append([what, where, (s or {}).get("last_ok"), bool(s and s["ok"])])
    ok_count = sum(1 for s in status.values() if s["ok"])

    data = {
        "built_at": iso(), "sources_count": str(ok_count), "period": "past year",
        "funders_total": f"{len(ranked):,}", "beneficiaries_total": f"{len(agency_count):,}",
        "links": {"data": f"{REPO_URL}/tree/data", "code": REPO_URL,
                  "report": f"{REPO_URL}/issues/new?template=error.yml"},
        "analytics": {"goatcounter": "dukewilder"},
        "exhibit": exhibit, "index": index,
        "fears_tracked": [f["name"] if f["name"].startswith(("AI", "China")) else f["name"][0].lower() + f["name"][1:]
                          for f in fears],
        "fears": fear_rows, "feed_types": FEED_TYPES, "feed": feed[:80], "funders": funders,
        "beneficiaries": beneficiaries, "controls": control_rows, "sources": source_rows,
        "schedule": SCHEDULE, "fear_pages": fear_pages, "org_pages": org_pages,
    }
    (out / "site_data.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    (out / "status.json").write_text(json.dumps(status, indent=1, default=str))
    write_csvs(out / "public", measures, lob_recent, ranked)
    save_snapshot(db, today, {"fear_rank": {r["slug"]: r["rank"] for r in fear_rows},
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


def build_feed(db, ents, measures, lob, receipts, today):
    items = []
    horizon = (today - dt.timedelta(days=21)).isoformat()
    for m in measures:
        when = m["latest_action_date"] or m["introduced_date"] or ""
        if when >= horizon:
            label = KIND_LABEL.get(m["kind"], "Bill")
            action = f" ({m['latest_action'][:90]})" if m["latest_action"] and m["kind"] not in ("rule", "order") else ""
            items.append({"type": "rule" if m["kind"] in ("rule", "order") else "bill", "label": label,
                          "text": f"{m['jurisdiction_name']} {m['identifier']}: {(m['title'] or 'Untitled')[:150]}{action}",
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
                          "fears": [], "org": ents.match_name(r["counterparty"]) or slugify(nice_name(r["counterparty"]))})
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
        items.append({"type": "statement", "label": "Statement", "text": f"{ent.get('name', p['entity'])}: {(p['title'] or '')[:160]}",
                      "url": p["url"], "time_iso": p["published"], "time": p["published"][:10],
                      "fears": sorted(post_tags.get(p["id"], [])), "org": p["entity"]})
    items.sort(key=lambda i: i["time_iso"] or "", reverse=True)
    return items


def build_exhibit(db, fears, fear_stats, today, suppressed=()):
    d7, d14 = (today - dt.timedelta(days=7)).isoformat(), (today - dt.timedelta(days=14)).isoformat()
    best, best_v, prev_v = None, 0, 0
    for f in fears:
        cur = db.execute("SELECT SUM(value) v FROM series WHERE series=? AND date > ?", (f"news:{f['slug']}", d7)).fetchone()["v"] or 0
        old = db.execute("SELECT SUM(value) v FROM series WHERE series=? AND date > ? AND date <= ?",
                         (f"news:{f['slug']}", d14, d7)).fetchone()["v"] or 0
        if cur > best_v:
            best, best_v, prev_v = f, cur, old
    if not best:
        return None
    arts = [dict(a) for a in db.execute("SELECT * FROM articles WHERE fear=? AND seen > ? AND length(title) > 25",
                                        (best["slug"], d7)) if a["url"] not in suppressed]
    if not arts:
        return None
    words = [set(w for w in re.findall(r"[a-z]{4,}", a["title"].lower())) for a in arts]
    def score(i):
        return sum(len(words[i] & w) / (len(words[i] | w) or 1) for j, w in enumerate(words) if j != i)
    pick = arts[max(range(len(arts)), key=score)]
    st = fear_stats[best["slug"]]
    change = None
    if prev_v:
        pct = round(100 * (best_v - prev_v) / prev_v)
        change = f"up {pct}% from last week" if pct > 0 else f"down {-pct}% from last week" if pct < 0 else "flat from last week"
    rows = [["Coverage", f"{int(best_v):,} articles this week"]]
    if change:
        rows.append(["Change", change])
    rows += [["Bills citing it", f"{len(st['measures']):,} since January 2025"],
             ["Controls in them", f"{st['controls']:,}"],
             ["Headline from", pick["domain"]]]
    return {"chyron": f"Loudest this week: {best['name']}", "line": pick["title"], "line_url": pick["url"],
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
    lanes = ["Lobbying money", "Bills citing it", "Wikipedia views"]
    timeline = {"aria": f"Lobbying money, bills, and public attention for {f['name']} by quarter",
                "years": [str(axis[0][0]), str(axis[4][0]), str(axis[8][0])], "funding": funding,
                "messages": dots, "concern": concern, "lanes": lanes,
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
    push_rows = []
    for i, (key, p) in enumerate(sorted(pushers.items(), key=lambda kv: -kv[1]["amount"])[:5], 1):
        slug = p["entity"] or slugify(p["name"])
        ent = ents.by_slug.get(p["entity"]) if p["entity"] else None
        push_rows.append({"rank": i, "name": p["name"], "slug": slug if slug in page_slugs else None,
                          "type": ent["type"] if ent else "Lobbying client", "messages": f"{p['n']}",
                          "amount": money(p["amount"]), "flag": (ent or {}).get("flag")})
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
    bill_rows = [{"name": f"{m['identifier'] or 'Measure'}, {m['jurisdiction_name']}", "title": (m["title"] or "")[:160],
                  "status": STATUS_LABEL.get(m["status"], "Pending"), "url": m["url"],
                  "controls": [control_by[c]["chip"] for c in m["controls"]]} for m in bills]
    evidence = [[f"{len(st['measures']):,}", "bills, rules, and orders cite this fear since January 2025"],
                [f"{st['with_controls']:,}", "of them would add new controls"]]
    if st["states"]:
        evidence.append([f"{len(st['states'])} states", "have bills citing it" + (", plus Congress or federal agencies" if st["federal"] else "")])
    if lob_year:
        evidence.append([money(lob_year), "in lobbying on filings that mention it, past year"])
    latest = [i for i in feed if f["slug"] in (i.get("fears") or [])][:6]
    return {"slug": f["slug"], "name": f["name"], "rank": str(rank), "of": str(of), "score": f"{st['controls']:,}",
            "score_text": "new controls in the bills, rules, and orders that cite this fear",
            "change": f"▲ {st['new_week']} new this week" if st["new_week"] else None, "dir": "up",
            "evidence": evidence, "timeline": timeline,
            "timeline_note": (f"Each dot is {unit} bills." if unit > 1 else "Each dot is one bill."),
            "timeline_sources": "LDA.gov lobbying filings that mention this fear, bills tagged with it, and Wikipedia pageviews for its topic.",
            "events": [], "latest": latest, "pushers": push_rows, "beneficiaries": ben_rows, "bills": bill_rows,
            "sentence": None, "reach": None}


def org_page(o, of, lob, receipts, committees, feed, measures, control_by):
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
    money_in = []
    for c in own:
        money_in += [{"from": nice_name(r["counterparty"]), "amount": money(r["amount"]), "year": (r["date"] or "")[:4],
                      "source": "FEC", "url": r["url"]} for r in receipts if r["committee_id"] == c["id"]][:10]
    latest = [i for i in feed if i.get("org") == o["slug"]][:6]
    parts = []
    if o["lobbying"]:
        parts.append(f"{money(o['lobbying'])} in lobbying that mentions AI")
    if o["election"]:
        parts.append(f"{money(o['election'])} in election money")
    return {"slug": o["slug"], "name": o["name"], "type": o["type"], "flag": o["flag"], "rank": str(o["rank"]),
            "of": f"{of:,}", "spent": money(o["total"]), "period": "past year", "breakdown": " and ".join(parts),
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


def write_csvs(folder, measures, lob_recent, ranked):
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
        w.writerow(["rank", "name", "type", "lobbying mentioning AI", "election money", "total"])
        for o in ranked:
            w.writerow([o["rank"], o["name"], o["type"], round(o["lobbying"]), round(o["election"]), round(o["total"])])
