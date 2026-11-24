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
# Contract modifications and direct-award pre-announcements (VEAT) don't carry
# structured winner fields, so they are found by free-text search.
MODS_QUERY = 'FT~"netcompany" AND notice-type=can-modif'
VEAT_QUERY = 'FT~"netcompany" AND notice-type=veat'

FIELDS = [
    "publication-number", "publication-date", "notice-type", "buyer-name", "buyer-country",
    "notice-title", "title-proc", "winner-name", "total-value", "total-value-cur",
    "result-value-lot", "result-value-cur-lot", "contract-conclusion-date",
    "framework-agreement-lot", "organisation-name-tenderer", "tendering-party-leader",
    "procedure-identifier", "contract-identifier",
]

COLUMNS = ["Original date", "Country", "Customers", "Contract", "Size (DKKm)", "Note", "Link"]

TZ = ZoneInfo("Europe/Copenhagen")
RUN_HOURS = {8}
LOOKBACK_DAYS = 45            # daily runs only page through recent notices
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


# TED's ISO 639-2 language codes -> two-letter codes the translation services expect.
LANG2 = {"bul": "bg", "ces": "cs", "dan": "da", "deu": "de", "ell": "el", "eng": "en",
         "spa": "es", "est": "et", "fin": "fi", "fra": "fr", "gle": "ga", "hrv": "hr",
         "hun": "hu", "ita": "it", "lit": "lt", "lav": "lv", "mlt": "mt", "nld": "nl",
         "pol": "pl", "por": "pt", "ron": "ro", "slk": "sk", "slv": "sl", "swe": "sv",
         "nor": "no", "nob": "nb", "nno": "nn", "isl": "is"}


class _SkipText(Exception):
    """This service can't handle this particular text; try the next one, keep the service."""


class Translator:
    """Optional machine translation into English.

    Tries, in order: DeepL (if the DEEPL_API_KEY secret is set; free tier is
    500k characters/month), MyMemory (free, no sign-up; set MYMEMORY_EMAIL to
    raise its daily limit to 50k characters), then Google. A service that errors
    is skipped for the rest of the run. Returns (text, ok); ok=False means
    "leave the original and retry on a later run"."""

    PAUSE = 0.6        # seconds between requests
    MAX_PER_RUN = 300  # leave the rest for the next run

    def __init__(self, enabled: bool):
        self.cache: dict[str, str] = {}
        self.calls = 0
        self.backends: list = []
        self.enabled = enabled
        if not enabled:
            return
        if os.environ.get("DEEPL_API_KEY"):
            self.backends.append(("DeepL", self._deepl))
        self.backends.append(("MyMemory", self._mymemory))
        self.backends.append(("Google", self._google))

    @property
    def active(self) -> bool:
        return bool(self.backends) and self.calls < self.MAX_PER_RUN

    def __call__(self, text: str, lang: str) -> tuple[str, bool]:
        if not text or lang in ("eng", "") or not self.enabled:
            return text, True
        if text in self.cache:
            return self.cache[text], True
        if not self.active:
            return text, False
        src = LANG2.get(lang, "auto")
        if src == "auto" and re.search(r"[\u0370-\u03ff]", text):
            src = "el"
        i = 0
        while i < len(self.backends):
            name, fn = self.backends[i]
            try:
                time.sleep(self.PAUSE)
                self.calls += 1
                out = (fn(text[:1500], src) or "").strip()
                if not out:
                    raise ValueError("empty response")
                self.cache[text] = out
                return out, True
            except _SkipText:
                i += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  {name} translation failed ({str(exc)[:90]}); dropping it for this run")
                self.backends.pop(i)
        if not self.backends:
            print("  no translation service available; untranslated rows will be retried next run")
        return text, False

    @staticmethod
    def _deepl(text: str, src: str) -> str:
        key = os.environ["DEEPL_API_KEY"]
        host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
        data = {"text": text, "target_lang": "EN-GB"}
        if src != "auto":
            data["source_lang"] = src.upper()[:2]
        r = requests.post(f"https://{host}/v2/translate", data=data,
                          headers={"Authorization": f"DeepL-Auth-Key {key}"}, timeout=30)
        r.raise_for_status()
        return r.json()["translations"][0]["text"]

    @staticmethod
    def _mymemory(text: str, src: str) -> str:
        if src == "auto":
            raise _SkipText()
        params = {"q": text[:480], "langpair": f"{src}|en"}
        if os.environ.get("MYMEMORY_EMAIL"):
            params["de"] = os.environ["MYMEMORY_EMAIL"]
        r = requests.get("https://api.mymemory.translated.net/get", params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        if str(j.get("responseStatus")) != "200":
            raise ValueError(f"{j.get('responseStatus')}: {str(j.get('responseDetails'))[:80]}")
        out = j["responseData"]["translatedText"]
        if "MYMEMORY WARNING" in out.upper():
            raise ValueError(out[:80])
        return out

    @staticmethod
    def _google(text: str, src: str) -> str:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target="en").translate(text)


def is_netcompany(name: str) -> bool:
    return "netcompany" in name.lower()


def clean_name(name: str) -> str:
    return re.sub(r"[«»\"]", "", name).strip()


NUMBER_WORDS = {2: "Two", 3: "Three"}
FORMAT_VERSION = 4   # bump when row formatting changes; older rows are rebuilt once


def _one_language(field) -> list[str]:
    """Names from a multilingual field, taking one language only (otherwise the
    same company in two languages would count twice)."""
    if isinstance(field, dict) and field:
        field = field["eng"] if "eng" in field else field[next(iter(field))]
    if isinstance(field, str):
        field = [field]
    return [clean_name(x) for x in (field or [])]


def _norm(name: str) -> str:
    return re.sub(r"[^\w]", "", name.lower())


def vendor_info(n: dict, kind: str = "award") -> tuple[int, bool, bool]:
    """(number of distinct vendors, Netcompany leads the consortium, Netcompany listed).

    Award notices: vendors come from `winner-name`. `organisation-name-tenderer`
    also lists losing bidders there, so it's only used, with the aligned
    `tendering-party-leader` flags, to see who leads: Netcompany is "the lead" when
    it is flagged leader and another *winner* is a non-leading consortium member.

    Modification notices have no `winner-name`; their `organisation-name-tenderer`
    lists the contractors of the modified contract, so that is used instead."""
    tenderers = _one_language(n.get("organisation-name-tenderer"))
    winners = _one_language(n.get("winner-name")) or (tenderers if kind == "modif" else [])
    win_keys = {_norm(w) for w in winners}
    vendors = len(win_keys)
    listed = not winners or any(is_netcompany(w) for w in winners)
    leaders = n.get("tendering-party-leader") or []
    net_lead = False
    if vendors > 1 and len(leaders) == len(tenderers):
        flags = [(name, flag) for name, flag in zip(tenderers, leaders) if _norm(name) in win_keys]
        net_lead = (any(flag is True and is_netcompany(nm) for nm, flag in flags)
                    and any(flag is False and not is_netcompany(nm) for nm, flag in flags))
    return vendors, net_lead, listed


def vendor_note(count: int, net_lead: bool) -> str:
    if count < 2:
        return ""
    note = f"{NUMBER_WORDS.get(count, count)} vendors"
    return note + (" (Net the lead)" if net_lead else "")


def repeat_keys(n: dict, buyer: str, value: float, cur: str) -> tuple[str, list[str]]:
    """Keys for spotting the same award published more than once.

    exact: same procedure, same contract number(s) and same value. Some buyers
    (notably the Greek tax authority) republish an identical award several times;
    these are left out of the paste block.
    loose: same procedure and value, or same buyer and value. Often a repeat, but
    can be a new call-off or extra lots, so these stay in and are flagged."""
    proc = str(n.get("procedure-identifier") or "")
    contracts = sorted({str(c).strip() for c in (n.get("contract-identifier") or []) if str(c).strip()})
    v = f"{round(value)}|{cur}"
    exact = f"x|{proc}|{'/'.join(contracts)}|{v}" if proc and contracts else ""
    loose = [f"b|{buyer.lower()}|{v}"] + ([f"p|{proc}|{v}"] if proc else [])
    return exact, loose


def is_framework(n: dict) -> bool:
    return any(str(x).startswith("fa-") for x in (n.get("framework-agreement-lot") or []))


def to_row(n: dict, rates: dict, translate, fingerprints: dict | None = None,
           kind: str = "award") -> dict:
    """kind: "award" (contract award notice), "modif" (contract modification) or
    "veat" (voluntary ex-ante transparency notice = direct award pre-announcement)."""
    pub_no = n["publication-number"]
    pub_date = n.get("publication-date", "")[:10]

    buyer, buyer_lang = _pick(n.get("buyer-name"))
    title, title_lang = _pick(n.get("title-proc"))
    if not title:
        title, title_lang = strip_title_prefix(_pick(n.get("notice-title"))[0]), ""

    value, cur = notice_value(n)
    notes = []
    if kind == "veat":
        notes.append("Direct award pre-announcement")
    else:
        count, lead, listed = vendor_info(n, kind)
        v = vendor_note(count, lead)
        if v:
            notes.append(v)
        if kind == "modif":
            notes.append("extension" if notes else "Extension")
            if not listed:
                notes.append("Net not listed as contractor, check")

    repeat = ""
    if kind == "award" and fingerprints is not None and value is not None:
        exact, loose = repeat_keys(n, buyer, value, cur)
        if exact and fingerprints.get(exact, pub_no) != pub_no:
            repeat = "exact"
            notes = [f"Republished, same contract as {fingerprints[exact]}"]
        else:
            earlier = next((fingerprints[k] for k in loose if fingerprints.get(k, pub_no) != pub_no), None)
            if earlier:
                repeat = "possible"
                notes.append(f"possible repeat of {earlier}" if notes else f"Possible repeat of {earlier}")
        for k in ([exact] if exact else []) + loose:
            fingerprints.setdefault(k, pub_no)

    row = {
        "Original date": month_label(pub_date) if pub_date else "",
        "Country": country_name(_first(n.get("buyer-country")) or ""),
        "Customers": buyer,
        "Contract": title,
        "Size (DKKm)": size_dkkm(value, cur, rates),
        "Note": " - ".join(notes),
        "Link": NOTICE_URL.format(pub_no),
        "_id": pub_no,
        "_pub": pub_date,
        "_kind": kind,
        "_repeat": repeat,
        "_v": FORMAT_VERSION,
        "_suffix": " - framework agreement" if is_framework(n) else "",
        # originals kept so a failed translation can be retried on a later run
        "_orig": {"Customers": buyer, "Contract": title},
        "_langs": {"Customers": buyer_lang, "Contract": title_lang},
        "_translated": False,
    }
    row["Contract"] = title + row["_suffix"]
    return apply_translation(row, translate)


def carry_translation(new: dict, old: dict | None) -> dict:
    """Reuse an older row's finished translation when the source text is unchanged."""
    if old and old.get("_translated") and old.get("_orig", {}).get("Customers") == new["_orig"]["Customers"]:
        old_contract = old["Contract"][: len(old["Contract"]) - len(old.get("_suffix", ""))] \
            if old.get("_suffix") else old["Contract"]
        if old.get("_orig", {}).get("Contract") == new["_orig"]["Contract"]:
            new["Customers"] = old["Customers"]
            new["Contract"] = old_contract + new["_suffix"]
            new["_translated"] = True
    return new


def _short(name: str, n: int = 45) -> str:
    return name if len(name) <= n else name[: n - 1].rstrip() + "…"


def apply_translation(row: dict, translate) -> dict:
    """Fill Customers/Contract with English where possible. Marks the row
    `_translated` only once both are handled, so failures retry later."""
    if translate is None:
        return row
    ok_all = True
    for field in ("Customers", "Contract"):
        text, ok = translate(row["_orig"][field], row["_langs"].get(field, "eng"))
        row[field] = text + (row.get("_suffix", "") if field == "Contract" else "")
        ok_all &= ok
    row["_translated"] = ok_all
    return row


def tsv_line(row: dict) -> str:
    return "\t".join(str(row[c]).replace("\t", " ").replace("\n", " ") for c in COLUMNS)


# --------------------------------------------------------------------------- outputs

def write_xlsx(rows: list[dict], path: Path = XLSX_FILE) -> None:
    """Main sheet: everything to track. Second sheet: exact republications."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    sheets = [(wb.active, "Netcompany wins", [r for r in rows if r.get("_repeat") != "exact"]),
              (wb.create_sheet(), "Republished (excluded)", [r for r in rows if r.get("_repeat") == "exact"])]
    for ws, title, subset in sheets:
        ws.title = title
        ws.append(COLUMNS)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
        for r in sorted(subset, key=lambda r: (r["_pub"], r["_id"]), reverse=True):
            ws.append([r[c] for c in COLUMNS])
            link = ws.cell(row=ws.max_row, column=len(COLUMNS))
            link.hyperlink = r["Link"]
            link.font = Font(color="0563C1", underline="single")
        for col, width in zip("ABCDEFG", (16, 14, 45, 70, 12, 45, 50)):
            ws.column_dimensions[col].width = width
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    wb.save(path)


def build_alert(new_rows: list[dict], broken: str | None) -> str:
    now = datetime.now(TZ).strftime("%d %b %Y %H:%M")
    out = [f"Checked {now} (Copenhagen time).", ""]
    keep = [r for r in new_rows if r.get("_repeat") != "exact"]
    dupes = [r for r in new_rows if r.get("_repeat") == "exact"]
    if keep:
        out += [f"### {len(keep)} new Netcompany notice(s)", "",
                "Copy the block below and paste it into Excel (columns: "
                + " | ".join(COLUMNS) + "):", "", "```"]
        out += [tsv_line(r) for r in keep]
        out += ["```", "", "| " + " | ".join(COLUMNS[:-1]) + " | Notice |",
                "|" + "---|" * len(COLUMNS)]
        for r in keep:
            cells = [str(r[c]).replace("|", "/") for c in COLUMNS[:-1]]
            out.append("| " + " | ".join(cells) + f" | [{r['_id']}]({r['Link']}) |")
        out.append("")
    if dupes:
        out += ["### Republished awards (left out of the paste block)", "",
                "Same procedure, same contract number and same value as a notice already seen:", ""]
        for r in dupes:
            out.append(f"- [{r['_id']}]({r['Link']}): {r['Customers']} · {r['Contract']} · {r['Note']}")
        out.append("")
    if broken:
        out += ["### TED search failing", "", f"Failed {FAILURES_BEFORE_ALERT} runs in a row: {broken}", ""]
    out.append("Size = notice value converted at today's ECB rate (the whole notice, not "
               "Netcompany's share). Names not published in English are machine-translated. "
               "Extensions and direct-award pre-announcements are found by text search, so check "
               "Netcompany is the supplier.")
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
    seen = set(state["seen"]) | set(state.pop("seen_mods", []))
    # Notice kinds whose existing history has been recorded. A kind seen for the
    # first time is baselined silently, like the very first run.
    baselined = set(state.setdefault("baselined", ["award"] if not first_run else []))

    session = requests.Session()
    # Rows written by an older version of the formatting are rebuilt once from a
    # full fetch of TED (finished translations are carried over).
    outdated = {pid for pid, r in state["rows"].items() if r.get("_v", 0) < FORMAT_VERSION}
    full = first_run or bool(outdated) or bool({"award", "modif", "veat"} - baselined)
    lookback = None
    if not full:
        lookback = date.today() - timedelta(days=LOOKBACK_DAYS)
        if args.since and args.since < lookback:
            lookback = args.since

    found: dict[str, list] = {}
    print(f"Searching TED (since {lookback or 'the beginning'})")
    try:
        for kind, query in (("award", WINS_QUERY), ("modif", MODS_QUERY), ("veat", VEAT_QUERY)):
            found[kind] = search(session, query, lookback)
            print(f"  {kind}: {len(found[kind])} notices")
    except RuntimeError as exc:
        state["failures"] += 1
        print(f"FAILED ({state['failures']} in a row): {exc}")
        broken = str(exc)[:300] if state["failures"] == FAILURES_BEFORE_ALERT else None
        if not args.dry_run:
            save_state(state)
            if broken:
                body = build_alert([], broken)
                ALERT_FILE.write_text(body, encoding="utf-8")
                write_github_output(alert="true", title="Netcompany monitor: TED search failing")
        return 0
    state["failures"] = 0

    rates = fetch_dkk_rates(session)
    translate = Translator(enabled=not args.no_translate and os.environ.get("TRANSLATE", "1") != "0")
    if outdated:
        state["fingerprints"] = {}       # rebuilt in date order below

    items = [(kind, n) for kind, ns in found.items() for n in ns]
    # Oldest first so "possible repeat" points back at the earlier notice.
    items.sort(key=lambda kn: (kn[1].get("publication-date", ""), kn[1]["publication-number"]))
    new_rows, done = [], set()
    for kind, n in items:
        pid = n["publication-number"]
        if pid in done:                  # same notice matched by two queries
            continue
        done.add(pid)
        old = state["rows"].get(pid)
        is_new = pid not in seen
        in_since = bool(args.since) and n.get("publication-date", "")[:10] >= args.since.isoformat()
        if old is None or pid in outdated:
            row = to_row(n, rates, None, state["fingerprints"], kind)
            row = carry_translation(row, old)
            state["rows"][pid] = row
        row = state["rows"][pid]
        if (is_new and kind in baselined) or in_since:
            new_rows.append(row)
        seen.add(pid)
    new_rows.sort(key=lambda r: (r["_pub"], r["_id"]), reverse=True)

    # Translate new rows and retry rows where translation failed earlier (newest first).
    pending = [r for r in state["rows"].values() if not r.get("_translated", True)]
    for r in sorted(pending, key=lambda r: r["_pub"], reverse=True):
        if not translate.active:
            break
        apply_translation(r, translate)
    left = sum(1 for r in state["rows"].values() if not r.get("_translated", True))
    if left:
        print(f"{left} row(s) still awaiting translation; will retry next run.")

    newly_baselined = {"award", "modif", "veat"} - baselined
    if newly_baselined:
        print(f"Recorded existing {', '.join(sorted(newly_baselined))} notices as the baseline, no alert.")
    state["baselined"] = sorted(baselined | newly_baselined)
    state["seen"] = sorted(seen)
    if first_run:
        state["baseline"] = datetime.now(TZ).isoformat(timespec="minutes")

    if not args.dry_run:
        save_state(state)
        write_xlsx(list(state["rows"].values()), XLSX_FILE)

    if not new_rows:
        print("Nothing new.")
        return 0

    n_keep = sum(1 for r in new_rows if r.get("_repeat") != "exact")
    title = (f"Netcompany: {n_keep} new TED notice(s)" if n_keep
             else f"Netcompany: {len(new_rows)} republished award(s), nothing new to add")
    body = build_alert(new_rows, None)
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
