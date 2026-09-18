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


def main():
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
    for i in range(30):
        y, q = today.year - (i // 12), (i % 4) + 1
        upsert(db, "lobbying", {"id": f"l{i}", "client": ["OPENAI OPCO, LLC", "META PLATFORMS, INC.", "BUSINESS ROUNDTABLE"][i % 3],
                                "client_key": name_key(["OPENAI OPCO, LLC", "META PLATFORMS, INC.", "BUSINESS ROUNDTABLE"][i % 3]),
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
    assert data["fears"] and data["funders"] and data["controls"]
    site = ROOT / "site" / "build.py"
    subprocess.run([sys.executable, str(site), "--data", str(tmp / "site_data.json"), "--out", str(tmp / "dist"),
                    "--base", "/ai-fear-index"], check=True)
    subprocess.run([sys.executable, str(site), "--data", str(tmp / "site_data.json"), "--preview", str(tmp / "p.html")],
                   check=True)
    pages = sorted(str(p.relative_to(tmp / "dist")) for p in (tmp / "dist").rglob("index.html"))
    print("pages:", len(pages), pages[:6])
    print("index:", json.dumps(data["index"])[:200])
    print("exhibit:", json.dumps(data["exhibit"])[:300])
    print("top fear:", data["fears"][0], "top funder:", data["funders"][0])
    print("OK", tmp)


if __name__ == "__main__":
    main()
