# netcompany-monitor
Weekday 08:00 Copenhagen check of TED for Netcompany contract awards; formats rows for the SEB sheet (Original date | Country | Customers | Contract | Size (DKKm) | Note | Link) and alerts via GitHub issue (+ optional Gmail).

- Run tests: `pytest`
- Data source: TED Search API v3, `POST https://api.ted.europa.eu/v3/notices/search`, keyless. Query `winner-name~"netcompany"` (covers Netcompany-Intrasoft, Norway, Greek branch). Extensions: `FT~"netcompany" AND notice-type=can-modif`; direct-award pre-announcements: `... notice-type=veat`.
- Multilingual fields come back as `{"dan": [...]}`; English preferred, else machine-translated (DeepL if DEEPL_API_KEY set → MyMemory → Google; Google blocks GitHub runners; rows keep `_orig` text and `_translated` flag, failed ones retried each run).
- Size = `total-value` (falls back to summed `result-value-lot`) × ECB rate → DKKm. Value is the notice total, not Netcompany's share; joint awards are flagged in Note.
- State: `state/seen.json` (seen ids + cached formatted rows), committed by the workflow along with `data/netcompany_wins.xlsx`.
- GitHub cron is UTC, so the workflow fires at 06/07 UTC and `monitor.py` gates on the Copenhagen hour.
- Note style follows the manual sheet: "Two vendors", "Three vendors", "4 vendors", "(Net the lead)" (from `tendering-party-leader`, aligned with `organisation-name-tenderer`), "Extension", "Direct award pre-announcement". Contract gets " - framework agreement" when any `framework-agreement-lot` is `fa-*`.
- Row format is versioned (`FORMAT_VERSION`); bumping it rebuilds all rows from a full fetch on the next run, carrying finished translations over. New notice kinds are baselined silently (`baselined` in state).
- Repeats: exact = same `procedure-identifier` + `contract-identifier` set + signing dates + winner count + value → `_repeat="exact"`, note "Republished, same contract as X", excluded from paste block and on the xlsx's second sheet. Loose (same buyer+value or procedure+value) → "possible repeat of X", kept.
- Extensions (can-modif) have no `winner-name`; vendors are counted from `organisation-name-tenderer`, which there lists only the contractors.
