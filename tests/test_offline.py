"""Offline check: build a synthetic database, export it, and render every page.

Run with: python -m tests.test_offline
Nothing here touches the network or the real data.
"""
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import export  # noqa: E402
from pipeline.common import config, connect, iso, name_key, upsert  # noqa: E402


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
    body = "ohio would put every ai operator under the ohio department of commerce"
    good = "Ohio would put every AI operator under the Ohio Department of Commerce, which decides who may run one."
    assert brief.why_not(good, "would put every ai operator under the ohio department", body, False,
                         row["office"]) == "", "a sound pending sentence was rejected"
    assert brief.why_not(good, "would put every ai operator under the ohio department", body, True,
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
    # Duties a measure puts on an office are the office's job, not a power to impose duties on others.
    pa_raw = ("An Act providing for artificial intelligence risk prevention; establishing standards for frontier "
              "developers in addressing critical safety incidents and catastrophic risks; imposing duties on the "
              "Pennsylvania Emergency Management Agency and the Attorney General; and imposing penalties.")
    pa_bad = ("Pennsylvania would give the Pennsylvania Emergency Management Agency and the Attorney General the "
              "power to impose duties on frontier developers over critical incidents and catastrophic risks.")
    pa_body = brief.norm(pa_raw)
    assert "duties" in brief.why_not(pa_bad, "imposing duties on the Pennsylvania Emergency Management Agency",
                                     pa_body, False, "", "", "Pennsylvania", raw=pa_raw), "the duties gate is not wired in"
    assert "Duties a measure puts on an office" in brief.RULES
    # A power the text does not state is refused; one it states, by verb or by object, is kept.
    pa_v2 = ("Pennsylvania would give the Pennsylvania Emergency Management Agency the power to demand reports of "
             "critical incidents and catastrophic risks from frontier developers.")
    assert "does not say" in brief.ungrounded_power(pa_v2, pa_raw)
    nj = "New Jersey gives the BPU the power to demand semi-annual water and energy usage reports from data centers."
    assert brief.ungrounded_power(nj, "requires semi-annual reports on water and energy usage by data centers") == ""
    ca = ("California gives the Attorney General the power to, for cause, request and obtain a copy of an audit "
          "report from the operator of a companion chatbot.")
    assert brief.ungrounded_power(ca, "the Attorney General may request an audit report for cause") == ""
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
    # Dating a measure is not explaining it, and a sentence opening on its year keeps its tense after it
    assert not brief.EXPLAINING.search("California puts companion chatbot operators under independent "
                                       "auditors whose reports the Attorney General can demand, beginning 2027.")
    assert brief.EXPLAINING.search("California puts chatbots under audits, reflecting a national trend.")
    assert brief.CONDITIONAL.search(brief.tense_of("From 2027, California would put companion chatbots under "
                                                   "audits reported to the Attorney General.", "California"))
    assert brief.CONDITIONAL.search(brief.tense_of("Beginning January 1, 2028, Texas would put data centers "
                                                   "under a permit.", "Texas"))
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
    candidates = config("fear_candidates")
    assert not {f["slug"] for f in fears} & {c["slug"] for c in candidates}, "a fear is both followed and a candidate"
    fears_only = fears
    fears = fears + candidates  # a candidate is written the same way, so it can move onto the list whole
    keys = set(fears[0])
    assert keys >= {"slug", "name", "sentence", "short", "definition", "keywords", "gdelt",
                    "match", "wikipedia", "because"}, f"the first fear is missing fields: {keys}"
    seen = set()
    for f in fears:
        assert set(f) == keys, f"{f.get('slug')} has fields {set(f) ^ keys} the others do not"
        assert f["slug"] not in seen, f"two fears called {f['slug']}"
        seen.add(f["slug"])
        for field in ("name", "sentence", "short", "definition", "gdelt", "because"):
            assert isinstance(f[field], str) and f[field].strip(), f"{f['slug']} has an empty {field}"
        # "sentence" is the name dropped mid-sentence, after "cite", so it carries no heading
        # capital. Same allowlist as "because" below: only a proper noun keeps one.
        s = f["sentence"]
        # It has to be the same name, not a second one: an article in front is the only licence,
        # for the fears whose name will not take a bare "cite" in front of it.
        bare = s[4:] if s.lower().startswith("the ") else s
        assert bare.lower() == f["name"].lower(), f"{f['slug']}: sentence is not the name, it is {s!r}"
        assert s[0] == s[0].lower() or s.split()[0] in ("AI", "China", "OpenAI", "Congress"), \
            f"{f['slug']}: sentence starts with a capital that is not a proper noun"
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
    print(f"{len(fears_only)} fears and {len(candidates)} candidates, all well formed: ok")


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
        # Tags, expressions and Jinja comments all come out first. A {# #} comment reaches no
        # reader, and the note explaining why a word is banned has to be able to name the word.
        # It flagged its own explanation the first time, which is the check working and the strip
        # being one character short.
        text = _re.sub(r"\{[%{#].*?[#%}]\}", " ", t.read_text(), flags=_re.S)
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

    def add(mid, kind, code, name, ident):
        upsert(db, "measures", {"id": mid, "kind": kind, "jurisdiction": code, "jurisdiction_name": name,
                                "session": "", "identifier": ident, "title": f"t {mid}", "summary": "s",
                                "status": "passed", "url": f"u{mid}", "introduced_date": day, "source": "test",
                                "first_seen": iso()})
        db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, "control", "c", "q", "t", iso()))
        db.commit()
    add("m4", "order", "us-exec", "Executive Office of the President", "EO 1")
    n, word = brief.places(db, "control", "c")
    assert (n, word) == (4, "jurisdiction"), \
        f"an executive order counted as a state: the plate would have said {n} {word}s"
    # The federal government is one place however many offices issued its measures. Counting names
    # made it one place per agency: Congress, the President and the FDA were three jurisdictions.
    add("m5", "rule", "us-exec", "Health and Human Services Department, Food and Drug Administration", "FR 1")
    add("m6", "bill", "us", "Congress", "H.R. 1")
    n, word = brief.places(db, "control", "c")
    assert (n, word) == (4, "jurisdiction"), f"the federal government counted as {n - 3} places"
    print("an executive order is not a state, and the federal government is one place: ok")

    # Puerto Rico is not a state, on the plate or on the page
    db2 = _connect(pathlib.Path(_tempfile.mkdtemp()) / "places2.db")
    for mid, code, name in [("p1", "ca", "California"), ("p2", "pr", "Puerto Rico")]:
        upsert(db2, "measures", {"id": mid, "kind": "bill", "jurisdiction": code, "jurisdiction_name": name,
                                 "session": "2025", "identifier": "SB 1", "title": "t", "summary": "s",
                                 "status": "pending", "url": f"u{mid}", "introduced_date": day, "source": "test",
                                 "first_seen": iso()})
        db2.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        db2.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, "control", "c", "q", "t", iso()))
    db2.commit()
    assert brief.places(db2, "control", "c") == (2, "jurisdiction"), "Puerto Rico was called a state"
    from pipeline.export import where_phrase
    assert where_phrase({"ca", "ny", "pr", "us", "us-exec"}) == "2 states, Puerto Rico and the federal government"
    assert where_phrase({"ca", "us"}) == "1 state and Congress"
    assert where_phrase({"us-exec"}) == "the federal government"
    print("Puerto Rico is named, not counted as a state: ok")


def check_headline_keeps_the_power():
    """A headline that drops the body reads the measure backwards.

    The morning of 20 September the plate said "California would exempt data center projects from
    environmental review". The sentence it came from said the projects go under the Governor's
    authority to certify them as exempt. One office deciding who is exempt is a power; the plate
    said a rule had gone away, which is the opposite of what this report is for. Everything else
    guarding the headline stops it saying something untrue. This stops it saying the true thing
    backwards.
    """
    from pipeline import brief
    real = brief.call
    lead = {"sentence": "California would put data center projects under the Governor's authority "
                        "to certify them as environmental leadership development projects exempt "
                        "from standard environmental review.",
            "office": "California Office of Land Use and Climate Innovation",
            "jurisdiction": "California", "controls": "data-center-limits", "head": ""}
    names = {c["slug"]: c for c in config("controls")}
    assert brief.power_holder(lead["sentence"]) == {"governor"}, brief.power_holder(lead["sentence"])
    try:
        said = []

        def two_tries(key, system, prompt, max_tokens=0):
            said.append(prompt)
            return {"line": ["California would exempt data center projects from environmental review",
                             "California would let the Governor exempt data center projects"][len(said) - 1]}

        brief.call = two_tries
        got = brief.headline("k", [lead], names, {})
        assert "governor" in got.lower(), f"the plate dropped the body again: {got}"
        assert len(said) == 2, "the headline was not asked again after it dropped the body"

        # and when it will not keep the body, the plate falls back to the sentence's own words
        def never(key, system, prompt, max_tokens=0):
            return {"line": "California would exempt data center projects from review"}

        brief.call = never
        got = brief.headline("k", [lead], names, {})
        assert got == "California would put data center projects under the Governor's authority", got

        # a sentence that names no body is asked once, as before
        plainly = dict(lead, sentence="Texas would require an AI company to label synthetic media.",
                       jurisdiction="Texas", controls="labeling-mandates")
        asked = []

        def once(key, system, prompt, max_tokens=0):
            asked.append(1)
            return {"line": "Texas would require labels on synthetic media"}

        brief.call = once
        assert brief.headline("k", [plainly], names, {}) == "Texas would require labels on synthetic media"
        assert len(asked) == 1, "a sentence with no body was asked twice"
    finally:
        brief.call = real
    print("the plate keeps who holds the power: ok")


def check_label_rules():
    """The quote check refuses what a reader would refuse.

    A recital of the law already in force, a legislature named as an agency, a state bill carrying
    "state laws overridden by Washington", an agency's report to its own legislature called a
    company's duty to report, and a quote that only matches half a word.
    """
    from pipeline.tag import agreed, norm
    raw = ("Existing law requires the Department of Technology to conduct an inventory of automated decision "
           "systems. This bill would require a developer to register a frontier model with the Attorney General "
           "and would require the Department of Technology to submit a report to the Legislature. It would "
           "prohibit the use of drones and aircraft for surveillance, and would preempt any ordinance adopted "
           "by a city or county on the same subject.")
    body = norm(raw)

    def ok(kind, label, quote, code="ca", jur="California"):
        return bool(agreed({kind: {label: quote}}, body, kind, jur, raw=raw, code=code))
    assert not ok("controls", "mandatory-reporting", "requires the Department of Technology to conduct an inventory"), \
        "a label rested on the law already in force"
    assert ok("controls", "license-to-build", "require a developer to register a frontier model")
    assert not ok("controls", "mandatory-reporting", "require the Department of Technology to submit a report to the Legislature"), \
        "an agency reporting to its legislature was counted as a company's duty to report"
    assert not ok("controls", "preemption", "would preempt any ordinance adopted by a city or county"), \
        "a state bill carried the control named for Washington overriding the states"
    assert ok("controls", "preemption", "would preempt any ordinance adopted by a city or county", code="us")
    assert not ok("controls", "labeling-mandates", "prohibit the use of drones and ai"), "half a word matched"
    assert not ok("agencies", "Kansas Legislature (via task force)", "the Attorney General"), "a legislature counted as an agency"
    assert ok("agencies", "California Attorney General", "the Attorney General")
    print("the quote check refuses recitals, legislatures, half words and misplaced controls: ok")


def check_third_reading():
    """A label that passed the quote check can still say the opposite of what the measure does.

    The third reading asks one question per control and office label. A no takes the label off and
    is remembered against its quote; a label not yet checked cannot lead the post; and a key that
    cannot use the stronger model falls back to the next one instead of checking nothing.
    """
    from pipeline import brief, check
    from pipeline.common import connect as _connect
    import tempfile as _tempfile
    db = _connect(pathlib.Path(_tempfile.mkdtemp()) / "check.db")
    day = dt.date.today().isoformat()
    for mid, title, controls, office in (
            ("c1", "Sandbox for AI testing under waived rules", ["license-to-build"], "Federal Reserve Board"),
            ("c2", "Frontier model licensing", ["license-to-build"], "State AI Licensing Office")):
        upsert(db, "measures", {"id": mid, "kind": "bill", "jurisdiction": "ca", "jurisdiction_name": "California",
                                "session": "2025", "identifier": f"SB {mid}", "title": title, "summary": title,
                                "status": "pending", "url": f"u{mid}", "introduced_date": day,
                                "latest_action_date": day, "source": "test", "first_seen": iso()})
        db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        for c in controls:
            db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, "control", c, title, "t", iso()))
        db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, "agency", office, title, "t", iso()))
    db.commit()
    assert not brief.pool(db, day), "a measure led the post on labels nobody had checked"
    asked = []

    def fake(key, prompt):
        asked.append(prompt)
        no = "waived rules" in prompt
        return {"verdict": "no" if no else "yes", "reason": "a sandbox" if no else "it licenses"}, "fake-model"
    line = check.run(db, "k", ask_fn=fake)
    assert len(asked) == 4, f"expected four questions, asked {len(asked)}"
    left = {(r["target"], r["kind"]) for r in db.execute("SELECT target, kind FROM tags")}
    assert ("c1", "control") not in left and ("c1", "agency") not in left, "a refused label stayed on"
    assert ("c2", "control") in left and ("c2", "agency") in left, "a confirmed label was taken off"
    lead = brief.pool(db, day)
    assert [r["id"] for r in lead] == ["c2"] and lead[0]["office"] == "State AI Licensing Office", \
        "only the confirmed measure, with its confirmed office, should be able to lead"
    # Read again and landing on the same quote, the refused label goes without a second question
    db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", ("c1", "control", "license-to-build",
                                                        "Sandbox for AI testing under waived rules", "t", iso()))
    db.commit()
    asked.clear()
    check.run(db, "k", ask_fn=fake)
    assert not asked and not db.execute("SELECT 1 FROM tags WHERE target='c1' AND kind='control'").fetchone(), \
        "a label refused on this quote came back"
    assert "took off" in line

    # A key that cannot use the stronger model gets the next one, not no check at all
    class Reply:
        def __init__(self, code, text):
            self.status_code, self.text = code, text

        def json(self):
            return {"content": [{"type": "text", "text": self.text}]}
    calls = []

    def post(url, **kw):
        calls.append(kw["json"]["model"])
        if kw["json"]["model"] == check.MODELS[0]:
            return Reply(404, '{"type":"error","error":{"type":"not_found_error","message":"model: x"}}')
        return Reply(200, '{"verdict": "yes", "reason": "fine"}')
    real, check.requests.post = check.requests.post, post
    check._model["i"] = 0
    try:
        answer, model = check.ask("k", "question")
    finally:
        check.requests.post, check._model["i"] = real, 0
    assert answer["verdict"] == "yes" and model == check.MODELS[1] and calls == check.MODELS[:2], calls
    print("a third reading takes off what the measure does not do, and only checked labels lead: ok")

    # Fears are read too, on measures and on organizations' statements, and a label refused under a
    # definition since widened is put back once to be asked again
    db3 = _connect(pathlib.Path(_tempfile.mkdtemp()) / "fears.db")
    upsert(db3, "measures", {"id": "f1", "kind": "bill", "jurisdiction": "tx", "jurisdiction_name": "Texas",
                             "session": "2025", "identifier": "HB 1", "title": "AI workforce training grants",
                             "summary": "Creates grants to train workers in AI.", "status": "pending", "url": "uf1",
                             "introduced_date": day, "latest_action_date": day, "source": "test", "first_seen": iso()})
    db3.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", ("f1", "h", 1, iso()))
    db3.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", ("f1", "fear", "job-loss", "train workers in AI", "t", iso()))
    upsert(db3, "posts", {"id": "p1", "entity": "fli", "title": "Superintelligence could escape control",
                          "summary": "We warn that superintelligent AI could escape human control.",
                          "url": "https://futureoflife.org/x", "published": iso(), "feed": "f", "first_seen": iso()})
    db3.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", ("post:p1", "h", 1, iso()))
    db3.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", ("post:p1", "fear", "loss-of-control",
                                                         "superintelligent AI could escape human control", "t", iso()))
    db3.execute("INSERT INTO checks VALUES(?,?,?,?,?,?,?,?)", ("f1", "control", "labeling-mandates",
                                                               "tell applicants AI is used", "no", "narrow", "m", iso()))
    db3.commit()
    seen = []

    def reads(key, prompt):
        seen.append(prompt)
        no = "train workers in AI" in prompt
        return {"verdict": "no" if no else "yes", "reason": "a training grant" if no else "stated"}, "fake-model"
    check.run(db3, "k", ask_fn=reads)
    kinds = {r["target"] + ":" + r["kind"] for r in db3.execute("SELECT target, kind FROM tags")}
    assert "f1:fear" not in kinds and "post:p1:fear" in kinds, f"fears were not read: {kinds}"
    assert any(p.startswith("STATEMENT") and "Future of Life Institute" in p for p in seen), \
        "a statement was not read as a statement from its organization"
    assert any("tell applicants AI is used" in p for p in seen), "a label refused under the old wording was not asked again"
    print("fears on measures and statements are read, and widened labels are asked again: ok")

    # A run that refuses far more than the hand check ever found holds its refusals for a person
    db2 = _connect(pathlib.Path(_tempfile.mkdtemp()) / "hold.db")
    for i in range(70):
        mid = f"h{i}"
        upsert(db2, "measures", {"id": mid, "kind": "bill", "jurisdiction": "ny", "jurisdiction_name": "New York",
                                 "session": "2025", "identifier": f"A {i}", "title": f"AI bill {i}", "summary": "s",
                                 "status": "pending", "url": f"h{mid}", "introduced_date": day, "source": "test",
                                 "first_seen": iso()})
        db2.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        db2.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, "control", "labeling-mandates", f"q{i}", "t", iso()))
    db2.commit()
    check.run(db2, "k", ask_fn=lambda key, prompt: ({"verdict": "no", "reason": "misread"}, "fake-model"))
    assert db2.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 70, "a wave of refusals took the labels down"
    assert db2.execute("SELECT COUNT(*) FROM checks WHERE verdict='hold'").fetchone()[0] == 70
    from pipeline import review
    body, items = review.build(db2, dt.date.today())
    assert "Held: 70 refusals" in body and items >= 70, "the held refusals are not on the weekly list"
    from pipeline.common import kv_set
    kv_set(db2, "blindspots:report", {"at": "2026-09-24T06:00:00+00:00", "uncovered_measures": 428,
                                      "uncovered": [{"phrase": "health care", "measures": 17, "places": 8}]})
    body, _ = review.build(db2, dt.date.today())
    assert "Subjects none of the fears covers" in body and "| health care | 17 | 8 |" in body, \
        "what the fears leave out does not reach the weekly list"
    db2.execute("DELETE FROM kv WHERE key LIKE 'check:released:%'")
    assert check.release_holds(db2) == 70 and not db2.execute("SELECT COUNT(*) FROM tags").fetchone()[0], \
        "released refusals did not come off"
    assert check.release_holds(db2) == 0, "a release ran twice"
    print("a wave of refusals is held for a person, listed, and applied once released: ok")

    # An executive order the Register marks as revoked comes off the record
    from pipeline import collect_fedreg

    class FakeHttp:
        def json(self, url, params=None, tries=5):
            return {"executive_order_notes": "See: EO 14028\r\nRevoked by: EO 14318, July 23, 2025"} \
                if "2025-01395" in url else {"executive_order_notes": "Revokes: EO 14141, January 14, 2025"}
    for mid, ident in (("fr-2025-01395", "Executive Order 14141"), ("fr-2025-14212", "Executive Order 14318")):
        upsert(db2, "measures", {"id": mid, "kind": "order", "jurisdiction": "us-exec", "jurisdiction_name": "EOP",
                                 "session": "", "identifier": ident, "title": "AI", "summary": "", "status": "passed",
                                 "url": f"u{mid}", "introduced_date": day, "source": "test", "first_seen": iso()})
    db2.commit()
    gone = collect_fedreg.revoked_orders(db2, FakeHttp())
    left = {r["id"] for r in db2.execute("SELECT id FROM measures WHERE kind='order'")}
    assert gone == ["Executive Order 14141"] and left == {"fr-2025-14212"}, (gone, left)
    print("a revoked executive order comes off, and the order revoking it stays: ok")


def check_second_attempt():
    """A sentence refused for one fixable fault is written once more with the fault named.

    The launch edition lost its three best leads this way: the sponsor's "child safety", three
    characters over the limit, a would on a measure that had passed.
    """
    from pipeline import brief
    row = {"id": "m1", "jurisdiction_name": "California", "identifier": "SB 1119", "status": "pending",
           "title": "Companion chatbots.", "office": "California Attorney General", "controls": "mandatory-reporting",
           "fears": "", "url": "u", "summary": "This bill would require an operator to submit to independent audits "
                                             "and to make the audit reports available to the Attorney General."}
    said = []

    def fake(key, system, user, max_tokens=400):
        said.append(user)
        if "was refused" not in user:
            return {"sentence": "California would put companion chatbots under independent child safety audits "
                                "reported to the Attorney General.",
                    "quote": "require an operator to submit to independent audits"}
        return {"sentence": "California would give the Attorney General the power to demand the audit reports "
                            "of companion chatbot operators.",
                "quote": "make the audit reports available to the Attorney General"}
    real, brief.call = brief.call, fake
    try:
        attempts = []
        lines = brief.write_lines("k", [row], dt.date.today().isoformat(), 1, attempts)
    finally:
        brief.call = real
    assert len(said) == 2 and "child safety" in said[1], "the second attempt was not told what was wrong"
    assert lines and "child safety" not in lines[0]["sentence"], f"the fixed sentence was not used: {attempts}"
    print("a refused sentence gets one more attempt with its fault named: ok")
    assert brief.unpowered_body("New Jersey would put approvals under a moratorium and a commission's review.", [])
    assert not brief.unpowered_body("South Carolina would put data centers under a Public Service Commission certificate.",
                                    ["South Carolina Public Service Commission"])
    print("a sentence names a commission only when it holds a confirmed power: ok")


def check_power_is_the_office():
    """The office gaining the power is who the lead is about, never a firm a company hires.

    The launch edition led with "California puts companion chatbots under independent auditors
    whose reports the Attorney General can demand", and the plate said "California puts companion
    chatbots under independent auditors". The power in that law is the Attorney General's. A reader
    saw a rule protecting children from chatbots, and no office gaining anything. The rules had
    asked for exactly that shape, by example.
    """
    from pipeline import brief
    assert "an auditor whose report" not in brief.RULES, "the rules still ask for the auditor shape"
    assert "firm a company has to hire" in brief.RULES, "the rules no longer say a hired firm holds nothing"
    summary = ("This bill would require an operator to submit to independent audits of its compliance. The "
               "bill would authorize the Attorney General to, for cause, request and obtain a copy of an "
               "audit report from the operator, beginning July 1, 2027.")
    body = brief.norm(f"Companion chatbots.\n{summary}")
    launch = ("California puts companion chatbots under independent auditors whose reports the Attorney "
              "General can demand, beginning 2027.")
    why = brief.why_not(launch, "require an operator to submit to independent audits", body, True, "", "2027",
                        "California", raw=f"Companion chatbots.\n{summary}", offices=[])
    assert "Attorney General" in why and "open with" in why, f"the auditor shape was not refused: {why!r}"
    office = ("California gives the Attorney General the power to demand, for cause, the audit reports of "
              "companion chatbot operators, beginning 2027.")
    assert brief.why_not(office, "authorize the Attorney General to, for cause, request and obtain a copy",
                         body, True, "", "2027", "California", raw=f"Companion chatbots.\n{summary}",
                         offices=[]) == "", "a sentence leading with the office was refused"
    for text, firm in (
            (launch, "auditors"),
            ("California puts companion chatbots under independent child safety audits reported to the "
             "Attorney General.", "independent child safety audits"),
            ("California requires every companion chatbot to be certified by an auditor the Attorney "
             "General approves.", "auditor"),
            ("Illinois would put hiring tools under a bias audit by an independent auditor, which the "
             "Department of Labor could review.", "auditor"),
            (office, ""),
            ("California gives the Attorney General the power to demand the reports of independent "
             "auditors.", ""),
            ("Ohio would put every operator under the State Auditor, who could demand its records.", ""),
            ("New York would put frontier developers under an independent AI safety office that decides "
             "who may release a model.", ""),
            ("California requires operators to certify compliance under penalty of perjury.", ""),
            ("California would bar users under 18 from companion chatbots unless an independent auditor "
             "certifies them.", ""),
            ("California puts AI auditors under registration with the Government Operations Agency, "
             "which licenses them.", "")):
        assert brief.private_holder(text) == firm, f"{text[:70]!r}: {brief.private_holder(text)!r}, wanted {firm!r}"

    # through write_lines: the auditor shape is refused, and told which office to lead with
    row = {"id": "m1", "jurisdiction_name": "California", "identifier": "SB 1119", "status": "passed",
           "title": "Companion chatbots.", "office": "", "offices": "", "controls": "mandatory-reporting",
           "fears": "", "url": "u", "summary": summary}
    said = []

    def fake(key, system, user, max_tokens=400):
        said.append(user)
        if "was refused" not in user:
            return {"sentence": launch, "quote": "require an operator to submit to independent audits"}
        return {"sentence": office, "quote": "authorize the Attorney General to, for cause, request and obtain a copy"}
    real, brief.call = brief.call, fake
    try:
        lines = brief.write_lines("k", [row], "2026-09-21", 1, [])
    finally:
        brief.call = real
    assert len(said) == 2 and "open with what the Attorney General can do" in said[1], \
        "the second attempt was not told which office to lead with"
    assert lines and lines[0]["sentence"] == office, f"the office-first sentence was not used: {lines}"

    # and the plate keeps the office, even where the sentence says it without a power phrase
    lets = ("California lets the Attorney General demand, for cause, the audit reports of companion "
            "chatbot operators, beginning 2027.")
    assert brief.holder_words(lets) == {"attorney", "general"}, brief.holder_words(lets)
    assert brief.holder_words(launch) == {"attorney", "general"}, "the plate would keep the auditors"
    heads = ["California puts companion chatbots under independent auditors",
             "California lets its Attorney General demand chatbot audit reports"]
    asked = []

    def two(key, system, prompt, max_tokens=0):
        asked.append(prompt)
        return {"line": heads[len(asked) - 1]}
    brief.call = two
    try:
        got = brief.headline("k", [{"sentence": lets, "office": "", "jurisdiction": "California",
                                    "controls": "mandatory-reporting", "head": ""}],
                             {c["slug"]: c for c in config("controls")}, {})
    finally:
        brief.call = real
    assert got == heads[1] and len(asked) == 2, f"the plate dropped the office: {got!r}"
    assert brief.power_clause("Ohio would put every data center under the state regulators, who decide.") \
        .endswith("regulators"), "the fallback headline stops inside a word"
    print("the lead is about the office gaining the power, not a firm a company hires: ok")


def check_brief_attempts():
    """What the second run of the launch edition got wrong, one check each.

    The smaller model dropped "child safety" and then tucked the power into a clause on the end
    ("..., giving the Attorney General power to demand audit reports for cause"), was told only that
    it "explains rather than reports", and the measure was dropped. The edition then led with New
    York legislators who "would be required to" disclose AI-drafted remarks, a duty in the passive
    that the duty check did not recognise.
    """
    from pipeline import brief, tag
    tucked = ("California requires an operator of a companion chatbot, beginning July 1, 2027, to submit to "
              "independent audits, giving the Attorney General power to demand audit reports for cause.")
    summary = ("This bill would require an operator to submit to independent audits of its compliance. The bill "
               "would authorize the Attorney General to, for cause, request and obtain a copy of an audit "
               "report from the operator, beginning July 1, 2027.")
    raw = f"Companion chatbots.\n{summary}"
    why = brief.why_not(tucked, "require an operator to submit to independent audits", brief.norm(raw), True,
                        "", "2027", "California", raw=raw, offices=[])
    assert "clause on the end" in why and "giving the Attorney General" in why, why
    assert brief.why_not("California puts chatbots under audits, reflecting a national trend.", "x", "x",
                         True) == "explains rather than reports"
    passive = ("New York members of the legislature would be required to verbally disclose before remarks "
               "entered into the official record whether such remarks were drafted by artificial intelligence.")
    assert brief.DUTY_OPENER.search(brief.tense_of(passive, "New York")), "a passive duty was read as a power"
    for fine in ("California would bar chatbots directed at children from simulating romance.",
                 "California would bar a release without the required risk assessment."):
        assert not brief.DUTY_OPENER.search(brief.tense_of(fine, "California")), fine
    # an office the measure never names is refused, whatever the sentence around it
    assert brief.borrowed_office("Texas gives the Attorney General the power to demand the forecasts.",
                                 "This bill would require a utility to file forecasts with the Public Utility "
                                 "Commission.", "Texas") == "Attorney General"
    assert brief.borrowed_office("Texas puts every forecast under the Public Utilities Commission.",
                                 "file forecasts with the Public Utility Commission", "Texas") == ""

    # three attempts, the last told both faults, and the third one used
    row = {"id": "m1", "jurisdiction_name": "California", "identifier": "SB 1119", "status": "passed",
           "title": "Companion chatbots.", "office": "", "offices": "", "controls": "mandatory-reporting",
           "fears": "", "url": "u", "summary": summary}
    answers = [("California requires companion chatbot operators from July 1, 2027 to submit to independent "
                "child safety audits, giving the Attorney General power to demand audit reports for cause.",
                "require an operator to submit to independent audits"),
               (tucked, "require an operator to submit to independent audits"),
               ("California gives the Attorney General the power to demand, for cause, the audit reports of "
                "companion chatbot operators, beginning 2027.",
                "authorize the Attorney General to, for cause, request and obtain a copy")]
    said = []

    def fake(key, system, user, max_tokens=400):
        said.append(user)
        s, q = answers[len(said) - 1]
        return {"sentence": s, "quote": q}
    real, brief.call = brief.call, fake
    try:
        attempts = []
        lines = brief.write_lines("k", [row], "2026-09-21", 1, attempts)
    finally:
        brief.call = real
    assert len(said) == 3, f"asked {len(said)} times"
    assert "child safety" in said[2] and "clause on the end" in said[2], "the third attempt was not told both faults"
    assert lines and lines[0]["sentence"] == answers[2][0], attempts

    # the stronger model first, and the smaller one when the key cannot use it
    used = []

    def api(key, system, user, max_tokens=700, model=None):
        used.append(model)
        if model == brief.MODELS[0]:
            raise RuntimeError('Claude API 404: {"type":"error","error":{"type":"not_found_error","message":"model"}}')
        return {"sentence": "ok"}
    real_tag, tag.call = tag.call, api
    brief._model.update(i=0, unparsed=0)
    try:
        assert brief.call("k", "s", "u") == {"sentence": "ok"}
        assert brief.call("k", "s", "u") == {"sentence": "ok"}
    finally:
        tag.call = real_tag
        brief._model.update(i=0, unparsed=0)
    assert used == [brief.MODELS[0], brief.MODELS[1], brief.MODELS[1]], used

    # An answer with no JSON goes to the smaller model, and after two the smaller one takes the rest.
    # The first run on the stronger model lost seven candidates in a row to this.
    used, budgets = [], []

    def unparsed(key, system, user, max_tokens=700, model=None):
        used.append(model)
        budgets.append(max_tokens)
        assert user.endswith("starting with {."), "the reminder to answer in JSON is missing"
        if model == brief.MODELS[0]:
            raise ValueError("no JSON in reply")
        return {"line": "ok"}
    tag.call = unparsed
    try:
        for _ in range(3):
            assert brief.call("k", "s", "u", max_tokens=150) == {"line": "ok"}
    finally:
        tag.call = real_tag
        brief._model.update(i=0, unparsed=0)
    S, H = brief.MODELS
    assert used == [S, H, S, H, H], used
    assert min(budgets) >= 1000, f"the answer is still given {min(budgets)} tokens"

    # A start date belongs to the part of the measure it is written into. The launch edition put
    # "beginning July 1, 2027", the date of the law's duties, on the Attorney General's power over
    # audit reports, which sits in a section of its own over audits due by 2029.
    two_parts = ("This bill would require an operator to, beginning July 1, 2027, before making a new "
                 "companion chatbot available to users in the state, perform and document a risk assessment. "
                 "The bill would authorize the Attorney General to, for cause, request and obtain a copy of "
                 "an audit report from the operator.")
    dated = dict(row, summary=two_parts)
    power_q = "authorize the Attorney General to, for cause, request and obtain a copy"
    duty_q = "before making a new companion chatbot available to users in the state"
    assert "Attorney General" in brief.dated_part(f"Companion chatbots.\n{two_parts}", power_q)
    for answers, want, reason in (
            ([("California gives the Attorney General the power to request and obtain, for cause, a companion "
               "chatbot operator's audit report, beginning July 1, 2027.", power_q),
              ("California gives the Attorney General the power to request and obtain, for cause, a companion "
               "chatbot operator's audit report.", power_q)], 1, "different part"),
            ([("California puts every new companion chatbot under a risk assessment the operator must document.",
               duty_q),
              ("From July 1, 2027, California puts every new companion chatbot under a risk assessment the "
               "operator must document.", duty_q)], 1, "starts in 2027")):
        said.clear()

        def fake(key, system, user, max_tokens=400, answers=answers):
            said.append(user)
            s, q = answers[len(said) - 1]
            return {"sentence": s, "quote": q}
        brief.call = fake
        try:
            attempts = []
            lines = brief.write_lines("k", [dated], "2026-09-21", 1, attempts)
        finally:
            brief.call = real
        assert reason in attempts[0]["reason"], attempts
        assert lines and lines[0]["sentence"] == answers[want][0], attempts
    print("the brief is written by the stronger model, told every fault, and reads a passive duty: ok")
    print("a start date stays with the part of the measure it belongs to: ok")

    # An operator of what. The fourth writing of the launch edition was true and never said.
    vague = "California gives the Attorney General the power to request and obtain a copy of an operator's audit report for cause."
    q = "authorize the Attorney General to, for cause, request and obtain a copy"
    assert brief.why_not(vague, q, brief.norm(raw), True, "", "", "California", raw=raw, offices=[]) == brief.UNSAID
    named = vague.replace("an operator's", "a companion chatbot operator's")
    assert brief.why_not(named, q, brief.norm(raw), True, "", "", "California", raw=raw, offices=[]) == ""
    elsewhere = "This bill would require a utility to file load forecasts with the commission."
    assert "words the measure uses" in brief.why_not(
        "Texas would give the commission the power to demand companion chatbot forecasts.",
        "require a utility to file load forecasts", brief.norm(elsewhere), False, "", "", "Texas",
        raw=elsewhere, offices=[]), "a borrowed AI word was accepted"
    heads = ["California Attorney General can request operator audit reports for cause",
             "California Attorney General can request companion chatbot audit reports"]
    asked = []

    def head(key, system, prompt, max_tokens=0):
        asked.append(prompt)
        return {"line": heads[len(asked) - 1]}
    brief.call = head
    try:
        got = brief.headline("k", [{"sentence": named, "office": "", "jurisdiction": "California",
                                    "controls": "mandatory-reporting", "head": ""}],
                             {c["slug"]: c for c in config("controls")}, {})
    finally:
        brief.call = real
    assert got == heads[1] and len(asked) == 2 and "what AI" in asked[1], (got, len(asked))
    assert brief.power_clause(named) == "", "the fallback plate would read 'California gives the Attorney General'"
    # A data center bill that never says AI is counted all the same, and neither the sentence nor its
    # headline may call its data centers AI.
    dc_raw = "Data centers; site assessment. Requires a locality to review the sound profile of a data center."
    dc_q = "Requires a locality to review the sound profile of a data center"
    added = "Virginia would give each locality the power to review the sound profile of an AI data center."
    assert brief.why_not(added, dc_q, brief.norm(dc_raw), False, "", "", "Virginia", raw=dc_raw,
                         offices=[]) == brief.AI_ADDED
    plain = added.replace("an AI data center", "a data center")
    assert brief.why_not(plain, dc_q, brief.norm(dc_raw), False, "", "", "Virginia", raw=dc_raw,
                         offices=[]) != brief.AI_ADDED
    heads = ["Virginia localities would review AI data center noise",
             "Virginia localities would review data center noise"]
    asked.clear()
    brief.call = head
    try:
        got = brief.headline("k", [{"sentence": plain, "office": "", "jurisdiction": "Virginia",
                                    "controls": "data-center-limits", "head": ""}],
                             {c["slug"]: c for c in config("controls")}, {})
    finally:
        brief.call = real
    assert got == heads[1] and len(asked) == 2 and "says AI where the sentence does not" in asked[1], (got, asked)
    print("a sentence and its plate say what AI the measure is about: ok")


def check_lobbying_money():
    """A year of lobbying is four quarters of reports, and a client's money is counted once.

    The window took in the quarter still running, which has a handful of early filings, so "past
    year" was three quarters and a stub. And a client lobbying in-house reports what it paid its
    outside firms among its own expenses, while each firm reports the same money as income: summing
    every filing counted it twice.
    """
    from pipeline.export import recent_quarters, reported
    assert sorted(recent_quarters(4, dt.date(2026, 9, 21))) == [(2025, 3), (2025, 4), (2026, 1), (2026, 2)], \
        "the lobbying year includes the quarter still running"
    assert (2026, 3) not in recent_quarters(4, dt.date(2026, 10, 5)), "a quarter counted before its reports are due"
    assert (2026, 3) in recent_quarters(4, dt.date(2026, 10, 21)), "a quarter left out after its reports were due"

    def filing(client, own, amount, quarter="Q1"):
        return {"client_key": client, "year": 2026, "quarter": quarter, "amount": amount, "self": own}
    rows = [filing("msft", True, 2_300_000), filing("msft", False, 90_000), filing("msft", False, 60_000),
            filing("ari", False, 50_000), filing("msft", False, 70_000, "Q2")]
    assert reported(rows) == 2_300_000 + 50_000 + 70_000, \
        f"outside firms' fees were added on top of the client's own report: {reported(rows):,}"
    print("a lobbying year is four quarters of reports, counted once: ok")


def check_lobbying_keywords():
    """A lobbying filing names a fear, or it does not. Runs of letters are not names.

    Matching a keyword anywhere in the text found "agi" inside imaging, managing and agile, which
    is where 168 of the 197 filings tagged with it came from; "labor" inside laboratory and
    collaboration; "teen" inside fifteen; "minor" inside minority. Separately, the words
    themselves were too broad: 2,265 of the 2,443 filings tagged with the fear of AI cyberattacks
    rested on the bare string "cyber", which is how a filing reading "Cloud technology;
    Cybersecurity; Encryption policy" came to name a fear of AI attacks.
    """
    from pipeline.common import fear_keywords, fears_mentioned
    from pipeline.candidates import keyword_patterns
    kw = fear_keywords()
    # A fear taken off the list is a candidate now, still measured with the same words, so the
    # lessons in its words are checked there.
    for c in config("fear_candidates"):
        kw.setdefault(c["slug"], keyword_patterns(c["keywords"]))

    def names(text, fear):
        return any(p.search(text) for p in kw[fear])
    for text, fear, want in (
            ("medical imaging and managing engagement", "loss-of-control", False),
            ("laboratory collaboration agreements", "job-loss", False),
            ("fifteen reports on minority business", "kids-chatbots", False),
            ("Cloud technology; Cybersecurity; Encryption policy", "ai-cyberattacks", False),
            ("FY 2026 Labor/HHS Appropriations", "job-loss", False),
            ("pandemic preparedness and drug pricing", "bioweapons", False),
            ("AGI and superintelligence risk", "loss-of-control", True),
            ("discriminatory automated decisions", "bias", True),
            ("ransomware and hacking of AI systems", "ai-cyberattacks", True),
            ("reskilling for the future of work", "job-loss", True),
            ("companion chatbots and child safety", "kids-chatbots", True),
            ("rent-setting software used by landlords", "algorithmic-pricing", True),
            ("patient access to medicines; artificial intelligence", "ai-care-decisions", False),
            ("AI in prior authorization and claim denials", "ai-care-decisions", True),
            ("intellectual property theft by China; AI", "copyright", False),
            ("foreign influence operations; AI", "misinformation", False)):
        got = names(text, fear)
        assert got == want, \
            f"{text!r} {'should' if want else 'should not'} name {fear}"
    # the words the evidence refused, kept out by name so they are not quietly restored
    refused = {"cyber", "labor", "jobs", "worker", "workforce", "national security", "dominance",
               "pandemic", "algorithmic", "high-risk", "civil rights", "child", "children",
               "youth", "minor", "critical infrastructure"}
    for f in config("fears") + config("fear_candidates"):
        bare = {k.lower().rstrip("*") for k in f["keywords"]}
        clash = bare & refused
        assert not clash, f"{f['slug']} took back a word the filings showed to be generic: {clash}"
    print("lobbying keywords name a fear rather than matching letters: ok")


def check_position_carries_no_control():
    """A measure that opposes a thing was being counted among the measures that do it.

    Kansas HR 6023 is titled "Opposing the federal preemption of state laws that regulate
    artificial intelligence" and carried the preemption control. So did a Pennsylvania resolution
    urging Congress to drop federal legislation. Two of the five measures the site said would
    override state AI law were measures against doing that. A title that asks, urges or objects
    imposes nothing, so it carries no control; it can still name the fear it is worried about.
    """
    from pipeline.common import connect as _connect, states_a_position
    import tempfile as _tempfile
    for title, want in (
            ("Opposing the federal preemption of state laws that regulate artificial intelligence.", True),
            ("A Resolution urging the United States Congress to suspend any and all efforts", True),
            ("REQUESTING THE HAWAII STATE COMMISSION TO ESTABLISH A WORKING GROUP", True),
            ("A resolution condemning and calling for the reversal of the decision", True),
            ("Artificial intelligence: auditors: registration.", False),
            ("Data Center Moratorium", False),
            ("Companion chatbots.", False)):
        assert states_a_position(title) is want, f"{title[:50]!r} judged wrong"

    room = pathlib.Path(_tempfile.mkdtemp())
    db = _connect(room / "pos.db")
    day = iso()[:10]
    for mid, title in (("m-against", "Opposing the federal preemption of state AI laws."),
                       ("m-acts", "Artificial intelligence: auditors: registration.")):
        upsert(db, "measures", {"id": mid, "kind": "resolution" if "against" in mid else "bill",
                                "jurisdiction": "ks", "jurisdiction_name": "Kansas", "session": "2025",
                                "identifier": "HR 1", "title": title, "summary": "s",
                                "status": "pending", "url": f"u{mid}", "introduced_date": day,
                                "source": "test", "first_seen": iso()})
        db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
        for kind, value in (("control", "preemption"), ("fear", "china-race")):
            db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, kind, value, "q", "t", iso()))
    db.commit()
    from pipeline.common import drop_position_controls
    db.execute("DELETE FROM kv WHERE key LIKE 'repair:position-controls%'")
    db.commit()
    assert drop_position_controls(db) == 1, "the repair did not find the measure that takes a position"
    left = {(r["target"], r["kind"]) for r in db.execute("SELECT target, kind FROM tags")}
    assert ("m-against", "control") not in left, "a measure that opposes a thing still carries it"
    assert ("m-against", "fear") in left, "the fear it names was thrown out with the control"
    assert ("m-acts", "control") in left, "a measure that actually does something lost its control"
    assert drop_position_controls(db) == 0, "the repair ran twice"
    print("a measure that only takes a position carries no control: ok")


def check_overdue_daily_source():
    """A daily source that has not succeeded today is due, flag or no flag.

    The pass sets one done flag for itself whatever happened inside it. On 20 September the FEC
    register threw a 404 at 07:21, the flag went up anyway, and the fix pushed at noon could not
    run until the next morning: twenty-two hours with the whole election register missing and the
    site saying so in red. Success is recorded per source, so that is what decides.
    """
    from pipeline import run as runner
    from pipeline.common import connect as _connect, kv_set as _kv_set
    import tempfile as _tempfile
    import datetime as _dt
    db = _connect(pathlib.Path(_tempfile.mkdtemp()) / "due.db")
    today = _dt.date.today().isoformat()
    for name, last_ok in (("congress", today + "T07:17:09+00:00"),
                          ("openstates", today + "T07:20:50+00:00"),
                          ("lda", today + "T07:21:18+00:00"),
                          ("wikipedia", today + "T07:21:46+00:00"),
                          ("fec", "2000-01-01T00:00:00+00:00")):
        db.execute("INSERT INTO status(source,last_run,ok,added,message,last_ok) "
                   "VALUES(?,?,?,?,?,?)", (name, iso(), 1, 0, "", last_ok))
    db.commit()
    assert runner.overdue(db, today) == ["fec"], runner.overdue(db, today)
    # one that keeps failing is held off until the hour it asked for, rather than every pass
    _kv_set(db, "retry:fec", (_dt.datetime.now(_dt.timezone.utc)
                              + _dt.timedelta(hours=3)).isoformat(timespec="seconds"))
    db.commit()
    assert runner.overdue(db, today) == [], "a source that asked to be left alone was run anyway"
    _kv_set(db, "retry:fec", "2000-01-01T00:00:00")
    db.commit()
    assert runner.overdue(db, today) == ["fec"], "the hour it asked for came and it was not run"
    print("a daily source that has not succeeded today is due: ok")


def check_mark_geometry():
    """The mark is a square with the letters centred in it, and the square sits on the word.

    The box was 354 by 315 on the avatar, a third more air at the sides than above, which is what
    made a mark that measured centred look uncentred. The avatar also carried its own copy of the
    geometry, so it could drift from the wordmark without anything noticing. Drawn here on a plain
    field and measured off the pixels.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "site"))
    from PIL import Image, ImageDraw
    import brand

    def box_of(im, *colours):
        a = im.load()
        w, h = im.size
        xs, ys = [], []
        for y in range(h):
            for x in range(w):
                if any(sum(abs(a[x, y][i] - c[i]) for i in range(3)) < 90 for c in colours):
                    xs.append(x); ys.append(y)
        return (min(xs), min(ys), max(xs), max(ys)) if xs else None

    PAPER, VERM = (241, 237, 227), (179, 50, 31)
    im = Image.new("RGB", (520, 400), (0, 0, 0))
    d = ImageDraw.Draw(im)
    bw, bh = brand.badge(d, 60, 60, 200, fg=VERM, bg=PAPER)
    assert bw == bh, f"badge() returned {bw} by {bh}, which is not a square"
    x0, y0, x1, y1 = box_of(im, PAPER, VERM)
    side_w, side_h = x1 - x0 + 1, y1 - y0 + 1
    assert abs(side_w - side_h) <= 1, f"the drawn square is {side_w} by {side_h}"
    letters = box_of(im.crop((x0, y0, x1 + 1, y1 + 1)), VERM)
    lx0, ly0, lx1, ly1 = letters
    off_h = (lx0 - (side_w - 1 - lx1)) / 2
    off_v = (ly0 - (side_h - 1 - ly1)) / 2
    # Up and to the left by about a sixtieth of the box: the deliberate optical offset, no more.
    assert -0.06 * side_w < off_h <= 0.5, f"the letters sit {off_h:+.1f}px off across the square"
    assert -0.06 * side_h < off_v <= 0.5, f"the letters sit {off_v:+.1f}px off down the square"

    # and the square is centred on the cap height of FEAR REPORT, not on its line box
    im2 = Image.new("RGB", (1400, 400), (0, 0, 0))
    d2 = ImageDraw.Draw(im2)
    brand.wordmark(d2, 60, 100, 160, fill=PAPER, back=VERM)
    sq = box_of(im2.crop((0, 0, 400, 400)), PAPER, VERM)
    word = box_of(im2.crop((sq[2] + 8, 0, 1400, 400)), PAPER)
    drift = ((sq[1] + sq[3]) / 2) - ((word[1] + word[3]) / 2)
    assert abs(drift) <= 1.5, f"the square sits {drift:+.1f}px off the middle of FEAR REPORT"
    print("the mark is a square, centred on its letters and on the word: ok")


ALLOWED_HOSTS = {"gc.zgo.at"}   # the counter, and nothing else


def check_status_and_coverage():
    """What the site says has passed, and what it counts at all, against the records' own words.

    It went live saying 86 measures had passed. The status rule knew "Signed by Governor" and
    "Chaptered" and read every other state's signing as pending, so New York's frontier AI law and
    Utah's AI laws were on the site as pending. The tagger was asked whether a bill was
    "substantially about" AI and, given a bare title, said no to 180 bills titled with it. And a bill
    filed before January for a session that began then was never read. Every phrasing below is one
    found on file.
    """
    import sqlite3
    from pipeline import known
    from pipeline.common import (in_window, session_year, status_from_action, status_label,
                                 title_names_ai)
    for text, want in (
            ("Governor Signed", "passed"), ("Public Act . . . . . . . . . 104-0054", "passed"),
            ("SIGNED CHAP.438", "passed"), ("APPROVAL MEMO.76", "passed"), ("Chapter Number Assigned", "passed"),
            ("Act 123, 06/30/2025 (Gov. Msg. No. 1234).", "passed"), ("Chapter No. 2025-120", "passed"),
            ("Notification that HB1876 is now Act 927", "passed"), ("Signed", "passed"),
            ("House message: Governor approved bill on June 12, 2025", "passed"),
            ("delivered to Secretary of State (Acts Ch. 66)", "passed"), ("Secretary of State Chapter 20 5/2/2025", "passed"),
            ("Acts of Assembly Chapter text (CHAP0452)", "passed"), ("Becomes law without Governor's signature 5/1/2025", "passed"),
            ("Filed with Secretary Of State 04/10", "passed"), ("Signed by Gov. 7/1/2025", "passed"),
            ("Approved P.L.2025, c.12.", "passed"), ("Chapter 12, Acts, Regular Session, 2025", "passed"),
            ("Item passed notwithstanding objections of the Governor", "passed"), ("Letter of approval from the Governor", "passed"),
            ("Chaptered by Secretary of State. Chapter 138, Statutes of 2025.", "passed"), ("Effective date 7/27/2025.", "passed"),
            ("Became Public Law No: 119-12.", "passed"), ("Signed by the Governor. Becomes Act No. 312.", "passed"),
            ("action postponed indefinitely", "failed"), ("In committee upon adjournment.", "failed"),
            ("ENACTING CLAUSE STRICKEN", "failed"), ("Inexpedient to Legislate: MA VV 01/08/2026", "failed"),
            ("House Committee on Judiciary Postpone Indefinitely", "failed"), ("House sustained Governor's veto", "failed"),
            ("Consideration of Governor's veto stricken from file.", "failed"), ("Withdrawn Because Approved P.L.2025, c.3.", "failed"),
            ("Failed to pass notwithstanding the objections of the Governor pursuant to Joint Rule", "failed"),
            ("Passed by indefinitely in General Laws and Technology with letter (12-Y 10-N)", "failed"),
            ("House Committee on Appropriations Lay Over Unamended - Amendment(s) Failed", "pending"),
            ("Signed by the Speaker of the House", "pending"), ("Signed in the House", "pending"),
            ("Enrolled and presented to the Governor at 3 p.m.", "pending"), ("Carried over to 2026 Regular Session.", "pending"),
            ("Referred to Assignments", "pending"), ("SUBSTITUTED BY S6953B", "pending")):
        assert status_from_action(text) == want, f"{text!r} read as {status_from_action(text)}, wanted {want}"
    # A one-chamber resolution is adopted when that chamber adopts it. The Senate agreed to S.Res. 896
    # and the site called it not passed. An amendment or a motion adopted is not the resolution, and a
    # concurrent resolution one chamber adopts still needs the other.
    for text, ident, kind, want in (
            ("Submitted in the Senate, considered, and agreed to without amendment and with a preamble by "
             "Unanimous Consent.", "S.Res. 896", "resolution", "passed"),
            ("Senate Passed/Adopted By Substitute", "SR 789", "resolution", "passed"),
            ("House Read and Adopted", "HR 43", "resolution", "passed"),
            ("Report and Resolution Adopted.  Transmitted to House.", "HCR 206", "resolution", "pending"),
            ("Resolution agreed to in House", "H.Con.Res. 5", "resolution", "pending"),
            ("Motion to table agreed to", "S.Res. 5", "resolution", "pending"),
            ("Committee amendment adopted", "SR 5", "resolution", "pending"),
            ("Perfected with Amendments (H) - HA 1, adopted", "HB 362", "bill", "pending"),
            ("Senate floor amendments read and adopted.", "SB 474", "bill", "pending"),
            ("Referred to Energy", "HR 630", "resolution", "pending"),
            ("Died, not introduced, companion bill(s) passed, see HR 8003 (Adopted)", "SR 1768", "resolution", "failed")):
        got = status_from_action(text, ident, kind)
        assert got == want, f"{ident} {text!r} read as {got}, wanted {want}"
    assert status_label("pending") == "Not passed" and status_label("passed") == "Passed"
    assert status_label("failed", "House sustained Governor's veto") == "Vetoed"
    assert status_label("failed", "Died in Committee") == "Did not pass"
    assert status_label("pending", "", "rule") == "Proposed" and status_label("passed", "", "rule") == "Final"
    assert status_label("passed", "", "resolution") == "Adopted" and status_label("passed", "", "order") == "Signed"
    # A bill the legislature has passed and handed to the governor says so, rather than "not passed".
    assert status_label("pending", "Enrolled and presented to the Governor at 2 p.m.") == "Sent to the governor"
    assert status_label("pending", "Presented to President.") == "Sent to the president"
    assert status_label("pending", "Re-referred to Rules & Executive Nominations") == "Not passed"
    assert status_label("failed", "Vetoed by Governor") == "Vetoed"
    assert status_label("passed", "Chaptered by Secretary of State - Chapter 116, Statutes of 2026") == "Passed"
    # A bill left pending when its session closed cannot lead the post, unless it is with the governor.
    from pipeline.common import can_still_pass, closed_sessions
    shut = closed_sessions()
    assert shut[("ca", "20252026")] == "2026-08-31", shut
    dead = {"jurisdiction": "ca", "session": "20252026", "status": "pending",
            "latest_action": "Ordered to inactive file at the request of Senator Padilla."}
    assert not can_still_pass(dead, "2026-09-22", shut)
    assert can_still_pass(dead, "2026-08-30", shut), "the session was still open"
    assert can_still_pass({**dead, "latest_action": "Enrolled and presented to the Governor at 3 p.m."}, "2026-09-22", shut)
    assert can_still_pass({**dead, "status": "passed"}, "2026-09-22", shut)
    assert can_still_pass({**dead, "jurisdiction": "nj", "session": "222"}, "2026-09-22", shut)
    for title, want in (("Artificial Intelligence Amendments", True), ("AI Whistleblower Protection Act", True),
                        ("AN ACT CONCERNING ARTIFICIAL INTELLIGENCE.", True), ("Regulate the use of pricing algorithms", True),
                        ("CHATBOT Act", True), ("A.I. in Environmental Permitting.", True),
                        ("AI/AN CAPTA", False), ("Accelerating Innovation (AI) for Kids with Cancer Act", False),
                        ("Large-Load Data Centers", False), ("Hawaiian ai pono kitchens", False),
                        ("Artificial Intelligence (AI) Literacy Act", True)):
        assert title_names_ai(title) == want, f"title {title!r}"
    # Data center bills count whether or not they say AI, and a title naming data centers settles it,
    # except where "data center" is the name of a data program rather than a building.
    from pipeline.common import title_names_data_centers, title_settles, retag_data_centers, RETAG_DATA_CENTERS
    for title, want in (("Large-Load Data Centers", True), ("HYPERSCALE DATA CENTERS", True),
                        ("AN ACT relating to data centers.", True), ("Data center tax revenue; creates local program.", True),
                        ("An Act establishing an education-to-career data center", False),
                        ("State government; Oklahoma State Data Center; Legislative Service Bureau", False),
                        ("Maryland Longitudinal Data System Center - External Data Sharing With Third-Party Data Centers", False),
                        ("Establishes NJ Water Data Center at public institution of higher education", False),
                        ("Relating to property tax exemptions", False)):
        assert title_names_data_centers(title) == want, f"data center title {title!r}"
    assert title_settles("Large-Load Data Centers") and title_settles("AI Whistleblower Protection Act")
    assert not title_settles("Relating to property tax exemptions")
    # Self-driving vehicle bills count whether or not they say AI (the owner's call, 25 September 2026),
    # and a title naming them settles it, by any of the names statutes give them.
    from pipeline.common import AI_TEXT, retag_self_driving, RETAG_SELF_DRIVING
    for title, want in (("Autonomous vehicles; operation without a human driver", True),
                        ("Relating to the operation of automated motor vehicles", True),
                        ("Automated driving systems; commercial vehicles", True), ("Self-Driving Trucks Safety Act", True),
                        ("Prohibits driverless heavy-duty trucks", True), ("Robotaxi permits; local authority", True),
                        ("Highly automated vehicles; insurance", True), ("Relating to motor vehicle registration fees", False),
                        ("Authorizing the use of automated vehicle noise enforcement cameras", False)):
        assert title_settles(title) == want, f"self-driving title {title!r}"
    assert AI_TEXT.search("requires a permit before an automated driving system operates on a highway")
    sdb = connect(":memory:")
    for mid, title, summary, ai in (("s1", "Motor vehicles; amendments", "Allows autonomous vehicles without a driver.", 0),
                                    ("s2", "Autonomous vehicles; permits", "", 1),
                                    ("s3", "Relating to fishing licenses", "Raises the fee.", 0)):
        upsert(sdb, "measures", {"id": mid, "kind": "bill", "jurisdiction": "tx", "session": "89R",
                                 "identifier": mid, "title": title, "summary": summary, "status": "pending",
                                 "introduced_date": "2025-03-01", "url": f"u-{mid}"})
        sdb.execute("INSERT INTO tag_runs(target, text_hash, ai_related, tagged_at) VALUES(?, 'h', ?, 'then')", (mid, ai))
    # s1 was taken out by the question about AI under the wording that did not count these vehicles
    sdb.execute("INSERT INTO checks(target, kind, value, evidence, verdict, reason, model, checked_at) VALUES("
                "'s1', 'about', 'ai', 'x', 'no', 'not about AI', 'fake', 'then')")
    sdb.execute("DELETE FROM kv WHERE key = ?", (f"retag:self-driving:{RETAG_SELF_DRIVING}",))
    sdb.commit()
    assert retag_self_driving(sdb) == 2 and retag_self_driving(sdb) == 0, "the self-driving requeue is not once only"
    assert {r[0] for r in sdb.execute("SELECT target FROM tag_runs WHERE text_hash IS NULL")} == {"s1", "s2"}
    assert not sdb.execute("SELECT 1 FROM checks WHERE target = 's1'").fetchall(), "the old verdict still takes it out"
    rdb = connect(":memory:")
    for mid, title, summary, ai in (("d1", "Large-Load Data Centers", "", 0),
                                    ("d2", "Relating to utilities", "Sets a rate class for large load customers.", 0),
                                    ("d3", "Relating to fishing licenses", "Raises the fee.", 0),
                                    ("d4", "Data centers; moratorium", "", 1)):
        upsert(rdb, "measures", {"id": mid, "kind": "bill", "jurisdiction": "co", "session": "2026A",
                                 "identifier": mid, "title": title, "summary": summary, "status": "pending",
                                 "introduced_date": "2026-02-01", "url": f"u-{mid}"})
        rdb.execute("INSERT INTO tag_runs(target, text_hash, ai_related, tagged_at) VALUES(?, 'h', ?, 'then')", (mid, ai))
    # An office refused on a data center bill because the power was over data centers, not AI.
    rdb.execute("INSERT INTO checks(target, kind, value, evidence, verdict, reason, model, checked_at) VALUES("
                "'d4', 'agency', 'Colorado Public Utilities Commission', 'approve a data center connection', 'no', "
                "'authority over data centers, not over AI', 'fake', 'then')")
    rdb.execute("DELETE FROM kv WHERE key = ?", (f"retag:data-centers:{RETAG_DATA_CENTERS}",))
    rdb.commit()
    assert retag_data_centers(rdb) == 3 and retag_data_centers(rdb) == 0, "the data center requeue is not once only"
    queued = {r["target"] for r in rdb.execute("SELECT target FROM tag_runs WHERE text_hash IS NULL")}
    assert queued == {"d1", "d2", "d4"}, queued
    assert rdb.execute("SELECT evidence FROM tags WHERE target = 'd4' AND kind = 'agency'").fetchone()[0] == \
        "approve a data center connection", "the refused office was not put back to be asked again"
    assert not rdb.execute("SELECT 1 FROM checks WHERE target = 'd4'").fetchall(), "the old refusal was kept"
    # A control or fear widened to data centers is asked again where it was refused on one, and only there.
    from pipeline.common import recheck_data_center_labels, RECHECK_DATA_CENTER_LABELS
    for target, kind, value in (("d4", "control", "new-agency-powers"), ("d3", "control", "new-agency-powers"),
                                ("d4", "control", "license-to-build")):
        rdb.execute("INSERT INTO checks(target, kind, value, evidence, verdict, reason, model, checked_at) "
                    "VALUES(?, ?, ?, 'a quote', 'no', 'over data centers, not AI', 'fake', 'then')", (target, kind, value))
    rdb.execute("DELETE FROM kv WHERE key = ?", (f"recheck:data-center-labels:{RECHECK_DATA_CENTER_LABELS}",))
    rdb.commit()
    assert recheck_data_center_labels(rdb) == 1 and recheck_data_center_labels(rdb) == 0
    left = {(r["target"], r["value"]) for r in rdb.execute("SELECT target, value FROM checks")}
    assert left == {("d3", "new-agency-powers"), ("d4", "license-to-build")}, left
    assert rdb.execute("SELECT 1 FROM tags WHERE target = 'd4' AND value = 'new-agency-powers'").fetchone()
    from pipeline.common import config as _config
    powers = next(c for c in _config("controls") if c["slug"] == "new-agency-powers")
    bills = next(f for f in _config("fears") if f["slug"] == "power-bills")
    assert "data centers" in powers["definition"] and "for AI" not in bills["definition"], "the widened wording was lost"
    print("a data center bill counts whether or not it says AI, and those turned down are read again: ok")
    assert session_year("mt", "2025") == 2025 and session_year("tx", "89R") == 2025 and session_year("tx", "891") == 2025
    assert session_year("nj", "221") == 2024 and session_year("az", "57th-1st-regular") == 2025
    assert session_year("zz", "12") is None
    row = {"kind": "bill", "jurisdiction": "mt", "session": "2025", "introduced_date": "2024-09-23"}
    assert in_window(row), "a bill filed in 2024 for Montana's 2025 session is outside the report"
    assert not in_window(dict(row, jurisdiction="nj", session="221", introduced_date="2024-03-01")), \
        "a 2024 session bill came into the report"
    print("status is read from every legislature's wording, and a title naming AI counts: ok")

    # the known-laws check finds what is missing, what is not counted, and what is marked wrong
    laws = known.laws()
    assert laws and all(l["outcome"] in ("passed", "failed") and l["jurisdiction"] and l["session"] and l["identifier"]
                        and l["confirmed"] for l in laws), "config/known_laws.json is malformed"
    db = connect(":memory:")
    first = laws[0]
    upsert(db, "measures", {"id": "k1", "kind": "bill", "jurisdiction": first["jurisdiction"], "session": first["session"],
                            "identifier": first["identifier"], "title": first["name"], "status": "pending",
                            "latest_action": "Referred", "introduced_date": "2025-02-01", "url": "u1",
                            "jurisdiction_name": "X", "source": "Open States"})
    db.execute("INSERT INTO tag_runs(target, text_hash, ai_related, tagged_at) VALUES('k1', 'h', 0, 'now')")
    db.commit()
    found = dict(((l["jurisdiction"], l["identifier"]), p) for l, p in known.problems(db))
    key = (first["jurisdiction"], first["identifier"])
    assert "judged not about AI" in found[key] or "known to be" in found[key], found[key]
    second = laws[1]
    assert "not in the record" in found[(second["jurisdiction"], second["identifier"])]
    assert known.ids(db) == {"k1"}
    print("the record is held against laws whose outcome is known from outside it: ok")

    # Congress: a bill whose title is an acronym is found by its summary, and one already on file is
    # followed whatever its title says.
    from pipeline import collect_congress as cc
    upsert(db, "measures", {"id": "us-119-hr-9", "kind": "bill", "jurisdiction": "us", "session": "119",
                            "identifier": "H.R. 9", "title": "NO FAKES Act", "status": "pending", "url": "u9",
                            "jurisdiction_name": "Congress", "source": "Congress.gov"})
    db.commit()

    class Fake:
        def __init__(self, *a, **k):
            pass

        def json(self, url, params=None, **kw):
            if url.split("?")[0].endswith("/bill/119"):
                return {"bills": [{"type": "S", "number": "146", "title": "TAKE IT DOWN Act", "updateDate": "a"},
                                  {"type": "HR", "number": "1", "title": "AI Ads Act", "updateDate": "b"},
                                  {"type": "HR", "number": "9", "title": "NO FAKES Act", "updateDate": "c"}]}
            if url.split("?")[0].endswith("/summaries/119"):
                # a short first page with a next link, as the API pages: the scan must not stop there
                if params.get("offset", 0) == 0:
                    return {"summaries": [{"bill": {"type": "S", "number": "146"}, "text": "<p>covers deepfakes</p>"},
                                          {"bill": {"type": "HR", "number": "2"}, "text": "<p>highway funding</p>"}],
                            "pagination": {"count": 3, "next": "https://api.congress.gov/v3/summaries/119?offset=2"}}
                return {"summaries": [{"bill": {"type": "HR", "number": "7"},
                                       "text": "<p>requires disclosure of artificial intelligence use</p>"}],
                        "pagination": {"count": 3}}
            raise AssertionError(url)
    calls = []
    saved = (cc.Http, cc.store_bill, cc.env, cc.current_congress)
    cc.Http, cc.env, cc.current_congress = Fake, (lambda name: "k"), (lambda: 119)
    cc.store_bill = lambda db, http, key, congress, kind, number, listing=None: calls.append((kind, int(number), bool(listing))) or 1
    try:
        cc.run(db, {}, "hourly")
    finally:
        cc.Http, cc.store_bill, cc.env, cc.current_congress = saved
    assert ("HR", 1, True) in calls and ("HR", 9, True) in calls, calls
    assert ("S", 146, False) in calls and not any(c[:2] == ("HR", 2) for c in calls), calls
    assert ("HR", 7, False) in calls, "the summary scan stopped at a short page"
    print("Congress bills are found by their summaries as well as their titles: ok")

    # State searches: the ones already caught up run first, so a backfill cannot starve them.
    from pipeline import collect_openstates as cos
    from pipeline.common import kv_set as _kv_set
    _kv_set(db, "openstates_cursors", {"artificial intelligence": {"backfilling": False, "since": "2026-09-20", "page": 1},
                                       "deepfake": {"backfilling": False, "since": "2026-09-20", "page": 1},
                                       "chatbot": {"backfilling": False, "since": "2026-09-20", "page": 1},
                                       "data center": {"backfilling": True, "page": 203}})
    _kv_set(db, "openstates_offset", 3)
    # the weekly look for legislatures the search cannot see is tested on its own below
    _kv_set(db, "openstates_blind", {"checked": __import__("pipeline.common", fromlist=["iso"]).iso()[:10], "sweeps": {}})
    asked = []

    class FakeOS:
        def __init__(self, *a, **k):
            pass

        def json(self, url, params=None, **kw):
            if "identifier" in params:  # finding bills' own pages, tested on its own
                return {"results": []}
            if params.get("sort") != "first_action_asc":  # the pre-filed sweep is tested below
                asked.append(params["q"])
            return {"results": [], "pagination": {"max_page": 1}}
    saved = (cos.Http, cos.env)
    cos.Http, cos.env = FakeOS, (lambda name: "k")
    try:
        cos.run(db, {}, "hourly")
    finally:
        cos.Http, cos.env = saved
    assert asked[:3] == ["artificial intelligence", "deepfake", "chatbot"], asked
    assert asked[3] == "automated decision" and asked[-1] == "data center", asked
    print("state searches already caught up run before any backfill: ok")

    # A pass through a search resumes where it stopped, and the next pass reads from the day it began,
    # so a bill changed while a backfill ran is still read. A page the results no longer reach ends it.
    from pipeline.common import kv_get as _kv_get
    _kv_set(db, "openstates_prefile", {q: {"page": 1, "done": True} for q in cos.QUERIES})
    _kv_set(db, "openstates_resweep", cos.RESWEEP)
    _kv_set(db, "openstates_cursors", {q: {"backfilling": False, "since": "2026-09-20", "page": 1}
                                       for q in cos.QUERIES if q != "digital replica"})
    _kv_set(db, "openstates_offset", cos.QUERIES.index("digital replica"))
    got, clock = [], {"day": "2026-09-22"}

    class Paged:
        def __init__(self, *a, **k):
            pass

        def json(self, url, params=None, **kw):
            if "identifier" in params:  # finding bills' own pages, tested on its own
                return {"results": []}
            got.append((params["q"], params["page"], params.get("updated_since")))
            if params["q"] == "digital replica":
                return {"results": [{"id": "x"}], "pagination": {"max_page": 3}}
            if params["q"] == "chatbot" and params["page"] > 1:
                raise _HttpError(400, url, "page out of range")
            if params["q"] == "chatbot":
                return {"results": [{"id": "y"}], "pagination": {"max_page": 2}}
            return {"results": [], "pagination": {"max_page": 1}}
    from pipeline.common import HttpError as _HttpError
    saved = (cos.Http, cos.env, cos.iso, cos.store)
    cos.Http, cos.env, cos.store = Paged, (lambda name: "k"), (lambda db_, b: 0)
    cos.iso = lambda *a: f"{clock['day']}T10:00:00+00:00"
    try:
        # a budget that leaves the new search two pages after one each for the seven caught up
        _kv_set(db, "openstates_spend", {"date": clock["day"], "n": cos.DAY_BUDGET - (len(cos.QUERIES) - 1) - 2})
        cos.run(db, {}, "hourly")
        cur = _kv_get(db, "openstates_cursors")
        assert cur["digital replica"] == {"backfilling": True, "page": 3, "since": None, "started": "2026-09-22"}, cur
        assert cur["chatbot"] == {"backfilling": False, "since": "2026-09-22", "page": 1}, \
            f"a page past the end did not end the pass: {cur['chatbot']}"
        clock["day"] = "2026-09-24"
        got.clear()
        cos.run(db, {}, "hourly")
        cur = _kv_get(db, "openstates_cursors")
        assert ("digital replica", 3, None) in got, got
        assert cur["digital replica"] == {"backfilling": False, "since": "2026-09-22", "page": 1}, \
            f"a finished backfill forgot the day it began: {cur['digital replica']}"
        # every search already caught up is read once from the collector's first day
        _kv_set(db, "openstates_resweep", 0)
        got.clear()
        cos.run(db, {}, "hourly")
        assert ("artificial intelligence", 1, cos.FIRST_RUN) in got, got
        assert _kv_get(db, "openstates_resweep") == cos.RESWEEP
    finally:
        cos.Http, cos.env, cos.iso, cos.store = saved
    print("a pass through a state search resumes, and the next reads what changed while it ran: ok")

    # A legislature whose bill text the search cannot see is found once a week and read bill by bill:
    # Indiana's 2026 session answered the search with nothing though it filed 935 bills, and the DC
    # Council answered nothing in either year.
    bdb = connect(":memory:")
    for i in range(6):  # a state the search sees, which is not asked about
        upsert(bdb, "measures", {"id": f"tx{i}", "kind": "bill", "jurisdiction": "tx", "session": "89R",
                                 "identifier": f"HB {i}", "title": "t", "introduced_date": "2025-03-01",
                                 "source": "Open States"})
    for y in ("2025", "2026"):
        for code in sorted(__import__("pipeline.common", fromlist=["STATES"]).STATES - {"tx", "in"}):
            for i in range(6):
                upsert(bdb, "measures", {"id": f"{code}{y}{i}", "kind": "bill", "jurisdiction": code, "session": y,
                                         "identifier": f"B {i}", "title": "t", "introduced_date": f"{y}-03-01",
                                         "source": "Open States"})
    for i in range(6):
        upsert(bdb, "measures", {"id": f"tx26{i}", "kind": "bill", "jurisdiction": "tx", "session": "2026",
                                 "identifier": f"SB {i}", "title": "t", "introduced_date": "2026-03-01",
                                 "source": "Open States"})
        upsert(bdb, "measures", {"id": f"in25{i}", "kind": "bill", "jurisdiction": "in", "session": "2025",
                                 "identifier": f"SB {i}", "title": "t", "introduced_date": "2025-02-01",
                                 "source": "Open States"})
    bdb.commit()
    asks = []
    pages = {1: [{"id": "ocd-bill/in1", "identifier": "HB 1182", "title": "Digital sexual image abuse.",
                  "abstracts": [{"abstract": "Makes it a crime to share an image made with artificial intelligence."}]},
                 {"id": "ocd-bill/in2", "identifier": "HB 1002", "title": "Electric utility affordability.", "abstracts": []}],
             2: [{"id": "ocd-bill/in3", "identifier": "SB 99", "title": "Data centers.", "abstracts": []},
                 {"id": "ocd-bill/in4", "identifier": "SB 100", "title": "Bail procedures.", "abstracts": []}]}

    class Blind:
        def json(self, url, params=None, **kw):
            asks.append(dict(params))
            where = params["jurisdiction"]
            if params.get("per_page") == 1:  # a count
                if "district:dc" in where:
                    n = 0 if params.get("q") else (2400 if params["created_since"] < "2026" else 1100)
                elif "state:in" in where:
                    n = (2 if params["created_since"] < "2026" else 0) if params.get("q") else \
                        (1900 if params["created_since"] < "2026" else 935)
                else:
                    n = 12
                return {"results": [], "pagination": {"total_items": n, "max_page": 1}}
            if "state:in" in where:
                return {"results": pages[params["page"]], "pagination": {"max_page": 2}}
            return {"results": [], "pagination": {"max_page": 1}}
    kept = []
    saved = cos.store
    cos.store = lambda db_, b: kept.append(b["identifier"]) or 1
    try:
        used = cos.find_blind(bdb, Blind(), 80, "2026-09-22")
        st = _kv_get(bdb, "openstates_blind")
        assert set(st["sweeps"]) == {"in:2026-01-01", "dc:2025-01-01"}, st
        assert not any("state:tx" in a["jurisdiction"] for a in asks), "a state the search sees was asked about"
        assert cos.find_blind(bdb, Blind(), 80, "2026-09-25") == 0, "asked again inside the week"
        n, stored = cos.sweep_blind(bdb, Blind(), 10, __import__("time").time(), "2026-09-22")
        assert kept == ["HB 1182", "SB 99"], f"kept {kept}; only what names AI or data centers belongs"
        st = _kv_get(bdb, "openstates_blind")
        assert st["sweeps"]["in:2026-01-01"] == {"page": 1, "started": None, "done": True,
                                                 "updated_since": "2026-09-22"}, st["sweeps"]
        assert st["sweeps"]["dc:2025-01-01"]["done"], st["sweeps"]
    finally:
        cos.store = saved
    print("a legislature the search cannot see is read bill by bill: ok")

    # Bills pre-filed in late 2024 for a 2025 session are swept once, stopping at the first bill
    # acted on in 2025, and a refusal ends the sweep for that search rather than the run.
    from pipeline.common import HttpError as _HttpError
    _kv_set(db, "openstates_prefile", {})
    pages = {1: [{"first_action_date": "2024-11-12", "id": "p1"}, {"first_action_date": "2024-12-02", "id": "p2"}],
             2: [{"first_action_date": "2024-12-20", "id": "p3"}, {"first_action_date": "2025-01-14", "id": "p4"},
                 {"first_action_date": "2025-01-15", "id": "p5"}]}
    stored_ids = []

    class Sweep:
        def json(self, url, params=None, **kw):
            if params["q"] == "deepfake":
                raise _HttpError(400, url, "sort not supported")
            if params["q"] != "artificial intelligence":
                return {"results": [], "pagination": {"max_page": 1}}
            assert params["sort"] == "first_action_asc" and params["created_since"] < "2025"
            return {"results": pages[params["page"]], "pagination": {"max_page": 3}}
    saved_store = cos.store
    cos.store = lambda db, b: stored_ids.append(b["id"]) or 1
    try:
        used, found = cos.prefile_sweep(db, Sweep(), 30, __import__("time").time())
    finally:
        cos.store = saved_store
    assert stored_ids == ["p1", "p2", "p3"], stored_ids
    from pipeline.common import kv_get as _kv_get
    swept = _kv_get(db, "openstates_prefile")
    assert swept["artificial intelligence"]["done"] and swept["deepfake"]["done"], swept
    print("bills pre-filed for a 2025 session are swept once: ok")

    # The third reading asks of every counted measure whose title does not name AI whether AI is a
    # subject of it at all. A no takes it out of every count; a title naming AI, or a person-confirmed
    # law, is not asked.
    from pipeline import check
    from pipeline.common import measures_in_scope as _in_scope
    adb = connect(":memory:")
    for mid, title, summary in (("a1", "CATCH Fentanyl Act", "Directs a pilot of detection technology, which may include artificial intelligence."),
                                ("a2", "An act relating to a person's voice and likeness", "Bars unauthorized AI-generated digital replicas."),
                                ("a3", "Artificial Intelligence Amendments", ""),
                                ("a0", "HYPERSCALE DATA CENTERS", "")):
        upsert(adb, "measures", {"id": mid, "kind": "bill", "jurisdiction": "tx", "session": "89R", "identifier": mid,
                                 "title": title, "summary": summary, "status": "pending", "introduced_date": "2025-03-01",
                                 "url": f"u-{mid}", "jurisdiction_name": "Texas", "source": "Open States"})
        adb.execute("INSERT INTO tag_runs(target, text_hash, ai_related, tagged_at) VALUES(?, 'h', 1, 'now')", (mid,))
    adb.commit()
    asked = []

    def judge(key, user, max_tokens=250):
        asked.append(user)
        assert "THE LABEL RESTS ON THIS QUOTE" not in user, "the about question borrowed the label wording"
        return ({"verdict": "no", "reason": "AI is one technology a pilot may use"} if "Fentanyl" in user
                else {"verdict": "yes", "reason": "it regulates digital replicas"}), "fake"
    check.run(adb, "k", ask_fn=judge)
    counted = {m["id"] for m in _in_scope(adb)}
    assert len(asked) == 2, f"asked {len(asked)} times; a title naming AI or data centers should not be asked"
    assert counted == {"a2", "a3", "a0"}, counted
    check.run(adb, "k", ask_fn=judge)
    assert len(asked) == 2, "a measure already answered on the same text was asked again"
    # What the report counts with AI is not the second reading's to narrow. The first wording took
    # out 171 data center bills the site launched with, and an ELVIS Act; the question names them.
    yes_part = check.ABOUT_QUESTION.split("Answer no")[0]
    for subject in ("data centers", "voice and likeness", "synthetic", "automated decision", "drive themselves"):
        assert subject in yes_part, f"the about question no longer counts {subject}"
    # Measures the first wording took out are put back and asked again, once. One the tagger has
    # read again since, on new text, keeps the tagger's answer.
    for mid, tagged in (("a5", "2026-09-21T10:00:00+00:00"), ("a6", "2026-09-21T13:00:00+00:00")):
        upsert(adb, "measures", {"id": mid, "kind": "bill", "jurisdiction": "tx", "session": "89R", "identifier": mid,
                                 "title": "Relating to electric utilities and local permits",
                                 "summary": "Pauses permits for new data centers and sets large-load electricity rates.",
                                 "status": "pending", "introduced_date": "2025-03-01", "url": f"u-{mid}",
                                 "jurisdiction_name": "Texas", "source": "Open States"})
        adb.execute("INSERT INTO tag_runs(target, text_hash, ai_related, tagged_at) VALUES(?, 'h', 0, ?)", (mid, tagged))
        adb.execute("INSERT INTO checks(target, kind, value, evidence, verdict, reason, model, checked_at) "
                    "VALUES(?, 'about', 'ai', 'old', 'no', 'data centers not said to be for AI', 'fake', "
                    "'2026-09-21T12:00:00+00:00')", (mid,))
    adb.execute("DELETE FROM kv WHERE key = ?", (f"check:about:{check.ABOUT_VERSION}",))
    adb.commit()
    line = check.run(adb, "k", ask_fn=judge)
    counted = {m["id"] for m in _in_scope(adb)}
    assert "put back 1 measure to ask again whether it is about AI" in line, line
    assert "a5" in counted and "a6" not in counted, counted
    assert len(asked) == 3 and "Pauses permits for new data centers" in asked[-1], "the one put back was not asked again"
    assert not adb.execute("SELECT 1 FROM checks WHERE kind = 'about' AND verdict = 'no'").fetchall(), \
        "an old refusal was kept"
    check.run(adb, "k", ask_fn=judge)
    assert len(asked) == 3 and "a5" in {m["id"] for m in _in_scope(adb)}, "measures were put back twice"
    print("a measure is counted as about AI only when AI is a subject of it: ok")


def check_links_open_right(dist):
    """A link off the site opens a new tab; a link within it does not.

    A reader who follows a bill to its source, or the footer to GitHub, should still have the
    report open behind it. A page of this site opening in a new tab would do the opposite, piling
    tabs up and breaking the back button, and bitcoin: and mailto: hand off to an app, so a new tab
    there is left empty.
    """
    from html.parser import HTMLParser

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.found = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.found.append(dict(attrs))
    checked = 0
    for page in pathlib.Path(dist).rglob("*.html"):
        parser = Links()
        parser.feed(page.read_text(encoding="utf-8"))
        for a in parser.found:
            href, blank = a.get("href") or "", a.get("target") == "_blank"
            away = re.match(r"https?://", href) and not re.match(r"https?://(www\.)?aifearreport\.com", href)
            if away:
                assert blank and "noopener" in (a.get("rel") or ""), f"{page}: {href} opens in the same tab"
            else:
                assert not blank, f"{page}: {href} is part of the site and opens a new tab"
            checked += 1
    assert checked, "no links were checked"
    print(f"links off the site open a new tab, links within it do not ({checked} checked): ok")


def check_states(dist, data):
    """The map, the states page and a page for every square on it, ranked by what the report is about.

    Every square is a link, so every square needs a page that exists; the front page shows five and
    sends the rest to the states page; the ranking is by measures carrying a control, the number the
    map is shaded by, so the two cannot disagree about who leads.
    """
    from pipeline.export import TILES
    rows, pages, m = data["states"], data["state_pages"], data["state_map"]
    assert [r["c"] for r in rows] == sorted((r["c"] for r in rows), reverse=True), \
        "the states are not ranked by measures carrying a control"
    assert len(m["tiles"]) == len(TILES) == 52, f"{len(m['tiles'])} squares on the map"
    assert {t["code"] for t in m["tiles"]} == set(TILES), "a state is missing from the map"
    top = max(t["c"] for t in m["tiles"])
    assert all((t["level"] == 0) == (t["c"] == 0) for t in m["tiles"]), "a state with none is shaded, or one with some is not"
    assert all(t["level"] == 4 for t in m["tiles"] if t["c"] == top and top), "the leader is not the darkest square"
    for t in m["tiles"] + m["federal"]:
        assert (dist / "states" / t["slug"] / "index.html").exists(), f"the square for {t['name']} goes nowhere"
        assert t["code"] in m["counts"] or t["code"] in m["fed_counts"], f"{t['name']} has no counts for the switches"
    states_page = (dist / "states" / "index.html").read_text()
    assert states_page.count('class="tile ') == 52 and "data-map-ctl" in states_page, "the states page map is incomplete"
    assert states_page.count('<a class="row" href="/states/') == len(rows), "the states page does not list every one"
    assert "data-all" in states_page, "the full list on the states page is cut to five"
    home = (dist / "index.html").read_text()
    at = home.index('id="states"')
    section = home[at:home.index("<section", at)]
    assert section.count('<a class="row" href="/states/') == min(5, len(rows)), "the front page should show five states"
    assert 'href="/states/"' in section and "data-map-ctl" not in section, "the front page map should be plain"
    assert "data-map-cap" not in section, "the front page map repeats the line above it"
    # The map sits above the money: it is the page's one thing to play with, not its last section.
    assert home.index('id="states"') < home.index('id="funding"'), "the map fell below the money"
    present = re.findall(r'<section class="block[^"]*" id="([a-z]+)"', home)
    jumps = [x for x in re.findall(r'data-jump="([a-z]+)"', home) if x in present]
    sections = [x for x in present if x in jumps]
    assert jumps == sections, f"the jump bar is out of order with the page: {jumps} against {sections}"
    # The page's own sections alternate; the brief and the pitch-in under </main> are every page's
    # and keep their own look.
    own = home[home.index('class="jumpbar"'):home.index("</main>")]
    shades = re.findall(r'<section class="block( band)?" id="[a-z]+"', own)
    assert len(shades) > 3 and all(a != b for a, b in zip(shades, shades[1:])), "two sections in a row share a shade"
    assert "filling" not in home and "being read in" not in home.lower(), "a read-in note is back on the page"
    for page in dist.rglob("index.html"):
        assert 'data-nav="states"' in page.read_text(), f"{page} has no States link"
    for p in pages:
        body = (dist / "states" / p["slug"] / "index.html").read_text()
        assert f">{p['name']}<" in body, f"the {p['name']} page does not name it"
        if p["n"]:
            assert body.count('class="item"') >= min(len(p["bills"]), 1), f"the {p['name']} page lists no measures"
            assert p["receipt"].startswith(p["name"] + ":") and p["receipt"].endswith(f"/states/{p['slug']}/")
    print(f"a map of {len(m['tiles'])} squares, {len(pages)} place pages, ranked by what they would control: ok")


def check_links(dist):
    """Every link inside the site reaches a page that exists, and every anchor a section that exists.

    Funders' pages linked back to /#funders for months while the section was called funding.
    """
    import html as _html, urllib.parse as _up
    pages = {p: p.read_text(errors="replace") for p in dist.rglob("*.html")}
    ids = {p: set(re.findall(r'\bid="([^"]+)"', s)) for p, s in pages.items()}
    bad, n = [], 0
    for p, s in pages.items():
        for href in re.findall(r'\bhref="([^"]*)"', s):
            u = _up.urlparse(_html.unescape(href))
            if u.scheme or href.startswith(("mailto:", "bitcoin:")) or not (u.path or u.fragment):
                continue
            n += 1
            target = p
            if u.path:
                path = u.path if u.path.startswith("/") else "/" + str(p.parent.relative_to(dist) / u.path)
                t = dist / path.lstrip("/")
                target = t / "index.html" if (t.is_dir() or path.endswith("/")) else t
                if not target.exists():
                    bad.append((str(p.relative_to(dist)), href, "no such page"))
                    continue
            if u.fragment and target.suffix == ".html" and u.fragment not in ids.get(target, set()):
                bad.append((str(p.relative_to(dist)), href, "no such section"))
    assert not bad, f"{len(bad)} links inside the site go nowhere: {bad[:6]}"
    print(f"all {n} links inside the site reach a page and a section that exist: ok")


def check_fears_explained(dist, data):
    """The site says how many fears it follows and how they were chosen, and claims no more.

    A reader pointed out that the method page ranked the fears without saying how the list was
    made or that it was a list of ten, while the tagline said "every fear about AI". The list is now
    the highest-scoring of every fear measured on the day it was set, the ones left off are named and
    still measured, and the controls are counted whatever a measure cites. The method page says so
    without a count that moves, so it is true on any day, and it names every control.
    """
    method = re.sub(r"\s+", " ", (dist / "method" / "index.html").read_text())
    home = re.sub(r"\s+", " ", (dist / "index.html").read_text())
    word = data["fear_word"]
    fl = data["fear_list"]
    assert f'id="fears">The {word} fears' in method and fl["chosen"] and \
        f"scored highest on the Fear Index out of {fl['measured']} fears measured on {fl['chosen']}" in method, \
        "the method page does not say how the fears were chosen"
    assert fl["left_off"] and "The fears measured and left off are " + ", ".join(fl["left_off"][:-1]) in method, \
        "the fears left off are not named"
    assert "They are scored every day the same way" in method, "the fears left off are not said to be measured"
    assert "A measure counts whether or not it cites a fear, and so does every control it would create" in method
    assert "The controls are " + ", ".join(data["control_names"][:-1]) in method, "the controls are not all named"
    # Evergreen: no running total on the method page, where it would go stale beside the live ones.
    body = method.split('id="sources"')[0]
    for n in (data["bills_meta"]["total"], data["bills_meta"]["controlled"]):
        assert n < 100 or f"{n:,}" not in body, f"the method page carries a running total ({n:,})"
    assert f"The {word} fears this report follows" in home and 'href="/method/#fears"' in home
    # Each fear's page says what the fear covers: the one definition every label rests on.
    import html as _html
    for fp in data["fear_pages"]:
        page = _html.unescape(re.sub(r"\s+", " ", (dist / "fear" / fp["slug"] / "index.html").read_text()))
        assert fp["definition"] and fp["definition"] in page, \
            f"the {fp['slug']} page does not show its definition"
    for page in dist.rglob("*.html"):
        assert "Every fear about AI" not in page.read_text(), f"{page} still claims every fear"
    assert "Every fear" not in data["site"]["tagline"]
    print(f"the {word} fears are said to be the top {word} of {fl['measured']} measured, with the ones left off named: ok")


def check_bills_page(dist, data):
    """Every measure on one page, filterable, each saying where its link goes.

    A reader asked for a way to read all the bills sorted by category rather than state by state, and
    took Open States' app, where the links landed, for this site's own reading of the bill.
    """
    page = (dist / "bills" / "index.html").read_text()
    items = re.findall(r'<li class="item" data-place="([^"]*)" data-fears="([^"]*)" data-controls="([^"]*)" '
                       r'data-status="([^"]*)">(.*?)</li>', page, re.S)
    total = data["index"]["total_measures"]
    assert len(items) == total == data["bills_meta"]["total"], f"{len(items)} items for {total} measures"
    assert all("Read it on " in body for *_, body in items), "an item does not say where its link goes"
    for name in ("q", "place", "fear", "control", "status", "sort"):
        assert f'name="{name}"' in page, f"the filters have no {name}"
    assert "data-bills-form hidden" in page, "the filters show before the script that works them"
    fears = {f["slug"] for f in data["bills_meta"]["fears"]}
    assert {f for _, fs, _, _, _ in items for f in fs.split()} <= fears, "an item cites a fear the filter cannot pick"
    assert sum(1 for _, _, cs, _, _ in items if cs) == data["bills_meta"]["controlled"] == data["index"]["controlled"]
    assert len(re.findall(r"<h1[^>]*>", page)) == 1
    home = (dist / "index.html").read_text()
    assert 'href="/bills/?control=' in home, "the front page's controls do not open their bills"
    fear = data["fear_pages"][0]
    body = (dist / "fear" / fear["slug"] / "index.html").read_text()
    if fear["bills_total"]:
        assert f'href="/bills/?fear={fear["slug"]}"' in body, "a fear page does not open all the bills citing it"
    for p in dist.rglob("index.html"):
        assert 'data-nav="bills"' in p.read_text(), f"{p} has no Bills link"
    print(f"every one of {total} measures on one page, filterable, saying where each link goes: ok")


def check_search(dist, data_path, tmp):
    """What search engines are told, page by page.

    The site, its publisher and its dataset are said once, on the front page; every other page gives
    its breadcrumbs. They used to ride on every fear and organization page too, each claiming to be
    the website at its own address. A place with too little on it is kept out of search and out of
    the sitemap. Titles lead with what people search for and carry the page's own numbers, so no
    two are the same sentence with a name swapped. Nothing a page needs to render comes from another
    host: the type was a Google Fonts stylesheet in front of every first paint.
    """
    def graph(body):
        m = re.search(r'<script type="application/ld\+json">(.*?)</script>', body, re.S)
        return json.loads(m.group(1))["@graph"] if m else []

    data = json.loads(pathlib.Path(data_path).read_text())
    home = (dist / "index.html").read_text()
    types = [g["@type"] for g in graph(home)]
    assert {"WebSite", "Organization", "Dataset"} <= set(types), f"the front page says {types}"
    ds = next(g for g in graph(home) if g["@type"] == "Dataset")
    assert 50 <= len(ds["description"]) <= 5000 and len(ds["distribution"]) == 4, ds
    org = next(g for g in graph(home) if g["@type"] == "Organization")
    assert org["logo"]["url"].endswith("/icon/512.png") and "https://x.com/aiFearReport" in org["sameAs"], org
    sitemap = (dist / "sitemap.xml").read_text()
    assert "changefreq" not in sitemap and "priority" not in sitemap, "the sitemap still carries fields both engines ignore"
    listed = set(re.findall(r"<loc>([^<]+)</loc>", sitemap))
    assert sitemap.count("<lastmod>") == len(listed), "a sitemap entry has no date"
    titles = {}
    thin = {p["slug"] for p in data["state_pages"] if p["n"] < 3}
    # A fear taken off the list keeps its address with a note, out of search like the 404.
    retired = {f"fear/{r['slug']}/index.html" for r in data.get("retired_fears") or []}
    for rel in retired:
        body = (dist / rel).read_text()
        assert 'content="noindex"' in body and "No longer on the list" in body, f"{rel} is not marked retired"
        assert "https://aifearreport.com/" + rel[:-len("index.html")] not in listed, f"{rel} is offered to search"
    for page in dist.rglob("index.html"):
        body, rel = page.read_text(), page.relative_to(dist).as_posix()
        if rel in retired:
            continue
        title = re.search(r"<title>(.*?)</title>", body).group(1)
        assert title not in titles, f"{rel} and {titles.get(title)} share the title {title!r}"
        titles[title] = rel
        robots = re.search(r'<meta name="robots" content="([^"]*)"', body).group(1)
        url = "https://aifearreport.com/" + rel[:-len("index.html")]
        slug = rel.split("/")[1] if rel.startswith("states/") and rel.count("/") == 2 else None
        if slug in thin:
            assert robots == "noindex" and url not in listed, f"{rel} is too thin for search but is offered to it"
        else:
            assert robots == "max-image-preview:large" and url in listed, f"{rel}: robots {robots!r}, listed {url in listed}"
        if rel != "index.html":
            kinds = [g["@type"] for g in graph(body)]
            assert "WebSite" not in kinds and "Dataset" not in kinds, f"{rel} claims to be the site: {kinds}"
            assert "BreadcrumbList" in kinds, f"{rel} has no breadcrumbs"
        for tag in re.findall(r'<link[^>]+rel="(?:stylesheet|preload|preconnect|dns-prefetch)"[^>]*>', body):
            assert 'href="/' in tag, f"{rel} loads from another host: {tag}"
        assert "fonts.googleapis" not in body and "fonts.gstatic" not in body, f"{rel} still asks Google for its type"
        for src in re.findall(r"src:url\(([^)]+)\)", body):
            assert src.startswith("/fonts/") and (dist / src.lstrip("/")).exists(), f"{rel} names a font that is not there: {src}"
    for p in data["state_pages"]:
        title = next(t for t, r in titles.items() if r == f"states/{p['slug']}/index.html")
        if p.get("kind") == "rules and executive orders":
            assert title.startswith("Federal AI rules and executive orders:"), title
        elif p["code"] == "us":
            assert title.startswith("AI bills in Congress:"), title
        else:
            assert title.startswith(f"{p['name']} AI bills and laws: {p['n']:,} measure"), title
    key = (data.get("site") or {}).get("indexnow_key")
    assert key and (dist / f"{key}.txt").read_text() == key, "the IndexNow key is not at the site root"
    print(f"search sees one site, {len(listed)} pages in the sitemap, {len(thin)} kept out, no fonts from elsewhere: ok")


def check_page_dates(data_path, tmp):
    """A page's date in the sitemap moves when what it says changes, and only then.

    It used to be the export time for every page, every twenty minutes, which is a date search
    engines learn to ignore. Built twice from the same data, every date holds; built again after one
    state's receipt changes, that page and only that page is dated anew and marked to announce.
    """
    site = ROOT / "site" / "build.py"
    record = tmp / "pages.json"
    record.unlink(missing_ok=True)

    def build(data, out):
        path = tmp / "dated.json"
        path.write_text(json.dumps(data))
        subprocess.run([sys.executable, str(site), "--data", str(path), "--out", str(tmp / out), "--base", "",
                        "--pages", str(record)], check=True, stdout=subprocess.DEVNULL)
        return json.loads(record.read_text())

    data = json.loads(pathlib.Path(data_path).read_text())
    first = build(data, "d1")
    assert first and all(e["lastmod"] == data["built_at"] and e["pending"] == data["built_at"] for e in first.values())
    data["built_at"] = "2030-01-01T00:20:00+00:00"   # a later pass, same data
    second = build(data, "d2")
    assert {u: e["lastmod"] for u, e in second.items()} == {u: e["lastmod"] for u, e in first.items()}, \
        "a page was dated anew though nothing on it changed"
    target = next(p for p in data["state_pages"] if p["n"] >= 3)   # one that is in the sitemap
    target["receipt"] = target["receipt"].replace(":", ": changed,", 1)
    data["built_at"] = "2030-01-01T00:40:00+00:00"
    third = build(data, "d3")
    moved = sorted(u for u in third if third[u]["lastmod"] != second[u]["lastmod"])
    assert moved == [f"https://aifearreport.com/states/{target['slug']}/"], f"dated anew: {moved}"
    lastmods = re.findall(r"<lastmod>([^<]+)</lastmod>", (tmp / "d3" / "sitemap.xml").read_text())
    assert sorted(set(lastmods)) == sorted({first[next(iter(first))]["lastmod"], data["built_at"]}), set(lastmods)
    # A page that leaves the site is announced once and then forgotten.
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "site"))
    import build as sitebuild
    gone = sitebuild.dated([], {"https://aifearreport.com/org/x/": {"hash": "a", "lastmod": "t0", "sent": "t0",
                                                                   "pending": None, "index": True}},
                           "t1", "https://aifearreport.com")
    assert gone["https://aifearreport.com/org/x/"]["gone"] and gone["https://aifearreport.com/org/x/"]["pending"] == "t1"
    gone["https://aifearreport.com/org/x/"]["pending"] = None   # announced
    assert sitebuild.dated([], gone, "t2", "https://aifearreport.com") == {}, "an announced removal was kept"
    print("a page is dated when what it says changes, and only then: ok")


def check_indexnow():
    """Changed pages are announced once they have settled, no page more often than every six hours,
    a refusal waits instead of hammering, and nothing here touches the network."""
    from pipeline import indexnow
    room = pathlib.Path(tempfile.mkdtemp())
    path, key, site = room / "pages.json", "0123456789abcdef0123456789abcdef", "https://aifearreport.com"
    t0 = dt.datetime(2030, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    stamp = t0.isoformat(timespec="seconds")
    path.write_text(json.dumps({
        f"{site}/": {"hash": "a", "lastmod": stamp, "pending": stamp, "sent": None, "index": True},
        f"{site}/states/": {"hash": "b", "lastmod": "2029-12-01T00:00:00+00:00", "pending": None,
                            "sent": "2029-12-01T00:00:00+00:00", "index": True},
        f"{site}/org/gone/": {"hash": None, "lastmod": stamp, "pending": stamp, "sent": None, "gone": True,
                              "index": False}}))
    calls = []

    def ok(body):
        calls.append(body)
        return 202, None

    indexnow.run(path, site, key, now=t0 + dt.timedelta(minutes=5), send=ok)
    assert not calls, "a change was announced before the published page could settle"
    later = t0 + dt.timedelta(minutes=21)
    line = indexnow.run(path, site, key, now=later, send=ok)
    assert len(calls) == 1 and calls[0]["host"] == "aifearreport.com" and calls[0]["key"] == key, calls
    assert calls[0]["keyLocation"] == f"{site}/{key}.txt"
    assert sorted(calls[0]["urlList"]) == [f"{site}/", f"{site}/org/gone/"], calls[0]["urlList"]
    rec = json.loads(path.read_text())
    assert rec[f"{site}/"]["pending"] is None and rec[f"{site}/"]["sent"] == later.isoformat(timespec="seconds"), line
    # the front page changes again an hour on: it waits out six hours from the last announcement
    rec[f"{site}/"]["pending"] = (later + dt.timedelta(hours=1)).isoformat(timespec="seconds")
    path.write_text(json.dumps(rec))
    indexnow.run(path, site, key, now=later + dt.timedelta(hours=2), send=ok)
    assert len(calls) == 1, "a page was announced twice inside six hours"
    indexnow.run(path, site, key, now=later + dt.timedelta(hours=6, minutes=1), send=ok)
    assert len(calls) == 2 and calls[1]["urlList"] == [f"{site}/"], calls[-1]

    def refused(body):
        calls.append(body)
        return 429, "7200"

    rec = json.loads(path.read_text())
    rec[f"{site}/states/"]["pending"] = (later + dt.timedelta(hours=7)).isoformat(timespec="seconds")
    path.write_text(json.dumps(rec))
    now = later + dt.timedelta(hours=8)
    indexnow.run(path, site, key, now=now, send=refused)
    assert json.loads(path.read_text())[f"{site}/states/"]["pending"], "a refused announcement was marked sent"
    n = len(calls)
    indexnow.run(path, site, key, now=now + dt.timedelta(minutes=30), send=ok)
    assert len(calls) == n, "it asked again inside the wait the endpoint gave"
    indexnow.run(path, site, key, now=now + dt.timedelta(hours=2, minutes=1), send=ok)
    assert calls[-1]["urlList"] == [f"{site}/states/"], calls[-1]
    assert "not set up" in indexnow.run(path, site, None, send=ok), "it ran without a key"
    print("IndexNow hears about changed pages once they settle, at most every six hours: ok")


def check_bill_links():
    """A bill links to its legislature's own page, not to Open States.

    Open States' bill pages now redirect into Plural's app, which is a blank page without script and
    often with it, and a reader took its copied text for this site's reading of the bill. Its records
    carry the legislature's links as sources, alongside every data feed, search page and sponsor page
    the scraper read. The cases are the ones its scrapers record, state by state.
    """
    from pipeline import collect_openstates as cos
    from pipeline.common import read_link

    def pick(ident, sources, versions=()):
        return cos.pick_link({"identifier": ident, "sources": [{"url": u, "note": n} for u, n in sources],
                              "versions": list(versions)})
    cases = {
        "New York: the API first, then the Senate's page and the Assembly's": (
            pick("S 10701", [("https://legislation.nysenate.gov/api/3/bills/2025/S10701", ""),
                             ("https://www.nysenate.gov/legislation/bills/2025/S10701", ""),
                             ("https://assembly.state.ny.us/leg/?default_fld=&bn=S10701&term=2025", "")]),
            "https://www.nysenate.gov/legislation/bills/2025/S10701"),
        "Texas: the FTP history file, then the bill's history page": (
            pick("HB 149", [("ftp://ftp.legis.state.tx.us/bills/89R/billhistory/house_bills/HB00100_HB00199/HB 149.xml", ""),
                            ("https://capitol.texas.gov/BillLookup/History.aspx?LegSess=89R&Bill=HB149", "")]),
            "https://capitol.texas.gov/BillLookup/History.aspx?LegSess=89R&Bill=HB149"),
        "Connecticut: a CSV on FTP, then the status page": (
            pick("SB 2", [("ftp://ftp.cga.ct.gov/pub/data/bill_info.csv", ""),
                          ("https://www.cga.ct.gov/asp/cgabillstatus/cgabillstatus.asp?selBillType=Bill&which_year=2025&bill_num=2", "")]),
            "https://www.cga.ct.gov/asp/cgabillstatus/cgabillstatus.asp?selBillType=Bill&which_year=2025&bill_num=2"),
        "Nebraska: the day's listing first, then the bill": (
            pick("LB 504", [("https://nebraskalegislature.gov/bills/search_by_date.php?SessionDay=2025-01-17&special=1", ""),
                            ("https://nebraskalegislature.gov/bills/view_bill.php?DocumentID=58210", "")]),
            "https://nebraskalegislature.gov/bills/view_bill.php?DocumentID=58210"),
        "New Jersey: the bill search page for the bill, and a database index": (
            pick("S 3611", [("https://www.njleg.state.nj.us/bill-search/2024/S3611", ""),
                            ("https://pub.njleg.state.nj.us/leg-databases/", "")]),
            "https://www.njleg.state.nj.us/bill-search/2024/S3611"),
        "Georgia: two API sources marked as such, then the page": (
            pick("SB 9", [("https://www.legis.ga.gov/api/legislation/detail/69923", "api"),
                          ("https://www.legis.ga.gov/api/legislation/list", "api"),
                          ("https://www.legis.ga.gov/legislation/69923", "")]),
            "https://www.legis.ga.gov/legislation/69923"),
        "North Dakota: the page, then the session's JSON": (
            pick("HB 1167", [("https://ndlegis.gov/assembly/69-2025/regular/bill-overview/bo1167.html", "HTML bill detail page"),
                             ("https://ndlegis.gov/api/assembly/69-2025/data/bills.json", "JSON page of session bills")]),
            "https://ndlegis.gov/assembly/69-2025/regular/bill-overview/bo1167.html"),
        "Mississippi: the bill's text, then sponsor and status files": (
            pick("HB 1234", [("https://billstatus.ls.state.ms.us/documents/2026/html/HB/1200-1299/HB1234IN.htm", ""),
                             ("http://billstatus.ls.state.ms.us/2026/pdf/House_authors/Smith.xml", ""),
                             ("https://billstatus.ls.state.ms.us/2026/pdf/history/HB/HB1234.xml", "")]),
            "https://billstatus.ls.state.ms.us/documents/2026/html/HB/1200-1299/HB1234IN.htm"),
        "DC: the bulk data listing, then the Council's page": (
            pick("B26-0491", [("https://lims.dccouncil.gov/api/v2/PublicData/BulkData/1/26", ""),
                              ("https://lims.dccouncil.gov/Legislation/B26-0491", "")]),
            "https://lims.dccouncil.gov/Legislation/B26-0491"),
        "Arizona: one page, keyed by an internal number": (
            pick("HB 2175", [("https://apps.azleg.gov/BillStatus/BillOverview/83456?SessionId=128", "")]),
            "https://apps.azleg.gov/BillStatus/BillOverview/83456?SessionId=128"),
        "Alabama: only the search form, so the bill's latest text": (
            pick("SB 88", [("https://alison.legislature.state.al.us/bill-search", "")],
                 [{"note": "Introduced", "date": "2026-01-10",
                   "links": [{"url": "https://alison.legislature.state.al.us/files/pdfs/SearchableInstruments/2026RS/SB88-int.pdf",
                              "media_type": "application/pdf"}]},
                  {"note": "Engrossed", "date": "2026-02-10",
                   "links": [{"url": "https://alison.legislature.state.al.us/files/pdfs/SearchableInstruments/2026RS/SB88-eng.pdf",
                              "media_type": "application/pdf"}]}]),
            "https://alison.legislature.state.al.us/files/pdfs/SearchableInstruments/2026RS/SB88-eng.pdf"),
        "Rhode Island: the site's front page only, and no text either": (
            pick("H 5100", [("https://status.rilegislature.gov/", "")]), ""),
        "Vermont: a listing of the year's bills, then the bill": (
            pick("H.123", [("http://legislature.vermont.gov/bill/loadBillsIntroduced/2026/", ""),
                           ("http://legislature.vermont.gov/bill/status/2026/H.123", "")]),
            "http://legislature.vermont.gov/bill/status/2026/H.123"),
    }
    for what, (got, want) in cases.items():
        assert got == want, f"{what}: picked {got!r}, not {want!r}"
    assert cos.pick_link({"identifier": "HB 1"}) == "", "no sources and no versions is no page"
    # How a link is described under the bill, and the Open States fallback owned up to.
    assert read_link({"source_url": "https://www.palegis.us/legislation/bills/2025/hb2800", "url": "https://openstates.org/x"}) == \
        ("https://www.palegis.us/legislation/bills/2025/hb2800", "palegis.us")
    assert read_link({"source_url": "", "url": "https://openstates.org/pa/bills/2025-2026/HB2800/"})[1] == "Open States"
    assert read_link({"source_url": None, "url": "https://www.congress.gov/bill/119th-congress/house-bill/1"})[1] == "congress.gov"
    assert read_link({"source_url": "https://alison.legislature.state.al.us/files/SB88-eng.pdf", "url": "u"})[1] == \
        "alison.legislature.state.al.us, PDF"

    # Stored with a search result that carries sources; an unchanged bill seen again is given its page;
    # a record without sources leaves what is there.
    db = connect(":memory:")
    b = {"id": "ocd-bill/abc", "identifier": "HB 2800", "session": "2025-2026", "title": "AI risk",
         "jurisdiction": {"id": "ocd-jurisdiction/country:us/state:pa/government", "name": "Pennsylvania"},
         "updated_at": "2026-09-20T00:00:00", "openstates_url": "https://openstates.org/pa/bills/2025-2026/HB2800/",
         "sources": [{"url": "https://www.palegis.us/legislation/bills/2025/hb2800"}]}
    cos.store(db, {k: v for k, v in b.items() if k != "sources"})
    assert db.execute("SELECT source_url FROM measures").fetchone()[0] is None
    cos.store(db, b)  # same updated_at: not rewritten, but its page is filled in
    assert db.execute("SELECT source_url FROM measures").fetchone()[0] == "https://www.palegis.us/legislation/bills/2025/hb2800"
    cos.store(db, {**{k: v for k, v in b.items() if k != "sources"}, "updated_at": "2026-09-21T00:00:00"})
    assert db.execute("SELECT source_url FROM measures").fetchone()[0] == "https://www.palegis.us/legislation/bills/2025/hb2800", \
        "a record without its sources erased the page already found"

    # The bills on the site stored without a page are asked about twenty at a time, by jurisdiction and
    # session, labelled ones first; one the answer leaves out is marked asked and keeps its address.
    ldb = connect(":memory:")
    for i in range(45):
        mid = f"os-pa{i}"
        upsert(ldb, "measures", {"id": mid, "kind": "bill", "jurisdiction": "pa", "jurisdiction_name": "Pennsylvania",
                                 "session": "2025-2026", "identifier": f"HB {i + 1}", "title": "Artificial intelligence",
                                 "status": "pending", "introduced_date": "2025-03-01",
                                 "latest_action_date": f"2025-{3 + i % 9:02d}-01",
                                 "url": f"https://openstates.org/pa/bills/2025-2026/HB{i + 1}/", "source": "Open States"})
        ldb.execute("INSERT INTO tag_runs(target, text_hash, ai_related, tagged_at) VALUES(?, 'h', 1, 'now')", (mid,))
    upsert(ldb, "measures", {"id": "os-ny1", "kind": "bill", "jurisdiction": "ny", "jurisdiction_name": "New York",
                             "session": "2025-2026", "identifier": "S 1", "title": "Artificial intelligence",
                             "status": "pending", "introduced_date": "2025-03-01", "url": "https://openstates.org/ny/1",
                             "source": "Open States"})
    ldb.execute("INSERT INTO tag_runs(target, text_hash, ai_related, tagged_at) VALUES('os-ny1', 'h', 1, 'now')")
    ldb.execute("INSERT INTO tags(target, kind, value, evidence) VALUES('os-ny1', 'control', 'mandatory-reporting', 'q')")
    # HB 2's summary was cut at the old 6,000 characters; HB 3's is short and whole
    ldb.execute("UPDATE measures SET summary=? WHERE id='os-pa1'", ("x" * 6000,))
    ldb.execute("UPDATE measures SET summary='A short one.' WHERE id='os-pa2'")
    ldb.commit()
    whole = {"HB 2": [{"abstract": "a" * 5000}, {"abstract": "b" * 4000 + " END"}],
             "HB 3": [{"abstract": "Something longer than the summary on file."}]}
    asked = []

    class Links:
        def json(self, url, params=None, **kw):
            asked.append(params)
            assert len(params["identifier"]) <= 20 and "sources" in params["include"], params
            assert "abstracts" in params["include"], "the backfill no longer brings the whole summary"
            code = params["jurisdiction"].split(":")[-1].split("/")[0]
            out = []
            for ident in params["identifier"]:
                if ident == "HB 7":
                    continue  # one the answer leaves out
                n = ident.split()[-1]
                out.append({"id": f"ocd-bill/{code}{int(n) - 1 if code == 'pa' else n}", "identifier": ident,
                            "jurisdiction": {"id": params["jurisdiction"]},
                            "sources": [{"url": f"https://www.example-{code}.gov/bill/{n}"}],
                            "abstracts": whole.get(ident, [])})
            return {"results": out}
    used, found, late = cos.fill_links(ldb, Links(), 2, __import__("time").time())
    assert used == 2 and not late, (used, late)
    assert asked[0]["jurisdiction"].endswith("state:ny/government") and asked[0]["identifier"] == ["S 1"], \
        "the labelled bill was not asked about first"
    assert asked[1]["session"] == "2025-2026" and len(asked[1]["identifier"]) == 20, asked[1]
    used, found, late = cos.fill_links(ldb, Links(), 10, __import__("time").time())
    rows = dict(ldb.execute("SELECT id, source_url FROM measures").fetchall())
    assert all(v is not None for v in rows.values()), "a bill on the site was left unasked"
    assert rows["os-pa6"] == "", "the one the answer left out was not marked asked"
    assert rows["os-pa0"] == "https://www.example-pa.gov/bill/1" and rows["os-ny1"] == "https://www.example-ny.gov/bill/1"
    assert cos.fill_links(ldb, Links(), 10, __import__("time").time())[0] == 0, "asked again about bills already asked about"
    sums = dict(ldb.execute("SELECT id, summary FROM measures WHERE id IN ('os-pa1', 'os-pa2')").fetchall())
    assert len(sums["os-pa1"]) == 9005 and sums["os-pa1"].endswith(" END"), "a summary the old cut left short stayed short"
    assert sums["os-pa2"] == "A short one.", "a whole summary was rewritten"

    # The tagger and the check read all of a long summary, not its first few thousand characters.
    from pipeline import check, tag
    from pipeline.common import SUMMARY_MAX
    assert SUMMARY_MAX >= 50000
    long = "Requires state agencies to inventory their AI systems. " + "More provisions. " * 1500 + "TAIL"
    tdb = connect(":memory:")
    upsert(tdb, "measures", {"id": "m-long", "kind": "bill", "jurisdiction": "ky", "jurisdiction_name": "Kentucky",
                             "session": "2025RS", "identifier": "SB 4", "title": "Artificial intelligence",
                             "summary": long, "status": "passed", "introduced_date": "2025-02-01",
                             "url": "https://openstates.org/ky/bills/2025RS/SB4/", "source": "Open States"})
    tdb.commit()
    read = [t for t in tag.targets(tdb, 10)[0] if t[1] == "m-long"]
    assert read and read[0][2].rstrip().endswith("TAIL") and read[0][4].rstrip().endswith("TAIL"), "the tagger reads a cut summary"
    controls = {c["slug"]: c for c in config("controls")}
    m = dict(tdb.execute("SELECT * FROM measures").fetchone())
    assert "TAIL" in check.question(m, "control", "mandatory-reporting", "q", controls), "the check reads a cut summary"
    print("bills link to the legislature's own page, found for the ones on file too, and summaries are read whole: ok")


def check_use_restrictions():
    """A ban on a use of AI is a control, and the tagger and the check are told what one is.

    The ten controls counted licences, caps, reports, labels and new powers, and no ban: a bill
    that forbade landlords to set rents by algorithm, or made an AI-made sexual image a crime, put
    the people using AI under a new rule and counted as adding no control at all.
    """
    from pipeline import brief, check
    cs = {c["slug"]: c for c in config("controls")}
    c = cs["use-restrictions"]
    assert {"slug", "name", "chip", "definition", "head", "pattern"} <= set(c), c
    assert "government's own use" in c["definition"], "the definition lets the government's own use in"
    assert "use-restrictions" in check.ORDER and "use-restrictions" in check.CONTROL_NOTES
    assert "labels and notices" in check.CONTROL_NOTES["use-restrictions"], "the note does not keep labels out"
    assert brief.WEIGHT.get("use-restrictions"), "the brief does not know what a ban is worth"
    print("a ban or limit on a use of AI counts as a control: ok")


def check_candidates():
    """Fears the report does not follow are measured beside the ones it does, the same way.

    The list of fears is fixed so its labels mean something, and a fixed list can fall behind: a
    fear that grows after it was settled is invisible however many bills cite it. candidates.py
    counts every candidate and every followed fear on the index's four channels by the same rules
    and scores them with the index's formula, and the weekly list names a candidate that has come
    to outscore a fear the site follows.
    """
    import datetime as _dt
    from pipeline import candidates, review
    from pipeline.common import kv_set
    from pipeline.export import recent_quarters
    db = connect(":memory:")
    bills = [("m1", "Artificial intelligence; prior authorization; health insurers"),
             ("m2", "Artificial intelligence in utilization review by health plans"),
             ("m3", "Deepfakes in elections"),
             ("m4", "Tractor safety")]
    for mid, title in bills:
        upsert(db, "measures", {"id": mid, "kind": "bill", "jurisdiction": "tx" if mid != "m2" else "ca",
                                "jurisdiction_name": "X", "session": "2025", "identifier": f"SB {mid[1]}",
                                "title": title, "summary": "", "status": "pending", "introduced_date": "2025-03-01",
                                "url": f"https://example.com/{mid}", "source": "test", "first_seen": iso()})
        db.execute("INSERT INTO tag_runs(target, text_hash, ai_related, tagged_at) VALUES(?, 'h', ?, 'now')",
                   (mid, 0 if mid == "m4" else 1))
    y, q = sorted(recent_quarters(4, _dt.date.today()))[-1]
    for i, issues in enumerate(["AI in prior authorization", "AI and health care claims", "deepfake robocalls"]):
        upsert(db, "lobbying", {"id": f"l{i}", "client": f"C{i}", "client_key": f"c{i}", "registrant": "R",
                                "year": y, "quarter": f"Q{q}", "amount": 10000, "issues": issues, "posted": "2026-01-01"})
    today = _dt.date.today()
    for d in range(30):
        day = (today - _dt.timedelta(days=d + 1)).isoformat()
        db.execute("INSERT INTO series VALUES(?,?,?)", ("news:deepfakes", day, 5))
        db.execute("INSERT INTO series VALUES(?,?,?)", ("cand-news:ai-care-decisions", day, 20))
    db.commit()
    followed = [{"slug": "deepfakes", "name": "Deepfakes", "match": ["deep ?fake"], "keywords": ["deepfake*"],
                 "wikipedia": []}]
    cands = [{"slug": "ai-care-decisions", "name": "AI deciding care", "match": ["artificial intelligence",
              "prior authori[sz]ation|utilization review"], "keywords": ["prior authorization", "health care"],
              "wikipedia": []}]
    rows = {r["slug"]: r for r in candidates.measure(db, followed, cands)}
    care, fakes = rows["ai-care-decisions"], rows["deepfakes"]
    assert (care["bills"], care["states"], care["filings"], care["news30"]) == (2, 2, 2, 600), care
    assert (fakes["bills"], fakes["filings"], fakes["news30"]) == (1, 1, 150), fakes
    assert care["score"] > fakes["score"] and care["kind"] == "candidate", rows
    kv_set(db, "candidates:report", {"rows": list(rows.values()), "lowest_followed": fakes["score"]})
    body, _ = review.build(db, today)
    assert "Fears the report does not follow that outscore one it does: 1" in body and "AI deciding care" in body, \
        "a candidate that outscores a followed fear is not on the weekly list"
    print("fears the report does not follow are measured beside the ones it does: ok")


def check_government_own_use():
    """A control binds people or organizations outside government, and an office has to gain power
    over them.

    Kentucky's SB 4 sets rules for the state's own use of AI and creates a committee to oversee it. It
    counted as new agency powers and mandatory reporting, and the committee led the front page's
    "goes to" line under deepfakes. Rules a government sets for itself bind the government, not the
    people the site counts controls over. The tagger, the check and the definitions all say so, and
    every control and office label is asked again under that wording, once.
    """
    from pipeline import check, tag
    import inspect
    assert "government bodies in their own use of AI" in check.NOT_THIS, "the check no longer rules out government's own use"
    assert "only government bodies' own use of AI" in check.OFFICE_QUESTION
    assert "which can include a government agency" not in check.CONTROL_NOTES["mandatory-reporting"]
    assert "own use of AI" in check.CONTROL_NOTES["new-agency-powers"]
    assert "bind the government itself" in inspect.getsource(tag.propose)
    # Preemption binds the states themselves, in the laws they may pass. The rule must not reach it.
    assert "counts only if the measure itself would impose it on people or organizations outside government" \
        not in " ".join(inspect.getsource(tag.propose).split()), "the tagger's rule would rule out preemption"
    assert "does not apply to it" in check.CONTROL_NOTES["preemption"]
    defs = {c["slug"]: c["definition"] for c in config("controls")}
    assert "outside government" in defs["mandatory-reporting"] and "own use of AI" in defs["new-agency-powers"]
    db = connect(":memory:")
    rows = [("m1", "control", "new-agency-powers", "yes"), ("m1", "agency", "Kentucky AI Governance Committee", "yes"),
            ("m1", "fear", "deepfakes", "yes"), ("m2", "control", "mandatory-reporting", "no")]
    for target, kind, value, verdict in rows:
        db.execute("INSERT INTO checks(target, kind, value, evidence, verdict, reason, model, checked_at) "
                   "VALUES(?,?,?,?,?,'r','m','t')", (target, kind, value, "q", verdict))
    db.commit()
    assert check.reask_narrowed(db) == 2
    left = {(r[0], r[1], r[2]) for r in db.execute("SELECT target, kind, verdict FROM checks")}
    assert left == {("m1", "fear", "yes"), ("m2", "control", "no")}, left
    assert check.reask_narrowed(db) == 0, "asked again a second time"
    print("rules for the government's own use of AI are not counted as controls: ok")


def check_loss_of_control_reread():
    """A measure that has frontier developers assess and report catastrophic risk cites loss of control.

    California's SB 53 did exactly that and was refused the fear, because "catastrophic risk, as
    defined" read to the check as a defined term. The fear's definition names catastrophic risk, so the
    check is told as much for measures, and the measures it refused are asked once more. Statements
    keep their verdicts.
    """
    from pipeline import check
    fears = {f["slug"]: f for f in config("fears")}
    assert "catastrophic" in fears["loss-of-control"]["definition"]
    m = {"jurisdiction_name": "California", "identifier": "SB 53", "title": "Artificial intelligence models",
         "summary": "requires a summary of any assessment of catastrophic risk, as defined"}
    asked = check.question(m, "fear", "loss-of-control", "catastrophic risk, as defined", {}, fears)
    assert check.FEAR_NOTES["loss-of-control"] in asked, "the check is not told what the fear includes"
    post = {"post": True, "org": "Org", "entity": "org", "title": "t", "summary": "catastrophic risk"}
    assert check.FEAR_NOTES["loss-of-control"] not in check.question(post, "fear", "loss-of-control", "q", {}, fears)
    db = connect(":memory:")
    for target, value, verdict in (("m1", "loss-of-control", "no"), ("post:1", "loss-of-control", "no"),
                                   ("m2", "deepfakes", "no"), ("m3", "loss-of-control", "yes")):
        db.execute("INSERT INTO checks(target, kind, value, evidence, verdict, reason, model, checked_at) "
                   "VALUES(?,'fear',?,'q',?,'r','m','t')", (target, value, verdict))
    db.commit()
    assert check.recheck_fears(db) == 1
    assert {r[0] for r in db.execute("SELECT target FROM tags WHERE kind='fear'")} == {"m1"}
    assert {r[0] for r in db.execute("SELECT target FROM checks")} == {"post:1", "m2", "m3"}
    assert check.recheck_fears(db) == 0, "asked again a second time"
    print("a measure acting on catastrophic risk is asked again about loss of control: ok")


def check_every_page_asks(dist, db, day):
    """The brief and the pitch-in are on every page, in that order, and the card is real.

    They used to live in the front page's own template, so a reader who arrived on a fear page
    from a link reached the end of the report with nothing to do. Moved into base.html, which is
    easy to undo by accident, hence this.

    The card is checked as a path, because pass.sh copies state/brief into dist/brief after the
    build: an <img> naming an edition that was never written is a broken picture on every page at
    once, not one. And nothing on any page may load from another host. The obvious way to show an
    X account is X's own timeline widget, which is a third-party script on every page, a set of
    X's cookies for every reader, and a box that does not match this site in either theme.
    """
    pages = sorted(dist.rglob("*.html"))
    assert len(pages) >= 5, f"only {len(pages)} pages built"
    for p in pages:
        t, where = p.read_text(), p.relative_to(dist).as_posix()
        assert 'id="brief"' in t, f"{where} has no daily brief section"
        assert 'id="keep"' in t, f"{where} has no pitch-in section"
        assert t.index('id="brief"') < t.index('id="keep"'), \
            f"{where} puts the pitch-in above the brief"
        assert 'class="brief-card"' in t, f"{where} names an edition but shows no card"
        assert f"/brief/{day}.png" in t, f"{where} points at the wrong edition"
        for m in re.finditer(r'<(?:img|script|iframe)[^>]+src="(?:https?:)?//([^"/]+)', t):
            # One exception, named rather than assumed: the counter, which sets no cookie and
            # follows nobody between sites. Anything else arriving here is a new third party
            # watching every reader of a site about who is watching, and has to be argued for.
            assert m.group(1) in ALLOWED_HOSTS, \
                f"{where} loads from {m.group(1)}; this site ships self-contained"
    # The pitch-in carries money, so three more things about it are held here rather than hoped.
    site_cfg = json.loads((ROOT / "config" / "site.json").read_text())
    addr = site_cfg.get("donate_btc", "")
    home = (dist / "index.html").read_text()
    if site_cfg.get("donate_url"):
        # On the button, not anywhere: the footer links to the same checkout, and a check for the
        # address on the page passed with the button pointing nowhere.
        assert re.search(r'<a class="pay-go" href="' + re.escape(site_cfg["donate_url"]) + '"', home), \
            "the card button does not go to the checkout"
    if addr:
        assert f'href="bitcoin:{addr}"' in home, "the Bitcoin address is not a wallet link"
        assert f'data-copy="{addr}"' in home, "the copy button copies something other than the address"
    # The copy button said Copied on success, on failure, and where no clipboard exists at all.
    # For an address that sends the reader to paste whatever was there before. It may only say
    # so on the path where the write resolved.
    js = (ROOT / "site" / "templates" / "base.html").read_text()
    assert "then(done, done)" not in js and "else done()" not in js, \
        "the copy button claims success when the copy failed"
    assert 'then(function () { say("Copied"); }, failed)' in js, \
        "the copy button no longer separates success from failure"

    # Three states, because the card comes from two places. With the key, the headline is known.
    # Without it, the table still knows which days were published, which is every edition written
    # before the key existed. With neither, the section has to render without a card rather than
    # point at a picture that was never made.
    db.execute("DELETE FROM kv WHERE key='brief:latest'")
    db.execute("INSERT OR REPLACE INTO brief(target, edition, sentence, evidence, office, "
               "controls, written_at) VALUES('t', '2026-09-19', 's', 'q', '', '', '')")
    db.commit()
    fell_back = export.brief_latest(db)
    assert fell_back and fell_back["date"] == "2026-09-19", \
        f"the table should still name an edition: {fell_back}"
    assert fell_back["alt"].strip(), "a card with no headline still needs alt text"
    db.execute("DELETE FROM brief")
    db.commit()
    assert export.brief_latest(db) is None, "a database with no edition at all must report no card"
    print(f"the brief and the pitch-in are on all {len(pages)} pages, brief first: ok")


def check_icon_centred():
    """The tab icon is one mark, centred, at every size a browser asks for.

    What shipped was the avatar shrunk: a red field, a cream square inset in it, then the letters
    inside that. Three shapes inside sixteen pixels, and the letters landed 1.5px right and 1px
    low of the cream square, which is 7.5% of a 20px box and plainly wrong in a tab. The head
    also carried an inline SVG drawn in Arial Narrow, so the icon changed face depending on which
    file the browser picked. One renderer, one face, measured off the pixels at each size.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "site"))
    from PIL import Image
    import brand

    VERM, PAPER = (179, 50, 31), (241, 237, 227)
    with tempfile.TemporaryDirectory() as tmp:
        for px in (16, 32, 180, 512):
            path = pathlib.Path(tmp) / f"{px}.png"
            brand.icon(path, size=px)
            im = Image.open(path).convert("RGB")
            assert im.size == (px, px), f"{px}: {im.size}"
            a = im.load()
            xs, ys = [], []
            for y in range(px):
                for x in range(px):
                    if sum(abs(a[x, y][i] - VERM[i]) for i in range(3)) > 80:
                        xs.append(x); ys.append(y)
            assert xs, f"{px}: nothing drawn on the field"
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            # The field is solid to the edge: no square inset inside another square.
            assert sum(abs(a[0, 0][i] - VERM[i]) for i in range(3)) < 30, \
                f"{px}: the corner is not the field colour, so something is inset"
            assert x0 > 0 and y0 > 0 and x1 < px - 1 and y1 < px - 1, \
                f"{px}: the letters touch the edge"
            off_h = (x0 - (px - 1 - x1)) / 2
            off_v = (y0 - (px - 1 - y1)) / 2
            # Never more than a pixel out, and what slack there is goes up and to the left,
            # which is the same optical offset the badge uses. The upper bound is 0, not 0.5:
            # at 16 pixels the only choices are half a pixel high or half a pixel low, and a
            # bound of 0.5 accepts both, which is how the first version of this check passed
            # over the very arithmetic it was written for.
            assert -0.02 * px - 1 <= off_h <= 0, f"{px}: letters {off_h:+.1f}px off across"
            assert -0.02 * px - 1 <= off_v <= 0, f"{px}: letters {off_v:+.1f}px off down"
            cap = (y1 - y0 + 1) / px
            assert 0.5 <= cap <= 0.68, f"{px}: the letters are {cap:.0%} of the frame"
    # and the head must not offer a second icon drawn by some other means
    head = (ROOT / "site" / "templates" / "base.html").read_text()
    assert "data:image/svg+xml" not in head, "the head still carries an inline SVG icon"
    for px in (16, 32):
        assert f"/icon/{px}.png" in head, f"the head does not offer the {px}px icon"
    print("the tab icon is one centred mark at every size: ok")


def check_share_card_fits():
    """The card gives way rather than running through its own rules.

    A two-line name with a two-line line of counts under it overflowed by sixty pixels: the top
    of the figure was cut off against the rule under the mark and the counts ran through the rule
    above the address. The figure steps down until the block fits.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "site"))
    from PIL import Image
    import tempfile as _tempfile
    import build as sitebuild
    room = pathlib.Path(_tempfile.mkdtemp())
    cases = {
        "short": ("3", "AI therapists", "3 of 100"),
        "long": ("1,420", "Bills, rules and orders about artificial intelligence since January 2025",
                 "1,420 measures tracked across 51 jurisdictions, 513 of them carrying at least "
                 "one new government control held by 180 named offices"),
    }
    for (name, (big, label, sub)), tall in [(c, t) for c in cases.items() for t in (630, 675)]:
        path = room / f"{name}-{tall}.png"
        sitebuild.share_card(path, big, label, sub, h=tall)
        im = Image.open(path).convert("RGB")
        px = im.load()
        w, h = im.size
        assert (w, h) == (1200, tall), f"{name}: card is {w} by {h}"

        def inked(y):
            return sum(1 for x in range(8, w - 8)
                       if sum(abs(px[x, y][i] - c) for i, c in enumerate((241, 237, 227))) > 90)

        # The content lives between the rule under the mark and the rule above the address. Find
        # where it actually reaches, rather than probing rows and hoping the overflow lands on one.
        band, foot = 118, tall - 74
        rows = [y for y in range(band + 3, foot - 2) if inked(y) > 3]
        assert rows, f"{name}: the card came out empty between its rules"
        assert rows[0] >= band + 18, \
            f"{name}: content reaches y={rows[0]}, hard against the rule under the mark at {band}"
        assert rows[-1] <= foot - 10, \
            f"{name}: content reaches y={rows[-1]}, hard against the rule above the address at {foot}"
    print("a share card gives way rather than running through its rules: ok")


def check_office_names():
    """One office, one name. Spelled two ways it was counted twice on the front page on 25 September:
    the Pennsylvania Attorney General, and California's medical board as the California Medical
    Board and the California Medical Board of California. An office the tagger could only describe
    was listed as "Minnesota (state agency overseeing AI independent verification organizations)"."""
    from pipeline.common import office_label, name_key
    assert name_key(office_label("California Medical Board of California", "ca")) == \
        name_key(office_label("California Medical Board", "ca")), "one board counted as two"
    for raw, where, want in (
            ("Pennsylvania Office of Attorney General", "pa", "Pennsylvania Attorney General"),
            ("Virginia Office of the Attorney General", "va", "Virginia Attorney General"),
            ("Hawaii Department of the Attorney General", "hi", "Hawaii Attorney General"),
            ("Minnesota (state agency overseeing AI independent verification organizations)", "mn", ""),
            ("Texas Attorney General (Consumer Protection Division)", "tx", "Texas Attorney General"),
            ("Department of Commerce", "us", "U.S. Department of Commerce"),
            ("Department of Artificial Intelligence", "us", "U.S. Department of Artificial Intelligence"),
            ("U.S. Department of Labor", "us", "U.S. Department of Labor"),
            ("Federal Trade Commission", "us", "Federal Trade Commission"),
            ("Colorado civil rights division", "co", "Colorado Civil Rights Division"),
            ("California health care professional licensing boards", "ca",
             "California health care professional licensing boards"),
            ("Oregon State Department of Energy", "or", "Oregon Department of Energy"),
            ("West Virginia Department of Commerce", "wv", "West Virginia Department of Commerce"),
            ("California district attorneys, county counsels and city attorneys", "ca", "California local prosecutors"),
            ("Iowa state government (enforcement authority for civil penalties)", "ia", ""),
            ("Ohio state agencies", "oh", ""),
            ("State government", "ca", ""),
            ("Medical Board of California", "ca", "California Medical Board"),
            ("California District Attorney", "ca", "California local prosecutors"),
            ("California County Counsel", "ca", "California local prosecutors"),
            ("Governor of Pennsylvania", "pa", "Pennsylvania Governor"),
            ("New Jersey Office of Secretary of Higher Education", "nj", "New Jersey Secretary of Higher Education"),
            ("University of California", "ca", "University of California"),
            ("Office of the Comptroller of the Currency", "us", "U.S. Office of the Comptroller of the Currency")):
        got = office_label(raw, where)
        assert got == want, f"{raw!r} is shown as {got!r}, not {want!r}"
    # "New this week" is the week the chart beside it steps through: the seven days through today.
    from pipeline.export import new_this_week
    today = dt.date(2026, 9, 25)
    for day, want in (("2026-09-18", False), ("2026-09-19", True), ("2026-09-25", True), ("2026-09-26", False), ("", False)):
        assert new_this_week({"introduced_date": day}, today) == want, f"{day} counted as new this week: {not want}"


def check_law_text():
    """A law whose official summary is a line is read from its enacted text, and a label quoting
    that text stays on. On 25 September New York's RAISE Act, whose whole summary is "Relates to the
    training and use of artificial intelligence frontier models; defines terms; establishes remedies
    for violations.", was on the site as a law that imposes nothing."""
    from pipeline import texts as T
    from pipeline import tag as tagging
    from pipeline import check as checking
    page = ("<html><head><title>x</title><script>var leak = 1</script></head><body>"
            "<p>BE IT ENACTED BY THE LEGISLATURE OF THE STATE:</p>"
            "<p>SECTION 1. A large developer shall <s>not</s> report each safety incident to the attorney "
            "general within seventy-two hours of learning of it.</p><p>SECTION 2. This act takes effect.</p>"
            + "<p>" + "Filler words for length. " * 80 + "</p></body></html>")
    text = T.usable(T.tidy(T.from_html(page)))
    flat = " ".join(text.split())
    assert "leak" not in flat and "shall not report" not in flat, "a script or struck-out words were read as law"
    assert "shall report each safety incident to the attorney general" in flat, flat[:300]
    shell = T.tidy(T.from_html("<html><body>" + "<p>Legislators Sessions Calendars Journals Bills Code</p>" * 30
                               + "</body></html>"))
    try:
        T.usable(shell)
        raise AssertionError("a legislature's page with no law on it was taken for the law")
    except ValueError:
        pass
    assert T.tidy("regu-\nlation of") == "regulation of"
    # New York's HTML numbers every line down the margin, as most PDFs do. With the numbers in, no
    # quote that ran across a line could be found, and the RAISE Act came out of its reading bare.
    lines = ["The People of the State of New York, represented in Senate and Assem-",
             "bly, do enact as follows:",
             "Section 1. A large developer shall disclose each safety incident af-",
             "fecting the frontier model to the division of homeland security and",
             "emergency services within seventy-two hours of learning of it."] + \
            [f"Section {i}. The attorney general may bring a civil action for a violation of this article." for i in range(2, 40)]
    numbered = "<pre>" + "\n".join(f"{i:>2} {l}" for i, l in enumerate(lines, 1)) + "</pre>"
    flat = " ".join(T.usable(T.clean(T.from_html(numbered))).split())
    assert "incident affecting the frontier model to the division of homeland security and emergency services" in flat, \
        "a margin line number was left inside the law's words"
    assert "Senate and Assembly, do enact" in flat, flat[:200]
    # A short bill on a legislature's page, whose menus outnumber the bill's lines.
    menus = "<p>" + "</p><p>".join(["Assembly Members", "Legislative Info", "Public Hearings", "Bill Search Home"] * 12) + "</p>"
    short_bill = menus + "<pre>" + "\n".join(f"{i:>2} {l}" for i, l in enumerate(lines[:8], 1)) + "</pre>" + menus
    assert "incident affecting the frontier model" in " ".join(T.clean(T.from_html(short_bill)).split()), \
        "the menus around a short bill outvoted its line numbers"
    # Words a bill strikes are printed in brackets, and they are not the law it makes.
    assert " ".join(T.clean("Title 13, Chapter 72, is repealed [May] July 1, [2025]\n2027.").split()) == \
        "Title 13, Chapter 72, is repealed July 1, 2027."
    # The Federal Register's text of one order opens with the cover of its part of the issue, which
    # lists other orders, and carries NULs and page markers that are not the order's words.
    register = ("[Federal Register Volume 90, Number 20 (Friday, January 31, 2025)]\n[Presidential Documents]\n"
                "[Pages 8741-8742]\n\n[[Page 8739]]\n\nExecutive Order 14181--Emergency Measures To Provide Water "
                "Resources in California\n\x00\x00Presidential Documents\x00\n\n[[Page 8741]]\n\nExecutive Order 14179 "
                "of January 23, 2025\n\nBy the authority vested in me as President, it is hereby ordered:\n"
                "Section 1. Purpose. The agencies shall\n\n[[Page 8742]]\n\nrevise their guidance.")
    order = T.clean(register)
    assert order.startswith("Executive Order 14179") and "Water" not in order and "\x00" not in order, order[:200]
    assert "The agencies shall\n\nrevise their guidance" in order, order
    # Run over every text on file each pass, so it has to leave its own result as it is: a text that
    # changed each time would be read again each time.
    for text in (T.from_html(numbered), register, "is repealed [May] July 1, [2025]\n2027.", T.from_html(page)):
        once = T.clean(text)
        assert T.clean(once) == once, f"cleaning twice changed the text: {once[:120]!r}"
    # A short law is still a law. Utah's SB 332 runs 255 words; a page with no enacting clause is not one.
    short = "Be it enacted by the Legislature of the state of Utah: Section 1. " + "The act is repealed July 1, 2027. " * 20
    assert T.usable(short) == short
    try:
        T.usable(short.replace("Be it enacted by the Legislature of the state of Utah:", "Bill status and history:"))
        raise AssertionError("a short page with no enacting clause was taken for a law")
    except ValueError:
        pass
    bill = {"versions": [
        {"date": "2025-01-10", "note": "Introduced", "links": [{"url": "https://x.gov/intro.pdf", "media_type": "application/pdf"}]},
        {"date": "2025-05-20", "note": "Enrolled", "links": [{"url": "https://x.gov/enr.pdf", "media_type": "application/pdf"},
                                                             {"url": "https://x.gov/enr.htm", "media_type": "text/html"}]},
        {"date": "2025-04-01", "note": "Engrossed", "links": [{"url": "https://x.gov/eng.htm", "media_type": "text/html"}]}]}
    assert T.text_links(bill) == ["https://x.gov/enr.htm", "https://x.gov/enr.pdf"], T.text_links(bill)
    line = ("Relates to the training and use of artificial intelligence frontier models; defines terms; "
            "establishes remedies for violations.")
    law = {"id": "os-raise", "status": "passed", "summary": line}
    assert T.thin(line) and T.reading(law, {"os-raise": text}) == text
    assert T.reading({**law, "status": "pending"}, {"os-raise": text}) == "", "a bill still moving was read from a text"
    assert T.reading({**law, "summary": line * 3}, {"os-raise": text}) == "", "a law with a summary was read from text"
    method = (ROOT / "site" / "templates" / "pages" / "method.html").read_text()
    assert f"under {T.THIN} words" in method, "the method page and the code disagree on when a law's text is read"

    tmp = pathlib.Path(tempfile.mkdtemp())
    db = connect(tmp / "t.db")
    upsert(db, "measures", {"id": "os-raise", "kind": "bill", "jurisdiction": "ny", "jurisdiction_name": "New York",
                            "session": "2025-2026", "identifier": "S 6953",
                            "title": "Relates to the training and use of artificial intelligence frontier models",
                            "summary": line, "status": "passed", "latest_action": "SIGNED CHAP.699",
                            "latest_action_date": "2025-12-19", "introduced_date": "2025-03-27",
                            "url": "https://example.com/raise", "sponsors": "Sample", "source": "Open States",
                            "updated": "2025-12-19", "first_seen": iso()})
    db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", ("os-raise", "old", 1, iso()))
    db.execute("INSERT INTO texts(target, url, text, fetched, tries) VALUES(?,?,?,?,0)",
               ("os-raise", "https://x.gov/enr.htm", text, iso()))
    db.commit()
    work, _ = tagging.targets(db, 10)
    doc = next(w[2] for w in work if w[1] == "os-raise")
    assert "Text of the law:" in doc and "seventy-two hours" in doc, "the tagger was not given the law's text"
    quote = "shall report each safety incident to the attorney general within seventy-two hours"
    db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", ("os-raise", "control", "mandatory-reporting", quote, "test", iso()))
    db.commit()
    tagging.prune_tags(db)
    assert db.execute("SELECT COUNT(*) FROM tags WHERE target='os-raise'").fetchone()[0] == 1, \
        "a label quoting the law's text was pruned as unsupported"
    controls = {c["slug"]: c for c in config("controls")}
    todo, _, shown = checking.queue(db, controls, {f["slug"]: f for f in config("fears")})
    asked = checking.question(shown["os-raise"], "control", "mandatory-reporting", quote, controls)
    assert "Text of the law" in asked and "seventy-two hours" in asked, "the check was not shown the law's words"


def check_confirmed_labels_stay():
    """A label all three readings agreed on stays while its quote does.

    A measure is read afresh when its text changes, and a fresh reading does not always propose every
    label the last one found. On 25 September 179 confirmed labels were off the site for that alone,
    Texas's criminal offenses for AI-made sexual images of children among them."""
    from pipeline import tag as tagging
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = connect(tmp / "t.db")
    title = ("Relating to prosecution and punishment of certain criminal offenses prohibiting sexually explicit "
             "visual material involving depictions of computer-generated children; creating criminal offenses; "
             "increasing criminal penalties.")
    for mid, name in (("os-sb1621", title), ("os-gone", "Relating to deepfakes of candidates; creating a criminal offense.")):
        upsert(db, "measures", {"id": mid, "kind": "bill", "jurisdiction": "tx", "jurisdiction_name": "Texas",
                                "session": "89R", "identifier": "SB 1621" if mid == "os-sb1621" else "SB 9",
                                "title": name, "summary": "", "status": "passed", "latest_action": "Effective",
                                "latest_action_date": "2025-09-01", "introduced_date": "2025-03-01",
                                "url": f"https://example.com/{mid}", "sponsors": "Sample", "source": "Open States",
                                "updated": "2025-09-01", "first_seen": iso()})
        db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "an older text", 1, iso()))
    offense = "creating criminal offenses; increasing criminal penalties"
    images = "sexually explicit visual material involving depictions of computer-generated children"
    rows = (("control", "use-restrictions", offense, "yes"), ("fear", "deepfakes", images, "yes"),
            ("agency", "Texas Attorney General", "creating criminal offenses", "no"))
    for kind, value, quote, verdict in rows:
        db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", ("os-sb1621", kind, value, quote, "test", "2026-09-25T06:31:37"))
        db.execute("INSERT INTO checks VALUES(?,?,?,?,?,?,?,?)",
                   ("os-sb1621", kind, value, quote, verdict, "r", "m", "2026-09-25T06:39:00"))
    db.commit()
    # A fresh reading that finds the fear and misses the offense keeps the offense, and not the office
    # the check refused. A law read from its own text is read by the stronger model.
    db.execute("INSERT INTO texts(target, url, text, fetched, tries) VALUES(?,?,?,?,0)",
               ("os-sb1621", "https://example.com/enrolled.htm", "BE IT ENACTED BY THE LEGISLATURE. " + images, iso()))
    db.commit()
    real = (tagging.env, tagging.propose, tagging.verify, tagging.check_labels)
    read_by = {}

    def propose(key, fears, controls, doc, is_measure, model=None):
        read_by[next(l for l in doc.split("\n") if l.startswith("Identifier: "))] = model
        return {"ai_related": True, "fears": ["deepfakes"], "controls": [], "agencies": []}
    tagging.env = lambda name: "k"
    tagging.propose = propose
    tagging.verify = lambda key, fears, controls, doc, proposal, model=None: {
        "fears": {"deepfakes": images}, "controls": {}, "agencies": {}}
    tagging.check_labels = lambda db, key: "checked 0 labels"
    try:
        state = {}
        tagging.run(db, state, "hourly")
    finally:
        tagging.env, tagging.propose, tagging.verify, tagging.check_labels = real
    assert read_by == {"Identifier: SB 1621": tagging.TEXT_MODEL, "Identifier: SB 9": None}, read_by
    have = {(r["kind"], r["value"]) for r in db.execute("SELECT kind, value FROM tags WHERE target='os-sb1621'")}
    assert ("control", "use-restrictions") in have, "a confirmed control was lost to a fresh reading that missed it"
    assert ("fear", "deepfakes") in have and ("agency", "Texas Attorney General") not in have, have
    # One lost before this rule existed comes back on the quote the check confirmed, dated when it did.
    db.execute("DELETE FROM tags WHERE target='os-sb1621' AND kind='control'")
    db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", ("os-gone", "control", "use-restrictions", "creating a criminal offense",
                                                         "test", iso()))
    for target, quote in (("os-gone", "creating a criminal offense"), ("os-gone", "a quote no longer in the measure")):
        db.execute("INSERT OR REPLACE INTO checks VALUES(?,?,?,?,?,?,?,?)",
                   (target, "fear", "deepfakes", quote, "yes", "r", "m", "2026-09-25T06:39:00"))
    db.execute("UPDATE tag_runs SET ai_related = 0 WHERE target = 'os-gone'")
    db.execute("INSERT OR REPLACE INTO checks VALUES(?,?,?,?,?,?,?,?)",
               ("os-gone", "control", "use-restrictions", "creating a criminal offense", "yes", "r", "m", "2026-09-25T06:39:00"))
    db.execute("DELETE FROM tags WHERE target='os-gone'")
    db.commit()
    dropped, back = tagging.prune_tags(db)
    row = db.execute("SELECT evidence, tagged_at FROM tags WHERE target='os-sb1621' AND kind='control'").fetchone()
    assert row and row["evidence"] == offense and row["tagged_at"] == "2026-09-25T06:39:00", "a confirmed label stayed off"
    assert not db.execute("SELECT 1 FROM tags WHERE target='os-gone'").fetchone(), \
        "a label came back on a measure no longer read as about AI"
    assert back == 1, f"put back {back}"
    # Nor on a quote the measure no longer has, or one the check refused.
    db.execute("UPDATE tag_runs SET ai_related = 1 WHERE target = 'os-gone'")
    db.execute("UPDATE checks SET evidence = 'a quote no longer in the measure' WHERE target = 'os-gone'")
    db.commit()
    assert tagging.prune_tags(db) == (0, 0), "a label came back on words the measure does not have"
    assert not db.execute("SELECT 1 FROM tags t JOIN checks c ON c.target = t.target AND c.kind = t.kind "
                          "AND c.value = t.value WHERE c.verdict = 'no'").fetchone(), "a refused label came back"


def check_frontier_reread():
    """The first reading is told what the third already was: a harm defined by mass casualties or a
    billion dollars of damage, New York's "critical harm", is the catastrophic risk of loss of control.
    And the measures naming frontier models are read once more, which can now only add labels."""
    from pipeline import tag as tagging
    from pipeline.common import retag_frontier
    said = []
    real = tagging.call
    tagging.call = lambda key, system, user, max_tokens=700, model=None: said.append(user) or {
        "ai_related": True, "fears": ["loss-of-control"], "controls": [], "agencies": []}
    try:
        fears, controls = config("fears"), config("controls")
        proposal = tagging.propose("k", fears, controls, "Title: RAISE act", True)
        tagging.verify("k", fears, controls, "Title: RAISE act", proposal)
    finally:
        tagging.call = real
    assert len(said) == 2 and all("critical harm" in u for u in said), "the tagger was not told what the fear includes"
    # A key that cannot use the stronger model reads the law with the smaller one rather than not at all.
    class Reply:
        def __init__(self, code, text):
            self.status_code, self.text = code, text

        def json(self):
            return {"content": [{"type": "text", "text": self.text}]}
    models = []

    def post(url, **kw):
        models.append(kw["json"]["model"])
        if kw["json"]["model"] == tagging.TEXT_MODEL:
            return Reply(404, '{"type":"error","error":{"type":"not_found_error","message":"model"}}')
        return Reply(200, '{"ai_related": false}')
    real_post = tagging.requests.post
    tagging.requests.post = post
    try:
        assert tagging.call("k", "s", "u", model=tagging.TEXT_MODEL) == {"ai_related": False}
    finally:
        tagging.requests.post = real_post
    assert models == [tagging.TEXT_MODEL, tagging.MODEL], models
    # An answer that adds a note, or a second object, after its JSON is read for the first object.
    assert tagging.parse_json('{"ai_related": true, "fears": ["x"]}\n\nNote: {"also": 1}') == \
        {"ai_related": True, "fears": ["x"]}, "an answer with a note after its JSON went unread"
    assert tagging.parse_json('Here it is:\n```json\n{"a": {"b": "c"}}\n```') == {"a": {"b": "c"}}
    try:
        tagging.parse_json("no object here")
        raise AssertionError("an answer with no JSON was taken for one")
    except ValueError:
        pass
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = connect(tmp / "t.db")
    for mid, title in (("os-raise", "Relates to the training and use of artificial intelligence frontier models"),
                       ("os-other", "Relates to deepfakes of candidates")):
        upsert(db, "measures", {"id": mid, "kind": "bill", "jurisdiction": "ny", "jurisdiction_name": "New York",
                                "session": "2025-2026", "identifier": mid, "title": title, "summary": "",
                                "status": "passed", "introduced_date": "2025-03-27", "url": f"https://example.com/{mid}",
                                "source": "Open States", "first_seen": iso()})
        db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", (mid, "h", 1, iso()))
    db.execute("DELETE FROM kv WHERE key LIKE 'retag:frontier:%'")
    db.commit()
    assert retag_frontier(db) == 1 and retag_frontier(db) == 0, "queued more than once, or not at all"
    hashes = {r["target"]: r["text_hash"] for r in db.execute("SELECT target, text_hash FROM tag_runs")}
    assert hashes == {"os-raise": None, "os-other": "h"}, hashes
    # And every law read from its own text is read once more, by the stronger model.
    from pipeline.common import retag_law_texts
    db.execute("UPDATE tag_runs SET text_hash = 'h'")
    db.execute("INSERT INTO texts(target, url, text, fetched, tries) VALUES('os-other', 'u', 'text', ?, 0)", (iso(),))
    db.execute("DELETE FROM kv WHERE key LIKE 'retag:law-texts:%'")
    db.commit()
    assert retag_law_texts(db) == 1 and retag_law_texts(db) == 0
    hashes = {r["target"]: r["text_hash"] for r in db.execute("SELECT target, text_hash FROM tag_runs")}
    assert hashes == {"os-raise": "h", "os-other": None}, hashes


def main():
    check_frontier_reread()
    check_confirmed_labels_stay()
    check_office_names()
    check_law_text()
    check_indexnow()
    check_bill_links()
    check_government_own_use()
    check_loss_of_control_reread()
    check_candidates()
    check_use_restrictions()
    check_brief_prompt()
    check_plate_fits()
    check_mark_geometry()
    check_icon_centred()
    check_share_card_fits()
    check_headline_tidy()
    check_fears_config()
    check_no_euphemism()
    check_fec_sweep()
    check_post_needs_entries()
    check_failed_source_retries()
    check_overdue_daily_source()
    check_places_word()
    check_headline_keeps_the_power()
    check_label_rules()
    check_third_reading()
    check_second_attempt()
    check_power_is_the_office()
    check_brief_attempts()
    check_status_and_coverage()
    check_lobbying_money()
    check_lobbying_keywords()
    check_position_carries_no_control()
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
    # A bill carried into the next session. Open States files it again with a new id, and it was
    # counted twice on the page and on the plate. It carries an office of its own, so a count that
    # reads both copies also shows up in the offices.
    carried = dict(db.execute("SELECT * FROM measures WHERE id='os-test0'").fetchone())
    carried.update({"id": "os-test0-carried", "session": "2026", "latest_action_date": today.isoformat()})
    upsert(db, "measures", carried)
    db.execute("INSERT INTO tag_runs VALUES(?,?,?,?,NULL)", ("os-test0-carried", "h", 1, iso()))
    for kind, value in (("fear", "loss-of-control"), ("control", "mandatory-reporting"),
                        ("agency", "California Attorney General")):
        db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", ("os-test0-carried", kind, value, "quote", "test", iso()))
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
            # GDELT asked about every other fear too and found nothing: a count of nought, which the
            # index scores, unlike a fear it has not been asked about yet.
            for f in config("fears"):
                if f["slug"] != "deepfakes":
                    db.execute("INSERT OR REPLACE INTO series VALUES(?,?,?)", (f"news:{f['slug']}", day, 0))
    for i in range(12):
        upsert(db, "articles", {"id": f"a{i}", "fear": "deepfakes", "title": f"Deepfake scam wave hits voters in state {i}",
                                "url": "https://example.com", "domain": "example.com",
                                "seen": (today - dt.timedelta(days=1)).isoformat() + "T10:00:00", "entity": None})
    # GDELT files a story under a fear when the fear is anywhere in its text. These say nothing about
    # deepfakes in the headline and outnumber the ones that do, so they would lead without the rule.
    for i in range(15):
        upsert(db, "articles", {"id": f"u{i}", "fear": "deepfakes",
                                "title": f"Liberal Democrats to focus on tax cuts and Europe, says leader {i}",
                                "url": f"https://example.org/{i}", "domain": "example.org",
                                "seen": today.isoformat() + "T09:00:00", "entity": None})
    db.execute("INSERT INTO status VALUES('congress',?,1,5,'ok',?)", (iso(), iso()))
    db.commit()
    data = export.export(db, tmp)
    assert data["index"]["value"] != "0", data["index"]
    assert data["index"]["total_measures"] == 40, \
        f"a carried-over bill was counted twice: {data['index']['total_measures']} measures, not 40"
    # The plate goes out on X while the page it describes is one click away. They are counted by
    # two different pieces of code, so the numbers are held against each other here.
    from pipeline.brief import totals as plate_totals  # noqa: E402
    plate = plate_totals(db)
    chain = {label: value for value, label in data["index"]["chain"]}
    for word, key in (("bills, resolutions, rules and orders", "measures"),
                      ("new government control", "controlled"),
                      ("would hand new power", "offices")):
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
    # A fear count says which fear, and under a lead that cites one it counts that one. "Name the
    # same fear: Deepfakes" went out under a lead citing children and chatbots.
    fear_lines = [l for l in lines if l["key"].startswith("fear:")]
    assert fear_lines and not any("same fear" in l["sentence"] for l in fear_lines), fear_lines
    from pipeline.brief import spare_for as plate_spare  # noqa: E402
    for l in fear_lines:
        slug = l["key"].split(":")[1]
        got = plate_spare(db, {"fears": slug, "controls": ""}, dt.date.today().isoformat(), set())
        assert any(p["key"] == l["key"] for p in got), f"a lead citing {slug} got {[p['key'] for p in got]}"
    assert data["fears"] and data["controls"]
    # The top of the page has to say who ends up holding the controls, not just that they exist
    assert "office" in (data["exhibit"]["bought"] or ""), \
        f"the hero stopped naming who holds the controls: {data['exhibit']['bought']!r}"
    # The headline under the fear's name names the fear.
    assert "Deepfake" in (data["exhibit"]["line"] or ""), \
        f"the loudest deepfake headline does not mention deepfakes: {data['exhibit']['line']!r}"
    assert data["funders"], "advocacy lobbying should rank separately"
    assert data["industry"], "company and trade group lobbying should rank separately"
    assert not ({f["name"] for f in data["funders"]} & {i["name"] for i in data["industry"]}), \
        "an organization must appear in one ranking or the other, never both"
    # An edition on record, so the page shows the card rather than only the words about it. The
    # real one is written by pipeline.brief; this is the row it leaves behind.
    from pipeline.common import kv_set as _kv_set  # noqa: E402
    brief_day = dt.date.today().isoformat()
    _kv_set(db, "brief:latest", {"edition": brief_day, "headline": "An office takes a register",
                                 "alt": "A card from the AI Fear Report."})
    db.commit()
    data = export.export(db, tmp, "")
    assert data["brief"]["image"] == f"/brief/{brief_day}.png", data["brief"]
    site = ROOT / "site" / "build.py"
    subprocess.run([sys.executable, str(site), "--data", str(tmp / "site_data.json"), "--out", str(tmp / "dist"),
                    "--base", ""], check=True)
    subprocess.run([sys.executable, str(site), "--data", str(tmp / "site_data.json"), "--preview", str(tmp / "p.html")],
                   check=True)
    # A row whose count is empty rendered its unit on its own: "Lobbying client  filings"
    for page in (tmp / "dist").rglob("index.html"):
        body = page.read_text()
        for stray in ("</span> filings", "> filings<", "mentions  fear"):
            assert stray not in body, f"{page.name} renders a unit with no number: {stray!r}"
        # One h1 per page. The front page had none at all, which is the page that matters most.
        heads = re.findall(r"<h1[^>]*>", body)
        assert len(heads) == 1, f"{page.relative_to(tmp / 'dist')} has {len(heads)} h1 headings"
        # Election receipts run the length of a two-year cycle, not the last four quarters
        assert "election money, past year" not in body and "election money this year" not in body, \
            f"{page.name} dates cycle-to-date election money as a year"
    check_every_page_asks(tmp / "dist", db, brief_day)
    check_links_open_right(tmp / "dist")
    check_states(tmp / "dist", data)
    check_bills_page(tmp / "dist", data)
    check_fears_explained(tmp / "dist", data)
    check_links(tmp / "dist")
    check_search(tmp / "dist", tmp / "site_data.json", tmp)
    check_page_dates(tmp / "site_data.json", tmp)
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
