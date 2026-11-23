"""How a masked field is recovered when a bulk insert lands wrong.

The ladder runs against a fake supervisor that behaves like ``CDPSupervisor`` where it matters:
keystrokes are pinned to a page session, page scripts are answered by kind, and ``press_key``
records the keys it was asked to press. The pages the live matrix measured are modelled here as
verdicts: a field the mask mangles on a bulk insert and accepts after a select-all retype (the
cabinet), a field that refuses both and is reported with its counts (the phone widget), and a
channel that cannot press a key - which recovers the same way, since a key press is not needed.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from agent.vault_store import VaultStore
from tests.bot_desktop_lease import reset_lease
from tools.bot_desktop import runtime

PASSWORD = "s3cret-pw"
IDENTIFIER = "user-7731@example.com"
_ID = {"autocomplete": "username", "formIndex": 0, "index": 0, "label": "", "name": "email", "type": "email"}
_PW = {"autocomplete": "current-password", "formIndex": 0, "index": 1, "label": "", "name": "pw", "type": "password"}
_LOGIN = [_ID, _PW]


class _Supervisor:
    """A page whose verdicts come from a plan: one entry per read-back, popped in order.

    ``press_ok`` is whether the clear key reaches the page at all; ``clear_script_works`` is whether
    emptying the value out from under the mask leaves it empty. Every keystroke, key press and clear
    is recorded in call order, so a test can say which recovery ran first.
    """

    def __init__(self, plan, *, press_ok=True, clear_script_works=True, can_press=True, counts=None):
        self.plan = list(plan)
        # what the field kept, as the read-back reports it: counts only, never a value
        self.counts = counts if counts is not None else [
            {"index": 0, "typed": 20, "held": 18, "typed_digits": 11, "held_digits": 9,
             "kept": 18, "kept_digits": 9}]
        self.press_ok = press_ok
        self.clear_script_works = clear_script_works
        self.sid, self.document = "S1", 1
        self.typed: list[str] = []
        self.keys: list[str] = []
        self.calls: list[str] = []
        self.empty_after_clear_script = False
        if can_press:
            self.press_key = self._press_key

    def current_page_session(self):
        return self.sid

    def focus_page(self, origin, accept=None):
        return {"ok": True, "url": origin + "/login", "session_id": self.sid}

    def evaluate_runtime(self, expression, *, session_id=None, **kwargs):
        if session_id is not None and session_id != self.sid:
            return {"ok": False, "error": "the page session changed since it was validated", "code": "session_changed"}
        return {"ok": True, "result": self._answer(expression), "document": self.document}

    def _answer(self, expression):
        if "location.href" in expression:
            return "https://example.com/login"
        if "ok: true" in expression:
            return {"ok": True}
        if "deleteContentBackward" in expression:
            # The fill's value write. On a page whose mask keeps its own count this leaves the field
            # looking empty while the mask is stale; the model says whether the value really holds.
            self.calls.append("clear_script")
            self.empty_after_clear_script = True
            return {"cleared": 1}
        if "reverted" in expression:
            self.calls.append("verify")
            fills = expression.count('"index"')
            verdict = self.plan.pop(0) if self.plan else "landed"
            if verdict == "landed":
                return {"landed": fills, "reverted": 0, "missing": [], "counts": []}
            return {"landed": 0, "reverted": fills, "missing": ["email"], "counts": list(self.counts)}
        if "focus_lost" in expression:
            self.calls.append("focus")
            return {"focused": int(expression.split('"index": ', 1)[1].split(",", 1)[0])}
        if "unstamped" in expression:
            return {"unstamped": True}
        if "filled }" in expression:
            return {"filled": 1}
        return _LOGIN

    def insert_text(self, text, *, session_id, document=None, **kwargs):
        assert session_id == self.sid and document == self.document
        self.typed.append(text)
        self.calls.append("insert")
        return {"ok": True}

    def _press_key(self, key, *, session_id, document=None, **kwargs):
        assert session_id == self.sid and document == self.document
        self.keys.append(key)
        self.calls.append("press")
        return {"ok": True} if self.press_ok else {"ok": False, "error": "the page refused the key"}


@pytest.fixture(autouse=True)
def _fresh_lease(monkeypatch):
    reset_lease()
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":37"})
    yield
    reset_lease()


def _fill(sup, tmp_path):
    from tools import browser_vault_tool

    store = VaultStore(base_dir=tmp_path / "vault")
    meta = store.add_item(kind="login", label="Example", origin="https://example.com",
                          secret={"identifier_type": "email", "identifier": IDENTIFIER,
                                  "password": PASSWORD, "origin": "https://example.com"})
    with ExitStack() as stack:
        stack.enter_context(patch("agent.vault_store.get_vault_store", return_value=store))
        stack.enter_context(patch.object(browser_vault_tool, "_ensure_supervisor", return_value=sup))
        stack.enter_context(patch.object(browser_vault_tool, "_page_backend", return_value=None))
        stack.enter_context(patch.object(browser_vault_tool, "_bot_desktop_browser_session", return_value=False))
        stack.enter_context(patch.object(browser_vault_tool, "_FILL_VERIFY_STABLE_S", 0))
        return json.loads(browser_vault_tool.browser_vault_fill(meta.id, task_id="t", with_identifier=True))


def test_a_masked_field_is_retyped_over_the_selection(tmp_path):
    """The cabinet: a bulk insert loses a character, and the value is retyped one key at a time over
    the selection the focus check made. No clear runs at all — a value written out from under a mask
    leaves the mask holding a stale count, and the retype then loses characters."""
    sup = _Supervisor(["reverted", "landed"])
    out = _fill(sup, tmp_path)
    assert out["success"] is True
    assert sup.keys == []                                  # no key press is needed
    assert "clear_script" not in sup.calls
    assert sup.calls.index("insert") < sup.calls.index("verify")   # the bulk insert comes first
    assert sup.typed[0] == IDENTIFIER                      # the bulk insert
    assert "".join(sup.typed[1:]) == IDENTIFIER            # one key at a time, over the selection


def test_when_the_retype_does_not_hold_the_fill_reports_what_the_field_kept(tmp_path):
    """A field that refuses the bulk insert and the retype is reported, never repaired further."""
    sup = _Supervisor(["reverted", "reverted"], clear_script_works=True)
    out = _fill(sup, tmp_path)
    assert out["success"] is False
    assert out["error_type"] == "fill_reverted"
    assert out["error"] == "The field kept 9 of the 11 digits of the stored value."
    assert sup.calls.count("clear_script") == 0            # no value write at all
    assert sup.typed[0] == IDENTIFIER
    assert "".join(sup.typed[1:]) == IDENTIFIER            # one retype, then it stops


def test_when_nothing_holds_the_field_keeps_its_value_and_the_counts_are_named(tmp_path):
    """A field that refuses every value keeps what it took, and the failure says how much.

    Wiping it would hide the mismatch: the counts are the only thing the caller can act on."""
    sup = _Supervisor(["reverted", "reverted", "reverted"])
    out = _fill(sup, tmp_path)
    assert out["success"] is False
    assert out["error_type"] == "fill_reverted"
    assert out["error"] == "The field kept 9 of the 11 digits of the stored value."
    assert out["hint"] == ("Only 9 of 11 digits stayed in the field. "
                           "Check the value stored for this site.")
    # no value write anywhere: the field is never cleared out from under a mask
    assert sup.calls.count("clear_script") == 0


def test_a_value_with_no_digits_is_counted_in_characters(tmp_path):
    """A username is not a phone: the message names characters, not digits."""
    sup = _Supervisor(["reverted", "reverted", "reverted"],
                      counts=[{"index": 0, "typed": 13, "held": 11, "typed_digits": 0,
                               "held_digits": 0, "kept": 11, "kept_digits": 0}])
    out = _fill(sup, tmp_path)
    assert out["error"] == "The field kept 11 of the 13 characters of the stored value."
    assert out["hint"].startswith("Only 11 of 13 characters stayed in the field.")


def test_a_mask_that_adds_its_own_digit_is_not_counted_as_kept(tmp_path):
    """The field shows 11 digits and the value has 13, but one of the field's digits is the mask's
    own country code. Only 10 came from the value, and only those may be called kept."""
    sup = _Supervisor(["reverted", "reverted", "reverted"],
                      counts=[{"index": 0, "typed": 13, "held": 11, "typed_digits": 13,
                               "held_digits": 11, "kept": 10, "kept_digits": 10}])
    out = _fill(sup, tmp_path)
    assert out["error"] == "The field kept 10 of the 13 digits of the stored value."
    assert "11" not in out["error"]


def test_a_read_back_without_counts_still_names_the_failure(tmp_path):
    """A read-back that cannot say what the field kept must not invent numbers."""
    sup = _Supervisor(["reverted", "reverted", "reverted"], counts=[])
    out = _fill(sup, tmp_path)
    assert out["success"] is False
    assert "Nothing is filled." in out["error"]
    assert "hint" not in out


def test_the_retype_needs_no_key_press(tmp_path):
    """Typing over a selection replaces it, so a channel that cannot press a key (a plugin page, an
    older build) recovers the same way: no key press, no clear, just the retype."""
    sup = _Supervisor(["reverted", "landed"], can_press=False)
    out = _fill(sup, tmp_path)
    assert out["success"] is True
    assert sup.keys == []
    assert sup.calls.count("clear_script") == 0
    assert sup.typed[0] == IDENTIFIER
    assert "".join(sup.typed[1:]) == IDENTIFIER
