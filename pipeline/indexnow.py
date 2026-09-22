"""Tell the search engines that take IndexNow which pages changed.

Bing, Yandex, Naver, Seznam, Yep and Amazon share one endpoint, and a page announced to one is passed
to the rest. Google does not take IndexNow; it reads the sitemap, which carries the same dates.

site/build.py fingerprints every page as it writes it and keeps a record in state/pages.json: each
address, its fingerprint, when it last changed, and whether that change has been announced yet.
This runs after the site is published and announces what is due:

- a change is announced once it is ten minutes old, so an engine that fetches at once gets the
  published page and not the one GitHub Pages is still replacing;
- a page is announced at most every six hours, since the front page changes on most passes and the
  protocol asks for pages that changed, not a heartbeat;
- a page that has left the site is announced once, so the engines drop it, and the next build
  forgets it.

A failed request is logged and tried again later; it never stops a pass.

    python -m pipeline.indexnow --pages state/pages.json --data state/site_data.json
"""
import argparse
import datetime as dt
import json
import pathlib
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://api.indexnow.org/indexnow"
SETTLE = dt.timedelta(minutes=10)
AGAIN = dt.timedelta(hours=6)
BATCH = 10000            # the protocol's limit per request
BACKOFF = dt.timedelta(hours=1)
KEY_TROUBLE = dt.timedelta(hours=6)   # a refused key is not fixed by asking again in twenty minutes


def when(value):
    try:
        return dt.datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def due(entries, now):
    """The addresses whose last change is old enough to announce and not announced too recently."""
    out = []
    for url, e in sorted(entries.items()):
        pending, sent = when(e.get("pending")), when(e.get("sent"))
        if not pending or now - pending < SETTLE:
            continue
        if sent and now - sent < AGAIN:
            continue
        out.append(url)
    return out


def post(body):
    """Send one batch. Returns the status code and any Retry-After the endpoint gave."""
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Retry-After")


def run(pages_path, site_url, key, state_path=None, now=None, send=post):
    """Announce what is due, mark it sent, and return a line for the log."""
    now = now or dt.datetime.now(dt.timezone.utc)
    pages_path = pathlib.Path(pages_path)
    if not key or not site_url or not pages_path.exists():
        return "[indexnow] not set up: no key, no site address or no page record"
    state_path = pathlib.Path(state_path or pages_path.with_name("indexnow.json"))
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except ValueError:
            state = {}
    wait = when(state.get("retry_at"))
    if wait and now < wait:
        return f"[indexnow] waiting until {state['retry_at']} after a {state.get('status')}"
    entries = json.loads(pages_path.read_text())
    urls = due(entries, now)
    if not urls:
        return "[indexnow] nothing due"
    host = urllib.parse.urlparse(site_url).netloc
    sent, status = 0, None
    for i in range(0, len(urls), BATCH):
        batch = urls[i:i + BATCH]
        status, retry_after = send({"host": host, "key": key, "keyLocation": f"{site_url.rstrip('/')}/{key}.txt",
                                    "urlList": batch})
        if status not in (200, 202):   # 202: received, key still being checked, which is normal at first
            pause = KEY_TROUBLE if status in (403, 422) else BACKOFF
            if retry_after and str(retry_after).isdigit():
                pause = max(pause, dt.timedelta(seconds=int(retry_after)))
            state.update({"retry_at": (now + pause).isoformat(timespec="seconds"), "status": status})
            break
        for url in batch:
            entries[url]["sent"] = now.isoformat(timespec="seconds")
            entries[url]["pending"] = None
        sent += len(batch)
        state.pop("retry_at", None)
    state.update({"last": now.isoformat(timespec="seconds"), "status": status, "sent": sent})
    pages_path.write_text(json.dumps(entries, indent=0, sort_keys=True) + "\n")
    state_path.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    if sent:
        return f"[indexnow] {status}: announced {sent} changed page{'' if sent == 1 else 's'}"
    return f"[indexnow] {status}: announced nothing, tries again after {state.get('retry_at')}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pages", required=True)
    ap.add_argument("--data", required=True, help="site_data.json, for the site's address and key")
    args = ap.parse_args()
    d = json.loads(pathlib.Path(args.data).read_text())
    key = (d.get("site") or {}).get("indexnow_key")
    try:
        print(run(args.pages, d.get("site_url") or "", key))
    except Exception as exc:  # the pass goes on whatever happens here
        print(f"::warning::[indexnow] {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
