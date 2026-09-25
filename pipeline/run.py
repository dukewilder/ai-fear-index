"""Run the pipeline: collect, tag, export.

    python -m pipeline.run --mode auto --db state/index.db --out state

auto = hourly sources every run, plus the daily sources once a day, on the first run at or
after 06:00 UTC that gets through. Any source that has never finished a run is added to the
next run and reaches back to January 2025, so the index fills itself in without anyone
starting a run by hand.
"""
import argparse
import importlib
import time

from .common import (annotate, config, connect, kv_get, kv_set, log, now, retry_at, retry_clear,
                     retry_due, source_run)

HOURLY = ["gdelt", "news", "rss", "fedreg"]
DAILY = ["congress", "openstates", "lda", "fec", "wikipedia"]
# How long a daily source that errored waits before it is tried again. The daily pass is
# marked done whatever happened in it, so without this a source that failed at six in the
# morning would sit red until six the next morning, and a fix pushed at noon would not run
# until then either. That is how one 404 kept the whole register out of the report for a day.
RETRY_HOURS = 3
MODULES = {"gdelt": "collect_gdelt", "news": "collect_news", "rss": "collect_rss",
           "fedreg": "collect_fedreg", "congress": "collect_congress",
           "openstates": "collect_openstates", "lda": "collect_lda", "fec": "collect_fec",
           "wikipedia": "collect_wikipedia", "tag": "tag"}


def overdue(db, today):
    """Daily sources that have not succeeded today.

    The daily pass sets one flag for itself when it finishes, whatever happened inside it. That
    flag marks the pass, not the source: the register errored at 07:21 one morning and the flag
    kept it out of every run for the next twenty-two hours, fix pushed at noon or not. Success is
    already recorded per source, so that is what decides. A source that keeps failing is held off
    by the retry it asks for when it fails, so this cannot become one source running every twenty
    minutes all day.
    """
    ok_today = {r["source"] for r in db.execute(
        "SELECT source FROM status WHERE last_ok >= ?", (today,))}
    return [s for s in DAILY if s not in ok_today
            and (not kv_get(db, f"retry:{s}") or retry_due(db, s))]


def ask_again(db, sources, asked):
    """A daily source that errored is tried again later today rather than tomorrow.

    A source that set its own retry during the run keeps it: openstates asks for another turn
    when it reaches its allowance, which is a success, not a failure. That is why this compares
    the key against what it held before the run instead of simply writing one.
    """
    for name in sources:
        if name not in DAILY:
            continue
        row = db.execute("SELECT ok FROM status WHERE source=?", (name,)).fetchone()
        if row is None or kv_get(db, f"retry:{name}") != asked.get(name):
            continue
        if not row["ok"]:
            retry_at(db, name, RETRY_HOURS)
            log(f"[{name}] errored; another turn in {RETRY_HOURS} hours rather than tomorrow")
        elif asked.get(name):
            retry_clear(db, name)
    db.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "hourly", "daily", "backfill"])
    ap.add_argument("--db", default="state/index.db")
    ap.add_argument("--out", default="state")
    ap.add_argument("--only", default="", help="comma-separated sources to run (for testing)")
    ap.add_argument("--base", default="")
    args = ap.parse_args()
    db = connect(args.db)
    done = {r["source"] for r in db.execute("SELECT source FROM status WHERE last_ok IS NOT NULL")}
    mode = args.mode
    today = now().date().isoformat()
    if mode == "auto":
        # one daily pass per UTC day, however many runs the schedule manages to start
        mode = "daily" if now().hour >= 6 and kv_get(db, "daily_done") != today else "hourly"
    sources = HOURLY + (DAILY if mode in ("daily", "backfill") else [])
    if mode == "hourly":
        sources += [s for s in DAILY if s not in done]  # first run of a source catches it up
    # a source that asked to be tried again later today, having been refused for nothing
    sources += [s for s in DAILY if s not in sources and retry_due(db, s)]
    sources += [s for s in overdue(db, today) if s not in sources]
    # A fear added after the day's reading has no readership until tomorrow's, and the index
    # scores it on fewer channels than it actually has for a day. Wikipedia backfills a fear the
    # first time it sees one, so asking again costs one pass and settles it. Once a day, though:
    # the collector marks a fear done only when it comes back with something, so an article that
    # has no readership to report would otherwise pull this in on every pass for good.
    # The day is kept with the fears still waiting, so an article given to a fear later the same
    # day is read that day too, not after tomorrow's.
    waiting = sorted(f["slug"] for f in config("fears")
                     if f.get("wikipedia") and not kv_get(db, f"wiki_done:{f['slug']}"))
    if "wikipedia" not in sources and waiting and kv_get(db, "wiki_catchup") != f"{today}:{','.join(waiting)}":
        sources.append("wikipedia")
        kv_set(db, "wiki_catchup", f"{today}:{','.join(waiting)}")
        db.commit()
    if args.only:
        sources = [s for s in args.only.split(",") if s in MODULES and s not in ("tag", "texts")]
    log(f"mode={mode} sources={sources} first run for: {[s for s in sources if s not in done] or 'none'}")
    started = time.time()
    asked = {s: kv_get(db, f"retry:{s}") for s in sources}
    for name in sources:
        module = importlib.import_module(f"pipeline.{MODULES[name]}")
        with source_run(db, name) as state:
            # a source that has never finished reaches all the way back, whatever the run mode
            module.run(db, state, "backfill" if name not in done else mode)
    ask_again(db, sources, asked)
    # A known AI law that no search has reached is looked up by its number, so it is on the site
    # today rather than whenever the keyword backfill gets to it.
    if not args.only:
        from . import known
        try:
            known.fetch_missing(db)
        except Exception as exc:  # never the reason a run fails
            log(f"[known] {str(exc)[:200]}")
    # A law whose official summary is missing or a line gets its enacted text read, before tagging,
    # so the tagger reads it the same run (pipeline.texts).
    if not args.only or "texts" in args.only.split(","):
        from . import texts
        with source_run(db, "texts") as state:
            texts.run(db, state, mode)
    if not args.only or "tag" in args.only.split(","):
        from . import tag
        with source_run(db, "tag") as state:
            tag.run(db, state, mode)
    # Once a day, count what the fears on file do not cover. The categories are hand-written, so a
    # fear nobody has named is invisible unless something goes looking; this is what looks. It
    # decides nothing and changes nothing, it writes a list.
    if mode in ("daily", "backfill") and not args.only:
        from . import blindspots
        with source_run(db, "blindspots") as state:
            blindspots.run(db, state, mode)
        # Mondays: the labels most likely to be wrong and the groups the lists miss, as one issue
        # for a person to read. It changes nothing on the site.
        from . import review
        with source_run(db, "review") as state:
            review.run(db, state, mode)
    # Fears the report does not follow, measured beside the ones it does (candidates.py). Once a day:
    # GDELT limits the runner's address, and the fears on the site come first for its news.
    if not args.only and (mode in ("daily", "backfill") or not kv_get(db, "candidates:report")):
        from . import candidates
        with source_run(db, "candidates") as state:
            candidates.run(db, state, mode)
    # Every run, not only on Mondays: a known AI law the record is missing or has wrong shows as a
    # warning on the run, where it is seen the same day.
    from . import known
    wrong = known.problems(db)
    if wrong:
        annotate("warning", f"{len(wrong)} known AI law records are wrong",
                 "; ".join(f"{l['jurisdiction'].upper()} {l['identifier']}: {p}" for l, p in wrong[:8]))
    from . import export
    data = export.export(db, args.out, args.base)
    if mode == "daily":
        kv_set(db, "daily_done", today)
        db.commit()
    log(f"exported: {data['index']['value']} controlled measures, {len(data['fears'])} fears, "
        f"{len(data['funders'])} funders, {len(data['feed'])} feed items in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
