"""The fill behaviours we found on real sites, as checked-in pages. One eval, no network.

The pages live in ``evals/vault_fill_pages/`` - one per behaviour class, not one per site, because
the fill's contract is with what a page does, not with which library does it. Each page's HTML
comment names the real sites it stands for; that is the whole provenance. The 124-row list that
produced them lives in git history (8e93493f) and the measurements in the site-matrix notes, so
nothing here depends on a list, a checkout, or a live site.

Mirrors ``vault_fill_live_e2e.py``: real browser_exec, real supervisor, real browser_vault_fill,
and the secret absent from every model-facing result.

  plain           the control - the identifier is typed, the password goes by the guarded JS write
  stateful        the page discards untrusted writes; the password must land as ONE trusted insert
  mask-truncate   the field holds fewer digits than the vault value; the message names the counts
  mask-exact      the same mask with room for the whole value - must simply fill
  mask-short      the vault value shorter than the field - must simply fill
  mask-reformat   the mask rewrites the string; the digits survive, so judgement is by digits
  mask-two-tel    two telephone fields in one form; the form must be handled, not just one field
  mask-on-blur    the mask formats only on blur - nothing looks wrong until the fill is done
  click-reveal    no form until the caller clicks; the fill must work on what appears late

Run: HERMES_E2E_BROWSER=1 <venv>/bin/python evals/vault_fill_fixtures_e2e.py
"""
from __future__ import annotations

import http.server
import json
import os
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
PHONE = "79991234567"          # 11 digits
TASK = "vault-fixtures-e2e"

# every page but two carries a phone identifier; plain and stateful carry an email
EMAIL_PAGES = {"plain", "stateful"}
# the value the vault holds for each page: shorter where the page asks for the short side
IDENTIFIER = {name: ("a@b.c" if name in EMAIL_PAGES else PHONE) for name in
              (p.stem for p in PAGES_DIR.glob("*.html"))}
IDENTIFIER["mask-short"] = "79991234"          # 8 digits where 11 fit

PAGES = sorted(p.stem for p in PAGES_DIR.glob("*.html"))


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        page = PAGES_DIR / (self.path.lstrip("/") + ".html")
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


def _values(names: list[str]) -> str:
    return "['" + "','".join(names) + "'].map(n => document.querySelector('[name=' + n + ']').value).join('|')"


def _digits(sup, origin, bvt) -> str:
    """Every digit held by every telephone field on the page."""
    sup.focus_page(origin, accept=bvt.TAB_PROBES["login"])
    dom = sup.evaluate_runtime(
        "JSON.stringify(Array.from(document.querySelectorAll('[type=tel],[type=email]')).map(e => e.value))")
    return "".join(c for c in "".join(json.loads(dom.get("result") or "[]")) if c.isdigit())


def main() -> int:
    # one server per page: each page gets its own origin, so an item is bound to exactly one form
    servers, origins = {}, {}
    for name in PAGES:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers[name], origins[name] = srv, f"http://127.0.0.1:{srv.server_address[1]}"

    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    try:
        for name in PAGES:
            _exec(f"new_tab({origins[name] + '/' + name!r}); wait_for_load()")

        sup = SUPERVISOR_REGISTRY.get(TASK)
        assert sup is not None, "browser_exec did not attach a supervisor for its task"
        print(f"supervisor attached; {len(PAGES)} pages: " + ", ".join(PAGES))

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
        from tools.browser_cdp_tool import _redact_cdp_output
        from agent.vault_store import get_vault_store

        store = get_vault_store()
        items = {name: store.add_item("login", name, {"identifier_type": "email" if name in EMAIL_PAGES else "phone",
                                                      "identifier": IDENTIFIER[name],
                                                      "password": PASSWORD}, origin=origins[name])
                 for name in PAGES}

        def fill(name: str) -> dict:
            inserted.clear()                                  # what THIS fill sends, nothing earlier
            return json.loads(bvt.browser_vault_fill(items[name].id, task_id=TASK))

        # plain - the control
        out = fill("plain")
        assert out["success"] and out["filled_fields"] == 2, out
        raw = json.dumps(out)
        assert PASSWORD not in raw
        sup.focus_page(origins["plain"], accept=bvt.TAB_PROBES["login"])
        dom = sup.evaluate_runtime(_values(["email", "pw"]))
        assert dom["result"] == f"a@b.c|{PASSWORD}", dom
        assert PASSWORD not in json.dumps(_redact_cdp_output({"result": {"value": dom["result"]}}))
        print("plain: identifier typed, password written by the guarded JS write, both land")

        # stateful - the page discards the write, so the password is typed as trusted input
        out = fill("stateful")
        assert out["success"] and out["filled_fields"] == 2, out
        assert PASSWORD not in json.dumps(out)
        sup.focus_page(origins["stateful"], accept=bvt.TAB_PROBES["login"])
        dom = sup.evaluate_runtime(_values(["email", "pw"]))
        assert dom["result"] == f"a@b.c|{PASSWORD}", dom      # the password survived the rewrite
        assert PASSWORD in "".join(inserted)                  # the fallback typed it as trusted input
        assert len([s for s in inserted if s == PASSWORD]) == 1  # exactly once, not per key
        print("stateful: the write was discarded and the password was typed as trusted input, no keystrokes")

        # mask-truncate - the message names the counts, not the cause
        out = fill("mask-truncate")
        assert not out["success"], out
        message = f"{out.get('message', '')} {out.get('hint', '')}"
        assert "9" in message and "11" in message, message    # kept 9 of the 11 digits
        assert PASSWORD not in "".join(inserted)              # the fill aborted before the password
        assert PASSWORD not in json.dumps(out) and PHONE not in json.dumps(out)
        assert _digits(sup, origins["mask-truncate"], bvt) == PHONE[:9]
        print("mask-truncate: kept 9 of 11 named in the message; the field holds the 9 the mask allows")

        # the length axis where the vault value FITS - an ordinary fill, nothing to report
        for name in ("mask-exact", "mask-short"):
            out = fill(name)
            assert out["success"] and out["filled_fields"] == 2, f"{name}: {out}"
            assert _digits(sup, origins[name], bvt) == IDENTIFIER[name], name
            print(f"{name}: {len(IDENTIFIER[name])} digits into a field that takes 11 - filled in full")

        # mask-reformat - digits survive, the string is rewritten
        out = fill("mask-reformat")
        assert out["success"] and out["filled_fields"] == 2, out
        assert _digits(sup, origins["mask-reformat"], bvt) == PHONE
        print("mask-reformat: the string was rewritten, all 11 digits judged and held")

        # mask-two-tel - two phone fields in one form must not confuse the fill: it fills the one
        # best candidate (the autocomplete=tel one) and refuses to guess with the second, because
        # a second tel box can be "confirm phone" or an unrelated "landline"
        out = fill("mask-two-tel")
        assert out["success"] and out["filled_fields"] == 2, out
        sup.focus_page(origins["mask-two-tel"], accept=bvt.TAB_PROBES["login"])
        dom = sup.evaluate_runtime(
            "JSON.stringify(Array.from(document.querySelectorAll('[type=tel]')).map(e => e.value))")
        held = json.loads(dom.get("result") or "[]")
        digits = ["".join(c for c in v if c.isdigit()) for v in held]
        assert digits == [PHONE, ""], held            # the right field, and only that one
        print("mask-two-tel: the autocomplete=tel field filled; the bare second box left alone")

        # mask-on-blur - formats only after the fill is done
        out = fill("mask-on-blur")
        assert out["success"] and out["filled_fields"] == 2, out
        assert _digits(sup, origins["mask-on-blur"], bvt) == PHONE
        print("mask-on-blur: filled, then the blur reformat kept all 11 digits")

        # click-reveal - the CALLER reveals the form, then the fill fills what appeared. The
        # reveal is driven through browser_exec because the tab it drives is the only candidate
        # focus_page will consider; with many tabs open the supervisor's session can otherwise
        # still be sitting on the previous page when the fill asks for this one.
        _exec(f"goto_url({origins['click-reveal'] + '/click-reveal'!r}); wait_for_load()")
        _exec("js(\"document.getElementById('go').click()\")")
        out = fill("click-reveal")
        assert out["success"] and out["filled_fields"] == 2, out
        assert _digits(sup, origins["click-reveal"], bvt) == PHONE
        print("click-reveal: filled the form the caller revealed")

        from agent import redact
        redact.clear_vault_redaction_values()
        print("FIXTURES E2E OK")
        return 0
    finally:
        SUPERVISOR_REGISTRY.stop_all()
        from tools.browser_session_lifecycle import cleanup_all_browsers
        cleanup_all_browsers()
        for srv in servers.values():
            srv.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
