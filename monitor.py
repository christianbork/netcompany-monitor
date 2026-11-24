"""Netcompany contract-win monitor (TED).

Searches TED (Tenders Electronic Daily, ted.europa.eu) for contract award notices
where Netcompany is a winner, formats each one as a row for the SEB tracking sheet:

    Original date | Country | Customers | Contract | Size (DKKm) | Note | Link

and reports anything new since the last run. Designed to run on GitHub Actions
(see .github/workflows/), but runs anywhere with Python.

Usage:
    python monitor.py                   # scheduled runs only act at 08:00 Copenhagen
    python monitor.py --force           # ignore the time-of-day gate
    python monitor.py --dry-run         # check and print, but do not save state or alert
    python monitor.py --since 2026-10-01  # also report every win published since a date
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "seen.json"
XLSX_FILE = ROOT / "data" / "netcompany_wins.xlsx"
ALERT_FILE = ROOT / "alert.md"

API_URL = "https://api.ted.europa.eu/v3/notices/search"
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
NOTICE_URL = "https://ted.europa.eu/en/notice/-/detail/{}"

# Award notices with Netcompany (incl. Netcompany-Intrasoft, Netcompany Norway, the
# Greek branch...) among the winners. Matches every spelling seen on TED so far.
WINS_QUERY = 'winner-name~"netcompany"'
# Contract modifications only mention Netcompany in free text; listed separately.
MODS_QUERY = 'FT~"netcompany" AND notice-type=can-modif'

FIELDS = [
    "publication-number", "publication-date", "notice-type", "buyer-name", "buyer-country",
    "notice-title", "title-proc", "winner-name", "total-value", "total-value-cur",
    "result-value-lot", "result-value-cur-lot", "contract-conclusion-date",
]

COLUMNS = ["Original date", "Country", "Customers", "Contract", "Size (DKKm)", "Note", "Link"]

TZ = ZoneInfo("Europe/Copenhagen")
RUN_HOURS = {8}
LOOKBACK_DAYS = 45            # daily runs only page through recent notices
LATE_SIGNING_DAYS = 120       # flag contracts signed this long before publication
FAILURES_BEFORE_ALERT = 2
EUR_DKK_FALLBACK = 7.46       # DKK is pegged to EUR; used if the ECB feed is unreachable

HEADERS = {
    "User-Agent": "netcompany-monitor/1.0 (equity research contract tracker)",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

COUNTRY_OVERRIDES = {"GBR": "UK", "USA": "USA", "CZE": "Czech Republic", "NLD": "Netherlands"}


# --------------------------------------------------------------------------- TED

def search(session, query: str, since: date | None = None, attempts: int = 3) -> list[dict]:
    """Return every notice matching `query` (optionally published on/after `since`)."""
    if since:
        query = f"({query}) AND publication-date>={since:%Y%m%d}"
    query += " SORT BY publication-number DESC"
    notices, page = [], 1
    while True:
        body = {"query": query, "fields": FIELDS, "limit": 100, "page": page,
                "paginationMode": "PAGE_NUMBER", "scope": "ALL"}
        data = _post(session, body, attempts)
        batch = data.get("notices") or []
        notices.extend(batch)
        if len(batch) < 100 or len(notices) >= data.get("totalNoticeCount", 0) or page >= 50:
            return notices
        page += 1
        time.sleep(1)


def _post(session, body: dict, attempts: int) -> dict:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(API_URL, json=body, headers=HEADERS, timeout=90)
            if resp.status_code >= 400:
                raise ValueError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            print(f"  attempt {attempt}/{attempts} failed: {exc}")
            time.sleep(10 * attempt)
    raise RuntimeError(str(last))


# --------------------------------------------------------------------------- FX

def fetch_dkk_rates(session) -> dict[str, float]:
    """DKK per 1 unit of each currency, from the ECB daily reference rates."""
    try:
        resp = session.get(ECB_URL, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        eur = {c.get("currency"): float(c.get("rate")) for c in root.iter() if c.get("currency")}
        eur["EUR"] = 1.0
        dkk_per_eur = eur["DKK"]
        return {cur: dkk_per_eur / r for cur, r in eur.items()}
    except Exception as exc:  # noqa: BLE001 - FX is nice-to-have, never fatal
        print(f"ECB rates unavailable ({exc}); using EUR/DKK {EUR_DKK_FALLBACK} only.")
        return {"EUR": EUR_DKK_FALLBACK, "DKK": 1.0}


# --------------------------------------------------------------------------- formatting

def _texts(field) -> list[str]:
    """Flatten a TED multilingual field ({"eng": [...]} / {"dan": "..."}) to a list."""
    if not field:
        return []
    if isinstance(field, str):
        return [field]
    if isinstance(field, list):
        return [str(x) for x in field]
    out = []
    for v in field.values():
        out.extend(v if isinstance(v, list) else [v])
    return [str(x) for x in out]


def _pick(field) -> tuple[str, str]:
    """(text, language) preferring English."""
    if not field:
        return "", ""
    if isinstance(field, dict):
        lang = "eng" if "eng" in field else next(iter(field))
        val = field[lang]
        return (val[0] if isinstance(val, list) else str(val)), lang
    return _texts(field)[0], ""


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def country_name(iso3: str) -> str:
    if not iso3:
        return ""
    if iso3 in COUNTRY_OVERRIDES:
        return COUNTRY_OVERRIDES[iso3]
    try:
        import pycountry
        c = pycountry.countries.get(alpha_3=iso3)
        if c:
            return getattr(c, "common_name", None) or c.name
    except ImportError:
        pass
    return iso3


def month_label(pub_date: str) -> str:
    d = datetime.strptime(pub_date[:10], "%Y-%m-%d")
    return f"{d.year}{d:%B}"   # e.g. 2026September


def size_dkkm(value, currency: str, rates: dict) -> str | float:
    if value in (None, "") or not currency or currency not in rates:
        return ""
    m = float(value) * rates[currency] / 1e6
    if m >= 1:
        return round(m)
    return round(m, 1)


def notice_value(n: dict) -> tuple[float | None, str]:
    total, cur = n.get("total-value"), _first(n.get("total-value-cur"))
    if total not in (None, ""):
        return float(total), cur or ""
    lots = [float(x) for x in (n.get("result-value-lot") or []) if str(x).strip()]
    curs = n.get("result-value-cur-lot") or []
    if lots:
        return sum(lots), (curs[0] if curs else "")
    return None, ""


def strip_title_prefix(eng_title: str) -> str:
    # notice-title in English looks like "Denmark – IT services: ... – <original title>"
    parts = eng_title.split(" – ")
    return parts[-1] if len(parts) >= 3 else eng_title


class Translator:
    """Optional machine translation into English, throttled to stay under Google's
    free-tier limits. Returns (text, ok); ok=False means "retry on a later run"."""

    PAUSE = 0.6        # seconds between requests
    MAX_PER_RUN = 150  # leave the rest for the next run

    def __init__(self, enabled: bool):
        self.cache: dict[str, str] = {}
        self.tr = None
        self.calls = 0
        self.blocked = False
        if enabled:
            try:
                from deep_translator import GoogleTranslator
                self.tr = GoogleTranslator(source="auto", target="en")
            except ImportError:
                print("deep-translator not installed; keeping original-language text.")

    @property
    def active(self) -> bool:
        return self.tr is not None and not self.blocked and self.calls < self.MAX_PER_RUN

    def __call__(self, text: str, lang: str) -> tuple[str, bool]:
        if not text or lang in ("eng", "") or self.tr is None:
            return text, True
        if text in self.cache:
            return self.cache[text], True
        if not self.active:
            return text, False
        for attempt in range(3):
            time.sleep(self.PAUSE * (1 + 10 * attempt))
            self.calls += 1
            try:
                out = (self.tr.translate(text[:4500]) or text).strip()
                self.cache[text] = out
                return out, True
            except Exception as exc:  # noqa: BLE001
                print(f"  translation attempt {attempt + 1} failed: {str(exc)[:80]}")
        print("  translation unavailable; remaining rows will be retried next run")
        self.blocked = True
        return text, False


def is_netcompany(name: str) -> bool:
    return "netcompany" in name.lower()


def clean_name(name: str) -> str:
    return re.sub(r"[«»\"]", "", name).strip()


def to_row(n: dict, rates: dict, translate, fingerprints: dict | None = None) -> dict:
    pub_no = n["publication-number"]
    pub_date = n.get("publication-date", "")[:10]

    buyer, buyer_lang = _pick(n.get("buyer-name"))
    title, title_lang = _pick(n.get("title-proc"))
    if not title:
        title, title_lang = strip_title_prefix(_pick(n.get("notice-title"))[0]), ""

    value, cur = notice_value(n)
    notes = []

    winners = list(dict.fromkeys(clean_name(w) for w in _texts(n.get("winner-name"))))
    others = [w for w in winners if not is_netcompany(w)]
    if others:
        shown = ", ".join(_short(o) for o in others[:3]) + (f" +{len(others) - 3} more" if len(others) > 3 else "")
        notes.append(f"Shared award with {shown} - size is total notice value")

    if value is None:
        notes.append("No value published")
    elif cur and cur not in rates:
        notes.append(f"Value {value:,.0f} {cur} (no FX rate)")

    signed = (_first(n.get("contract-conclusion-date")) or "")[:10]
    if signed and pub_date:
        try:
            gap = (date.fromisoformat(pub_date) - date.fromisoformat(signed)).days
            if gap > LATE_SIGNING_DAYS:
                notes.append(f"Contract signed {signed} (published late)")
        except ValueError:
            pass

    if fingerprints is not None and value is not None:
        fp = f"{buyer.lower()}|{round(value)}|{cur}"
        earlier = fingerprints.get(fp)
        if earlier and earlier != pub_no:
            notes.append(f"Possible repeat of {earlier}")
        else:
            fingerprints.setdefault(fp, pub_no)

    row = {
        "Original date": month_label(pub_date) if pub_date else "",
        "Country": country_name(_first(n.get("buyer-country")) or ""),
        "Customers": buyer,
        "Contract": title,
        "Size (DKKm)": size_dkkm(value, cur, rates),
        "Note": "; ".join(notes),
        "Link": NOTICE_URL.format(pub_no),
        "_id": pub_no,
        "_pub": pub_date,
        # originals kept so a failed translation can be retried on a later run
        "_orig": {"Customers": buyer, "Contract": title, "Note": "; ".join(notes)},
        "_langs": {"Customers": buyer_lang, "Contract": title_lang,
                   "Note": "auto" if NON_LATIN.search("; ".join(notes)) else "eng"},
        "_translated": False,
    }
    return apply_translation(row, translate)


NON_LATIN = re.compile(r"[\u0370-\u03ff\u0400-\u04ff]")   # Greek, Cyrillic


def _short(name: str, n: int = 45) -> str:
    return name if len(name) <= n else name[: n - 1].rstrip() + "…"


def apply_translation(row: dict, translate) -> dict:
    """Fill Customers/Contract/Note with English where possible. Marks the row
    `_translated` only once every field has been handled, so failures retry later."""
    if translate is None:
        return row
    ok_all = True
    for field in ("Customers", "Contract", "Note"):
        text, ok = translate(row["_orig"][field], row["_langs"].get(field, "eng"))
        row[field] = text
        ok_all &= ok
    row["_translated"] = ok_all
    return row


def tsv_line(row: dict) -> str:
    return "\t".join(str(row[c]).replace("\t", " ").replace("\n", " ") for c in COLUMNS)


# --------------------------------------------------------------------------- outputs

def write_xlsx(rows: list[dict], path: Path = XLSX_FILE) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Netcompany wins"
    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for r in sorted(rows, key=lambda r: (r["_pub"], r["_id"]), reverse=True):
        ws.append([r[c] for c in COLUMNS])
        link = ws.cell(row=ws.max_row, column=len(COLUMNS))
        link.hyperlink = r["Link"]
        link.font = Font(color="0563C1", underline="single")
    for col, width in zip("ABCDEFG", (16, 14, 45, 70, 12, 45, 50)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)


def build_alert(new_rows: list[dict], mods: list[dict], broken: str | None) -> str:
    now = datetime.now(TZ).strftime("%d %b %Y %H:%M")
    out = [f"Checked {now} (Copenhagen time).", ""]
    if new_rows:
        out += [f"### {len(new_rows)} new Netcompany contract win(s)", "",
                "Copy the block below and paste it into Excel (columns: "
                + " | ".join(COLUMNS) + "):", "", "```"]
        out += [tsv_line(r) for r in new_rows]
        out += ["```", "", "| " + " | ".join(COLUMNS[:-1]) + " | Notice |",
                "|" + "---|" * len(COLUMNS)]
        for r in new_rows:
            cells = [str(r[c]).replace("|", "/") for c in COLUMNS[:-1]]
            out.append("| " + " | ".join(cells) + f" | [{r['_id']}]({r['Link']}) |")
        out.append("")
    if mods:
        out += ["### Contract modifications mentioning Netcompany (check manually)", ""]
        for m in mods:
            out.append(f"- {m['Original date']} · {m['Country']} · {m['Customers']} · "
                       f"{m['Contract']} · [{m['_id']}]({m['Link']})")
        out.append("")
    if broken:
        out += ["### TED search failing", "", f"Failed {FAILURES_BEFORE_ALERT} runs in a row: {broken}", ""]
    out.append("Size = notice value converted at today's ECB rate. Customer/contract names "
               "not published in English are machine-translated; check the notice.")
    return "\n".join(out)


def send_email(subject: str, body: str) -> None:
    user, password, to = (os.environ.get(k, "") for k in ("EMAIL_USER", "EMAIL_PASS", "EMAIL_TO"))
    if not (user and password and to):
        print("Email not configured (EMAIL_USER/EMAIL_PASS/EMAIL_TO); relying on the GitHub issue.")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"Email sent to {to}.")


def write_github_output(**values) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for k, v in values.items():
                fh.write(f"{k}={v}\n")


# --------------------------------------------------------------------------- state

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                          encoding="utf-8")


def should_run_now(force: bool) -> bool:
    if force or os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return True
    hour = datetime.now(TZ).hour
    if hour in RUN_HOURS:
        return True
    print(f"Copenhagen hour is {hour}, not one of {sorted(RUN_HOURS)}; skipping this scheduled slot.")
    return False


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore the 08:00 gate")
    ap.add_argument("--dry-run", action="store_true", help="do not save state or alert")
    ap.add_argument("--since", type=date.fromisoformat, help="also report wins published on/after YYYY-MM-DD")
    ap.add_argument("--no-translate", action="store_true", help="keep original-language names")
    args = ap.parse_args()
    if not should_run_now(args.force):
        return 0

    state = load_state()
    first_run = "seen" not in state
    state.setdefault("seen", [])
    state.setdefault("rows", {})          # publication-number -> formatted row (for the xlsx)
    state.setdefault("fingerprints", {})
    state.setdefault("failures", 0)
    seen = set(state["seen"])

    session = requests.Session()
    # Rows saved by an older version lack the original text needed to (re)translate
    # them; fetch the full history once to rebuild those.
    legacy = {pid for pid, r in state["rows"].items() if "_orig" not in r}
    lookback = None
    if not first_run and not legacy:
        lookback = date.today() - timedelta(days=LOOKBACK_DAYS)
        if args.since and args.since < lookback:
            lookback = args.since

    print(f"Searching TED: {WINS_QUERY} (since {lookback or 'the beginning'})")
    try:
        notices = search(session, WINS_QUERY, lookback)
        mod_notices = search(session, MODS_QUERY, date.today() - timedelta(days=LOOKBACK_DAYS))
    except RuntimeError as exc:
        state["failures"] += 1
        print(f"FAILED ({state['failures']} in a row): {exc}")
        broken = str(exc)[:300] if state["failures"] == FAILURES_BEFORE_ALERT else None
        if not args.dry_run:
            save_state(state)
            if broken:
                body = build_alert([], [], broken)
                ALERT_FILE.write_text(body, encoding="utf-8")
                write_github_output(alert="true", title="Netcompany monitor: TED search failing")
        return 0
    state["failures"] = 0
    print(f"  {len(notices)} award notices, {len(mod_notices)} recent modification notices")

    rates = fetch_dkk_rates(session)
    translate = Translator(enabled=not args.no_translate and os.environ.get("TRANSLATE", "1") != "0")

    # Oldest first so "possible repeat" points back at the earlier notice.
    notices.sort(key=lambda n: (n.get("publication-date", ""), n["publication-number"]))
    new_rows = []
    for n in notices:
        pid = n["publication-number"]
        is_new = pid not in seen
        in_since = args.since and n.get("publication-date", "")[:10] >= args.since.isoformat()
        if pid in legacy:
            state["rows"][pid] = to_row(n, rates, None, state["fingerprints"])
        if not is_new and not in_since:
            continue
        row = state["rows"].get(pid) if not is_new else None
        if row is None:
            row = to_row(n, rates, translate, state["fingerprints"])
            state["rows"][pid] = row
        if (is_new and not first_run) or in_since:
            new_rows.append(row)
        seen.add(pid)
    new_rows.sort(key=lambda r: (r["_pub"], r["_id"]), reverse=True)

    # Retry translation for rows where it failed earlier (newest first).
    pending = [r for r in state["rows"].values() if not r.get("_translated", True)]
    for r in sorted(pending, key=lambda r: r["_pub"], reverse=True):
        if not translate.active:
            break
        apply_translation(r, translate)
    left = sum(1 for r in state["rows"].values() if not r.get("_translated", True))
    if left:
        print(f"{left} row(s) still awaiting translation; will retry next run.")

    mods = []
    seen_mods = set(state.setdefault("seen_mods", []))
    for n in mod_notices:
        pid = n["publication-number"]
        if pid not in seen_mods:
            if not first_run:
                mods.append(to_row(n, rates, translate))
            seen_mods.add(pid)
    state["seen_mods"] = sorted(seen_mods)
    state["seen"] = sorted(seen)

    if first_run:
        state["baseline"] = datetime.now(TZ).isoformat(timespec="minutes")
        print(f"First run: recorded {len(seen)} existing wins as the baseline (all in {XLSX_FILE.name}), no alert.")

    if not args.dry_run:
        save_state(state)
        write_xlsx(list(state["rows"].values()), XLSX_FILE)

    if not (new_rows or mods):
        print("Nothing new.")
        return 0

    title = (f"Netcompany: {len(new_rows)} new contract win(s)" if new_rows
             else "Netcompany: new contract modification notice(s)")
    body = build_alert(new_rows, mods, None)
    print("\n" + title + "\n\n" + body)
    if args.dry_run:
        return 0
    ALERT_FILE.write_text(body, encoding="utf-8")
    write_github_output(alert="true", title=title)
    try:
        send_email(title, body)
    except Exception as exc:  # noqa: BLE001
        print(f"Email failed: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
