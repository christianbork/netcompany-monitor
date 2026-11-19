# netcompany-monitor
Weekday 08:00 Copenhagen check of TED for Netcompany contract awards; formats rows for the SEB sheet (Original date | Country | Customers | Contract | Size (DKKm) | Note | Link) and alerts via GitHub issue (+ optional Gmail).

- Run tests: `pytest`
- Data source: TED Search API v3, `POST https://api.ted.europa.eu/v3/notices/search`, keyless. Query `winner-name~"netcompany"` (covers Netcompany-Intrasoft, Norway, Greek branch). Modifications: `FT~"netcompany" AND notice-type=can-modif`.
- Multilingual fields come back as `{"dan": [...]}`; English preferred, else machine-translated with deep-translator.
- Size = `total-value` (falls back to summed `result-value-lot`) × ECB rate → DKKm. Value is the notice total, not Netcompany's share; joint awards are flagged in Note.
- State: `state/seen.json` (seen ids + cached formatted rows), committed by the workflow along with `data/netcompany_wins.xlsx`.
- GitHub cron is UTC, so the workflow fires at 06/07 UTC and `monitor.py` gates on the Copenhagen hour.
