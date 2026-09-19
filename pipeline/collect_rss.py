"""Institutional messaging: AI-related posts from tracked organizations' newsroom feeds."""
import re
import time
import urllib.parse

import feedparser

from .common import (Entities, Http, HttpError, iso, iso_from_struct, kv_get, kv_set, log, looks_ai, sha,
                     strip_html, upsert)

DISCOVERY_BUDGET = 360  # seconds per run; unfinished sites are picked up next run
LINK_TAG = re.compile(r"<link[^>]+>", re.I)
COMMON_PATHS = ["feed", "rss", "feed.xml", "rss.xml", "index.xml", "atom.xml", "news/rss.xml", "blog/rss.xml", "news/feed"]


def discover(http, homepage):
    try:
        html = http.get(homepage, tries=2).text
    except Exception:
        html = ""
    for tag in LINK_TAG.findall(html):
        if re.search(r"application/(rss|atom)\+xml", tag, re.I):
            m = re.search(r'href=["\']([^"\']+)', tag, re.I)
            if m:
                return urllib.parse.urljoin(homepage, m.group(1))
    base = homepage if homepage.endswith("/") else homepage + "/"
    for path in COMMON_PATHS:
        url = urllib.parse.urljoin(base, path)
        try:
            resp = http.get(url, tries=1)
        except Exception:
            continue
        if re.search(r"<(rss|feed)[\s>]", resp.text[:2000], re.I):
            return url
    return None


def run(db, state, mode):
    http = Http(min_interval=1.0, timeout=12)
    ents = Entities()
    feeds = kv_get(db, "feeds", {})
    added, found, failed, deferred, undated = 0, 0, [], 0, 0
    started = time.time()
    for ent in ents.items:
        home = ent.get("homepage")
        known = ent.get("rss")  # a feed found by hand, where the site advertises none
        if not home and not known:
            continue
        feed_url = known or feeds.get(ent["slug"])
        if not known and (feed_url is None or (feed_url == "" and mode == "backfill")):
            if time.time() - started > DISCOVERY_BUDGET:
                deferred += 1  # leave it unrecorded so the next run tries again
                continue
            feed_url = discover(http, home) or ""
            feeds[ent["slug"]] = feed_url
        if not feed_url:
            failed.append(ent["name"])
            continue
        try:
            body = http.get(feed_url, tries=2).content
        except Exception as exc:
            failed.append(ent["name"])
            continue
        parsed = feedparser.parse(body)
        found += 1
        for e in parsed.entries[:60]:
            title = strip_html(e.get("title", ""))
            summary = strip_html(e.get("summary", "") or e.get("description", ""))[:3000]
            link = e.get("link") or ""
            if not link or not looks_ai(title, summary):
                continue
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if not ts:
                undated += 1
                continue  # no date on the entry, and stamping it "now" would date it wrongly
            published = iso_from_struct(ts)
            added += upsert(db, "posts", {
                "id": sha(ent["slug"], link), "entity": ent["slug"], "title": title, "summary": summary,
                "url": link, "published": published, "feed": feed_url, "first_seen": iso()})
        db.commit()
    stale = drop_undated(db)
    kv_set(db, "feeds", feeds)
    db.commit()
    state["added"] = added
    state["message"] = (f"{found} feeds read"
                        + (f"; {deferred} sites left for the next run" if deferred else "")
                        + (f"; skipped {undated} undated entries" if undated else "")
                        + (f"; removed {stale} wrongly dated" if stale else "")
                        + (f"; no feed found for {len(failed)}: {', '.join(failed[:12])}" if failed else ""))


def drop_undated(db):
    """Remove posts stored before undated entries were skipped, which carry the time they were fetched."""
    cur = db.execute("DELETE FROM posts WHERE published = first_seen")
    return cur.rowcount
