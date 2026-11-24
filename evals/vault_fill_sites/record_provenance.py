"""Fold what we verified back into sites.json: one ``needs`` per target, and a corrected scan
reason where the original pass guessed wrong.

The fill question is just "is there a field to type into?", so the list is split in two files:

    fillable.json   status "fillable - yes"            a field is there when the page loads
                    status "fillable - yes - needs click"  a field appears after a click
    error.json      status "error - ip-blocked"        the site serves no page to this browser
                    status "error - no-field-to-fill"  nothing to type into

Hand-verified entries (looked at on 2026-11-23) win over whatever the earlier scan guessed.
Sweep rows then fill in what they support; nothing is guessed.

usage:  record_provenance.py            # update sites.json in place
        record_provenance.py --dry-run  # show what would change
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

LISTS = Path("/Git/hermes/.workspaces/hermes-agent/site-matrix/evals/vault_fill_sites")
FILLABLE = LISTS / "fillable.json"
ERRORS = LISTS / "error.json"
RESULTS = Path("/var/lib/hermes/work/site-matrix/fill-results-v2.jsonl")

# Checked by hand in a browser, with screenshots where it mattered.
HAND_VERIFIED = {
    "https://www.bol.com/nl/nl/login": ("click",
                                        "stored url 404s; 'Inloggen' opens login.bol.com/wsp/login "
                                        "with email + password"),
    "https://cab.uksn.ru/login": ("none",
                                  "login form at load: phone + password + two consent boxes"),
    "https://www.avito.ru/": ("ip-blocked",
                              "no page served: 'Страница не загрузилась из-за проблем с IP'"),
    "https://www.americanas.com.br/": ("click",
                                       "login link opens a dialog with an email field; the page it "
                                       "lands on paints blank"),
    "https://www.drom.ru/": ("no-field",
                             "'Вход' leads to my.drom.ru/verify first; no input on the gate page"),
    "https://login.mts.ru/": ("no-field",
                              "redirects into an OAuth flow; no input renders"),
    "https://www.instacart.com/login": ("none",
                                        "email field on the page; no password field exists"),
    "https://www.flipkart.com/account/login": ("none",
                                               "tel field on the page; login is by code"),
}


# Rows to add when absent: hosts we know about from the vault but never scanned, or that live
# behind another host's front page.
ADD = [
    {"name": "https://lk.fkr-spb.ru", "url": "https://lk.fkr-spb.ru", "tier": "fill",
     "needs": "ip-blocked",
     "status": "error - ip-blocked",
     "scan": "ip-blocked: the cabinet behind fkr-spb.ru; resolves (46.243.177.64) but serves no page"},
    {"name": "https://lk.vodokanal.spb.ru", "url": "https://lk.vodokanal.spb.ru", "tier": "fill",
     "needs": "ip-blocked",
     "status": "error - ip-blocked",
     "scan": "ip-blocked: the vault's cabinet host; blocks this browser by ip"},
]

# Notes that are truer than what the scan recorded.
NOTE = {
    "https://fkr-spb.ru": ("error - no-field-to-fill",
                           "no-field-to-fill: the public site has only a search box; its "
                           "'Личный кабинет' links open lk.fkr-spb.ru in a new tab"),
}


def main() -> int:
    dry = "--dry-run" in sys.argv
    rows = []
    for path in (FILLABLE, ERRORS):
        if path.exists():
            doc = json.loads(path.read_text())
            rows.extend(doc["targets"] if isinstance(doc, dict) else doc)

    sweep: dict[str, dict] = {}
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            sweep[r["name"]] = r

    status_of = {"none": "fillable - yes",
                 "click": "fillable - yes - needs click",
                 "ip-blocked": "error - ip-blocked",
                 "no-field": "error - no-field-to-fill"}

    known = {r["url"] for r in rows}
    for entry in ADD:
        if entry["url"] not in known:
            rows.append(dict(entry))
            print("added:", entry["url"])

    changed = 0
    for r in rows:
        if r["url"] in NOTE:
            status, scan = NOTE[r["url"]]
            r["status"], r["scan"] = status, scan
        before = (r.get("needs"), r.get("status"), r.get("scan"))
        # hand-verified wins
        if r["url"] in HAND_VERIFIED:
            needs, note = HAND_VERIFIED[r["url"]]
            r["needs"] = needs
            r["scan"] = f"{needs}: {note}"
        else:
            s = sweep.get(r["name"])
            if s:
                # what counts is whether a field is reachable, not whether something was clicked
                if s.get("verdict") == "pass":
                    r["needs"] = "click" if s.get("needs") == "clicked" else "none"
                elif s.get("verdict") == "no-fields" or s.get("needs") == "no-form":
                    r["needs"] = "no-field"
                elif s.get("before") or s.get("after"):
                    # fields were present and the fill still failed: a fill target. Wrong vault data
                    # or a mask too small is not a page problem.
                    r["needs"] = "click" if s.get("needs") == "clicked" else "none"
            if not r.get("needs"):
                # never swept: trust the reason the scan recorded
                scan = r.get("scan") or ""
                if "wall" in scan or "geo-blocked" in scan:
                    r["needs"] = "ip-blocked"
                elif "no inputs" in scan or "no identifier" in scan or "not a login" in scan:
                    r["needs"] = "no-field"
        r["status"] = status_of.get(r.get("needs") or "") or r.get("status")
        if (r.get("needs"), r.get("status"), r.get("scan")) != before:
            changed += 1
            if dry:
                print(f"  {r['name'][:38]:40} {before[0]} -> {r.get('needs')}")

    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("needs") or "unset"] = counts.get(r.get("needs") or "unset", 0) + 1
    print(f"{len(rows)} targets | changed {changed} | needs: {counts}")

    fillable, errors = [], []
    for r in rows:
        status = r.get("status")
        if not status:
            continue
        (fillable if status.startswith("fillable") else errors).append(r)

    if not dry:
        for name, payload in ((FILLABLE.name, fillable), (ERRORS.name, errors)):
            path = LISTS / name
            path.write_text(json.dumps({"targets": payload}, ensure_ascii=False, indent=2) + "\n")
            print(f"written: {path} ({len(payload)} targets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
