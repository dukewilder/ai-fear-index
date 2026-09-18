"""News coverage per fear from the GDELT DOC 2.0 API (rolling three months)."""
import datetime as dt
import urllib.parse

from .common import Entities, Http, HttpError, config, iso, log, sha, upsert

BASE = "https://api.gdeltproject.org/api/v2/doc/doc"


def run(db, state, mode):
    http = Http(min_interval=6.0)
    ents = Entities()
    added, errors = 0, []
    for fear in config("fears"):
        query = f"{fear['gdelt']} sourcelang:english"
        try:
            arts = http.json(BASE, params={"query": query, "mode": "ArtList", "maxrecords": 250,
                                           "timespan": "3d" if mode != "backfill" else "2w",
                                           "sort": "DateDesc", "format": "json"}).get("articles", [])
        except HttpError as exc:
            errors.append(f"{fear['slug']} articles: {exc}")
            arts = []
        for a in arts:
            url = a.get("url") or ""
            if not url:
                continue
            domain = (a.get("domain") or urllib.parse.urlparse(url).netloc).lower()
            seen = a.get("seendate") or ""
            seen_iso = f"{seen[0:4]}-{seen[4:6]}-{seen[6:8]}T{seen[9:11]}:{seen[11:13]}:00" if len(seen) >= 13 else iso()
            added += upsert(db, "articles", {
                "id": sha(fear["slug"], url), "fear": fear["slug"], "title": (a.get("title") or "").strip(),
                "url": url, "domain": domain, "seen": seen_iso, "entity": ents.match_domain(domain)})
        try:
            tl = http.json(BASE, params={"query": query, "mode": "TimelineVolRaw", "timespan": "3m",
                                         "format": "json"}).get("timeline", [])
            daily = {}
            for series in tl[:1]:
                for point in series.get("data", []):
                    day = f"{point['date'][0:4]}-{point['date'][4:6]}-{point['date'][6:8]}"
                    daily[day] = daily.get(day, 0) + (point.get("value") or 0)
            for day, value in daily.items():
                db.execute("INSERT INTO series(series,date,value) VALUES(?,?,?) "
                           "ON CONFLICT(series,date) DO UPDATE SET value=excluded.value",
                           (f"news:{fear['slug']}", day, value))
        except HttpError as exc:
            errors.append(f"{fear['slug']} timeline: {exc}")
        db.commit()
    cutoff = (dt.date.today() - dt.timedelta(days=120)).isoformat()
    db.execute("DELETE FROM articles WHERE seen < ?", (cutoff,))
    db.commit()
    state["added"] = added
    if errors:
        if len(errors) >= len(config("fears")):
            raise RuntimeError("; ".join(errors)[:700])
        state["message"] = "partial: " + "; ".join(errors)[:600]
