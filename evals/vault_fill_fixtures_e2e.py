"""The fill behaviours we found on real sites, as checked-in pages. One eval, no network.

The pages live in ``evals/vault_fill_pages/`` - one per behaviour class, not one per site, because
the fill's contract is with what a page does, not with which library does it. Each page's HTML
comment names the real sites it stands for; that is the whole provenance. The 124-row list that
produced them lives in git history (8e93493f) and the measurements in the site-matrix notes, so
nothing here depends on a list, a checkout, or a live site.

EVERY PAGE DECLARES ITS OWN CONTRACT, in a ``<script type="application/json" id="fill-contract">``
block: how it must be driven (``reveal`` - the selector to click, or nothing) and what the end state
must be. This file is one generic driver: read the declaration, drive the page, compare the end
state. There is no per-page branch here, and nothing guesses what a page is - a page that needs a
reveal names its own button, so a wrong click can never look like a broken fill.

Mirrors ``vault_fill_live_e2e.py``: real browser_exec, real supervisor, real browser_vault_fill,
and the secret absent from every model-facing result.

Contract keys (all optional except ``expect.result``):

  identifier_type / identifier   what the vault holds for this page
  reveal                         CSS selector of the control that reveals the form, if any
  expect.result                  filled | refused | limit | either
  expect.filled_fields           exact count of fields the fill wrote
  expect.values                  {input name: value}; "$PASSWORD" stands for the vault password
  expect.frame_values            same, read inside the first iframe of the page
  expect.digits                  digits held across every tel/email field, in order
  expect.tel_values              digit-per-tel-field, in document order
  expect.message_contains        substrings the limit message must name (counts, not secrets)
  expect.password_inserted       false: the fill must abort before the password (limit pages)
  expect.trusted_inserts         how many exact password inserts the trusted-typing path made
  expect.error_contains          substring the refusal must carry
  expect.child_touched           a cross-origin child's own input-event count must stay this

Run: HERMES_E2E_BROWSER=1 <venv>/bin/python evals/vault_fill_fixtures_e2e.py
"""
from __future__ import annotations

import http.server
import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = Path(tempfile.mkdtemp(prefix="hermes-vault-fixtures-"))
os.environ["HERMES_HOME"] = str(HOME)

PAGES_DIR = Path(__file__).resolve().parent / "vault_fill_pages"
PASSWORD = "pw-E2E-8842"
TASK = "vault-fixtures-e2e"
CONTRACT_RE = re.compile(
    r'<script type="application/json" id="fill-contract">\s*(\{.*?\})\s*</script>', re.S)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        rel = self.path.lstrip("/")
        page = PAGES_DIR / rel
        if not page.is_file():
            page = PAGES_DIR / (rel + ".html")
        body = page.read_bytes() if page.is_file() else b"nope"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


def _exec(code: str) -> dict:
    from tools import browser_use_cli as bu

    res = bu.browser_exec(code, task_id=TASK, timeout_s=90)
    out = json.loads(res if isinstance(res, str) else json.dumps(res))
    assert out.get("success"), out
    return out


def contracts() -> dict[str, dict]:
    """Every page declares its own contract; a page without one is a broken fixture, not a skip."""
    out = {}
    for path in sorted(PAGES_DIR.glob("*.html")):
        found = CONTRACT_RE.search(path.read_text())
        assert found, f"{path.name}: no fill-contract block - the page must declare its end state"
        out[path.stem] = json.loads(found.group(1))
    return out


def _sub(value):
    return PASSWORD if value == "$PASSWORD" else value


def _read(sup, origin, bvt, expr: str):
    sup.focus_page(origin, accept=bvt.TAB_PROBES["login"])
    dom = sup.evaluate_runtime(f"JSON.stringify((() => {{ try {{ return {expr}; }} catch (e) {{ return null; }} }})())")
    raw = dom.get("result")
    return None if raw in (None, "null") else json.loads(raw)


def _by_name(sup, origin, bvt, names, frame=False):
    doc = "document.querySelector('iframe').contentDocument" if frame else "document"
    return _read(sup, origin, bvt,
                 "[" + ",".join(f"{doc}.querySelector('[name={n}]').value" for n in names) + "]")


def _digits(sup, origin, bvt):
    held = _read(sup, origin, bvt,
                 "Array.from(document.querySelectorAll('[type=tel],[type=email]')).map(e => e.value)") or []
    return "".join(c for c in "".join(held) if c.isdigit())


def _tel_values(sup, origin, bvt):
    held = _read(sup, origin, bvt, "Array.from(document.querySelectorAll('[type=tel]')).map(e => e.value)") or []
    return ["".join(c for c in v if c.isdigit()) for v in held]


def check(name: str, spec: dict, out: dict, sup, origin, bvt, inserted: list[str]) -> str:
    """Compare one page's declared end state with what the fill actually did."""
    result = spec["result"]
    if result == "either":
        assert "success" in out, out
    elif result == "filled":
        assert out.get("success"), f"{name}: {out}"
    elif result == "limit":
        # the field holds fewer digits than the vault value: kept, named, not a failure
        assert not out.get("success"), f"{name}: {out}"
    elif result == "refused":
        assert not out.get("success"), f"{name}: {out}"
    else:
        raise AssertionError(f"{name}: unknown declared result {result!r}")

    if not out.get("success") and result in ("refused", "either"):
        # a refusal's end state is "nothing filled": only the refusal itself can be checked
        if "error_contains" in spec:
            assert spec["error_contains"] in str(out.get("error", "")), f"{name}: {out}"
        if "child_touched" in spec:
            touched = sup.evaluate_runtime("String(window.__childTouched || 0)")
            assert touched.get("result") == str(spec["child_touched"]), f"{name}: {touched}"
        return result

    if "filled_fields" in spec:
        assert out.get("filled_fields") == spec["filled_fields"], f"{name}: {out}"
    if "values" in spec:
        names = list(spec["values"])
        held = _by_name(sup, origin, bvt, names)
        assert held == [_sub(spec["values"][n]) for n in names], f"{name}: page holds {held}"
    if "frame_values" in spec:
        names = list(spec["frame_values"])
        held = _by_name(sup, origin, bvt, names, frame=True)
        assert held == [_sub(spec["frame_values"][n]) for n in names], f"{name}: frame holds {held}"
    if "digits" in spec:
        assert _digits(sup, origin, bvt) == spec["digits"], f"{name}: digits {_digits(sup, origin, bvt)}"
    if "tel_values" in spec:
        assert _tel_values(sup, origin, bvt) == spec["tel_values"], name
    if "message_contains" in spec:
        message = f"{out.get('message', '')} {out.get('hint', '')}"
        for part in spec["message_contains"]:
            assert part in message, f"{name}: message does not name {part!r}: {message}"
    if "password_inserted" in spec:
        assert spec["password_inserted"] is not True, "use password_untouched for the positive case"
    if spec.get("password_inserted") is False:
        assert PASSWORD not in "".join(inserted), f"{name}: the password was typed before the limit"
    if spec.get("password_untouched"):
        # The page sets a password: every password box must still be empty, and the fill must say
        # why. This is the signup case - a saved login is never typed into a new-password form.
        held = _read(sup, origin, bvt,
                     "Array.from(document.querySelectorAll('input[type=password]')).map(e => e.value)") or []
        assert held and all(v == "" for v in held), f"{name}: a password box holds a value"
        assert out.get("password_skipped"), f"{name}: the fill did not report the skip: {out}"
    if spec.get("traps_untouched"):
        # Honeypot traps: a field no human can see must stay empty, or the site marks the
        # submission as automation. This is the off-screen/transparent shape, not display:none.
        held = _by_name(sup, origin, bvt, spec["traps_untouched"])
        assert held is not None and all(v == "" for v in held), f"{name}: a trap holds a value: {held}"
    if "trusted_inserts" in spec:
        assert len([s for s in inserted if s == PASSWORD]) == spec["trusted_inserts"], f"{name}: {inserted}"
    if "error_contains" in spec:
        assert spec["error_contains"] in str(out.get("error", "")), f"{name}: {out}"
    if "child_touched" in spec:
        touched = sup.evaluate_runtime("String(window.__childTouched || 0)")
        assert touched.get("result") == str(spec["child_touched"]), f"{name}: {touched}"
    return result


def main() -> int:
    pages = contracts()
    # one server per page: each page gets its own origin, so an item is bound to exactly one form
    servers, origins = {}, {}
    # a second origin on a FIXED port, so the iframe in iframe-cross-origin.html is genuinely
    # another origin - the port is hardcoded in that page
    servers["cross"] = http.server.ThreadingHTTPServer(("127.0.0.1", 8477), _Handler)
    threading.Thread(target=servers["cross"].serve_forever, daemon=True).start()
    for name in pages:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers[name], origins[name] = srv, f"http://127.0.0.1:{srv.server_address[1]}"

    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    try:
        for name in pages:
            _exec(f"new_tab({origins[name] + '/' + name!r}); wait_for_load()")

        sup = SUPERVISOR_REGISTRY.get(TASK)
        assert sup is not None, "browser_exec did not attach a supervisor for its task"
        print(f"supervisor attached; {len(pages)} pages, each declaring its own contract: "
              + ", ".join(sorted(pages)))

        # every character the fill sends into a page goes through insert_text and nothing else:
        # recording it is how this eval proves what was typed, and that a secret never travelled
        # as a keystroke (the real supervisor has no key path at all).
        inserted: list[str] = []
        _insert = sup.insert_text

        def _recording_insert(text, **kwargs):
            inserted.append(text)
            return _insert(text, **kwargs)

        sup.insert_text = _recording_insert

        from tools import browser_vault_tool as bvt
        from agent.vault_store import get_vault_store

        store = get_vault_store()
        items = {}
        for name, c in pages.items():
            items[name] = store.add_item(
                "login", name,
                {"identifier_type": c.get("identifier_type", "phone"),
                 "identifier": c.get("identifier", ""), "password": PASSWORD},
                origin=origins[name])

        seen = {}
        for name, c in pages.items():
            # always drive the exec tab to the page first: it is the only tab focus_page is
            # guaranteed to consider, so a 12-tab session cannot fill the wrong page
            _exec(f"goto_url({origins[name] + '/' + name + '.html'!r}); wait_for_load()")
            if c.get("reveal"):
                _exec(f"js(\"document.querySelector({c['reveal']!r}).click()\")")
                _exec("js('new Promise(r => setTimeout(r, 150))')")
            inserted.clear()                      # what THIS fill sends, nothing earlier
            out = json.loads(bvt.browser_vault_fill(items[name].id, task_id=TASK))
            assert PASSWORD not in json.dumps(out), f"{name}: the tool echoed the password"
            result = check(name, c["expect"], out, sup, origins[name], bvt, inserted)
            seen[name] = result
            print(f"{name}: {result} as declared "
                  + (f"({out.get('filled_fields')} fields)" if out.get("success") else ""))

        assert set(seen) == set(pages), "every page must be driven"
        from agent import redact
        redact.clear_vault_redaction_values()
        print(f"FIXTURES E2E OK - {len(seen)} pages, each declaring its own end state")
        return 0
    finally:
        SUPERVISOR_REGISTRY.stop_all()
        from tools.browser_session_lifecycle import cleanup_all_browsers
        cleanup_all_browsers()
        for srv in servers.values():
            srv.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
