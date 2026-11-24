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
    "organisation-name-tenderer": {"ell": ["«NETCOMPANY S.A. Ελληνικό Υποκατάστημα»", "«ΟΤΕ Α.Ε.»", "«BRIGHT BUSINESS SOLUTION Α.E.»"]},
    "tendering-party-leader": [True, False, False],
    "framework-agreement-lot": ["none"],
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


def test_joint_award_note_matches_sheet_style():
    row = monitor.to_row(GREEK_JOINT, RATES, NO_TR)
    assert row["Note"] == "Three vendors (Net the lead)"
    assert row["Size (DKKm)"] == round(93022265.8 * 7.46 / 1e6)
    assert not row["Contract"].endswith("framework agreement")


def test_possible_repeat_flagged():
    fps = {}
    monitor.to_row(GREEK_JOINT, RATES, NO_TR, fps)
    again = dict(GREEK_JOINT, **{"publication-number": "999999-2026"})
    assert monitor.to_row(again, RATES, NO_TR, fps)["Note"] == \
        "Three vendors (Net the lead) - possible repeat of 764865-2026"


def test_framework_and_vendor_counts():
    fw = dict(PRISONS, **{
        "framework-agreement-lot": ["fa-wo-rc", "none"],
        "winner-name": {"dan": ["Netcompany A/S", "KMD A/S", "Netcompany A/S"]},
        "organisation-name-tenderer": {"dan": ["Netcompany A/S", "KMD A/S", "Netcompany A/S"]},
        "tendering-party-leader": [True, True, True],      # separate bids, no consortium
    })
    row = monitor.to_row(fw, RATES, NO_TR)
    assert row["Contract"] == "Udbud af kontrakt om Ny Klientløsning - framework agreement"
    assert row["Note"] == "Two vendors"                     # duplicates (lots) counted once
    five = dict(fw, **{"winner-name": {"dan": ["Netcompany", "A", "B", "C", "D"]},
                       "organisation-name-tenderer": {"dan": ["Netcompany", "A", "B", "C", "D"]},
                       "tendering-party-leader": [False, True, False, False, False]})
    assert monitor.to_row(five, RATES, NO_TR)["Note"] == "5 vendors"   # Net not the lead


def test_losing_bidders_are_not_vendors():
    # Real case 749910-2026: one winner, but other bidders appear among the tenderers.
    row = dict(PRISONS, **{"organisation-name-tenderer": {"dan": ["Netcompany A/S", "KMD A/S"]},
                           "tendering-party-leader": [True, True]})
    assert monitor.to_row(row, RATES, NO_TR)["Note"] == ""
    lost_partner = dict(GREEK_JOINT, **{
        "winner-name": {"ell": ["NETCOMPANY", "ΟΤΕ"]},
        "organisation-name-tenderer": {"ell": ["NETCOMPANY", "ΟΤΕ", "LOSER", "LOSER PARTNER"]},
        "tendering-party-leader": [True, False, True, False]})
    assert monitor.to_row(lost_partner, RATES, NO_TR)["Note"] == "Two vendors (Net the lead)"


def test_translation_keeps_framework_suffix():
    fw = dict(PRISONS, **{"framework-agreement-lot": ["fa-w-rc"]})
    row = monitor.to_row(fw, RATES, FakeTranslator())
    assert row["Contract"] == "UDBUD AF KONTRAKT OM NY KLIENTLØSNING - framework agreement"


def test_modification_and_veat_rows():
    mod = {k: v for k, v in PRISONS.items() if k != "winner-name"}
    assert monitor.to_row(mod, RATES, NO_TR, kind="modif")["Note"] == "Extension"
    assert monitor.to_row(GREEK_JOINT, RATES, NO_TR, kind="modif")["Note"] == \
        "Three vendors (Net the lead) - extension"
    assert monitor.to_row(mod, RATES, NO_TR, kind="veat")["Note"] == "Direct award pre-announcement"


def test_carry_translation_reuses_finished_work():
    old = monitor.to_row(PRISONS, RATES, FakeTranslator())
    fw = dict(PRISONS, **{"framework-agreement-lot": ["fa-wo-rc"]})
    new = monitor.carry_translation(monitor.to_row(fw, RATES, None), old)
    assert new["_translated"] and new["Contract"] == "UDBUD AF KONTRAKT OM NY KLIENTLØSNING - framework agreement"


def test_small_values_keep_a_decimal_and_missing_values_noted():
    small = dict(CEDEFOP, **{"total-value": 30000})
    assert monitor.to_row(small, RATES, NO_TR)["Size (DKKm)"] == 0.2
    none = {k: v for k, v in CEDEFOP.items() if k not in ("total-value", "result-value-lot")}
    assert monitor.to_row(none, RATES, NO_TR)["Size (DKKm)"] == ""


def test_tsv_and_alert(tmp_path):
    rows = [monitor.to_row(CEDEFOP, RATES, NO_TR)]
    line = monitor.tsv_line(rows[0])
    assert line.split("\t")[0] == "2026September" and len(line.split("\t")) == 7
    body = monitor.build_alert(rows, None)
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
                        lambda s, q, since=None: list(feed["wins" if "winner" in q else "mods"]) if "veat" not in q else [])
    monkeypatch.setattr("sys.argv", ["monitor.py"])

    monitor.main()                                   # baseline: no alert
    assert not (tmp_path / "alert.md").exists()
    assert len(json.loads((tmp_path / "seen.json").read_text())["seen"]) == 2

    feed["wins"].append(PRISONS)
    monitor.main()                                   # one new win
    alert = (tmp_path / "alert.md").read_text()
    assert "1 new Netcompany notice" in alert and "749910-2026" in alert
    assert "670345-2026" not in alert


class FakeTranslator:
    """Fails the first `fail` calls, then 'translates' by upper-casing."""

    def __init__(self, fail=0):
        self.fail, self.calls = fail, 0
        self.active = True

    def __call__(self, text, lang):
        if not text or lang in ("eng", ""):
            return text, True
        self.calls += 1
        if self.calls <= self.fail:
            return text, False
        return text.upper(), True


def test_failed_translation_is_retried_later():
    row = monitor.to_row(PRISONS, RATES, FakeTranslator(fail=99))
    assert row["_translated"] is False
    assert row["Customers"] == "Danmarks Fængsler v. Styrelsen for Danmarks Fængsler"
    monitor.apply_translation(row, FakeTranslator())
    assert row["_translated"] is True
    assert row["Customers"] == "DANMARKS FÆNGSLER V. STYRELSEN FOR DANMARKS FÆNGSLER"
    assert row["Size (DKKm)"] == 148


def test_english_notice_needs_no_translation():
    t = FakeTranslator(fail=99)
    row = monitor.to_row(CEDEFOP, RATES, t)
    assert row["_translated"] is True and t.calls == 0


def test_legacy_rows_rebuilt_and_retranslated(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "STATE_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(monitor, "XLSX_FILE", tmp_path / "wins.xlsx")
    monkeypatch.setattr(monitor, "ALERT_FILE", tmp_path / "alert.md")
    monkeypatch.setattr(monitor, "fetch_dkk_rates", lambda s: RATES)
    monkeypatch.setattr(monitor, "Translator", lambda enabled: FakeTranslator())
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    legacy_row = {c: "old" for c in monitor.COLUMNS} | {"_id": "749910-2026", "_pub": "2026-10-29"}
    (tmp_path / "seen.json").write_text(json.dumps(
        {"seen": ["749910-2026"], "rows": {"749910-2026": legacy_row}, "fingerprints": {}, "failures": 0,
         "baselined": ["award"]}))
    calls = []
    monkeypatch.setattr(monitor, "search",
                        lambda s, q, since=None: calls.append(since) or ([PRISONS] if "winner" in q else []))
    monkeypatch.setattr("sys.argv", ["monitor.py"])
    monitor.main()
    assert calls[0] is None                                   # full history fetched once
    row = json.loads((tmp_path / "seen.json").read_text())["rows"]["749910-2026"]
    assert row["_translated"] and row["Customers"].startswith("DANMARKS")
    assert not (tmp_path / "alert.md").exists()               # not reported as new


def test_translator_falls_back_between_services(monkeypatch):
    monkeypatch.setattr(monitor.Translator, "PAUSE", 0)
    t = monitor.Translator(enabled=True)
    def boom(text, src):
        raise RuntimeError("429 too many requests")
    t.backends = [("A", boom), ("B", lambda text, src: f"EN[{src}]:{text}")]
    assert t("Udbud", "dan") == ("EN[da]:Udbud", True)
    assert [n for n, _ in t.backends] == ["B"]            # failing service dropped
    t.backends = [("A", boom)]
    assert t("Noget andet", "dan") == ("Noget andet", False)
    assert not t.active


def test_disabled_translator_marks_done():
    t = monitor.Translator(enabled=False)
    assert t("Udbud", "dan") == ("Udbud", True)


def test_skip_keeps_service_and_greek_is_detected(monkeypatch):
    monkeypatch.setattr(monitor.Translator, "PAUSE", 0)
    t = monitor.Translator(enabled=True)
    seen = []
    def picky(text, src):
        seen.append(src)
        if src == "auto":
            raise monitor._SkipText()
        return "ok"
    t.backends = [("P", picky)]
    assert t("Some text", "") == ("Some text", True)       # no lang, English-ish: untouched
    assert t("??", "xxx") == ("??", False)                   # unknown lang skipped...
    assert [n for n, _ in t.backends] == ["P"]               # ...but service kept
    assert t("ΟΤΕ Α.Ε.", "xxx") == ("ok", True) and seen[-1] == "el"


def test_new_kinds_baselined_silently_then_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "STATE_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(monitor, "XLSX_FILE", tmp_path / "wins.xlsx")
    monkeypatch.setattr(monitor, "ALERT_FILE", tmp_path / "alert.md")
    monkeypatch.setattr(monitor, "fetch_dkk_rates", lambda s: RATES)
    monkeypatch.setenv("TRANSLATE", "0")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setattr("sys.argv", ["monitor.py"])
    old_mod = dict(PRISONS, **{"publication-number": "100-2025", "publication-date": "2025-01-01+01:00"})
    feed = {"award": [CEDEFOP], "modif": [old_mod], "veat": []}
    monkeypatch.setattr(monitor, "search", lambda s, q, since=None: list(
        feed["veat" if "veat" in q else "modif" if "modif" in q else "award"]))
    (tmp_path / "seen.json").write_text(json.dumps({"seen": ["670345-2026"], "rows": {}, "baselined": ["award"]}))

    monitor.main()                                   # modif/veat history recorded, no alert
    assert not (tmp_path / "alert.md").exists()
    state = json.loads((tmp_path / "seen.json").read_text())
    assert state["rows"]["100-2025"]["Note"] == "Extension"

    feed["veat"].append(dict(LATE, **{"publication-number": "200-2026"}))
    monitor.main()
    alert = (tmp_path / "alert.md").read_text()
    assert "Direct award pre-announcement" in alert and "100-2025" not in alert
