"""Live E2E for the three fill behaviours the site matrix found, each on its own local page.

Mirrors ``vault_fill_live_e2e.py``: real browser_exec, real supervisor, real ``browser_vault_fill``,
and the secret absent from every model-facing result. The pages are inline and self-contained so the
test does not depend on any fixture checkout.

What each page pins down:

  /plain     the happy path - the identifier is typed, the password goes by the guarded JS write,
             both land and hold. Control for the other two.
  /stateful  a form that keeps its own state and rewrites any value written without a trusted input
             event. The JS write is discarded; the password must then be typed as a trusted insert
             (``Input.insertText``: a trusted ``input``, no keydown/keyup) and survive.
  /mask      a phone mask that keeps only its first 9 digits. The value must not be reported as a
             revert but as what the field kept, and the message must name the counts - while never
             describing a secret.

Run: HERMES_E2E_BROWSER=1 <venv>/bin/python evals/vault_fill_masks_e2e.py
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
HOME = Path(tempfile.mkdtemp(prefix="hermes-vault-masks-"))
os.environ["HERMES_HOME"] = str(HOME)

PASSWORD = "pw-E2E-8842"
PHONE = "79991234567"          # 11 digits; the mask keeps 9

PAGES = {
    "/plain": b"""<!doctype html><title>plain</title>
<form><input name=email type=email autocomplete=username><input name=pw type=password autocomplete=current-password></form>""",
    "/stateful": b"""<!doctype html><title>stateful</title>
<form><input id=email name=email type=email><input id=pw name=pw type=password></form>
<script>
  // Keeps its own state and believes only edits a human made: a value written from script is
  // refused at the setter, so it is never there at all - no read-back can catch one in place and
  // the fill always meets the discard. A real page reconciles on a timer instead; the setter is
  // the same rule made synchronous so this test cannot race it.
  //
  // The guard sits on the PROTOTYPE, not on the element: a value written through the element's own
  // property can be bypassed by calling the native setter, and the guarded JS fill does exactly
  // that. Typed text does not pass through the setter at all, so a human - or Input.insertText -
  // still lands.
  const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  Object.defineProperty(HTMLInputElement.prototype, 'value', {
    get() { return desc.get.call(this); },
    set(v) { /* an untrusted write is dropped */ },
  });
</script>""",
    "/mask": b"""<!doctype html><title>mask</title>
<form><input id=phone name=phone type=tel autocomplete=tel><input name=pw type=password></form>
<script>
  const el = document.getElementById('phone');
  el.addEventListener('input', () => { el.value = el.value.replace(/\\D/g, '').slice(0, 9); });
</script>""",
    # the same mask family, but the vault value fits: 11 digits where 11 are allowed, and 9 where 9
    # are enough. Only the first case is a limit; these two must simply fill.
    "/mask-exact": b"""<!doctype html><title>mask-exact</title>
<form><input id=phone name=phone type=tel autocomplete=tel><input name=pw type=password></form>
<script>
  const el = document.getElementById('phone');
  el.addEventListener('input', () => { el.value = el.value.replace(/\\D/g, '').slice(0, 11); });
</script>""",
    "/mask-short": b"""<!doctype html><title>mask-short</title>
<form><input id=phone name=phone type=tel autocomplete=tel><input name=pw type=password></form>
<script>
  const el = document.getElementById('phone');
  el.addEventListener('input', () => { el.value = el.value.replace(/\\D/g, '').slice(0, 11); });
</script>""",
}
TASK = "vault-masks-e2e"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path, b"nope")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _exec(code: str) -> dict:
    from tools import browser_use_cli as bu

    out = json.loads(bu.browser_exec(code, task_id=TASK, timeout_s=90))
    assert out.get("success"), out
    return out


def _serve() -> tuple[http.server.ThreadingHTTPServer, str]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _values(names: list[str]) -> str:
    return "['" + "','".join(names) + "'].map(n => document.querySelector('[name=' + n + ']').value).join('|')"


def main() -> int:
    # one server per page: each page gets its own origin, so an item is bound to exactly one form
    servers = {}
    origins = {}
    for path in PAGES:
        srv, origin = _serve()
        servers[path] = srv
        origins[path] = origin

    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    try:
        for path in PAGES:
            _exec(f"new_tab({origins[path] + path!r}); wait_for_load()")

        sup = SUPERVISOR_REGISTRY.get(TASK)
        assert sup is not None, "browser_exec did not attach a supervisor for its task"
        print("supervisor attached; pages:", ", ".join(origins[p] + p for p in PAGES))

        # every character the fill sends into the page goes through insert_text and nothing else:
        # recording it is how this eval proves what was typed, and that a secret never travelled as a
        # keystroke (the real supervisor has no key path at all).
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
        login = store.add_item("login", "plain", {"identifier_type": "email", "identifier": "a@b.c",
                                                  "password": PASSWORD}, origin=origins["/plain"])
        stateful = store.add_item("login", "stateful", {"identifier_type": "email", "identifier": "a@b.c",
                                                        "password": PASSWORD}, origin=origins["/stateful"])
        phone = store.add_item("login", "mask", {"identifier_type": "phone", "identifier": PHONE,
                                                 "password": PASSWORD}, origin=origins["/mask"])
        exact = store.add_item("login", "mask-exact", {"identifier_type": "phone", "identifier": PHONE,
                                                       "password": PASSWORD}, origin=origins["/mask-exact"])
        short = store.add_item("login", "mask-short", {"identifier_type": "phone", "identifier": "79991234",
                                                       "password": PASSWORD}, origin=origins["/mask-short"])

        # /plain - the control
        raw = bvt.browser_vault_fill(login.id, task_id=TASK)
        out = json.loads(raw)
        assert out["success"] and out["filled_fields"] == 2, out
        assert PASSWORD not in raw
        sup.focus_page(origins["/plain"], accept=bvt._TAB_PROBES["login"])
        dom = sup.evaluate_runtime(_values(["email", "pw"]))
        assert dom["result"] == f"a@b.c|{PASSWORD}", dom
        assert PASSWORD not in json.dumps(_redact_cdp_output({"result": {"value": dom["result"]}}))
        print("plain: identifier typed, password written by the guarded JS write, both land")

        # /stateful - the page discards the write, so the password is typed as trusted input
        inserted.clear()
        raw = bvt.browser_vault_fill(stateful.id, task_id=TASK)
        out = json.loads(raw)
        assert out["success"] and out["filled_fields"] == 2, out
        assert PASSWORD not in raw
        sup.focus_page(origins["/stateful"], accept=bvt._TAB_PROBES["login"])
        dom = sup.evaluate_runtime(_values(["email", "pw"]))
        assert dom["result"] == f"a@b.c|{PASSWORD}", dom      # the password survived the rewrite
        assert PASSWORD in "".join(inserted)                   # the fallback typed it as trusted input
        assert len([s for s in inserted if s == PASSWORD]) == 1 # exactly once, not per key
        assert PASSWORD not in json.dumps(_redact_cdp_output({"result": {"value": dom["result"]}}))
        print("stateful: the write was discarded and the password was typed as trusted input, no keystrokes")

        # /mask - a mask that truncates: the message names the counts, not the cause
        inserted.clear()                                       # what THIS fill sends, nothing earlier
        raw = bvt.browser_vault_fill(phone.id, task_id=TASK)
        out = json.loads(raw)
        assert not out["success"], out
        message = f"{out.get('message', '')} {out.get('hint', '')}"
        assert "9" in message and "11" in message, message     # kept 9 of the 11 digits
        assert PASSWORD not in "".join(inserted)               # the fill aborted before the password
        assert PASSWORD not in raw and PHONE not in raw
        sup.focus_page(origins["/mask"], accept=bvt._TAB_PROBES["login"])
        dom = sup.evaluate_runtime(_values(["phone"]))
        assert dom["result"] == PHONE[:9], dom                 # exactly what the mask allowed
        print("mask: kept 9 of 11 named in the message; the field holds the 9 the mask allows")

        # the same mask family where the vault value FITS. Only a vault value longer than the field
        # is a limit; a value that fits exactly, or is shorter, must simply fill.
        for label, item, value in (("mask-exact", exact, PHONE), ("mask-short", short, "79991234")):
            inserted.clear()
            out = json.loads(bvt.browser_vault_fill(item.id, task_id=TASK))
            assert out["success"] and out["filled_fields"] == 2, f"{label}: {out}"
            sup.focus_page(origins["/" + label], accept=bvt._TAB_PROBES["login"])
            dom = sup.evaluate_runtime("document.querySelector('[name=phone]').value")
            digits = "".join(c for c in (dom.get("result") or "") if c.isdigit())
            assert digits == value, f"{label}: {digits!r} != {value!r}"
            print(f"{label}: {len(value)} digits into a field that takes 11 - filled in full")

        from agent import redact
        redact.clear_vault_redaction_values()
        print("MASKS E2E OK")
        return 0
    finally:
        SUPERVISOR_REGISTRY.stop_all()
        from tools.browser_session_lifecycle import cleanup_all_browsers
        cleanup_all_browsers()
        for srv in servers.values():
            srv.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
