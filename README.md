# netcompany-monitor

Every weekday at 08:00 Copenhagen time this checks TED (the EU's tenders site,
[ted.europa.eu](https://ted.europa.eu)) for new contract award notices that list
Netcompany as a winner, and turns each one into a row for the tracking sheet:

| Original date | Country | Customers | Contract | Size (DKKm) | Note | Link |
|---|---|---|---|---|---|---|
| 2026September | Greece | European Centre for the Development of Vocational Training | Graphic design and layout services for Cedefop publications and communication activities | 2 | | https://ted.europa.eu/en/notice/-/detail/670345-2026 |

It runs on GitHub Actions, so it needs no PC, server or local Python.

## How it works

1. It queries TED's official search API for award notices where the winner name
   contains "netcompany". That covers Netcompany A/S, Netcompany Norway,
   Netcompany S.A. and its Greek branch, and Netcompany-Intrasoft.
2. Each notice is formatted like the manual sheet:
   - **Original date**: TED publication month, e.g. `2026September`.
   - **Country**: the buyer's country.
   - **Customers / Contract**: buyer name and procurement title. When the notice
     isn't in English these are machine-translated, so check them against the notice.
     Translation is throttled to stay within Google's free limits, so after a big
     backlog some rows can stay in the original language for a run or two; each
     run retries them.
   - **Size (DKKm)**: the notice's total awarded value at today's ECB rate. Whole
     numbers from 1 DKKm, one decimal below that.
   - **Note**: filled in automatically when something needs a second look:
     - *Shared award with …*: other companies also won (a consortium or other lots),
       so the size is the whole notice, not Netcompany's share.
     - *Contract signed … (published late)*: signed more than 120 days before TED published it.
     - *Possible repeat of …*: same buyer and value as an earlier notice.
     - *No value published*.
3. Anything not seen before opens a **GitHub issue** (GitHub emails you, and it
   shows up in the GitHub app). The issue has a block of tab-separated rows you
   can copy and paste straight into Excel, plus a readable table. You can also
   have it sent by email (see below).
4. Contract *modification* notices that mention Netcompany are listed separately
   in the issue, because they don't have structured winner or value fields.
5. Every win found so far is kept in `data/netcompany_wins.xlsx`, newest first.
6. The first run only records what's already on TED (all of it goes into the
   spreadsheet) and opens no issue.

## Day-to-day use

- **Run a check now:** Actions tab → *Netcompany TED monitor* → *Run workflow*.
- **Catch up after a gap / reconcile with your sheet:** *Run workflow* and put a
  date in "since", e.g. `2026-09-01`. The issue then lists every win since that date.
- **Full history:** download `data/netcompany_wins.xlsx` from the repo.
- GitHub turns off scheduled workflows after 60 days with no commits. This repo
  commits whenever there's a new win, so that should rarely happen. If it does,
  turn it back on in the Actions tab.

## Optional: email as well as the GitHub issue

Repo → Settings → Secrets and variables → Actions → *New repository secret*:

| Secret | Value |
|---|---|
| `EMAIL_USER` | a Gmail address to send from |
| `EMAIL_PASS` | a Gmail **app password** for that account |
| `EMAIL_TO` | where alerts go (separate several with commas) |

## Local use (optional)

```bash
pip install -r requirements.txt
python monitor.py --dry-run --since 2026-10-01   # print rows, change nothing
pytest
```

Add `--no-translate` to keep names in their original language.
