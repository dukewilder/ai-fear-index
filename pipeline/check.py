"""A third reading of every control and office label: does the measure actually do this?

The first pass proposes labels and the second backs each one with a quote that code then finds word
for word in the measure. That proves the words are there. It does not prove they bear the label out,
and in the rare, heavy categories they often did not: six of the nine measures labelled as requiring
a license to build AI did nothing of the kind. One was a sandbox that waives rules, one licensed
auditors, one certified a data center. A rule loosening export controls was labelled as imposing
them, and an affirmative defense for disclosed deepfakes as a labeling mandate.

So each control and office label gets one narrow question, asked by a separate and stronger model
against the label's own definition: does the measure itself do this? It is told the ways the answer
has turned out to be no. A no removes the label and is kept with its reason, so the same label
resting on the same quote never comes back. A label the check has not reached yet stays on the site,
but cannot lead the daily post (brief.pool reads only labels that passed).
"""
import concurrent.futures as cf
import datetime as dt
import threading
import time

import requests

from .common import annotate, config, iso, kv_get, kv_set, log, measures_in_scope

API = "https://api.anthropic.com/v1/messages"
# Stronger first. A key that cannot use the first model gets the next one down rather than no
# check at all, and the log says so.
MODELS = ["claude-sonnet-5", "claude-haiku-4-5-20251001"]
SECONDS = 900       # per run; the first run has the whole record to read, every run after a handful
DAY_CAP = 3000      # checks per day, so a loop gone wrong cannot run up the bill
WORKERS = 4
# A circuit breaker for a check that has misread its instructions: a run refusing more than this
# share of 60 or more labels applies none of them and holds them for a person. The first full pass
# refused 47%, and read by hand the refusals were right: mostly government bodies told to study,
# report, advise or run a grant, which the tagger had counted as offices gaining power, and
# disclosures to consumers counted as reporting to government. So the line sits well above that.
HOLD_OVER, HOLD_MIN = 0.65, 60
# Bump to apply refusals a run held, once a person has read them. 1: the first full pass, 349
# refusals of 749, read and found right on 2026-09-21.
RELEASE = 1

# The heavy, rare controls first: they decide which measure leads the post, and they were the ones
# most often wrong. Offices next, since the front page counts them.
ORDER = ["license-to-build", "training-caps", "open-model-limits", "id-age-checks", "export-controls",
         "preemption", "new-agency-powers", "data-center-limits", "agency", "mandatory-reporting",
         "labeling-mandates"]

SYSTEM = ("You check one label a public database has given a U.S. bill or rule. Read the measure and "
          "decide whether the measure itself does what the label says. Be literal and use only the "
          "text. Reply with a single JSON object and nothing else.")

NOT_THIS = (
    "Answer no when any of these is true:\n"
    "- it permits, allows, or offers something optional, such as a program, pilot, or sandbox a "
    "company may apply to, instead of requiring it;\n"
    "- it loosens, removes, waives, or exempts from a requirement instead of imposing one;\n"
    "- it rewards a practice, such as an affirmative defense, a safe harbor, or a grant, instead of "
    "requiring it;\n"
    "- the quote describes law already in force, not what this measure would do;\n"
    "- the measure only urges, requests, recognizes, or takes a position;\n"
    "- the quote is about something the definition does not cover.")

# What each control has turned out not to be, from the labels taken off by hand.
CONTROL_NOTES = {
    "license-to-build": "Permission has to be required before an AI system or model is developed, "
                        "trained, or deployed. Licensing or registering auditors, verifiers, or "
                        "professionals is not this, and neither is a certificate for a data center.",
    "mandatory-reporting": "The duty has to fall on those who build, deploy, or use AI, which can "
                           "include a government agency that uses it. A government body reporting to "
                           "a legislature, governor, or Congress about its own work, or about a study "
                           "it was asked to do, is not this.",
    "labeling-mandates": "A label, watermark, provenance data, or disclosure has to be required on AI "
                         "content or AI interactions. A defense or safe harbor for content that "
                         "carries a disclosure rewards a label; it does not require one.",
    "export-controls": "Exports or foreign access have to be restricted. A measure that loosens "
                       "restrictions, adds license exceptions, or promotes exports is the opposite.",
    "data-center-limits": "Building or operating data centers has to be paused, banned, or put under "
                          "new permitting restrictions or conditions. Incentives, tax breaks, or "
                          "faster permitting are the opposite.",
    "new-agency-powers": "A government office has to be created for AI, or an existing office handed "
                         "new authority over AI to make rules, inspect, license, or enforce. A study, "
                         "a task force that only reports, or money to buy software is not this.",
    "preemption": "It has to be a federal measure that overrides, preempts, or blocks state or local "
                  "AI laws, or conditions federal funding on states not regulating AI.",
    "id-age-checks": "Identity or age verification, or a know-your-customer check, has to be required "
                     "of the users, customers, or buyers of AI services or compute.",
    "training-caps": "A compute, cost, or capability threshold has to trigger restrictions or "
                     "obligations on who may train or release large AI models.",
    "open-model-limits": "Publishing, sharing, or open-sourcing model weights or code has to be "
                         "restricted.",
}

OFFICE_QUESTION = (
    "Does the measure itself create this office, or give it new authority over AI or over the people "
    "who build, deploy, or use AI: to make rules, license, certify, approve, inspect, audit, "
    "investigate, enforce, or require reports or records from them?\n"
    "Answer no when any of these is true:\n"
    "- the office is only mentioned, or is one place a person may choose to report to;\n"
    "- the office receives a report from another government body, or is asked to study, recommend, "
    "advise, train, or run a program, pilot, or grant;\n"
    "- the office only spends, receives, or hands out funds;\n"
    "- the office is a legislature, a legislative committee, bureau, or commission, or a court;\n"
    "- the quote describes law already in force, not what this measure would do;\n"
    "- the measure only urges, requests, or takes a position.")

_lock = threading.Lock()
_model = {"i": 0}


class Unavailable(RuntimeError):
    """Every model on the list refused this key."""


def _drop(model):
    with _lock:
        if _model["i"] < len(MODELS) and MODELS[_model["i"]] == model:
            _model["i"] += 1
            log(f"[check] {model} is not available to this key; "
                f"{'using ' + MODELS[_model['i']] if _model['i'] < len(MODELS) else 'no model left'}")


def ask(key, user, max_tokens=250):
    """One question, to the strongest model this key can use. Returns (answer, model)."""
    from .tag import parse_json
    last = ""
    while True:
        with _lock:
            if _model["i"] >= len(MODELS):
                raise Unavailable("no model on the list is available to this key")
            model = MODELS[_model["i"]]
        for attempt in range(5):
            try:
                r = requests.post(API, timeout=120, headers={
                    "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": model, "max_tokens": max_tokens, "system": SYSTEM,
                          "messages": [{"role": "user", "content": user}]})
            except requests.RequestException as exc:
                last = str(exc)
                time.sleep(4 * 2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504, 529):
                last = f"{r.status_code} {r.text[:200]}"
                time.sleep(min(60, 4 * 2 ** attempt))
                continue
            if r.status_code in (403, 404) or (r.status_code == 400 and "model" in r.text.lower()):
                _drop(model)
                break
            if r.status_code != 200:
                raise RuntimeError(f"Claude API {r.status_code}: {r.text[:300]}")
            text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
            try:
                return parse_json(text), model
            except ValueError:
                # One in twenty answers on the first pass had no JSON in it: an explanation that ran
                # past the token limit before the object. Asked once more, with room and a reminder.
                if max_tokens >= 600:
                    raise
                max_tokens, user = 600, user + "\n\nAnswer with the JSON object only, starting with {."
                continue
        else:
            raise RuntimeError(f"Claude API kept failing: {last}")


def question(m, kind, value, quote, controls):
    doc = (f"Jurisdiction: {m['jurisdiction_name']}\nIdentifier: {m['identifier']}\n"
           f"Title: {m['title'] or ''}\nSummary: {(m['summary'] or '(none)')[:5000]}")
    if kind == "agency":
        ask_this = f"LABEL: an office this measure would hand new power: {value}\n\n{OFFICE_QUESTION}"
    else:
        c = controls[value]
        note = CONTROL_NOTES.get(value, "")
        ask_this = (f"LABEL: {c['name']}\nDEFINITION: {c['definition']}\n{note}\n\n"
                    f"Does the measure itself impose what the definition describes?\n{NOT_THIS}")
    return (f"MEASURE\n{doc}\n\n{ask_this}\n\nTHE LABEL RESTS ON THIS QUOTE: \"{quote}\"\n\n"
            'Return JSON: {"verdict": "yes" or "no", "reason": "one short sentence"}')


def verdict_of(answer):
    v = str((answer or {}).get("verdict", "")).strip().lower()
    return v if v in ("yes", "no") else None


def queue(db, controls):
    """Labels without a verdict on the quote they rest on now, and labels already refused on it."""
    shown = {m["id"]: m for m in measures_in_scope(db)}
    rows = db.execute(
        "SELECT t.target, t.kind, t.value, t.evidence, c.evidence AS on_quote, c.verdict "
        "FROM tags t LEFT JOIN checks c ON c.target = t.target AND c.kind = t.kind AND c.value = t.value "
        "WHERE t.kind IN ('control', 'agency')").fetchall()
    todo, refused = [], []
    for r in rows:
        if r["target"] not in shown or (r["kind"] == "control" and r["value"] not in controls):
            continue
        if r["verdict"] is not None and r["on_quote"] == r["evidence"]:
            if r["verdict"] == "no":
                refused.append(r)  # the measure was read again and landed on the same quote
            continue  # yes stays; hold waits for a person
        todo.append(r)
    rank = {k: i for i, k in enumerate(ORDER)}
    # Measures that moved in the last 45 days first, because the daily post draws its lead from
    # them and can only use a label once it is checked. Then the groups in order, newest first
    # inside each. Each sort is stable, so the last one decides first.
    recent = (dt.date.today() - dt.timedelta(days=45)).isoformat()

    def moved(r):
        m = shown[r["target"]]
        return (m["latest_action_date"] or m["updated"] or m["first_seen"] or "")[:10]
    todo.sort(key=lambda r: shown[r["target"]]["introduced_date"] or "", reverse=True)
    todo.sort(key=lambda r: rank.get(r["value"] if r["kind"] == "control" else "agency", len(ORDER)))
    todo.sort(key=lambda r: moved(r) < recent)
    return todo, refused, shown


def remove(db, target, kind, value):
    db.execute("DELETE FROM tags WHERE target=? AND kind=? AND value=?", (target, kind, value))


def release_holds(db):
    """Apply held refusals once a person has read them: the labels come off and the verdicts stand."""
    key = f"check:released:{RELEASE}"
    if kv_get(db, key):
        return 0
    held = db.execute("SELECT target, kind, value FROM checks WHERE verdict = 'hold'").fetchall()
    for r in held:
        remove(db, r["target"], r["kind"], r["value"])
    db.execute("UPDATE checks SET verdict = 'no' WHERE verdict = 'hold'")
    kv_set(db, key, True)
    db.commit()
    if held:
        log(f"[check] {len(held)} held refusals applied")
    return len(held)


def run(db, key, seconds=SECONDS, ask_fn=None):
    """Check what has not been checked. Returns a short line for the status message."""
    ask_fn = ask_fn or ask
    released = release_holds(db)
    controls = {c["slug"]: c for c in config("controls")}
    todo, refused, shown = queue(db, controls)
    for r in refused:
        remove(db, r["target"], r["kind"], r["value"])
    db.commit()
    day = iso()[:10]
    spent = kv_get(db, "check_spend", {})
    used = spent.get("n", 0) if spent.get("date") == day else 0
    room = max(0, DAY_CAP - used)
    todo = todo[:room]
    started, done, failed, models, verdicts = time.time(), 0, 0, set(), []

    def one(r):
        if time.time() - started > seconds:
            return r, None, None
        answer, model = ask_fn(key, question(shown[r["target"]], r["kind"], r["value"], r["evidence"], controls))
        return r, answer, model

    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(one, r) for r in todo]
        for fut in cf.as_completed(futures):
            try:
                r, answer, model = fut.result()
            except Unavailable as exc:
                failed += 1
                if failed == 1:
                    log(f"[check] {exc}")
                continue
            except Exception as exc:  # one odd answer must not cost the rest
                failed += 1
                if failed <= 3:
                    log(f"[check] {exc}")
                continue
            v = verdict_of(answer)
            if v is None:
                continue  # out of time, or an answer that was neither; asked again next run
            verdicts.append((r, v, str(answer.get("reason") or "")[:400], model))
            done += 1
            models.add(model)
    refusals = sum(1 for _, v, _, _ in verdicts if v == "no")
    hold = done >= HOLD_MIN and refusals / done > HOLD_OVER
    if hold:
        annotate("error", "label check held its refusals",
                 f"{refusals} of {done} labels refused, over {HOLD_OVER:.0%}; nothing taken off. "
                 f"The refusals are kept as verdict 'hold' for a person to read.")
    for r, v, reason, model in verdicts:
        stored = "hold" if (hold and v == "no") else v
        db.execute("INSERT OR REPLACE INTO checks(target, kind, value, evidence, verdict, reason, model, "
                   "checked_at) VALUES(?,?,?,?,?,?,?,?)",
                   (r["target"], r["kind"], r["value"], r["evidence"], stored, reason, model, iso()))
        if stored == "no":
            remove(db, r["target"], r["kind"], r["value"])
    kv_set(db, "check_spend", {"date": day, "n": used + done})
    db.commit()
    left = len(todo) - done
    return ((f"applied {released} held refusals; " if released else "")
            + f"checked {done} labels, took off {(0 if hold else refusals) + len(refused)}"
            + (f", held {refusals} refusals for review" if hold else "")
            + (f", {left} still to check" if left > 0 else "")
            + (f", {failed} errors" if failed else "")
            + (f" ({', '.join(sorted(models))})" if models else ""))


def passed(db, target, kind):
    """The values of one kind on one measure that the check has confirmed on their current quote."""
    return [r["value"] for r in db.execute(
        "SELECT t.value FROM tags t JOIN checks c ON c.target = t.target AND c.kind = t.kind "
        "AND c.value = t.value AND c.evidence = t.evidence AND c.verdict = 'yes' "
        "WHERE t.target = ? AND t.kind = ?", (target, kind))]
