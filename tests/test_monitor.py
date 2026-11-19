"""Offline tests built from real TED v3 responses (fields trimmed)."""

import json

import monitor

RATES = {"EUR": 7.46, "DKK": 1.0, "NOK": 0.64}
NO_TR = monitor.Translator(enabled=False)

CEDEFOP = {
    "publication-number": "670345-2026",
    "publication-date": "2026-09-29+01:00",
    "notice-type": "can-standard",
    "buyer-country": ["GRC"],
    "buyer-name": {"eng": ["European Centre for the Development of Vocational Training"]},
    "title-proc": {"eng": "Graphic design and layout services for Cedefop publications and communication activities"},
    "winner-name": {"eng": ["NETCOMPANY S.A."]},
    "total-value": 244000,
    "total-value-cur": ["EUR"],
    "result-value-lot": ["244000"],
    "contract-conclusion-date": ["2026-09-22+02:00"],
}

PRISONS = {
    "publication-number": "749910-2026",
    "publication-date": "2026-10-29+01:00",
    "buyer-country": ["DNK"],
    "buyer-name": {"dan": ["Danmarks Fængsler v. Styrelsen for Danmarks Fængsler"]},
    "title-proc": {"dan": "Udbud af kontrakt om Ny Klientløsning"},
    "notice-title": {"eng": "Denmark – IT services: consulting, software development, Internet and support – Udbud af kontrakt om Ny Klientløsning"},
    "winner-name": {"dan": ["Netcompany A/S"]},
    "total-value": 148000000,
    "total-value-cur": ["DKK"],
    "contract-conclusion-date": ["2026-09-07+02:00"],
}

GREEK_JOINT = {
    "publication-number": "764865-2026",
    "publication-date": "2026-11-04+01:00",
    "buyer-country": ["GRC"],
    "buyer-name": {"ell": ["ΤΕΧΝΙΚΟ ΕΠΙΜΕΛΗΤΗΡΙΟ ΕΛΛΑΔΑΣ"]},
    "title-proc": {"ell": "Εντοπισμός αυθαίρετης δόμησης με σύγχρονα τεχνολογικά μέσα"},
    "winner-name": {"ell": ["«NETCOMPANY S.A. Ελληνικό Υποκατάστημα»", "«ΟΤΕ Α.Ε.»", "«BRIGHT BUSINESS SOLUTION Α.E.»"]},
    "total-value": 93022265.8,
    "total-value-cur": ["EUR"],
    "contract-conclusion-date": ["2026-10-30+02:00"],
}

LATE = {
    "publication-number": "708393-2026",
    "publication-date": "2026-10-14+01:00",
    "buyer-country": ["GRC"],
    "buyer-name": {"ell": ["Ανεξάρτητη Αρχή Δημοσίων Εσόδων"]},
    "title-proc": {"ell": "Έργο"},
    "winner-name": {"ell": ["NETCOMPANY S.A."]},
    "total-value": 6399000,
    "total-value-cur": ["EUR"],
    "contract-conclusion-date": ["2022-12-15+02:00"],
}


def test_matches_manual_row_format():
    row = monitor.to_row(CEDEFOP, RATES, NO_TR)
    assert row["Original date"] == "2026September"
    assert row["Country"] == "Greece"
    assert row["Customers"] == "European Centre for the Development of Vocational Training"
    assert row["Contract"].startswith("Graphic design and layout services")
    assert row["Size (DKKm)"] == 2          # 244k EUR ≈ 1.8 DKKm, as in the manual sheet
    assert row["Note"] == ""
    assert row["Link"] == "https://ted.europa.eu/en/notice/-/detail/670345-2026"


def test_dkk_value_and_original_language():
    row = monitor.to_row(PRISONS, RATES, NO_TR)
    assert row["Size (DKKm)"] == 148
    assert row["Country"] == "Denmark"
    assert row["Contract"] == "Udbud af kontrakt om Ny Klientløsning"


def test_joint_award_is_flagged():
    row = monitor.to_row(GREEK_JOINT, RATES, NO_TR)
    assert "Shared award with" in row["Note"]
    assert "NETCOMPANY" not in row["Note"]
    assert row["Size (DKKm)"] == round(93022265.8 * 7.46 / 1e6)


def test_late_signing_flagged():
    assert "Contract signed 2022-12-15" in monitor.to_row(LATE, RATES, NO_TR)["Note"]


def test_possible_repeat_flagged():
    fps = {}
    monitor.to_row(GREEK_JOINT, RATES, NO_TR, fps)
    again = dict(GREEK_JOINT, **{"publication-number": "999999-2026"})
    assert "Possible repeat of 764865-2026" in monitor.to_row(again, RATES, NO_TR, fps)["Note"]


def test_small_values_keep_a_decimal_and_missing_values_noted():
    small = dict(CEDEFOP, **{"total-value": 30000})
    assert monitor.to_row(small, RATES, NO_TR)["Size (DKKm)"] == 0.2
    none = {k: v for k, v in CEDEFOP.items() if k not in ("total-value", "result-value-lot")}
    row = monitor.to_row(none, RATES, NO_TR)
    assert row["Size (DKKm)"] == "" and "No value published" in row["Note"]


def test_tsv_and_alert(tmp_path):
    rows = [monitor.to_row(CEDEFOP, RATES, NO_TR)]
    line = monitor.tsv_line(rows[0])
    assert line.split("\t")[0] == "2026September" and len(line.split("\t")) == 7
    body = monitor.build_alert(rows, [], None)
    assert "```" in body and line in body
    monitor.write_xlsx(rows, tmp_path / "x.xlsx")
    assert (tmp_path / "x.xlsx").stat().st_size > 0


def test_end_to_end_first_run_then_new(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "STATE_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(monitor, "XLSX_FILE", tmp_path / "wins.xlsx")
    monkeypatch.setattr(monitor, "ALERT_FILE", tmp_path / "alert.md")
    monkeypatch.setattr(monitor, "fetch_dkk_rates", lambda s: RATES)
    monkeypatch.setenv("TRANSLATE", "0")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    feed = {"wins": [CEDEFOP, LATE], "mods": []}
    monkeypatch.setattr(monitor, "search",
                        lambda s, q, since=None: list(feed["wins" if "winner" in q else "mods"]))
    monkeypatch.setattr("sys.argv", ["monitor.py"])

    monitor.main()                                   # baseline: no alert
    assert not (tmp_path / "alert.md").exists()
    assert len(json.loads((tmp_path / "seen.json").read_text())["seen"]) == 2

    feed["wins"].append(PRISONS)
    monitor.main()                                   # one new win
    alert = (tmp_path / "alert.md").read_text()
    assert "1 new Netcompany contract win" in alert and "749910-2026" in alert
    assert "670345-2026" not in alert
