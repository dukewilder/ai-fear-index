"""Daily public attention per fear from Wikimedia pageviews."""
import datetime as dt

from .common import Http, HttpError, config, kv_get, kv_set, log

BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia.org/all-access/user"


def run(db, state, mode):
    http = Http(min_interval=0.3)
    end = (dt.date.today() - dt.timedelta(days=1)).strftime("%Y%m%d")
    added, missing = 0, []
    for fear in config("fears"):
        start = "20230101" if mode == "backfill" or not kv_get(db, f"wiki_done:{fear['slug']}") \
            else (dt.date.today() - dt.timedelta(days=10)).strftime("%Y%m%d")
        totals = {}
        for article in fear.get("wikipedia", []):
            try:
                items = http.json(f"{BASE}/{article}/daily/{start}/{end}").get("items", [])
            except HttpError as exc:
                missing.append(f"{article} ({exc.status})")
                continue
            for it in items:
                ts = it["timestamp"]
                day = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
                totals[day] = totals.get(day, 0) + it.get("views", 0)
        for day, views in totals.items():
            db.execute("INSERT INTO series(series,date,value) VALUES(?,?,?) "
                       "ON CONFLICT(series,date) DO UPDATE SET value=excluded.value",
                       (f"wiki:{fear['slug']}", day, views))
            added += 1
        if totals:
            kv_set(db, f"wiki_done:{fear['slug']}", True)
        db.commit()
    state["added"] = added
    state["message"] = ("missing articles: " + ", ".join(missing)) if missing else "ok"
