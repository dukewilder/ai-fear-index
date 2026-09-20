"""Offline check: build a synthetic database, export it, and render every page.

Run with: python -m tests.test_offline
Nothing here touches the network or the real data.
"""
import datetime as dt
import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import export  # noqa: E402
from pipeline.common import connect, iso, name_key, upsert  # noqa: E402


def check_brief_prompt():
    """Build the prompt the brief sends, for both statuses, without touching the network.

    A rewrite of the rules once deleted the two tense strings and left the line that formats them
    in. Every sentence then died of a NameError that the row-by-row catch swallowed, so a whole
    day of editions fell back to counts while the log said the sentences did not hold up. This
    costs nothing and would have caught it.
    """
    from pipeline import brief
    for status, expect in (("passed", "present tense"), ("pending", "conditional")):
        tense = brief.LAW if status == "passed" else brief.PENDING
        rules = brief.RULES.format(tense=tense)
        assert expect in rules, f"{status}: the rules do not tell the model the tense"
        assert "{tense}" not in rules, f"{status}: the placeholder survived"
    row = {"jurisdiction_name": "Ohio", "identifier": "HB 1", "title": "A title",
           "summary": "A summary long enough to quote from.", "status": "pending",
           "office": "Ohio Department of Commerce"}
    assert "Ohio" in brief.doc(row)
    # and the gates, both ways round
    body = "ohio would put every operator under the ohio department of commerce"
    good = "Ohio would put every operator under the Ohio Department of Commerce, which decides who may run one."
    assert brief.why_not(good, "would put every operator under the ohio department", body, False,
                         row["office"]) == "", "a sound pending sentence was rejected"
    assert brief.why_not(good, "would put every operator under the ohio department", body, True,
                         row["office"]), "a passed measure written conditionally was accepted"
    # the gates that decide whether a sentence is reporting or repeating the sponsor
    FDA = "Health and Human Services Department, Food and Drug Administration"
    SHAPES = [
        # the sponsor's grammar, including the ways it can look like the other kind
        ("California requires operators of companion chatbots to perform risk assessments and submit "
         "to independent audits reported to the Attorney General.", "California", False),
        (FDA + " requires operators to file reports and keep records.", FDA, False),
        ("California requires operators to certify compliance under penalty of perjury.", "California", False),
        ("California requires operators to file reports that may be audited later.", "California", False),
        ("Michigan would require utilities to file forecasts under the Clean Energy Act.", "Michigan", False),
        ("Texas requires a developer to give notice to every user of the system.", "Texas", False),
        ("Michigan would require utilities to file data center load forecasts with the Public "
         "Service Commission.", "Michigan", False),
        # and sentences that do name a power
        ("California puts AI auditors under registration with the Government Operations Agency, "
         "which licenses them and investigates violations.", "California", True),
        ("New York would require every frontier developer to register with the Office for AI Model "
         "Developer Oversight, which decides who may release one.", "New York", True),
        ("California puts companion chatbots under independent child safety audits reported to the "
         "Attorney General beginning July 1, 2027.", "California", True),
        ("Michigan would put every data center's load forecast under the Public Service Commission.",
         "Michigan", True),
        ("California requires every companion chatbot to be certified by an auditor the Attorney "
         "General approves.", "California", True),
        ("Ohio prohibits a person from deploying an artificial intelligence that represents itself "
         "as a therapist.", "Ohio", True),
    ]
    for text, place, keep in SHAPES:
        head = brief.tense_of(text, place)
        kept = not (brief.DUTY_OPENER.search(head) and not brief.POWER.search(text))
        assert kept == keep, f"power framing: {text[:60]!r} should be {'kept' if keep else 'rejected'}"
    # and through why_not itself, so removing the gate fails here rather than in production
    duty = ("California requires operators of companion chatbots to perform risk assessments and "
            "submit to independent audits reported to the Attorney General.")
    dbody = ("california requires operators of companion chatbots to perform risk assessments and "
             "submit to independent audits reported to the attorney general")
    assert "duty" in brief.why_not(duty, "requires operators of companion chatbots to perform risk",
                                   dbody, True, "", "", "California"), "the duty gate is not wired in"
    assert brief.starts_later("commencing January 1, 2029, the agency", "2026-09-20") == "2029"
    assert brief.starts_later("effective January 1, 2025, the agency", "2026-09-20") == ""
    later = ("California puts AI auditors under registration with the Government Operations Agency, "
             "which licenses them.")
    lbody = ("california puts ai auditors under registration with the government operations agency "
             "which licenses them")
    assert "2029" in brief.why_not(later, "puts ai auditors under registration with the government",
                                   lbody, True, "", "2029", "California"), "the start-year gate is not wired in"
    assert brief.why_not(later.replace("Agency,", "Agency from 2029,"),
                         "puts ai auditors under registration with the government",
                         lbody, True, "", "2029", "California") == "", "a sentence carrying the year was rejected"
    print("brief prompt and gates: ok")


def check_plate_fits():
    """The card must carry the whole headline, at whatever size that takes.

    It used to set three lines at one size and drop the rest, so an eleven word headline went out
    reading "...UNDER ENVIRONMENTAL" and stopping. A smaller headline is a headline. Half of one
    is a different claim.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "site"))
    try:
        import brand
    except Exception as exc:  # the fonts are not always present, and the pages matter more
        print("plate check skipped:", exc)
        return
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new("RGB", (1600, 900)))
    room = (900 - 92 - 30) - 34 - (92 + 24 + 120 * 1.62 + 132)
    for head in ("California puts companion chatbots under child safety audits",
                 "Pennsylvania would put commercial data center water supplies under Environmental Protection",
                 "New York puts every frontier model developer under an office that decides who may release one"):
        _, lines, step = brand.fit_headline(d, head, 1600 - 184, room)
        assert " ".join(lines).split() == head.upper().split(), f"the card would drop words from: {head}"
        assert len(lines) * step <= room, f"the card would run into its own footer: {head}"
    print("plate fits the whole headline: ok")


def check_headline_tidy():
    """Tidying a headline may move punctuation and cut a repeated publisher. Never a word.

    Every headline on the site goes through this, so the thing to hold it to is that the letters
    survive. Checked against all 354 stored at the time it was written: 152 changed punctuation
    only, 3 had a credit cut from the end, none was altered in any other way.
    """
    import re as _re
    from pipeline.common import tidy_headline
    letters = lambda s: _re.sub(r"[^a-z0-9]", "", (s or "").lower())
    KEEP = [
        ("U . S . weighs new rules on chips", "reuters.com", "U.S. weighs new rules on chips"),
        ("Trump Announces  AI Force , Czar Appointment", "dailycaller.com",
         "Trump Announces AI Force, Czar Appointment"),
        ("How Can AI Kill All Humans ? Experts Explain", "nypost.com",
         "How Can AI Kill All Humans? Experts Explain"),
        ("The case for AI - and against it", "theguardian.com", "The case for AI - and against it"),
        ("AI and the state-by-state patchwork", "axios.com", "AI and the state-by-state patchwork"),
    ]
    CUT = [
        ("House Passes Bill \u2013 NaturalNews . com", "naturalnews.com", "House Passes Bill"),
        ("AI , open models , China | Homeland Security Newswire", "homelandsecuritynewswire.com",
         "AI, open models, China"),
        ("Anthropic raises round - Axios", "axios.com", "Anthropic raises round"),
    ]
    for raw, dom, want in KEEP:
        got = tidy_headline(raw, dom)
        assert got == want, f"tidy changed {raw!r} to {got!r}, wanted {want!r}"
        assert letters(got) == letters(raw), f"tidy lost letters from {raw!r}"
    for raw, dom, want in CUT:
        got = tidy_headline(raw, dom)
        assert got == want, f"tidy changed {raw!r} to {got!r}, wanted {want!r}"
        assert letters(raw).startswith(letters(got)), f"tidy did more than cut a tail from {raw!r}"
    for raw, dom, _ in KEEP + CUT:
        once = tidy_headline(raw, dom)
        assert tidy_headline(once, dom) == once, f"tidy is not idempotent on {raw!r}"
    print("headline tidying keeps every letter: ok")


def check_fears_config():
    """Every fear has to carry the same fields and patterns that compile.

    The tagger, the news matcher, GDELT and the Wikipedia collector each read a different key off
    these, and a fear missing one fails whichever collector wanted it, hours later and quietly.
    """
    from pipeline.common import config
    import re as _re
    fears = config("fears")
    keys = set(fears[0])
    assert keys >= {"slug", "name", "short", "definition", "keywords", "gdelt", "match",
                    "wikipedia", "because"}, f"the first fear is missing fields: {keys}"
    seen = set()
    for f in fears:
        assert set(f) == keys, f"{f.get('slug')} has fields {set(f) ^ keys} the others do not"
        assert f["slug"] not in seen, f"two fears called {f['slug']}"
        seen.add(f["slug"])
        for field in ("name", "short", "definition", "gdelt", "because"):
            assert isinstance(f[field], str) and f[field].strip(), f"{f['slug']} has an empty {field}"
        assert f["keywords"], f"{f['slug']} has no keywords"
        assert f["match"], f"{f['slug']} has no match patterns"
        for pat in f["match"]:
            _re.compile(pat)   # raises here rather than inside a collector
        # "because" is dropped in after "It cites the fear that", so it is a clause, not a sentence
        why = f["because"]
        assert not why.endswith("."), f"{f['slug']}: because ends in a full stop and one is added"
        assert not why.lower().startswith("that "), f"{f['slug']}: because repeats the 'that'"
        assert why[0] == why[0].lower() or why.split()[0] in ("AI", "China", "OpenAI", "Congress"), \
            f"{f['slug']}: because starts mid-sentence, so only a proper noun is capitalised"
    print(f"{len(fears)} fears, all well formed: ok")


def check_no_euphemism():
    """Nothing the site says in its own voice may use the vocabulary the report exists to undo.

    Every string here is one somebody wrote by hand: the control taxonomy, the fear names and the
    lines that follow "It cites the fear that", the brief's opener, the tagline, and the headings
    and captions in the templates. A measure's own title and anything quoted from one are not in
    this list and never will be, because the gap between how a power is sold and what it does is
    the exhibit, and editing a quote would destroy it.

    The same list gates the sentence and the card the model writes each morning, so the check on
    the copy and the check on the writing are one definition in one place.
    """
    import json as _json, re as _re
    from pipeline.common import EUPHEMISM
    found = []

    def scan(where, text):
        for m in EUPHEMISM.finditer(text or ""):
            found.append((where, m.group(0), (text or "")[:90]))

    for c in _json.loads((ROOT / "config" / "controls.json").read_text()):
        for k in ("name", "chip", "head", "pattern", "definition"):
            scan(f"controls/{c['slug']}/{k}", c.get(k))
    # A fear's definition is the one string here the reader never sees. It goes to the tagger, and
    # the tagger has to speak the sponsor's language to find the sponsor's bills: a fear about
    # children and chatbots is written in bills that say "minors' safety". Control definitions are
    # not exempt, because the site prints those on the row they label.
    for f in _json.loads((ROOT / "config" / "fears.json").read_text()):
        for k in ("name", "short", "because"):
            scan(f"fears/{f['slug']}/{k}", f.get(k))
    for name in ("brief", "site"):
        blob = _json.loads((ROOT / "config" / f"{name}.json").read_text())
        for k, v in (blob.items() if isinstance(blob, dict) else []):
            if isinstance(v, str):
                scan(f"{name}/{k}", v)
    for t in sorted((ROOT / "site" / "templates").rglob("*.html")):
        text = _re.sub(r"\{[%{].*?[%}]\}", " ", t.read_text(), flags=_re.S)
        scan(f"template {t.name}", " ".join(_re.sub(r"<[^>]+>", " ", text).split()))

    # and the copy written straight into the code, which is neither config nor template: the tile
    # captions, the section labels, the words around every figure.
    import ast as _ast
    PROSE = _re.compile(r"^[A-Za-z][A-Za-z0-9 ,.:;'\u2019()%$-]{12,}$")
    SQLISH = _re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|GROUP BY|ORDER BY|JOIN|VALUES)\b")
    for src in ("pipeline/export.py", "pipeline/brief.py", "site/build.py", "site/brand.py"):
        path = ROOT / src
        for node in _ast.walk(_ast.parse(path.read_text())):
            if not isinstance(node, _ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value
            if not PROSE.match(text) or SQLISH.search(text) or "\\" in text:
                continue
            scan(f"{path.name}:{node.lineno}", text)

    if found:
        lines = "\n".join(f"    {w}: {word!r} in {ctx}" for w, word, ctx in found)
        raise AssertionError(f"the site writes the sponsor's vocabulary in its own voice:\n{lines}")
    print("no euphemism in anything the site says itself: ok")


def check_fec_sweep():
    """The register sweep writes money into the database unattended, so it is exercised here.

    Four things have to hold. A committee on the entity list is left to the loop that owns it. One
    the sweep found before is read again, because its receipts keep changing after the day it was
    found. One it should never have taken is dropped. And a name that only matches inside the
    bracket the FEC adds to tell similar committees apart is not a match at all.
    """
    from pipeline import collect_fec as fec
    from pipeline.common import connect as _connect
    import tempfile as _tempfile
    db = _connect(pathlib.Path(_tempfile.mkdtemp()) / "fec.db")
    for cid, name, ent in (("C1", "AI SAFETY PAC", None),
                           ("C2", "APPRAISAL INSTITUTE PAC (AI PAC)", None),
                           ("C3", "LEADING THE FUTURE", "leading-the-future")):
        db.execute("INSERT INTO committees(id,name,entity,query,receipts) VALUES(?,?,?,?,?)",
                   (cid, name, ent, "sweep:x" if ent is None else "by name", 1000))
    db.commit()
    register = [{"committee_id": "C1", "name": "AI SAFETY PAC", "committee_type": "O"},
                {"committee_id": "C4", "name": "HUMANITY ABOVE ARTIFICIAL INTELLIGENCE PAC",
                 "committee_type": "O"},
                {"committee_id": "C3", "name": "LEADING THE FUTURE", "committee_type": "O"},
                {"committee_id": "C5", "name": "AIR LINE PILOTS ASSOCIATION PAC", "committee_type": "N"}]

    class Http:
        asked = []

        def json(self, url, params=None, **kw):
            if url.endswith("/committees/"):
                return {"results": register if params["q"] == fec.SWEEP[0] else []}
            if "/totals/" in url:
                Http.asked.append(url.split("/committee/")[1].split("/")[0])
                return {"results": [{"receipts": 5000, "disbursements": 0,
                                     "independent_expenditures": 0}]}
            return {"results": []}

    fec.sweep(db, Http(), "key", None, 2026, [])
    left = {r["id"]: r for r in db.execute("SELECT id, name, entity, receipts FROM committees")}
    assert "C2" not in left, "the sweep kept a committee that only matches inside the bracket"
    assert "C5" not in left, "the sweep took a committee whose name is not about AI"
    assert "C4" in left, "the sweep missed a new committee"
    assert left["C1"]["receipts"] == 5000, "a committee the sweep found before was not read again"
    assert left["C3"]["receipts"] == 1000, "the sweep overwrote a committee the entity list owns"
    assert Http.asked == ["C1", "C4"], f"totals asked for the wrong set: {Http.asked}"
    print("the FEC register sweep: ok")


def check_post_needs_entries():
    """An edition is posted from the database, not from whatever file is lying about.

    The file on the data branch outlives the code that wrote it. A pass that clears the day to
    write it again and then cannot write it leaves the old file in place, and the poster reads
    files. Without this guard the morning post would carry a sentence the current code has
    already refused. So: rows for the day, or nothing goes out.
    """
    from pipeline import post
    from pipeline.common import connect as _connect, kv_get as _kv_get
    import tempfile as _tempfile
    room = pathlib.Path(_tempfile.mkdtemp())
    db = _connect(room / "post.db")
    day = "2026-09-20"
    briefs = room / "brief"
    briefs.mkdir()
    (briefs / f"{day}.json").write_text(json.dumps(
        {"edition": day, "headline": "stale", "text": "a sentence the code has since refused",
         "alt": "", "lines": []}))

    def refuse():
        raise AssertionError("the poster reached for credentials with nothing written for the day")

    kept, post.credentials = post.credentials, refuse
    try:
        assert post.run(db, str(briefs), day) is None, "a stale file was posted"
        db.execute("INSERT INTO brief(target, edition, sentence, evidence, office, controls, "
                   "written_at) VALUES(?,?,?,?,?,?,?)",
                   ("t1", day, "a sentence this code wrote", "", "", "", iso(dt.datetime.now())))
        db.commit()
        try:
            post.run(db, str(briefs), day)
        except AssertionError as e:
            if "reached for credentials" not in str(e):
                raise
        else:
            raise AssertionError("the guard held an edition the database does have")
    finally:
        post.credentials = kept
    # And when X refuses the post, the reason is written down where it can be read later
    room2 = pathlib.Path(_tempfile.mkdtemp())
    db2 = _connect(room2 / "post.db")
    briefs2 = room2 / "brief"
    briefs2.mkdir()
    (briefs2 / f"{day}.json").write_text(json.dumps(
        {"edition": day, "headline": "h", "text": "x" * 444, "alt": "", "lines": []}))
    db2.execute("INSERT INTO brief(target, edition, sentence, evidence, office, controls, "
                "written_at) VALUES(?,?,?,?,?,?,?)",
                ("t1", day, "s", "", "", "", iso(dt.datetime.now())))
    db2.commit()

    def refuse_post(*a, **k):
        raise RuntimeError("X refused the post: 403 Your account is not allowed to post this")

    kept_creds, kept_pub = post.credentials, post.publish
    post.credentials, post.publish = (lambda: {"key": "k", "secret": "s", "token": "t",
                                               "token_secret": "ts"}), refuse_post
    try:
        try:
            post.run(db2, str(briefs2), day)
        except RuntimeError:
            pass
        else:
            raise AssertionError("a refusal from X was swallowed instead of failing the run")
    finally:
        post.credentials, post.publish = kept_creds, kept_pub
    noted = _kv_get(db2, f"post:refused:{day}")
    assert noted, "X refused the post and nothing was written down"
    assert noted["characters"] == 444, f"the length was not recorded: {noted}"
    assert "403" in noted["why"], f"the reason was not recorded: {noted}"
    print("the poster refuses a stale edition, and records a refusal: ok")


def check_failed_source_retries():
    """A daily source that errors is tried again today, not tomorrow.

    The daily pass marks itself done whatever happened inside it, so before this a source that
    threw at six in the morning sat red for twenty-four hours, and a fix pushed at noon did not
    run until the next morning either. Four things have to hold: a daily source that errored is
    asked for again; one that succeeded lets go of the key it was carrying; one that set its own
    retry during the run keeps it, because openstates asks for another turn when it reaches its
    allowance, which is not a failure; and an hourly source is left alone, since the next run is
    twenty minutes away.
    """
    from pipeline import run as runner
    from pipeline.common import connect as _connect, kv_get as _kv_get
    import tempfile as _tempfile
    db = _connect(pathlib.Path(_tempfile.mkdtemp()) / "runs.db")
    for name, ok in (("fec", 0), ("lda", 1), ("openstates", 1), ("congress", 1), ("rss", 0)):
        db.execute("INSERT INTO status(source,last_run,ok,added,message) VALUES(?,?,?,?,?)",
                   (name, iso(dt.datetime.now()), ok, 0, ""))
    db.execute("INSERT INTO kv(key,value) VALUES('retry:lda', '\"2026-01-01T00:00:00\"')")
    db.execute("INSERT INTO kv(key,value) VALUES('retry:openstates', '\"2026-01-01T00:00:00\"')")
    db.commit()
    asked = {"fec": None, "lda": "2026-01-01T00:00:00", "congress": None, "rss": None,
             "openstates": "2026-01-01T00:00:00"}
    # openstates reached its allowance during the run and moved its own key on the way past
    db.execute("UPDATE kv SET value='\"2026-09-20T18:00:00\"' WHERE key='retry:openstates'")
    db.commit()
    runner.ask_again(db, list(asked), asked)
    assert _kv_get(db, "retry:fec"), "a daily source that errored was not asked for again"
    assert not _kv_get(db, "retry:lda"), "a daily source that succeeded kept a spent retry"
    assert _kv_get(db, "retry:openstates") == "2026-09-20T18:00:00", \
        "a source that asked for its own next turn had the request overwritten"
    assert not _kv_get(db, "retry:congress"), "a source that succeeded was asked for again"
    assert not _kv_get(db, "retry:rss"), "an hourly source was given a retry it does not need"
    print("a failed daily source is tried again today: ok")


def check_places_word():
    """An executive order is not a state, whatever its office is called.

    The plate says "in ten states" or "in ten jurisdictions" depending on what it counted. The
    test for that read the end of the place's name, so Education Department was federal and
    Executive Office of the President was a state. The jurisdiction code is the answer.
    """
    from pipeline import brief
    from pipeline.common import connect as _connect
    import tempfile as _tempfile
    db = _connect(pathlib.Path(_tempfile.mkdtemp()) / "places.db")
    day = dt.date.today().isoformat()
    rows = [("m1", "ca", "California"), ("m2", "ny", "New York"), ("m3", "tx", "Texas")]
    for mid, code, name in rows:
        upsert(db, "measures", {"id": mid, "kind": "bill", "jurisdiction": code,
                                "jurisdiction_name": name, "session": "2025", "identifier": "SB 1",
                                "title": "t", "summary": "s", "status": "pending", "url": f"u{mid}",
                                "introduced_date": day, "source": "test", "first_seen": iso()})
        db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, "control", "c", "q", "t", iso()))
    db.commit()
    assert brief.places(db, "control", "c") == (3, "state"), "three states are three states"
    upsert(db, "measures", {"id": "m4", "kind": "order", "jurisdiction": "us-exec",
                            "jurisdiction_name": "Executive Office of the President", "session": "",
                            "identifier": "EO 1", "title": "t", "summary": "s", "status": "passed",
                            "url": "u4", "introduced_date": day, "source": "test", "first_seen": iso()})
    db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", ("m4", "h", 1, iso()))
    db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", ("m4", "control", "c", "q", "t", iso()))
    db.commit()
    n, word = brief.places(db, "control", "c")
    assert (n, word) == (4, "jurisdiction"), \
        f"an executive order counted as a state: the plate would have said {n} {word}s"
    print("an executive order is not a state: ok")


def main():
    check_brief_prompt()
    check_plate_fits()
    check_headline_tidy()
    check_fears_config()
    check_no_euphemism()
    check_fec_sweep()
    check_post_needs_entries()
    check_failed_source_retries()
    check_places_word()
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = connect(tmp / "index.db")
    today = dt.date.today()
    for i in range(40):
        day = (today - dt.timedelta(days=i * 9)).isoformat()
        mid = f"os-test{i}"
        upsert(db, "measures", {"id": mid, "kind": "bill", "jurisdiction": ["ca", "ny", "tx", "us"][i % 4],
                                "jurisdiction_name": ["California", "New York", "Texas", "Congress"][i % 4],
                                "session": "2025", "identifier": f"SB {100 + i}" if i % 4 != 3 else f"H.R. {1000 + i}",
                                "title": f"Sample AI safety bill {i}", "summary": "A sample summary.",
                                "status": ["pending", "passed"][i % 2], "latest_action": "Referred to committee",
                                "latest_action_date": day, "introduced_date": day, "url": "https://example.com",
                                "sponsors": "Sample", "source": "test", "updated": day, "first_seen": iso()})
        db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        for kind, value in [("fear", ["loss-of-control", "deepfakes", "kids-chatbots", "china-race"][i % 4]),
                            ("control", ["mandatory-reporting", "new-agency-powers", "labeling-mandates"][i % 3]),
                            # The third spelling is the second office again. Real tags arrive
                            # worded however the bill worded them, and a count of distinct
                            # strings reports one office as two.
                            ("agency", ["California Attorney General", "Federal Trade Commission",
                                        "the FEDERAL  TRADE COMMISSION"][i % 3])]:
            db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, kind, value, "quote", "test", iso()))
    # A measure the front page does not show: read, judged not about AI, and tagged all the same.
    # Anything counting over the whole tags table rather than the page's own set picks it up.
    upsert(db, "measures", {"id": "os-offpage", "kind": "bill", "jurisdiction": "ca",
                            "jurisdiction_name": "California", "session": "2025", "identifier": "SB 999",
                            "title": "A bill about something else", "summary": "Not about AI.",
                            "status": "pending", "latest_action": "Referred", "latest_action_date": iso()[:10],
                            "introduced_date": iso()[:10], "url": "https://example.com/offpage",
                            "sponsors": "Sample", "source": "test", "updated": iso()[:10], "first_seen": iso()})
    db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", ("os-offpage", "h", 0, iso()))
    for kind, value in (("control", "new-agency-powers"), ("agency", "Nevada Gaming Control Board")):
        db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", ("os-offpage", kind, value, "quote", "test", iso()))
    clients = ["OPENAI OPCO, LLC", "META PLATFORMS, INC.", "BUSINESS ROUNDTABLE",
               "CENTER FOR AI SAFETY ACTION FUND", "AMERICANS FOR RESPONSIBLE INNOVATION"]
    for i in range(30):
        y, q = today.year - (i // 12), (i % 4) + 1
        upsert(db, "lobbying", {"id": f"l{i}", "client": clients[i % len(clients)],
                                "client_key": name_key(clients[i % len(clients)]),
                                "registrant": "Firm", "year": y, "period": "q", "quarter": f"Q{q}", "amount": 50000 + i * 1000,
                                "issues": "Artificial intelligence safety, deepfakes, children online, H.R. 1003",
                                "bill_refs": "H.R. 1003", "gov_entities": "SENATE", "url": "https://example.com",
                                "posted": (today - dt.timedelta(days=i)).isoformat() + "T10:00:00", "filing_type": "Q",
                                "first_seen": iso()})
    upsert(db, "committees", {"id": "C001", "name": "LEADING THE FUTURE", "entity": "leading-the-future", "query": "x",
                              "committee_type": "Super PAC", "receipts": 1e8, "disbursements": 2e7,
                              "independent_expenditures": 2.4e7, "cycle": 2026, "updated": iso()})
    upsert(db, "fec", {"id": "a1", "kind": "receipt", "committee_id": "C001", "committee_name": "LEADING THE FUTURE",
                       "counterparty": "ANDREESSEN HOROWITZ", "counterparty_key": name_key("ANDREESSEN HOROWITZ"),
                       "amount": 2.5e7, "date": today.isoformat(), "description": "", "support_oppose": None,
                       "candidate": None, "url": "https://example.com", "first_seen": iso()})
    for i in range(700):
        day = (today - dt.timedelta(days=i)).isoformat()
        db.execute("INSERT OR REPLACE INTO series VALUES(?,?,?)", ("wiki:loss-of-control", day, 1000 + i))
        if i < 60:
            db.execute("INSERT OR REPLACE INTO series VALUES(?,?,?)", ("news:deepfakes", day, 300 - i))
    for i in range(12):
        upsert(db, "articles", {"id": f"a{i}", "fear": "deepfakes", "title": f"Deepfake scam wave hits voters in state {i}",
                                "url": "https://example.com", "domain": "example.com",
                                "seen": (today - dt.timedelta(days=1)).isoformat() + "T10:00:00", "entity": None})
    db.execute("INSERT INTO status VALUES('congress',?,1,5,'ok',?)", (iso(), iso()))
    db.commit()
    data = export.export(db, tmp)
    assert data["index"]["value"] != "0", data["index"]
    # The plate goes out on X while the page it describes is one click away. They are counted by
    # two different pieces of code, so the numbers are held against each other here.
    from pipeline.brief import totals as plate_totals  # noqa: E402
    plate = plate_totals(db)
    chain = {label: value for value, label in data["index"]["chain"]}
    for word, key in (("bills, rules and orders", "measures"),
                      ("new government control", "controlled"),
                      ("handed new power", "offices")):
        label = next((l for l in chain if word in l), None)
        assert label, f"the front page stopped saying {word!r}; the plate still counts it"
        assert chain[label] == f"{plate[key]:,}", \
            f"the plate says {plate[key]:,} and the page says {chain[label]} for {word!r}"
    # The plate counts offices in two places of its own, and they drifted apart once already.
    from pipeline.brief import patterns as plate_patterns  # noqa: E402
    lines = plate_patterns(db, dt.date.today().isoformat(), ())
    offices = next((l for l in lines if l["key"].startswith("offices:")), None)
    assert offices, "the plate stopped counting offices"
    assert offices["key"] == f"offices:{plate['offices']}", \
        f"the plate's office line says {offices['key']} and its own total says {plate['offices']}"
    assert data["fears"] and data["controls"]
    assert data["funders"], "advocacy lobbying should rank separately"
    assert data["industry"], "company and trade group lobbying should rank separately"
    assert not ({f["name"] for f in data["funders"]} & {i["name"] for i in data["industry"]}), \
        "an organization must appear in one ranking or the other, never both"
    site = ROOT / "site" / "build.py"
    subprocess.run([sys.executable, str(site), "--data", str(tmp / "site_data.json"), "--out", str(tmp / "dist"),
                    "--base", ""], check=True)
    subprocess.run([sys.executable, str(site), "--data", str(tmp / "site_data.json"), "--preview", str(tmp / "p.html")],
                   check=True)
    pages = sorted(str(p.relative_to(tmp / "dist")) for p in (tmp / "dist").rglob("index.html"))
    print("pages:", len(pages), pages[:6])
    print("index:", json.dumps(data["index"])[:200])
    print("exhibit:", json.dumps(data["exhibit"])[:300])
    print("top fear:", data["fears"][0])
    print("top funder:", data["funders"][0]["name"], data["funders"][0]["amount"],
          "| top industry:", data["industry"][0]["name"], data["industry"][0]["amount"])
    print("OK", tmp)


if __name__ == "__main__":
    main()
