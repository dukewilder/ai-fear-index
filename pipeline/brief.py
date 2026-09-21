"""Write one edition of the daily brief: the post text and the card that goes with it.

The site can show the whole chain and let a reader take it at their own pace. A post gets one
scroll, so an edition has to make the case in a few lines. It does that by choosing measures that
already carry a control, stating each one in the terms the measure itself uses rather than the
terms its sponsor prefers, and ending each line at the office that would hold the power.

Nothing is said here that the site cannot show. Every sentence is written from the measure's own
title and summary, and then checked back against them the way a tag is checked: the model has to
quote the words it relied on, and the quote has to be in the text. A sentence that fails the check
is dropped rather than softened, and the edition runs a line shorter.

    python -m pipeline.brief --db state/index.db --out state/brief --dry-run
"""
import argparse
import collections
import datetime as dt
import zoneinfo
import json
import pathlib
import re
import sys

from .common import (EUPHEMISM, STATES, Entities, annotate, config, connect, env, kv_set, log,
                     measures_in_scope, name_key, now)
from .export import gave
from .tag import agreed, call, norm

MODEL_NOTE = "claude-haiku-4-5-20251001"
WANT = 3          # lines in an edition: enough to show a pattern, few enough to read
POOL_DAYS = 45    # measures move slower than a day, so an edition draws from a rolling pool
MAX_SENTENCE = 210  # a sentence that names the power needs more room than one that names a duty


EASTERN = zoneinfo.ZoneInfo("America/New_York")


def edition_date():
    """The date an American reader would call today, which is not the date in UTC.

    A post sent at nine in the morning in New York goes out on the UTC date that matches, but one
    sent in the evening does not, and the plate then carries tomorrow.
    """
    return now().astimezone(EASTERN).date().isoformat()


def controls_by_slug():
    return {c["slug"]: c for c in config("controls")}


def fears_by_slug():
    return {f["slug"]: f for f in config("fears")}


ENFORCER = re.compile(r"\b(attorney general|commission|department|board|bureau|authority|agency|"
                      r"secretary|director|administrator|office of)\b", re.I)

# What a control is worth to an edition. A duty to file a report is real but it is not the story;
# a licence, an identity check, or a new office with a power over you is.
WEIGHT = {"license-to-build": 5, "training-caps": 5, "id-age-checks": 5, "open-model-limits": 4,
          "new-agency-powers": 4, "preemption": 4, "export-controls": 3, "data-center-limits": 3,
          "mandatory-reporting": 1, "labeling-mandates": 1}


OTHER_TONGUE = re.compile(r"\b(para|que|los|las|del|por|una|con|sobre|ley|articulo|art[ií]culo|"
                          r"gubernamental|inteligencia)\b", re.I)


def english(row):
    """The collectors reach every jurisdiction, and Puerto Rico legislates in Spanish.

    A Spanish summary is almost all ASCII, so the give-away is the vocabulary rather than the
    characters. Three or more of these words in the first stretch of text and the row waits for a
    version of this that can read it.
    """
    text = f"{row['title'] or ''} {row['summary'] or ''}"[:600]
    return bool(text) and len(OTHER_TONGUE.findall(text)) < 3


def score(row, today, recent=()):
    """How well one measure would carry an edition.

    recent is the jurisdictions the last few editions led with. A state that files the most bills
    would otherwise lead most mornings, and a week of California reads as one state's problem
    rather than the same thing happening in thirty. The penalty is small enough that a measure
    that actually matters more still wins.
    """
    controls = (row["controls"] or "").split("|")
    points = max((WEIGHT.get(c, 1) for c in controls), default=0)
    if (row["status"] or "") == "passed":
        points += 3
    if row["office"] and ENFORCER.search(row["office"]):
        points += 2
    if row["fears"]:
        points += 2
    moved = row["moved"] or ""
    if moved >= (dt.date.fromisoformat(today) - dt.timedelta(days=14)).isoformat():
        points += 1
    points -= 2 * list(recent).count(row["jurisdiction_name"])
    return points


# Only labels the third reading has confirmed on the quote they rest on now. A control or an office
# decides which measure leads the post and what the post says it hands out, and the rare, heavy
# controls were the labels most often wrong, so an unconfirmed one can sit on the site while it waits
# to be checked but cannot put a measure at the top of the post.
PASSED = ("JOIN checks c ON c.target = t.target AND c.kind = t.kind AND c.value = t.value "
          "AND c.evidence = t.evidence AND c.verdict = 'yes'")


def pool(db, today, days=POOL_DAYS, limit=12):
    """Measures that carry a control, have not been in an edition, and moved recently.

    A resolution is left out on purpose: it states an opinion rather than binding anyone, and every
    line of an edition claims somebody is bound. A measure that names an office is worth more than
    one that does not, which the score handles, but it is not required: measures carrying both a
    control and an office arrive at under three a week, and a daily post cannot wait on that.
    """
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT m.id, m.kind, m.jurisdiction_name, m.identifier, m.title, m.summary, m.status, m.url, "
        "  COALESCE(m.latest_action_date, m.updated, m.first_seen) AS moved, "
        "  (SELECT GROUP_CONCAT(t.value, '|') FROM tags t " + PASSED + " WHERE t.target=m.id AND t.kind='control') AS controls, "
        "  (SELECT GROUP_CONCAT(t.value, '|') FROM tags t " + PASSED + " WHERE t.target=m.id AND t.kind='fear') AS fears, "
        "  (SELECT t.value FROM tags t " + PASSED + " WHERE t.target=m.id AND t.kind='agency' "
        "   ORDER BY LENGTH(t.value) LIMIT 1) AS office, "
        "  (SELECT GROUP_CONCAT(t.value, '|') FROM tags t " + PASSED + " WHERE t.target=m.id AND t.kind='agency') AS offices "
        "FROM measures m JOIN tag_runs r ON r.target = m.id "
        "WHERE r.ai_related = 1 AND controls IS NOT NULL AND m.kind != 'resolution' "
        "  AND m.id NOT IN (SELECT target FROM brief) "
        "  AND moved >= ? AND moved <= ? "
        "ORDER BY moved DESC", (since, today)).fetchall()
    # Only what the site shows. A suppressed measure, one filed before the report starts, or the
    # older copy of a carried-over bill could otherwise lead the card while the page has no row
    # for it, and the card says nothing the site cannot show.
    shown = in_scope(db)
    rows = [r for r in rows if r["id"] in shown and english(r)]
    recent = [r["jurisdiction"] for r in db.execute(
        "SELECT b.edition, m.jurisdiction_name AS jurisdiction FROM brief b JOIN measures m ON m.id = b.target "
        "ORDER BY b.edition DESC LIMIT 4")]
    rows.sort(key=lambda r: (-score(r, today, recent), r["moved"] or ""))
    return rows[:limit]


def doc(row):
    return f"Jurisdiction: {row['jurisdiction_name']}\nIdentifier: {row['identifier']}\n" \
           f"Title: {row['title'] or ''}\nSummary: {(row['summary'] or '')[:6000]}"


SYSTEM = (
    "You write one sentence about a U.S. bill or rule for a public record of what fear of AI is "
    "buying and who ends up holding it. The reader is about to be bound by these measures. Reply "
    "with a single JSON object and nothing else."
)

RULES = (
    "Write one sentence, at most 30 words. {tense}\n"
    "If the measure says it starts in a later year, put that year in the sentence. A law on the "
    "books that bites in 2029 is still the story, and saying from 2029 is more of it, not less.\n"
    "Say what the measure puts under whose control. Name the government body that would decide, "
    "inspect, licence, or be reported to, inside the sentence. If the measure names no body, say "
    "the state or the jurisdiction by name.\n"
    "A duty on a company is a power for whoever it answers to, and it is the power that goes in "
    "the sentence. Not required to restore the water, but put under the department that decides "
    "whether it has. Not required to submit to audits reported to the attorney general, but put "
    "under an auditor whose report the attorney general can demand.\n"
    "So do not open with the jurisdiction and then requires, mandates, directs or orders followed "
    "by a company. Open with what is put under whose control.\n"
    "If a duty falls on everyone in order to identify some people, the sentence says everyone: a "
    "rule that checks whether a user is a child checks every user. If the measure deletes a "
    "condition that had narrowed who a duty applied to, it is widening that duty and the sentence "
    "says so.\n"
    "Do not carry a compound the sponsor built to name the reason rather than the act: child "
    "safety audits, safety standards, trust and safety, transparency requirements, consumer "
    "protection. Name the act and who it lands on. An audit of whether a service is safe for "
    "children is an audit of the service, and everyone using it is inside it.\n"
    "Never use a word a sponsor chose to make a power sound like a courtesy. Not safeguards, "
    "guardrails, protections, safety net, duty of care. Not responsible, accountable, ethical. "
    "Not modernize, streamline, future-proof. Not framework, oversight, governance, guidance, "
    "guidelines, best practices, common sense, sensible, balanced, appropriate, proportionate. "
    "Not stakeholders, public-private, voluntary commitments. Not empower, ensure, transparency. "
    "Name the power and who now holds it.\n"
    "Use the measure's own words for what it does. Do not reach for a near neighbour of one: a "
    "supply a data centre diminishes is a diminished supply, never a diminutive one.\n"
    "Report it. Do not argue it, do not say what it shows or reveals or highlights, and do not "
    "end on a clause beginning with a participle: no underscoring its importance, no reflecting a "
    "trend. Do not say it is not one thing but another. Do not ask a question and answer it.\n"
    "Plain words. Not robust, comprehensive, sweeping, landscape, framework, pivotal, crucial, "
    "significant, or key. No adjective that is not doing specific work.\n"
    "Do not say whether the measure is good or bad, and do not add a reason it was introduced.\n"
    "Do not name the bill number, and do not write the words bill, act, or legislation. Start with "
    "the jurisdiction.\n"
    "quote is copied word for word from the Title or Summary and is the text the sentence rests "
    "on, between 6 and 30 words."
)


LAW = ("This measure has passed. Write it in the present tense: it requires, it puts, it bans.")
PENDING = ("This measure has not passed. It was introduced and is somewhere in the process, so every "
           "verb is conditional: it would require, it would put. Nobody is bound by it yet, so do not "
           "write that anyone must do anything, or that anything is already the case.")


def sentence(key, row, refused=""):
    rules = RULES.format(tense=LAW if (row["status"] or "") == "passed" else PENDING)
    again = (f"\n\nA first attempt at this sentence was refused: {refused}. Write it again without "
             f"that fault, keeping every rule above.") if refused else ""
    out = call(key, SYSTEM, f"TEXT\n{doc(row)}\n\n{rules}{again}\n\n"
                            'Return JSON: {"sentence": "...", "quote": "..."}', max_tokens=400)
    return (out.get("sentence") or "").strip(), (out.get("quote") or "").strip()


BANNED = re.compile(
    # the machine's own tells, and the puffery under them
    r"\b(robust|comprehensive|sweeping|landscape|pivotal|crucial|vital|significant|"
    r"underscor\w*|highlight\w*|showcas\w*|delv\w*|intricate|nuanced|holistic|seamless|"
    r"transformative|groundbreaking|far-reaching|unprecedented)\b", re.I)


def sponsors_word(text, office="", loose=False):
    """The first word in a sentence that a sponsor would have chosen, or an empty string.

    Two lists, one check. EUPHEMISM is the vocabulary that makes a power sound like a courtesy and
    lives in common.py because the site is held to it everywhere, not only here. BANNED is the
    separate problem of a sentence that reads as though a machine wrote it.
    """
    clean = without_names(text, office, loose)
    m = EUPHEMISM.search(clean) or BANNED.search(clean)
    return m.group(0) if m else ""


# a sentence that stops reporting and starts explaining what it means
# A trailing ", reflecting a trend" explains; a trailing ", beginning 2027" dates. The rules ask for
# the start year when a measure bites later, and the model puts it there, so the dating words pass.
EXPLAINING = re.compile(r",\s+(?!(?:beginning|starting|commencing)\b)\w+ing\b[^.]*\.$"
                        r"|\bnot (just|only|merely)\b|\bit is not\b", re.I)


CONDITIONAL = re.compile(r"\b(would|could|may)\b", re.I)

# A sentence that names the power has a shape: something is put under a body, a body is handed
# something, or a body does the deciding. The words alone are not enough. "Under penalty of
# perjury", "under the Clean Energy Act" and "gives notice to every user" all contain the shape
# and none of them hands anybody anything, so what follows has to be a body.
BODY = (r"(?:the\s+|an?\s+)?(?:[A-Z][\w.'-]*\s+){0,4}"
        r"(?:Department|Office|Bureau|Division|Board|Commission|Agency|Authority|Council|Registry|"
        r"Attorney\s+General|Secretary|Director|Administrator|Comptroller|Inspector|Auditor|Regulator)"
        r"|(?:an?|the)\s+(?:independent\s+|outside\s+|third[- ]party\s+|state\s+)?"
        r"(?:auditor|inspector|regulator|registry|register|licence|license|permit|moratorium|"
        r"inventory|review|registration|certification|audit|approval)")
POWER = re.compile(
    rf"\bunder\s+(?:{BODY})"
    r"|\b(?:which|who|whom|that)\s+(?:\w+\s+){0,2}"
    r"(?:decid\w+|licens\w+|approv\w+|inspect\w+|certif\w+|investigat\w+|revok\w+|"
    r"registers?|determin\w+|enforc\w+|permits?|refus\w+|withhold\w+|bars?|blocks?|"
    r"suspends?|suspend|suspending|issu\w+|audits?|holds?|keeps?)\b"
    rf"|\b(?:gives?|hands?|grants?|leaves?)\s+(?:{BODY})"
    r"|\b(?:licen[sc]ed|approved|certified|registered|inspected|authoris?zed|vetted)\s+by\b", re.I)
DUTY_FRAMED = "written as a duty on a company, not the power it creates"
# Looked for in the main clause rather than the first few words, because "Health and Human Services
# Department, Food and Drug Administration requires operators to file reports" is the same sentence
# with a longer name on the front.
DUTY_OPENER = re.compile(r"\b(requires?|mandates?|obligates?|directs?|orders?|compels?)\b", re.I)

# When a measure that has passed says it starts in a later year, the present tense alone claims it
# is in force now. Four of the strongest candidates in the pool are exactly this: California bills
# chaptered this year whose registries and duties begin in 2027, 2028 or 2029.
STARTS = re.compile(r"\b(?:commencing|operative|effective|beginning|no later than|on or after)\s+"
                    r"(?:on\s+)?(?:January|February|March|April|May|June|July|August|September|"
                    r"October|November|December)\s+\d{1,2},\s*(20\d\d)", re.I)


def starts_later(text, today):
    """The first year after this one that the measure names as its own start, if it names one."""
    year = dt.date.fromisoformat(today).year if isinstance(today, str) else today.year
    for m in STARTS.finditer(text or ""):
        if int(m.group(1)) > year:
            return m.group(1)
    return ""

# Where the tense of the sentence lives. The sentence has to start with the jurisdiction, so the
# main verb is in the first clause; everything from the first comma or relative pronoun onward
# describes the office. That matters because "may" is the natural word for a power the office
# holds, and "decides who may operate" is a present-tense fact, not a measure that might pass.
MAIN_CLAUSE = re.compile(r",|\b(which|who|whom|whose|that|whether)\b", re.I)


# "From 2027, California would put..." carries its tense after the date, not in it.
LEADING_DATE = re.compile(r"^(?:from|beginning|starting|commencing|by|in|as of|effective)\s+"
                          r"(?:[A-Z][a-z]+\s+)?(?:\d{1,2},?\s+)?\d{4},\s*", re.I)


def tense_of(text, jurisdiction=""):
    """The first clause, which is the one carrying the tense.

    The jurisdiction comes off the front first, because one of them is called "Health and Human
    Services Department, Food and Drug Administration" and the comma in its name is not a clause
    boundary. Without this the clause is the name alone, which carries no verb at all: every
    pending rule from that agency would read as written in the present tense, and every duty
    sentence from it would slip the check for one.
    """
    body = LEADING_DATE.sub("", (text or "").strip())
    if jurisdiction and body.lower().startswith(jurisdiction.lower()):
        body = body[len(jurisdiction):].lstrip(" ,")
    return MAIN_CLAUSE.split(body, 1)[0]


OFFICIAL = re.compile(
    r"\b(?:[A-Z][\w.'-]*\s+){0,4}"
    r"(?:Department|Office|Bureau|Division|Board|Commission|Agency|Authority|Committee|Council)"
    r"(?:\s+(?:of|for|on|and|the)\s+[A-Z][\w.'-]*|\s+[A-Z][\w.'-]*){0,5}")


def without_names(text, office="", loose=False):
    """The sentence minus the proper names of the bodies in it.

    The banned list is there to stop the sponsor's vocabulary getting in: protections, safeguards,
    oversight. A body actually called the Department of Environmental Protection carries one of
    those words in its own name, and naming the body is the point of the sentence. Eight of the
    195 offices on file are in this position, the New York Office for AI Model Developer Oversight
    among them, and without this the edition throws away its best line and falls back to a count.
    """
    out = text
    if office:
        out = re.sub(re.escape(office), " ", out, flags=re.I)
    out = OFFICIAL.sub(" ", out)
    if loose and office:
        # A headline has nine words and drops "Department of", so the full name never survives it
        # and neither does the pattern that looks for one. Every word of the office's own name
        # comes out instead. Only for the headline: a sentence has room to write the name in full,
        # and stripping single words there would let the sponsor's own use of one through.
        for word in set(re.findall(r"[A-Za-z]{4,}", office)):
            out = re.sub(rf"\b{re.escape(word)}\b", " ", out, flags=re.I)
    return out


# A body the sentence says something is put under. A commission, council or board the measure only
# creates, with no power the third reading could confirm, is not holding anything: "under a six-month
# moratorium and a commission's review" was said of a New Jersey bill whose commission studies impacts.
BODY_WORD = re.compile(r"\b(commission|council|task force|committee|working group|board)\b", re.I)


def unpowered_body(text, offices):
    """True when the sentence names a commission, council or board that is not a confirmed office."""
    m = BODY_WORD.search(text or "")
    if not m:
        return False
    word, said = m.group(1).lower(), set(re.findall(r"[a-z]{4,}", (text or "").lower()))
    return not any(word in o.lower() and (set(re.findall(r"[a-z]{4,}", o.lower())) - {word}) & said
                   for o in offices if o)


def why_not(text, quote, body_norm, passed=False, office="", starts="", jurisdiction="", raw="", offices=None):
    """The reason a sentence was rejected, or an empty string if it holds up.

    Named rather than returned as a bare False, so a run that throws every candidate away says
    which gate did it instead of leaving the next person to guess.
    """
    if not text:
        return "no sentence"
    if not quote:
        return "no quote"
    head = tense_of(text, jurisdiction)
    if passed and CONDITIONAL.search(head):
        return f"passed but says {CONDITIONAL.search(head).group(0)!r}"
    if not passed and not CONDITIONAL.search(head):
        return "pending but written as though it binds"
    if len(text) > MAX_SENTENCE:
        return f"{len(text)} characters, over {MAX_SENTENCE}"
    if not text.endswith("."):
        return "no full stop"
    word = sponsors_word(text, office)
    if word:
        return f"sponsor's word {word!r}"
    if EXPLAINING.search(text):
        return "explains rather than reports"
    loose = re.search(r"\b(bill|act|legislation|lawmakers?)\b", text, re.I)
    if loose:
        return f"says {loose.group(0)!r} instead of naming who is bound"
    if passed and starts and starts not in text:
        return f"passed but starts in {starts}, and the sentence does not say so"
    if offices is not None and unpowered_body(text, offices):
        return "names a commission, council or board the measure gives no confirmed power"
    # The quote is checked before the duty test, not after. A duty-framed sentence is kept in
    # reserve and posted when nothing better was written, and it used to be kept without its
    # quote ever being looked at, so the fallback could go out resting on words not in the bill.
    if not agreed({"controls": {"line": quote}}, body_norm, "controls", raw=raw):
        return "the quote it leaned on is not in the measure"
    if DUTY_OPENER.search(head) and not POWER.search(text):
        return DUTY_FRAMED
    return ""


def usable(text, quote, body_norm, passed=False, office="", starts="", jurisdiction=""):
    """The sentence has to rest on words in the text, not borrow the sponsor's vocabulary, and not
    describe a bill sitting in committee as though it already bound anyone.

    The tense is the whole difference between a record and a rumour, so it is checked here rather
    than trusted to the instruction above: a measure that has not passed says would, and one that
    has does not.
    """
    return not why_not(text, quote, body_norm, passed, office, starts, jurisdiction)


def note(attempts, where, reason, sentence_text="", fatal=False):
    if attempts is not None:
        attempts.append({"measure": where, "reason": reason,
                         "sentence": (sentence_text or "")[:220], "unreachable": fatal})


def write_lines(key, rows, today, want=WANT, attempts=None):
    """Turn candidate measures into checked sentences, stopping once the edition is full.

    Every candidate that does not make it appends its reason to attempts, which the edition stores
    in the database. An edition that falls back to counts has thrown a dozen sentences away, and
    without this the only record of why is a CI log that expires.
    """
    lines, places, second = [], set(), None
    for row in rows:
        if len(lines) >= want:
            break
        # one state should not be the whole edition; the case is that this is everywhere
        if row["jurisdiction_name"] in places:
            continue
        where = f"{row['jurisdiction_name']} {row['identifier']}"
        body = norm(f"{row['title'] or ''}\n{row['summary'] or ''}")
        try:
            text, quote = sentence(key, row)
        except (NameError, AttributeError, TypeError, KeyError, IndexError):
            # A broken name or signature is a fault in this file. Swallowing it row by row is how
            # a missing tense string cost every edition for a day while the log said the sentences
            # did not hold up. It stops the run instead.
            raise
        except Exception as exc:  # one bad row must not cost the edition
            note(attempts, where, f"the model did not answer: {str(exc)[:160]}", fatal=True)
            log(f"[brief] {where}: the model did not answer, {str(exc)[:160]}")
            continue
        starts = starts_later(f"{row['title'] or ''} {row['summary'] or ''}", today)

        confirmed = [o for o in ((row["offices"] if "offices" in row.keys() else "") or "").split("|") if o]

        def fault(t, q):
            return why_not(t, q, body, (row["status"] or "") == "passed", row["office"] or "", starts,
                           row["jurisdiction_name"] or "", raw=f"{row['title'] or ''}\n{row['summary'] or ''}",
                           offices=confirmed)
        reason = fault(text, quote)
        if reason:
            note(attempts, where, reason, sentence_text=text)
            log(f"[brief] {where}: refused, {reason}; asking once more")
            tries = [(text, quote, reason)]
            # A second attempt told what was wrong. Most refusals are one fixable fault: the
            # sponsor's "child safety", three characters over the limit, a would on a measure that
            # passed. Dropping the measure for them cost the launch edition its three best leads.
            try:
                text, quote = sentence(key, row, refused=reason)
                reason = fault(text, quote)
            except (NameError, AttributeError, TypeError, KeyError, IndexError):
                raise
            except Exception as exc:
                reason = f"the model did not answer the second time: {str(exc)[:120]}"
            if reason:
                note(attempts, where, f"again: {reason}", sentence_text=text)
                log(f"[brief] {where}: skipped, {reason}")
                tries.append((text, quote, reason))
                for t, q, why in tries:
                    if why == DUTY_FRAMED and second is None:
                        # True, checkable, and the wrong way round. Worth keeping in reserve: an
                        # edition of counts says less than a sentence in the sponsor's grammar.
                        second = (row, where, t, q)
                continue
        note(attempts, where, "used")
        places.add(row["jurisdiction_name"])
        lines.append(made(row, text, quote))
    if not lines and second is not None:
        row, where, text, quote = second
        note(attempts, where, "used, though it is written as a duty")
        log(f"[brief] {where}: used, though it is written as a duty and nothing better was written")
        lines.append(made(row, text, quote))
    return lines


def made(row, text, quote):
    return {"target": row["id"], "sentence": text, "quote": quote,
            "office": row["office"] or "", "controls": row["controls"] or "",
            "fears": row["fears"] or "", "jurisdiction": row["jurisdiction_name"],
            "url": row["url"], "status": row["status"] or ""}


NUMBERS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


def words(n, cap=False):
    """Ten and under reads better spelled out, and a sentence never opens with a numeral."""
    word = NUMBERS[n] if n < len(NUMBERS) else f"{n:,}"
    return word[0].upper() + word[1:] if cap and word[0].islower() else word


def count(n, one, many=None, cap=False):
    return f"{words(n, cap)} {one if n == 1 else (many or one + 's')}"


def places(db, kind, value, keep=None):
    """Say states when they are all states. Congress, a federal agency and Puerto Rico are not.

    Counted by jurisdiction code, the federal government once. Counting names made it five places,
    one for every agency that had issued a rule, and called Puerto Rico a state.
    """
    keep = in_scope(db) if keep is None else keep
    codes = {r["jurisdiction"] or "" for r in db.execute(
        "SELECT DISTINCT m.id, m.jurisdiction FROM measures m "
        "JOIN tags t ON t.target = m.id WHERE t.kind = ? AND t.value = ?", (kind, value))
        if r["id"] in keep}
    where = {c for c in codes if c not in FEDERAL} | ({"us"} if codes & set(FEDERAL) else set())
    return len(where), ("state" if where and where <= STATES else "jurisdiction")


def patterns(db, today, recent):
    """Lines that state what the whole record adds up to, computed fresh every time.

    Measures that carry a control arrive at about three a week, which is not a daily brief. These
    are: each one is true of the database as it stands, so an edition can always be filled without
    reaching for a measure that does not carry its weight. They repeat only when the number moves,
    which is what makes them worth reading twice.
    """
    names = controls_by_slug()
    fears = fears_by_slug()
    out = []

    # Counted over the set the front page shows, not over every tag on file, because the plate
    # and the page are read side by side.
    keep = in_scope(db)
    tally = collections.defaultdict(set)
    for t in db.execute("SELECT kind, value, target FROM tags WHERE kind IN ('control','fear')"):
        if t["target"] in keep:
            tally[(t["kind"], t["value"])].add(t["target"])

    for (kind, slug), ids in sorted(tally.items(), key=lambda kv: -len(kv[1])):
        if kind != "control" or len(ids) < 5:
            continue
        r = {"slug": slug, "n": len(ids)}
        c = names.get(r["slug"])
        if not c:
            continue
        n, word = places(db, "control", r["slug"], keep)
        out.append({"key": f"control:{r['slug']}:{r['n']}", "weight": WEIGHT.get(r["slug"], 1),
                    "sentence": f"{count(r['n'], 'measure', cap=True)} in {count(n, word)} "
                                f"would {c['pattern']}.",
                    "meta": c["chip"], "office": "",
                    "head": f"{r['n']} measures in {count(n, word)}: {c['head']}"})

    for (kind, slug), ids in sorted(tally.items(), key=lambda kv: -len(kv[1])):
        if kind != "fear" or len(ids) < 5:
            continue
        r = {"slug": slug, "n": len(ids)}
        f = fears.get(r["slug"])
        if not f:
            continue
        n, word = places(db, "fear", r["slug"], keep)
        out.append({"key": f"fear:{r['slug']}:{r['n']}", "weight": 3,
                    "sentence": f"{count(r['n'], 'measure', cap=True)} in {count(n, word)} "
                                f"name the same fear: {f['name']}.",
                    "meta": f["name"], "office": "",
                    "head": f"{r['n']} measures name the same fear: {f['name']}"})

    # Normalized and taken over the page's own set, the same way the front page counts offices.
    # Counting distinct strings made this line say 195 while the page said 190, and split one
    # office across two spellings when deciding which is named most often. Only measures that
    # carry a control count, as on the page: an office gains nothing from one that imposes nothing.
    controlled = set().union(*[ids for (kind, slug), ids in tally.items() if kind == "control" and slug in names])
    named, by_office = {}, collections.defaultdict(set)
    for t in db.execute("SELECT target, value FROM tags WHERE kind='agency'"):
        if t["target"] not in controlled:
            continue
        k = name_key(t["value"])
        if not k:
            continue
        named.setdefault(k, (t["value"] or "").strip())
        by_office[k].add(t["target"])
    if named:
        k = max(by_office, key=lambda x: len(by_office[x]))
        out.append({"key": f"offices:{len(named)}", "weight": 4,
                    "sentence": f"{count(len(named), 'named office', 'named offices', cap=True)} would gain a "
                                f"power over AI. The one named most often is the "
                                f"{re.sub(r'^the ', '', named[k], flags=re.I)}, in "
                                f"{count(len(by_office[k]), 'measure')}.",
                    "meta": "", "office": "",  # the sentence already names it
                    "head": f"{len(named)} offices would gain a power over AI"})

    # the same line-number rule the site uses, so a total here matches the total there
    ents = Entities()
    given, donors, committees = 0.0, set(), set()
    for r in db.execute("SELECT committee_name, counterparty, counterparty_key, amount, line_number FROM fec"):
        if not gave(r["line_number"], ents.match_name(r["counterparty"] or "")):
            continue
        given += r["amount"] or 0
        donors.add(r["counterparty_key"])
        committees.add(r["committee_name"])
    if len(committees) >= 2:
        out.append({"key": f"money:{int(given)}", "weight": 4,
                    "sentence": f"${given / 1e6:,.1f} million from {count(len(donors), 'donor')} sits in "
                                f"{count(len(committees), 'political committee')} working on AI policy.",
                    "meta": ", ".join(sorted(c.title() for c in committees)), "office": "",
                    "head": f"${given / 1e6:,.1f} million in {len(committees)} committees on AI policy"})

    return [p for p in out if p["key"] not in recent]


HEAD_SYSTEM = ("You shorten one sentence about a U.S. bill into a headline for a public record. "
               "Reply with a single JSON object and nothing else.")

HEAD_RULES = (
    "Shorten the sentence to at most nine words.\n"
    "Keep the place it names and the thing it does. Drop everything else.\n"
    "If the sentence says something is put under a body, or that a body decides or licenses or "
    "refuses, that is the thing it does and it stays. A list of what a company has to do is not.\n"
    "It is a plain statement with a verb. Someone who reads only this line and nothing else "
    "should come away knowing one fact.\n"
    "Do not be clever, do not ask a question, and do not open with who, what, how, why, or when.\n"
    "Keep the tense exactly as the sentence has it. If the sentence says would, the headline says "
    "would. Dropping it turns a proposal into a law and the headline into a false one.\n"
    "Do not add a number, a place, or anything else the sentence does not already say.\n"
    "If the sentence puts something under a body, or says a body certifies, approves, exempts or "
    "decides, name that body. A measure that lets one office decide who is exempt is not the same "
    "as the requirement going away, and a headline without the body says the second one."
)

NUMERAL = re.compile(r"\d[\d,.]*")


# The words the POWER pattern supplies itself, as opposed to the ones naming who holds it.
POWER_GLUE = {"under", "that", "which", "who", "whom", "gives", "give", "hands", "hand", "grants",
              "grant", "leaves", "leave", "authority", "state", "independent", "outside", "party",
              "third"}


def power_holder(text):
    """The words that name who holds the power, out of the phrase saying there is one.

    A headline is nine words and the model decides which nine. Dropping the body turns "puts data
    centre projects under the Governor's authority to certify them as exempt" into "would exempt
    data centre projects from environmental review", which is the same measure read backwards: a
    rule disappearing rather than an office deciding who it disappears for. Whatever else the
    headline loses, it does not lose this.
    """
    m = POWER.search(text or "")
    if not m:
        return set()
    return {w.lower() for w in re.findall(r"[A-Za-z]{4,}", m.group(0))} - POWER_GLUE


def power_clause(text, limit=13):
    """The sentence's own words up to the end of the phrase that names the power.

    Cutting at a word count lands mid-phrase. The end of the power phrase is a boundary, and
    everything up to it is the sentence's own wording, already checked against the measure.
    """
    m = POWER.search(text or "")
    if not m:
        return ""
    head = (text[:m.end()] or "").strip().rstrip(",;:")
    return head if 5 <= len(head.split()) <= limit else ""


def plain(line, names):
    """A headline that is always well formed: the place, then what the measure does to people.

    Cutting a sentence off at nine words lands mid-phrase and says something the measure does
    not, so the fallback builds a new line out of two fields instead of trimming one.
    """
    held = power_clause(line.get("sentence") or "")
    if held:
        return held
    best = sorted((c for c in (line.get("controls") or "").split("|") if c in names),
                  key=lambda c: -WEIGHT.get(c, 1))
    # A federal rule's jurisdiction is its agencies, parent first: "Health and Human Services
    # Department, Food and Drug Administration". The office that acted is the last of them.
    where = (line.get("jurisdiction") or "").rsplit(", ", 1)[-1]
    if best and where:
        return f"{where}: {names[best[0]]['head']}"
    return names[best[0]]["head"].capitalize() if best else where


def headline(key, lines, names, totals):
    """The one line on the plate, and the only text on it that is not a fixed part of the mark.

    A pattern line brings its own, written from the same numbers that made it. A measure line is
    shortened by the model, and then checked: no digit may appear that the sentence did not
    already contain, which is the way a headline would go wrong if it went wrong. Anything that
    fails falls back to the sentence's own opening, which is already checked against the source.
    """
    lead = lines[0]
    if lead.get("head"):
        return lead["head"]
    text = lead["sentence"]
    # the headline is the largest text on the plate, so it answers for its tense like the rest
    source = set(NUMERAL.findall(text))
    place = lead.get("jurisdiction") or ""
    conditional = bool(CONDITIONAL.search(tense_of(text, place)))
    holder = power_holder(text)
    extra = ""
    for _ in range(2 if holder else 1):
        try:
            out = call(key, HEAD_SYSTEM, f"SENTENCE\n{text}\n\n{HEAD_RULES}{extra}\n\n"
                                        'Return JSON: {"line": "..."}', max_tokens=150)
            line = (out.get("line") or "").strip().rstrip(".")
        except Exception as exc:
            log(f"[brief] headline: {exc}")
            break
        if holder and not any(w in line.lower() for w in holder):
            # Everything else here stops the headline saying something untrue. This stops it
            # saying the true thing backwards, which is the only way it has gone wrong so far.
            log(f"[brief] headline dropped who holds the power: {line!r}")
            extra = ("\n\nThe attempt before this one dropped the body the sentence puts this "
                     f"under. The headline has to name {' or '.join(sorted(holder))}. A measure "
                     "that hands one office the power to decide is not the same as the rule it "
                     "decides about going away, and without the body the line says the second.")
            continue
        if line and len(line.split()) <= 10 \
                and not sponsors_word(line, lead.get("office") or "", loose=True) \
                and set(NUMERAL.findall(line)) <= source \
                and bool(CONDITIONAL.search(tense_of(line, place))) == conditional:
            return line
        break
    return plain(lead, names)


def alt(head, lines):
    """What the card says, for anyone who cannot see it."""
    body = " ".join(f"{l['sentence']}"
                    f"{' Held by ' + l['office'] + '.' if l['office'] else ''}" for l in lines)
    return f"A card from the AI Fear Report headed: {head}. {body}"


FEDERAL = ("us", "fed", "us-exec")


def in_scope(db):
    """The measures the front page counts, which is the set every number on the plate is taken over.

    The one list both use, from common.measures_in_scope: read, judged to be about AI, filed since
    the report starts or a rule or order, not suppressed, and a carried-over bill once. The plate
    goes out on X while the page it describes is one click away, so a count taken over a wider set
    than the page uses is a contradiction waiting to be noticed.
    """
    return {m["id"] for m in measures_in_scope(db)}


def totals(db):
    """The three counts the plate carries, on the rule the front page counts by.

    The plate goes out on X while the page it describes is one click away, so the two have to
    agree. Counting distinct agency strings instead of normalized names, over every measure on
    file instead of the ones the page shows, put five offices on the plate that the page did not
    have. The test suite holds these against the exported page numbers.
    """
    keep = in_scope(db)
    known = {c["slug"] for c in config("controls")}
    controlled, named = set(), collections.defaultdict(set)
    for t in db.execute("SELECT target, kind, value FROM tags WHERE kind IN ('control','agency')"):
        if t["target"] not in keep:
            continue
        if t["kind"] == "control" and t["value"] in known:
            controlled.add(t["target"])
        elif t["kind"] == "agency" and name_key(t["value"]):
            named[name_key(t["value"])].add(t["target"])
    # An office counts when a measure that carries a control names it, which is the page's rule.
    offices = {k for k, ids in named.items() if ids & controlled}
    return {"measures": len(keep), "controlled": len(controlled), "offices": len(offices)}


def subject(db, lead):
    """Which of a measure's controls the rest of the post talks about.

    A measure often carries several. The one worth the most is the one to lead on when nothing
    else decides it, but the reason and the pattern both have to be about the same one or the
    post says three things about three subjects.
    """
    controls = [c for c in (lead.get("controls") or "").split("|") if c]
    return max(controls, key=lambda c: WEIGHT.get(c, 1)) if controls else ""


def cited(lead, fears):
    """The fear the measure's own text cites, or nothing.

    Only what this measure says. An earlier version inferred a fear from the other measures
    imposing the same control and said so, which is an inference dressed as reporting.
    """
    for slug in (lead.get("fears") or "").split("|"):
        f = fears.get(slug)
        if f and f.get("because"):
            return f["because"]
    return ""


STATUS = {"passed": "Passed.", "failed": "Died in committee.", "pending": "Still live."}


def compose(lead, spare, tot, cfg, fears):
    """The measures, what they cite, and the counts. Nothing joining them up.

    The site does not tell a reader what to make of a row and neither does this. No line here
    says that one measure is part of a pattern; the measure is stated, the count is stated, and
    the reader puts them together or does not.
    """
    parts = [cfg["opener"], ""]
    first = [lead["sentence"]]
    if lead.get("status") in STATUS:
        first.append(STATUS[lead["status"]])
    fear = cited(lead, fears)
    if fear:
        first.append(f"It cites the fear that {fear}.")
    parts += [" ".join(first)]
    for p in spare:
        parts += ["", p["sentence"]]
    # Would hand, not hold: most of these have not passed, and nobody holds what a measure still in
    # committee would give them.
    parts += ["", f"{tot['measures']:,} measures tracked. {tot['controlled']:,} carry a control, and "
                  f"they would hand new power to {tot['offices']:,} offices."]
    return "\n".join(parts).strip()


FRESH = 1  # a measure that carries a control and names an office arrives about twice a week


def spare_for(db, lead, today, used):
    """The two counts under the measure: one about the control, one about a fear.

    A post that never names a fear does not answer for its own first line. A quarter of measures
    cite one in their own text, so the fear is carried by a count instead, which is a fact about
    the record rather than a claim about the measure above it.
    """
    spare = sorted(patterns(db, today, used), key=lambda p: -p["weight"])
    controls = [c for c in (lead.get("controls") or "").split("|") if c]
    mine = max(controls, key=lambda c: WEIGHT.get(c, 1)) if controls else ""
    by_control = [p for p in spare if p["key"].startswith("control:")]
    if mine:
        by_control.sort(key=lambda p: not p["key"].startswith(f"control:{mine}:"))
    # the fears all weigh the same, so the one worth printing is the one with the most behind it
    by_fear = sorted((p for p in spare if p["key"].startswith("fear:")),
                     key=lambda p: -int(p["key"].rsplit(":", 1)[1]))
    out = by_control[:1] + by_fear[:1]
    return out or spare[:2]


def edition(db, key, out_dir, today, want=WANT, dry_run=False):
    """One measure that moved, and the counts it sits among.

    Measures carrying a control arrive at about nine a week, which one a day can live on. The
    counts are computed from the whole record, so they hold on any day, and each is keyed to its
    own number: it comes round again only once that number has moved.
    """
    cfg = config("brief")
    names, fear_names = controls_by_slug(), fears_by_slug()
    day = dt.date.fromisoformat(today)
    if db.execute("SELECT 1 FROM brief WHERE edition=? LIMIT 1", (today,)).fetchone():
        log(f"[brief] {today} is already written")
        return None
    used = {r["target"] for r in db.execute("SELECT target FROM brief")}

    attempts = []
    lines = write_lines(key, pool(db, today), today, 1, attempts)
    kv_set(db, f"brief:attempts:{today}", attempts)
    db.commit()
    unreachable = [a for a in attempts if a.get("unreachable")]
    if attempts and len(unreachable) == len(attempts):
        # Not one candidate was judged: the model could not be reached at all. The counts below
        # are still true and the edition still goes out, because a day with no post is worse than
        # a plain one. But an edition of counts looks like an ordinary quiet day from outside, so
        # this marks the run red rather than letting it pass for one.
        annotate("error", "the brief reached no model",
                 f"none of {len(attempts)} candidates were judged: {unreachable[0]['reason']}")
        kv_set(db, f"brief:unreachable:{today}", unreachable[0]["reason"][:300])
    lead = lines[0] if lines else None
    if lead is None:
        spare = sorted(patterns(db, today, used), key=lambda p: -p["weight"])
        if not spare:
            log("[brief] nothing to say today, no edition")
            return None
        top = spare[0]
        lead = {"target": top["key"], "sentence": top["sentence"], "quote": "", "office": top["office"],
                "controls": "", "fears": "", "meta": top["meta"], "head": top["head"],
                "jurisdiction": "", "url": "", "status": ""}
        spare = spare[1:3]
    else:
        spare = spare_for(db, lead, today, used)

    tot = totals(db)
    head = headline(key, [lead], names, tot)
    text = compose(lead, spare, tot, cfg, fear_names)
    rows = [lead] + [{"target": p["key"], "sentence": p["sentence"], "quote": "", "office": p["office"],
                      "controls": "", "meta": p["meta"], "head": p["head"], "jurisdiction": "",
                      "url": "", "status": ""} for p in spare]

    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    image = str(out / f"{today}.png")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "site"))
    from brand import plate
    plate(image, day.strftime("%A %-d %B %Y"), head)
    edition_data = {"edition": today, "headline": head, "text": text, "image": image,
                    "alt": alt(head, rows), "lines": rows, "totals": tot}
    (out / f"{today}.json").write_text(json.dumps(edition_data, indent=1))
    if not dry_run:
        for l in rows:
            db.execute("INSERT OR REPLACE INTO brief(target, edition, sentence, evidence, office, "
                       "controls, written_at) VALUES(?,?,?,?,?,?,?)",
                       (l["target"], today, l["sentence"], l["quote"], l["office"], l["controls"],
                        now().isoformat()))
        # The site shows the latest card rather than describing it, and the site is built from the
        # database, not from this folder. The headline rides the database so the page can name the
        # edition it is showing; the plate itself is copied onto the site beside it.
        kv_set(db, "brief:latest", {"edition": today, "headline": head,
                                    "alt": edition_data["alt"]})
        db.commit()
    log(f"[brief] {today}: {len(rows)} entries, plate at {image}"
        f"{' (dry run, nothing recorded)' if dry_run else ''}")
    return edition_data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/index.db")
    ap.add_argument("--out", default="state/brief")
    ap.add_argument("--date", default="")
    ap.add_argument("--lines", type=int, default=WANT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db = connect(args.db)
    today = args.date or edition_date()
    data = edition(db, env("ANTHROPIC_API_KEY"), args.out, today, args.lines, args.dry_run)
    if data:
        print("\n" + "-" * 60)
        print(data["text"])
        print("-" * 60)


if __name__ == "__main__":
    main()
