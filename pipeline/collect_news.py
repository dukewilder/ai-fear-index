"""Headlines per fear, read straight from publishers' own feeds.

GDELT supplies the daily volume the index reads, and it answers or it does not. This does
not touch that series. It keeps the front page in fresh, directly linked stories when GDELT
is refusing, and it agrees with GDELT the rest of the time.

A story counts for a fear only when every pattern that fear lists appears in its headline
or standfirst, which is the same shape as the GDELT query beside it in config/fears.json.
"""
import datetime as dt
import re
import time
import urllib.parse

import feedparser

from .common import (Entities, Http, config, iso, iso_from_struct, kv_get, kv_set, sha, strip_html,
                     tidy_headline,
                     upsert)

BUDGET = 150   # seconds for the whole source; a feed left over is read on the next pass
MAX_AGE = 14   # days: a feed's archive is not news


def run(db, state, mode):
    http = Http(min_interval=0.4, timeout=12)
    ents = Entities()
    tests = [(f["slug"], [re.compile(p, re.I) for p in f.get("match") or []]) for f in config("fears")]
    tests = [(slug, pats) for slug, pats in tests if pats]
    floor = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=MAX_AGE)).isoformat(timespec="seconds")
    sources = config("news")
    # start where the last pass ran out of time, so the last feeds are not always the ones cut
    offset = kv_get(db, "news_offset", 0) % len(sources)
    added, read, matched, failed, skipped = 0, 0, 0, [], 0
    started, stopped = time.time(), None
    for i, src in enumerate(sources[offset:] + sources[:offset]):
        if time.time() - started > BUDGET:
            skipped += 1
            if stopped is None:
                stopped = (offset + i) % len(sources)
            continue
        try:
            body = http.get(src["url"], tries=2).content
        except Exception as exc:
            failed.append(src["name"])
            continue
        for entry in feedparser.parse(body).entries[:80]:
            link = entry.get("link") or ""
            title = strip_html(entry.get("title", ""))
            stamp = entry.get("published_parsed") or entry.get("updated_parsed")
            if not link or len(title) < 20 or not stamp:
                continue  # an undated entry would be dated the moment it was fetched
            seen = iso_from_struct(stamp)
            if seen < floor or seen > iso():
                continue
            read += 1
            text = title + " " + strip_html(entry.get("summary", "") or entry.get("description", ""))[:1200]
            domain = urllib.parse.urlparse(link).netloc.lower().removeprefix("www.")
            hit = False
            for slug, pats in tests:
                if all(p.search(text) for p in pats):
                    hit = True
                    added += upsert(db, "articles", {
                        "id": sha(slug, link), "fear": slug,
                        "title": tidy_headline(title, domain)[:300], "url": link,
                        "domain": domain, "seen": seen, "entity": ents.match_domain(domain)})
            matched += hit
        db.commit()
    kv_set(db, "news_offset", stopped if stopped is not None else 0)
    db.commit()
    state["added"] = added
    state["message"] = (f"{matched} of {read} stories matched a fear, from {len(sources) - len(failed) - skipped} feeds"
                        + (f"; {skipped} left for the next pass" if skipped else "")
                        + (f"; could not read {', '.join(failed[:8])}" if failed else ""))
