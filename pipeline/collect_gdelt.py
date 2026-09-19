"""News coverage per fear from the GDELT DOC 2.0 API (rolling three months).

GDELT asks for one request every five seconds and answers 429 when a shared
address goes over, which a CI runner often is through no fault of ours. So calls
are spaced well clear of the limit, the whole source gets a time budget, and a
fear that cannot be read this run is simply left for the next one.
"""
import datetime as dt
import time
import urllib.parse

from .common import Entities, Http, HttpError, config, iso, kv_get, kv_set, log, sha, upsert

BASE = "https://api.gdeltproject.org/api/v2/doc/doc"
SPACING = 11.0   # seconds between calls, double what GDELT asks for
BUDGET = 540     # seconds for the whole source, so it cannot eat the hour


def run(db, state, mode):
    http = Http(min_interval=SPACING)
    ents = Entities()
    fears = config("fears")
    # Start where the last run stopped. Without this the same early fears spend the
    # rate limit every hour and the last ones never get any news at all.
    offset = kv_get(db, "gdelt_offset", 0) % len(fears)
    ordered = fears[offset:] + fears[:offset]
    started = time.time()
    added, errors, read, skipped, handled = 0, [], 0, 0, 0
    unserved = None  # the first fear that came away with nothing; next run starts there

    for i, fear in enumerate(ordered):
        if time.time() - started > BUDGET:
            skipped += 1
            if unserved is None:
                unserved = (offset + i) % len(fears)
            continue
        handled += 1
        query = f"{fear['gdelt']} sourcelang:english"
        try:
            arts = http.json(BASE, tries=4, params={
                "query": query, "mode": "ArtList", "maxrecords": 250,
                "timespan": "3d" if mode != "backfill" else "2w",
                "sort": "DateDesc", "format": "json"}).get("articles", [])
        except HttpError as exc:
            errors.append(f"{fear['slug']}: {short(exc)}")
            arts = []
        read += len(arts)
        if not arts and unserved is None:
            unserved = (offset + i) % len(fears)
        for a in arts:
            url = a.get("url") or ""
            if not url:
                continue
            domain = (a.get("domain") or urllib.parse.urlparse(url).netloc).lower()
            seen = a.get("seendate") or ""
            seen_iso = (f"{seen[0:4]}-{seen[4:6]}-{seen[6:8]}T{seen[9:11]}:{seen[11:13]}:00"
                        if len(seen) >= 13 else iso())
            added += upsert(db, "articles", {
                "id": sha(fear["slug"], url), "fear": fear["slug"], "title": (a.get("title") or "").strip(),
                "url": url, "domain": domain, "seen": seen_iso, "entity": ents.match_domain(domain)})

        if needs_timeline(db, fear["slug"], mode) and time.time() - started <= BUDGET:
            try:
                tl = http.json(BASE, tries=4, params={
                    "query": query, "mode": "TimelineVolRaw", "timespan": "3m", "format": "json"}).get("timeline", [])
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
                errors.append(f"{fear['slug']} timeline: {short(exc)}")
        db.commit()

    kv_set(db, "gdelt_offset", unserved if unserved is not None else (offset + 1) % len(fears))
    cutoff = (dt.date.today() - dt.timedelta(days=120)).isoformat()
    db.execute("DELETE FROM articles WHERE seen < ?", (cutoff,))
    db.commit()
    state["added"] = added
    note = f"{read} articles read for {handled} fears, starting at {fears[offset]['slug']}"
    if skipped:
        note += f"; {skipped} fears left for the next run"
    if errors:
        note += "; " + "; ".join(errors)
    state["message"] = note[:700]
    if errors and not read:
        raise RuntimeError(note[:700])


def needs_timeline(db, slug, mode):
    """The volume series covers three months, so an hourly run rarely needs to refetch it."""
    if mode in ("daily", "backfill"):
        return True
    recent = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    row = db.execute("SELECT 1 FROM series WHERE series=? AND date >= ? LIMIT 1",
                     (f"news:{slug}", recent)).fetchone()
    return row is None


def short(exc):
    text = str(exc)
    if "429" in text:
        return "rate limited by GDELT"
    return text[:120]
