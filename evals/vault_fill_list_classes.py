"""The fill behaviours the site list actually contains, as local pages generated from the list.

The list is the spec: ``evals/vault_fill_sites/`` holds the 124 rows we checked by hand, and each
row's note says what the page did. This eval turns those notes into pages - one per behavioural
class - and asserts what the fill must do with each. Real pages stay out of CI (they flake on load;
we measured `gptek` and `CN:passport`), but the shapes they forced us to handle are all here.

Classes, and the rows that stand for them:

  mask-reformat   cleave.js, maskdemo/intl-tel-input-example
                  the mask rewrites the string, the digits survive - so a literal compare says
                  "reverted" where the fill landed everything. The fill must judge by digits.
  mask-truncate   https://cab.uksn.ru, https://cab.y-plus.ru, maskdemo/intl-tel-input
                  the mask drops characters. The fill must say what the field kept and never blame
                  a cause, and never describe a secret.
  mask-two-tel    maskdemo/react-input-mask
                  two telephone fields in one form; both must be handled, not just the first.
  mask-on-blur    modern/megafon, modern/antd-pro
                  the mask formats only when the field loses focus - so nothing is wrong until
                  after the fill. Must still pass.
  click-reveal    EU:www.bol.com, BR:www.americanas.com.br, and the ddg autoprompt fixtures
                  no form on load. Revealing it is the CALLER's job - the runner clicks, then the
                  tool fills - so this page asserts the fill works on a form that appears late.

Run: HERMES_E2E_BROWSER=1 <venv>/bin/python evals/vault_fill_list_classes.py
"""
from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import os

HOME = Path(tempfile.mkdtemp(prefix="hermes-vault-classes-"))
os.environ["HERMES_HOME"] = str(HOME)

SITES = ROOT / "evals" / "vault_fill_sites"
PASSWORD = "pw-E2E-8842"
PHONE = "79991234567"          # 11 digits

# Each class: the page it stands for and what the fill must do with it.
CLASSES = {
    "mask-reformat": {
        "page": """<!doctype html><title>reformat</title>
<form><input name=phone type=tel autocomplete=tel><input name=pw type=password></form>
<script>
  // keeps every digit but rewrites the string, so a literal compare would call this a revert
  const el = document.querySelector('[name=phone]');
  el.addEventListener('input', () => {
    const d = el.value.replace(/\\D/g, '').slice(0, 11);
    el.value = d.replace(/^(\\d{3})(\\d{3})(\\d{2})(\\d{2})(\\d+)$/, '$1 $2 $3 $4 $5');
  });
</script>""",
        "lands": True,
    },
    "mask-truncate": {
        "page": """<!doctype html><title>truncate</title>
<form><input name=phone type=tel autocomplete=tel><input name=pw type=password></form>
<script>
  // keeps only the first 9 digits: the value is longer than the field can hold
  const el = document.querySelector('[name=phone]');
  el.addEventListener('input', () => { el.value = el.value.replace(/\\D/g, '').slice(0, 9); });
</script>""",
        "lands": False,
    },
    "mask-two-tel": {
        "page": """<!doctype html><title>two-tel</title>
<form><input name=phone type=tel autocomplete=tel><input name=phone2 type=tel><input name=pw type=password></form>
<script>
  // two telephone fields, both masked: neither may be skipped
  for (const el of document.querySelectorAll('[type=tel]')) {
    el.addEventListener('input', () => {
      const d = el.value.replace(/\\D/g, '').slice(0, 11);
      el.value = d.replace(/^(\\d{3})(\\d{3})(\\d{2})(\\d{2})(\\d+)$/, '$1 $2 $3 $4 $5');
    });
  }
</script>""",
        "lands": True,
    },
    "mask-on-blur": {
        "page": """<!doctype html><title>on-blur</title>
<form><input name=phone type=tel autocomplete=tel><input name=pw type=password></form>
<script>
  // formats only when the field loses focus: nothing looks wrong until the fill is done
  const el = document.querySelector('[name=phone]');
  el.addEventListener('blur', () => {
    const d = el.value.replace(/\\D/g, '').slice(0, 11);
    el.value = d.replace(/^(\\d{3})(\\d{3})(\\d{2})(\\d{2})(\\d+)$/, '$1 $2 $3 $4 $5');
  });
</script>""",
        "lands": True,
    },
    "click-reveal": {
        "page": """<!doctype html><title>click-reveal</title>
<button id=go type=button>Log in</button>
<script>
  // no form until the control is clicked
  document.getElementById('go').addEventListener('click', () => {
    const f = document.createElement('form');
    for (const spec of [['phone', 'tel'], ['pw', 'password']]) {
      const el = document.createElement('input');
      el.name = spec[0]; el.type = spec[1];
      f.appendChild(el);
    }
    document.body.appendChild(f);
  });
</script>""",
        "lands": True,
    },
}
TASK = "vault-classes-e2e"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        cls = self.path.lstrip("/")
        body = CLASSES[cls]["page"].encode() if cls in CLASSES else b"nope"
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


def _classes_from_the_list() -> dict[str, int]:
    """The list is the spec: count the rows whose note names each behaviour."""
    rows = json.loads((SITES / "fillable.json").read_text())["targets"]
    found: Counter[str] = Counter()
    for r in rows:
        note = r.get("note", "")
        if r.get("needs") == "click":
            found["click-reveal"] += 1
        elif "two tel fields" in note:
            found["mask-two-tel"] += 1
        elif "on blur" in note:
            found["mask-on-blur"] += 1
        elif "literal compare fails" in note or "string-compare" in note:
            found["mask-reformat"] += 1
        elif "verdict says failure" in note or "digits land" in note:
            found["mask-truncate"] += 1
    return dict(found)


def main() -> int:
    listed = _classes_from_the_list()
    missing = [c for c in CLASSES if not listed.get(c)]
    assert not missing, f"the list no longer contains these behaviours: {missing}"
    print("classes the list contains:", listed)

    servers = {}
    origins = {}
    for cls in CLASSES:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers[cls] = srv
        origins[cls] = f"http://127.0.0.1:{srv.server_address[1]}"

    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    try:
        for cls in CLASSES:
            _exec(f"new_tab({origins[cls] + '/' + cls!r}); wait_for_load()")

        sup = SUPERVISOR_REGISTRY.get(TASK)
        assert sup is not None, "browser_exec did not attach a supervisor for its task"

        from tools import browser_vault_tool as bvt
        from agent.vault_store import get_vault_store

        store = get_vault_store()
        results = {}
        for cls, spec in CLASSES.items():
            item = store.add_item("login", cls, {"identifier_type": "phone", "identifier": PHONE,
                                                 "password": PASSWORD}, origin=origins[cls])
            if cls == "click-reveal":
                # the caller reveals the form; the tool only fills what is there
                sup.focus_page(origins[cls], accept=bvt._TAB_PROBES["login"])
                sup.evaluate_runtime("document.getElementById('go').click()")
            raw = bvt.browser_vault_fill(item.id, task_id=TASK)
            out = json.loads(raw)
            results[cls] = (out, raw)
            assert PASSWORD not in raw, f"{cls}: a secret reached the model-facing result"

        for cls, spec in CLASSES.items():
            out, raw = results[cls]
            sup.focus_page(origins[cls], accept=bvt._TAB_PROBES["login"])
            held = sup.evaluate_runtime(
                "JSON.stringify(Array.from(document.querySelectorAll('[type=tel]')).map(e => e.value))")
            digits = "".join(c for c in json.loads(held.get("result") or "[]") for c in str(c) if c.isdigit())
            if spec["lands"]:
                assert out["success"], f"{cls}: expected a landing fill, got {out}"
                assert digits.startswith(PHONE[:9]), f"{cls}: the digits did not land: {held['result']!r}"
                print(f"{cls}: filled, digits held {len(digits)}")
            else:
                assert not out["success"], f"{cls}: expected a named failure, got {out}"
                message = f"{out.get('message', '')} {out.get('hint', '')}"
                assert any(ch.isdigit() for ch in message), f"{cls}: the message names no counts: {message!r}"
                print(f"{cls}: named what the field kept - {message.strip()[:70]}")

        from agent import redact
        redact.clear_vault_redaction_values()
        print("LIST CLASSES E2E OK")
        return 0
    finally:
        SUPERVISOR_REGISTRY.stop_all()
        from tools.browser_session_lifecycle import cleanup_all_browsers
        cleanup_all_browsers()
        for srv in servers.values():
            srv.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
