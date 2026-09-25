"""Fears the report does not follow, measured the way the ones it does are.

The fears are a fixed list, because a label only means something if its definition holds still.
A fixed list can fall behind, though: a fear that grows after the list was settled is invisible
to the index, however many bills cite it. So every fear in config/fear_candidates.json is measured
here on the index's four channels, alongside the fears the site follows, and ranked with the
index's own formula:

    bills      AI measures whose title and summary carry every term the fear is defined by
    filings    lobbying filings from the last four reported quarters that name one of its keywords
    news       GDELT's article count for its query over the last 30 days it has counted
    wikipedia  readership of its articles over the last 30 days

Every fear is counted the same way here, the ones on the site included, so the comparison is like
for like; a followed fear's labelled bills are kept beside its count for reference. It changes
nothing on the site. The weekly review lists any candidate that outscores a fear the site follows,
and the list is changed by a person.

    python -m pipeline.candidates --db state/index.db
"""
import argparse
import datetime as dt
import math
import re
import time
import urllib.parse

from .common import Http, HttpError, config, connect, iso, kv_get, kv_set, log, measures_in_scope

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
WIKI = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia.org/all-access/user"
SPACING = 11.0   # GDELT asks for one call every five seconds; the news collector keeps to eleven
BUDGET = 300     # seconds of news calls per run; a candidate not reached waits for the next one
WEIGHTS = {"bills": 0.40, "filings": 0.30, "news30": 0.15, "wiki30": 0.15}  # the index's own


def keyword_patterns(words):
    """The lobbying keywords the way common.fear_keywords reads them: a word, or a stem with a star."""
    return [re.compile(r"\b" + re.escape(w.lower().rstrip("*")) + ("" if w.endswith("*") else r"\b"), re.I)
            for w in words]


def news_due(db, slug):
    recent = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    return db.execute("SELECT 1 FROM series WHERE series=? AND date >= ? LIMIT 1",
                      (f"cand-news:{slug}", recent)).fetchone() is None


def fetch_news(db, http, fears, started):
    """GDELT's daily volume for each candidate, three months at a time. Returns (read, errors)."""
    read, errors = 0, []
    for f in fears:
        if not news_due(db, f["slug"]):
            continue
        if time.time() - started > BUDGET:
            break
        try:
            tl = http.json(GDELT, tries=2, params={"query": f"{f['gdelt']} sourcelang:english",
                                                    "mode": "TimelineVolRaw", "timespan": "3m",
                                                    "format": "json"}).get("timeline", [])
        except HttpError as exc:
            errors.append(f"{f['slug']}: {'rate limited' if '429' in str(exc) else str(exc)[:80]}")
            continue
        daily = {}
        for series in tl[:1]:
            for point in series.get("data", []):
                day = f"{point['date'][0:4]}-{point['date'][4:6]}-{point['date'][6:8]}"
                daily[day] = daily.get(day, 0) + (point.get("value") or 0)
        for day, value in daily.items():
            db.execute("INSERT INTO series(series,date,value) VALUES(?,?,?) "
                       "ON CONFLICT(series,date) DO UPDATE SET value=excluded.value", (f"cand-news:{f['slug']}", day, value))
        read += bool(daily)
        db.commit()
    return read, errors


def fetch_wiki(db, http, fears):
    end = (dt.date.today() - dt.timedelta(days=1)).strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=40)).strftime("%Y%m%d")
    missing = []
    for f in fears:
        totals = {}
        for article in f.get("wikipedia", []):
            try:
                items = http.json(f"{WIKI}/{urllib.parse.quote(article, safe='')}/daily/{start}/{end}").get("items", [])
            except HttpError as exc:
                missing.append(f"{article} ({exc.status})")
                continue
            for it in items:
                ts = it["timestamp"]
                day = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
                totals[day] = totals.get(day, 0) + it.get("views", 0)
        for day, views in totals.items():
            db.execute("INSERT INTO series(series,date,value) VALUES(?,?,?) "
                       "ON CONFLICT(series,date) DO UPDATE SET value=excluded.value", (f"cand-wiki:{f['slug']}", day, views))
        db.commit()
    return missing


def last_30(db, series):
    """Thirty days ending at the last day the series has anything, as export.window_30 counts them."""
    last = db.execute("SELECT MAX(date) FROM series WHERE series=? AND value > 0", (series,)).fetchone()[0]
    if not last:
        return 0
    start = (dt.date.fromisoformat(last) - dt.timedelta(days=29)).isoformat()
    return db.execute("SELECT COALESCE(SUM(value), 0) FROM series WHERE series=? AND date BETWEEN ? AND ?",
                      (series, start, last)).fetchone()[0]


def measure(db, followed, candidates, today=None):
    """Every fear on every channel, counted the same way, and scored with the index's formula."""
    from .export import recent_quarters
    today = today or dt.date.today()
    ms = measures_in_scope(db)
    ids = {m["id"] for m in ms}
    labelled = {}
    for target, value in db.execute("SELECT target, value FROM tags WHERE kind='fear'"):
        if target in ids:
            labelled[value] = labelled.get(value, 0) + 1
    window = recent_quarters(4, today)
    latest = {}
    for r in db.execute("SELECT registrant, client_key, year, quarter, issues FROM lobbying "
                        "WHERE amount IS NOT NULL ORDER BY posted"):
        latest[(r["registrant"], r["client_key"], r["year"], r["quarter"])] = r["issues"] or ""
    filings = [issues for (_, _, year, quarter), issues in latest.items()
               if (year, int(quarter[1]) if quarter else 0) in window]
    rows = []
    for f, kind in [(f, "followed") for f in followed] + [(f, "candidate") for f in candidates]:
        rules = [re.compile(p, re.I) for p in f.get("match") or []]
        hits = [m for m in ms if rules and all(r.search(f"{m['title'] or ''} {m['summary'] or ''}") for r in rules)]
        pats = keyword_patterns(f.get("keywords", []))
        prefix = "news" if kind == "followed" else "cand-news"
        wprefix = "wiki" if kind == "followed" else "cand-wiki"
        rows.append({"slug": f["slug"], "name": f["name"], "kind": kind,
                     "bills": len(hits), "states": len({m["jurisdiction"] for m in hits}),
                     "labelled": labelled.get(f["slug"]) if kind == "followed" else None,
                     "filings": sum(1 for text in filings if any(p.search(text) for p in pats)),
                     "news30": int(last_30(db, f"{prefix}:{f['slug']}")),
                     "wiki30": int(last_30(db, f"{wprefix}:{f['slug']}")),
                     "has_wiki": bool(f.get("wikipedia")),
                     "has_news": db.execute("SELECT 1 FROM series WHERE series=? LIMIT 1",
                                            (f"{prefix}:{f['slug']}",)).fetchone() is not None})
    tops = {k: max((r[k] for r in rows), default=0) for k in WEIGHTS}
    for r in rows:
        parts, weight = 0.0, 0.0
        for key, w in WEIGHTS.items():
            if (key == "wiki30" and not r["has_wiki"]) or not tops[key]:
                continue
            parts += w * math.log1p(r[key]) / math.log1p(tops[key])
            weight += w
        r["score"] = round(100 * parts / weight) if weight else 0
    rows.sort(key=lambda r: -r["score"])
    return rows


def run(db, state, mode):
    followed, candidates = config("fears"), config("fear_candidates")
    if not candidates:
        state["message"] = "no candidates on file"
        return
    started = time.time()
    read, errors = fetch_news(db, Http(min_interval=SPACING), candidates, started)
    missing = fetch_wiki(db, Http(min_interval=0.3), candidates) if mode in ("daily", "backfill") \
        or not kv_get(db, "candidates:report") else []
    rows = measure(db, followed, candidates)
    waiting = [c["slug"] for c in candidates if news_due(db, c["slug"])]
    lowest = min((r["score"] for r in rows if r["kind"] == "followed"), default=0)
    above = [r for r in rows if r["kind"] == "candidate" and r["score"] > lowest]
    kv_set(db, "candidates:report", {"at": iso(), "rows": rows, "news_waiting": waiting,
                                     "lowest_followed": lowest})
    db.commit()
    state["message"] = (f"{len(candidates)} candidates measured, {len(above)} above the lowest fear followed"
                        + (f"; news still to read for {len(waiting)}" if waiting else "")
                        + (f"; {'; '.join(errors[:4])}" if errors else "")
                        + (f"; missing articles: {', '.join(missing)}" if missing else ""))[:700]
    if read:
        log(f"[candidates] news read for {read} candidates")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/index.db")
    ap.add_argument("--no-fetch", action="store_true", help="rank from what is already stored")
    args = ap.parse_args()
    db = connect(args.db)
    if not args.no_fetch:
        run(db, {}, "daily")
    for r in measure(db, config("fears"), config("fear_candidates")):
        print(f"{r['score']:3} {r['kind']:9} {r['name'][:34]:34} bills {r['bills']:4} filings {r['filings']:4} "
              f"news {r['news30']:6} wiki {r['wiki30']:7}")


if __name__ == "__main__":
    main()
