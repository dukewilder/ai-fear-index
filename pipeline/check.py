"""A third reading of every fear, control and office label: does the text actually say this?

The first pass proposes labels and the second backs each one with a quote that code then finds word
for word in the measure. That proves the words are there. It does not prove they bear the label out,
and in the rare, heavy categories they often did not: six of the nine measures labelled as requiring
a license to build AI did nothing of the kind. One was a sandbox that waives rules, one licensed
auditors, one certified a data center. A rule loosening export controls was labelled as imposing
them, and an affirmative defense for disclosed deepfakes as a labeling mandate.

So each label gets one narrow question, asked by a separate and stronger model against the label's
own definition: does the measure itself do this, or for a fear, does the text itself state it? It is
told the ways the answer has turned out to be no. Fears on organizations' statements are read too. A no removes the label and is kept with its reason, so the same label
resting on the same quote never comes back. A label the check has not reached yet stays on the site,
but cannot lead the daily post (brief.pool reads only labels that passed).
"""
import concurrent.futures as cf
import datetime as dt
import threading
import time

import requests

from .common import SUMMARY_MAX, annotate, config, iso, kv_get, kv_set, log, measures_in_scope, sha, title_settles
from . import known

API = "https://api.anthropic.com/v1/messages"
# Stronger first. A key that cannot use the first model gets the next one down rather than no
# check at all, and the log says so.
MODELS = ["claude-sonnet-5", "claude-haiku-4-5-20251001"]
SECONDS = 1200      # per run; a full reading of the record takes about ten minutes, every run after a handful
DAY_CAP = 3000      # checks per day, so a loop gone wrong cannot run up the bill
WORKERS = 6
# A circuit breaker for a check that has misread its instructions: a run refusing more than this
# share of 60 or more labels applies none of them and holds them for a person. The first full pass
# refused 47%, and read by hand the refusals were right: mostly government bodies told to study,
# report, advise or run a grant, which the tagger had counted as offices gaining power, and
# disclosures to consumers counted as reporting to government. So the line sat well above that.
# Then on 21 September it held 44 of 65 refusals on federal bills found through their CRS summaries,
# and read by hand those were right too: a China tech-transfer bill is not the AI race, an agency
# told to write voluntary standards gains no power. Twice now the hold kept wrong labels on the site,
# and a wrong label says something false while a wrongly removed one only says less. So it now
# catches only a check refusing nearly everything, which is what a misread prompt looks like.
HOLD_OVER, HOLD_MIN = 0.9, 60
# Bump to apply refusals a run held, once a person has read them. 1: the first full pass, 349
# refusals of 749, read and found right on 2026-09-21.
RELEASE = 2  # 2: the 44 held on 21 September, read by hand and right
# Bump, with the groups listed, to ask again about labels refused under a definition since widened.
# 1: disclosures now include a company telling the people it uses AI on, and reporting includes data
# center owners and operators. About 40 real mandates on private parties had been refused only
# because they fell outside the narrower wording.
RECHECK = (1, ("labeling-mandates", "mandatory-reporting"))
# Bump, with the kinds listed, to ask again about labels that passed under a wording since narrowed.
# 1: a control has to bind people or organizations outside government, and an office has to gain
#    authority over them. Kentucky's SB 4 sets rules for the state's own use of AI and creates a
#    committee to oversee it; it counted as new agency powers and mandatory reporting, and that
#    committee led the front page's "goes to" line under deepfakes. Rules a government sets for its
#    own use of AI bind the government, not the people the site is counting controls over.
NARROWED = (1, ("control", "agency"))
# Bump, with the fears listed, to ask again about the measures refused a fear whose question has
# since been told more. Statements keep their verdicts; they were read under the same question.
# 1: California's SB 53 has frontier developers assess and report catastrophic risk from their
#    models, and was refused loss of control because "catastrophic risk, as defined" read as a
#    defined term. The fear's own definition names catastrophic risk.
RECHECK_FEARS = (1, ("loss-of-control",))

# The heavy, rare controls first: they decide which measure leads the post, and they were the ones
# most often wrong. Offices next, since the front page counts them.
ORDER = ["about", "license-to-build", "training-caps", "open-model-limits", "id-age-checks", "export-controls",
         "preemption", "new-agency-powers", "data-center-limits", "use-restrictions", "agency",
         "mandatory-reporting", "labeling-mandates", "fear"]

SYSTEM = ("You check one label a public database has given a U.S. bill or rule, or an organization's "
          "statement. Read the text and decide whether it itself does or says what the label claims. Be "
          "literal and use only the text. Reply with a single JSON object and nothing else.")

NOT_THIS = (
    "Answer no when any of these is true:\n"
    "- it permits, allows, or offers something optional, such as a program, pilot, or sandbox a "
    "company may apply to, instead of requiring it;\n"
    "- it loosens, removes, waives, or exempts from a requirement instead of imposing one;\n"
    "- it rewards a practice, such as an affirmative defense, a safe harbor, or a grant, instead of "
    "requiring it;\n"
    "- the quote describes law already in force, not what this measure would do;\n"
    "- the measure only urges, requests, recognizes, or takes a position;\n"
    "- it binds only government bodies in their own use of AI, such as rules for the systems their "
    "agencies, schools, or police buy or run, inventories or assessments of those systems, or "
    "disclosures of the government's own use, and no one outside government;\n"
    "- the quote is about something the definition does not cover.")

# What a fear has turned out to include, where the check read it more narrowly than its definition.
FEAR_NOTES = {
    "loss-of-control": ("A measure that has developers of frontier or other advanced AI models assess, report, "
                        "prevent or manage catastrophic risk from them acts on this harm, even where it defines "
                        "the term: catastrophic risk is part of this definition."),
}

# What each control has turned out not to be, from the labels taken off by hand.
CONTROL_NOTES = {
    "license-to-build": "Permission has to be required before an AI system or model is developed, "
                        "trained, or deployed. Licensing or registering auditors, verifiers, or "
                        "professionals is not this, and neither is a certificate for a data center.",
    "mandatory-reporting": "The duty has to fall on people or organizations outside government that "
                           "build, deploy, operate, or use AI, or that own or operate data centers. "
                           "Telling customers, workers, or the public is not reporting to government. A "
                           "government body reporting on its own use of AI, such as an inventory or "
                           "assessment of the systems it buys or runs, is not this, and neither is one "
                           "reporting to a legislature, governor, or Congress about its own work or a "
                           "study it was asked to do.",
    "labeling-mandates": "A label, watermark, provenance data, or disclosure has to be required on AI "
                         "content or AI interactions, or a company, employer, insurer, landlord, or "
                         "other private party has to tell the people it uses AI on that it is doing so. "
                         "A government body disclosing its own use of AI is not this. A defense or safe "
                         "harbor for content that carries a disclosure rewards a label; it does not "
                         "require one.",
    "use-restrictions": "A use of AI has to be banned or restricted for someone outside government, a person has to "
                        "be required to make or review a decision in place of an AI, or creating or spreading "
                        "AI-made content has to become a crime or a civil violation. Extending an existing offense, "
                        "such as defamation, fraud or child sexual abuse material, to AI-made content counts, because "
                        "the use becomes newly unlawful. A label or disclosure requirement is labels and notices, and "
                        "a report to government is mandatory reporting, not this. A measure that only says existing "
                        "law already covers AI, only studies or urges, or only limits what government bodies may "
                        "do with AI is not this.",
    "export-controls": "Exports or foreign access have to be restricted. A measure that loosens "
                       "restrictions, adds license exceptions, or promotes exports is the opposite.",
    "data-center-limits": "Building or operating data centers has to be paused, banned, or put under "
                          "new permitting restrictions or conditions. Incentives, tax breaks, or "
                          "faster permitting are the opposite.",
    "new-agency-powers": "A government office has to be created for AI or data centers, or an existing "
                         "office handed new authority over AI or data centers to make rules, inspect, "
                         "license, or enforce, over people or organizations outside government. Data "
                         "centers count whether or not the measure says they are for AI. A study, a task "
                         "force that only reports, or money to buy software is not this, and neither is "
                         "an office that only oversees government bodies' own use of AI: their "
                         "purchases, inventories, policies, or the systems they run.",
    "preemption": "It has to be a federal measure that overrides, preempts, or blocks state or local "
                  "AI laws, or conditions federal funding on states not regulating AI. It binds states and "
                  "cities in the laws they may pass, not in their own use of AI, so the rule about government "
                  "bodies' own use does not apply to it.",
    "id-age-checks": "Identity or age verification, or a know-your-customer check, has to be required "
                     "of the users, customers, or buyers of AI services or compute.",
    "training-caps": "A compute, cost, or capability threshold has to trigger restrictions or "
                     "obligations on who may train or release large AI models.",
    "open-model-limits": "Publishing, sharing, or open-sourcing model weights or code has to be "
                         "restricted.",
}

OFFICE_QUESTION = (
    "Does the measure itself create this office, or give it new authority over AI, over data centers, "
    "or over the people and organizations outside government who build, deploy, or use AI: to make "
    "rules, license, certify, approve, inspect, audit, investigate, enforce, or require reports or "
    "records from them? Data centers count whether or not the measure says they are for AI.\n"
    "Answer no when any of these is true:\n"
    "- the office is only mentioned, or is one place a person may choose to report to;\n"
    "- its new authority reaches only government bodies' own use of AI, such as their purchases, "
    "inventories, policies, or the systems their agencies, schools, or police run;\n"
    "- the office receives a report from another government body, or is asked to study, recommend, "
    "advise, train, or run a program, pilot, or grant;\n"
    "- the office only spends, receives, or hands out funds;\n"
    "- the office is a legislature, a legislative committee, bureau, or commission, or a court;\n"
    "- the quote describes law already in force, not what this measure would do;\n"
    "- the measure only urges, requests, or takes a position.")

FEAR_QUESTION = (
    "Does the text itself state or clearly invoke this harm, as a reason for what it does or as the "
    "thing it acts on?\n"
    "Answer no when any of these is true:\n"
    "- the harm is only named in a list of topics, a definition, or the name of an unrelated program;\n"
    "- the text is about AI in general and the harm is inferred rather than stated;\n"
    "- a word from the definition appears but not the harm itself, such as China named in a trade or "
    "supply matter with no AI competition in it, or workers trained in AI with no jobs lost to it;\n"
    "- the definition itself says this case belongs to a different fear;\n"
    "- the quote describes law already in force, not what this measure would do.")

# Whether a measure is about AI at all, asked of every counted measure whose title does not say so.
# The tagger was asked the same thing and is lenient: federal bills found through their CRS
# summaries included the CATCH Fentanyl Act and the Promoting Precision Agriculture Act, which
# mention AI once each. A no takes the measure out of every count.
ABOUT_QUESTION = (
    "Is this measure itself about AI, or about one of the subjects this report counts with it? Answer "
    "yes if it regulates, restricts, requires, funds, studies or defines any of these: AI, machine "
    "learning, algorithms or automated decision systems; AI chatbots; deepfakes and other synthetic or "
    "digitally altered images, video, audio or voices; rights over a person's voice and likeness (not "
    "payments to athletes for their name, image and likeness); data centers or high performance "
    "computing facilities, including their power, water, siting, rates or taxes, whether or not the "
    "measure says AI (not a data program, office or database that is called a data center); vehicles "
    "that drive themselves (autonomous vehicles, automated driving systems, robotaxis, driverless "
    "trucks), including their testing, permits, operation, insurance and liability, whether or not "
    "the measure says AI.\n"
    "Answer no when the subject appears only in passing: one of several technologies or fields a "
    "program may use, fund or study; a finding, a definition or a statement of purpose; or one line "
    "in a measure about something else, such as defense, trade or health care. An intimate-image or "
    "privacy measure that does not cover altered or generated images is no.")
# Bump to ask again every measure the question above has taken out, under its current wording.
# 2: the first wording took out 171 data center bills whose text does not say the data centers are
#    for AI, which the site counted at launch and which its data center control and power bills
#    fear are built on. Which data center bills to count is the owner's call, so they are back as
#    the first reading left them until it is made. It also took out likeness and synthetic voice
#    bills (an ELVIS Act, robocalls in an artificial voice) that the report counts as synthetic media.
ABOUT_VERSION = 2


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


def question(m, kind, value, quote, controls, fears=None):
    if m.get("post"):
        what = "STATEMENT"
        doc = (f"Organization: {m.get('org') or m['entity']}\nTitle: {m['title'] or ''}\n"
               f"Text: {(m['summary'] or '(none)')[:SUMMARY_MAX]}")
    else:
        what = "MEASURE"
        doc = (f"Jurisdiction: {m['jurisdiction_name']}\nIdentifier: {m['identifier']}\n"
               f"Title: {m['title'] or ''}\nSummary: {(m['summary'] or '(none)')[:SUMMARY_MAX]}")
    if kind == "about":
        return (f"{what}\n{doc}\n\n{ABOUT_QUESTION}\n\n"
                'Return JSON: {"verdict": "yes" or "no", "reason": "one short sentence"}')
    if kind == "fear":
        f = (fears or {})[value]
        note = FEAR_NOTES.get(value, "") if not m.get("post") else ""
        ask_this = (f"LABEL: a fear this {what.lower()} cites: {f['name']}\nDEFINITION: {f['definition']}\n"
                    + (f"{note}\n" if note else "") + f"\n{FEAR_QUESTION}")
    elif kind == "agency":
        ask_this = f"LABEL: an office this measure would hand new power: {value}\n\n{OFFICE_QUESTION}"
    else:
        c = controls[value]
        note = CONTROL_NOTES.get(value, "")
        ask_this = (f"LABEL: {c['name']}\nDEFINITION: {c['definition']}\n{note}\n\n"
                    f"Does the measure itself impose what the definition describes?\n{NOT_THIS}")
    return (f"{what}\n{doc}\n\n{ask_this}\n\nTHE LABEL RESTS ON THIS QUOTE: \"{quote}\"\n\n"
            'Return JSON: {"verdict": "yes" or "no", "reason": "one short sentence"}')


def verdict_of(answer):
    v = str((answer or {}).get("verdict", "")).strip().lower()
    return v if v in ("yes", "no") else None


def queue(db, controls, fears=None):
    """Labels without a verdict on the quote they rest on now, and labels already refused on it."""
    fears = fears or {}
    shown = {m["id"]: m for m in measures_in_scope(db)}
    # Statements the site shows: an organization's own post, judged about AI. Only their fears.
    names = {e["slug"]: e["name"] for e in config("entities")}
    for p in db.execute("SELECT p.* FROM posts p JOIN tag_runs tr ON tr.target = 'post:' || p.id "
                        "WHERE tr.ai_related = 1"):
        shown["post:" + p["id"]] = {**dict(p), "post": True, "org": names.get(p["entity"]),
                                    "latest_action_date": p["published"],
                                    "updated": None, "first_seen": p["first_seen"],
                                    "introduced_date": (p["published"] or "")[:10]}
    rows = db.execute(
        "SELECT t.target, t.kind, t.value, t.evidence, c.evidence AS on_quote, c.verdict "
        "FROM tags t LEFT JOIN checks c ON c.target = t.target AND c.kind = t.kind AND c.value = t.value "
        "WHERE t.kind IN ('control', 'agency', 'fear')").fetchall()
    todo, refused = [], []
    for r in rows:
        subject = shown.get(r["target"])
        if not subject or (r["kind"] == "control" and r["value"] not in controls) \
                or (r["kind"] == "fear" and r["value"] not in fears) \
                or (subject.get("post") and r["kind"] != "fear"):
            continue
        if r["verdict"] is not None and r["on_quote"] == r["evidence"]:
            if r["verdict"] == "no":
                refused.append(r)  # the measure was read again and landed on the same quote
            continue  # yes stays; hold waits for a person
        todo.append(r)
    # Every counted measure whose title does not name AI or data centers, unless a person has
    # confirmed it is an AI law. Keyed to its text, so a new summary is asked about again.
    confirmed = known.ids(db)
    asked = {r["target"]: (r["evidence"], r["verdict"]) for r in
             db.execute("SELECT target, evidence, verdict FROM checks WHERE kind = 'about'")}
    for mid, m in shown.items():
        if m.get("post") or mid in confirmed or title_settles(m["title"]):
            continue
        r = {"target": mid, "kind": "about", "value": "ai",
             "evidence": sha(f"{m['title'] or ''}\n{m['summary'] or ''}")}
        was = asked.get(mid)
        if was and was[0] == r["evidence"]:
            if was[1] == "no":
                refused.append(r)
            continue
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
    todo.sort(key=lambda r: rank.get(r["value"] if r["kind"] == "control" else r["kind"], len(ORDER)))
    todo.sort(key=lambda r: moved(r) < recent)
    return todo, refused, shown


def remove(db, target, kind, value):
    if kind == "about":
        db.execute("UPDATE tag_runs SET ai_related = 0 WHERE target = ?", (target,))
        return
    db.execute("DELETE FROM tags WHERE target=? AND kind=? AND value=?", (target, kind, value))


def recheck(db):
    """Put back, once, the labels refused under a definition since widened, so they are asked again."""
    version, groups = RECHECK
    key = f"check:recheck:{version}"
    if kv_get(db, key):
        return 0
    marks = ",".join("?" * len(groups))
    rows = db.execute(f"SELECT * FROM checks WHERE kind = 'control' AND verdict = 'no' AND value IN ({marks})",
                      groups).fetchall()
    for r in rows:
        db.execute("INSERT OR IGNORE INTO tags(target, kind, value, evidence, model, tagged_at) VALUES(?,?,?,?,?,?)",
                   (r["target"], r["kind"], r["value"], r["evidence"], "recheck", iso()))
        db.execute("DELETE FROM checks WHERE target=? AND kind=? AND value=?", (r["target"], r["kind"], r["value"]))
    kv_set(db, key, True)
    db.commit()
    if rows:
        log(f"[check] {len(rows)} labels refused under the narrower definitions put back to be asked again")
    return len(rows)


def recheck_fears(db):
    """Put back, once, the measures refused one of the listed fears, so they are asked again."""
    version, fears = RECHECK_FEARS
    key = f"check:recheck-fears:{version}"
    if kv_get(db, key):
        return 0
    marks = ",".join("?" * len(fears))
    rows = db.execute(f"SELECT * FROM checks WHERE kind = 'fear' AND verdict = 'no' AND value IN ({marks}) "
                      "AND target NOT LIKE 'post:%'", fears).fetchall()
    for r in rows:
        db.execute("INSERT OR IGNORE INTO tags(target, kind, value, evidence, model, tagged_at) VALUES(?,?,?,?,?,?)",
                   (r["target"], r["kind"], r["value"], r["evidence"], "recheck", iso()))
        db.execute("DELETE FROM checks WHERE target=? AND kind=? AND value=?", (r["target"], r["kind"], r["value"]))
    kv_set(db, key, True)
    db.commit()
    if rows:
        log(f"[check] {len(rows)} fear labels refused under a narrower reading put back to be asked again")
    return len(rows)


def reask_narrowed(db):
    """Ask again, once per wording, about every label of the listed kinds that passed under a wider one.

    Its verdict goes, so it joins the queue; the label stays on the site until the answer comes back.
    """
    version, kinds = NARROWED
    key = f"check:narrowed:{version}"
    if kv_get(db, key):
        return 0
    marks = ",".join("?" * len(kinds))
    n = db.execute(f"DELETE FROM checks WHERE kind IN ({marks}) AND verdict = 'yes'", kinds).rowcount
    kv_set(db, key, True)
    db.commit()
    if n:
        log(f"[check] {n} labels that passed under a wider wording put back to be asked again")
    return n


def reask_about(db):
    """Put back, once per wording, every measure the about question took out, to be asked again.

    Only where the no is what took it out: a measure the tagger has read again since, on new text,
    keeps the tagger's answer. Its old verdict goes either way, since it was on other text.
    """
    key = f"check:about:{ABOUT_VERSION}"
    if kv_get(db, key):
        return 0
    back = [r["target"] for r in db.execute(
        "SELECT c.target FROM checks c JOIN tag_runs t ON t.target = c.target "
        "WHERE c.kind = 'about' AND c.verdict = 'no' AND t.ai_related = 0 "
        "AND (t.tagged_at IS NULL OR t.tagged_at <= c.checked_at)")]
    for target in back:
        db.execute("UPDATE tag_runs SET ai_related = 1 WHERE target = ?", (target,))
    db.execute("DELETE FROM checks WHERE kind = 'about' AND verdict IN ('no', 'hold')")
    kv_set(db, key, True)
    db.commit()
    if back:
        log(f"[check] {len(back)} measure{'' if len(back) == 1 else 's'} put back to be asked again "
            f"whether {'it is' if len(back) == 1 else 'they are'} about AI")
    return len(back)


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
    reasked_about = reask_about(db)
    reasked = recheck(db) + recheck_fears(db)
    narrowed = reask_narrowed(db)
    controls = {c["slug"]: c for c in config("controls")}
    fears = {f["slug"]: f for f in config("fears")}
    todo, refused, shown = queue(db, controls, fears)
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
        answer, model = ask_fn(key, question(shown[r["target"]], r["kind"], r["value"], r["evidence"], controls,
                                             fears))
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
            + (f"put back {reasked_about} measure{'' if reasked_about == 1 else 's'} to ask again whether "
               f"{'it is' if reasked_about == 1 else 'they are'} about AI; " if reasked_about else "")
            + (f"put back {reasked} labels to ask again; " if reasked else "")
            + (f"asking again about {narrowed} labels under a narrower wording; " if narrowed else "")
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
