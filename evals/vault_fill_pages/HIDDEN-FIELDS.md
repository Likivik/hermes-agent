# Hidden fields, honeypots, and what the research says

Why `honeypot-offscreen.html` exists, and why the inspection refuses a field it cannot see. Kept
apart from the pages so the evidence does not live only in a chat log.

## What a honeypot is for

A form ships an input no person can see and never fills. A human submission returns it empty. A
script that writes into every input it finds returns it full — and the server drops that submission
as automation. So the trap tests the *filler*, not the form.

## How the trap is hidden, and why `display:none` is the weak version

The trade moved off `display:none` because naive bots skip it. The recommended pattern today is:

```html
<input name="fax_number" autocomplete="new-password" tabindex="-1" aria-hidden="true"
       style="position:absolute; left:-9999px; width:1px; height:1px; opacity:0">
```

Off-screen, one pixel, transparent, still laid out — invisible to a person, present to a DOM
walker. Note the name: not `honeypot` or `trap`, but something a real form might carry.

## The measured damage

- **ACSAC'24, "Leaky Autofill: An Empirical Study on the Privacy Threat of Password Managers'
  Autofill Functionality"** (Fu & Wang, Nankai University) — 30 password managers, 15 concealment
  techniques, 1032 scenarios:
  - every manager fills hidden fields in at least one form; overall 58.7%;
  - **login forms are the worst case at 65.7%**;
  - `display:none` / `visibility:hidden` / `hidden` are resisted (21.74% filled overall — but
    built-in browser managers still fill login forms there 83.3% of the time);
  - the other 12 techniques are filled **68%** of the time; `clip-path` and `content-visibility`
    are filled with passwords by **every** manager tested;
  - built-in browser managers fill hidden fields 4.07× more often than separately installed ones.
  - <https://cssnku.github.io/uploads/publications/acsac24-pm.pdf>
- **A field report of the false positive** — a password manager autofilled an off-screen field
  named `website` on a password-reset form; the site flagged those submissions as bots and the
  resets failed silently for real users:
  <https://cornel.co/articles/when-your-honeypot-protection-blocks-real-users-using-autofill/>
- **Formspring's honeypot documentation** — the off-screen pattern above, plus the reason:
  off-screen rather than `display:none`:
  <https://formspring.io/docs/spam-protection/honeypot>
- **Stack Overflow, honeypot traps vs browser autofill** — Chrome ignores `autocomplete=off`
  (including `nope`, `false`, `new-password` per that thread), so traps keep being filled:
  <https://stackoverflow.com/questions/52760086/honeypot-spam-trap-conflicts-with-browsers-auto-fill-feature>
- **Chromium PSA, 2014** — "by default, Chrome ignores `autocomplete='off'` for password fields…
  This does not affect non-password fields":
  <https://groups.google.com/a/chromium.org/g/chromium-dev/c/zhhj7hCip5c>
- **MDN** — "many modern browsers do not support `autocomplete='off'` for login fields":
  <https://developer.mozilla.org/en-US/docs/Web/Security/Practical_implementation_guides/Turning_off_form_autocompletion>

## What our inspection does now

`build_inspection_js` drops any control that fails its visibility test: `display:none`,
`visibility:hidden`, no client rects, `checkVisibility()` with opacity and content-visibility,
a box smaller than 4×4, a box off the canvas (left/right/bottom), `opacity:0`, `clip-path`, or
`content-visibility:hidden`.

Not covered: a field of normal size covered by an overlay ("covered-by-overlay" is one of the 12
techniques in the study). Detecting it needs `elementFromPoint` and risks skipping a real field
under a sticky header, so it is left out until a fixture shows it matters.

## The holes our fixtures close

- `plain.html` — the control: a form with nothing special.
- `honeypot-offscreen.html` — the off-canvas 1×1 transparent trap and a laid-out transparent trap
  beside a real email + password. Declares: two fields filled, both traps empty.
