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


def main():
    check_brief_prompt()
    check_plate_fits()
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
                            ("agency", ["California Attorney General", "Federal Trade Commission"][i % 2])]:
            db.execute("INSERT INTO tags VALUES(?,?,?,?,?,?)", (mid, kind, value, "quote", "test", iso()))
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
