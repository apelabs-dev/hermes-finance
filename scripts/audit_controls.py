"""The controls gate, deadctl (docs/ui-invariants.md rule 21): every visible, enabled control on a page does something
you can see when it's clicked. A button with no handler, or one whose handler toggles a class on something that stays
hidden ("See suggestions" opening a panel that is still display: none), fails.

On each page, as it loads (no sheet or menu opened first), it lists the controls: buttons, the ARIA roles that act like
one (button, tab, switch, menu item, option, checkbox, radio), <summary>, check boxes and radio buttons, links that
lead nowhere (no href, "#", "javascript:"), and anything else the page styles as clickable (the outermost element of a
cursor: pointer region: a table row that opens its sheet). Not: a link with a real href (the browser follows it; this
gate never does: a link to another site would leave the machine), a select or a text field (the browser draws them),
a control that is hidden, disabled (disabled, aria-disabled, a disabled fieldset, inert, pointer-events: none) or
listed in ALLOW, and the option already chosen in a set of options (a segmented control's pressed button, the selected
tab, the kind chip in use: clicking it again changes nothing, by design). The same control repeated (one per transaction row) is clicked once: controls are grouped by their
signature (where they are, their tag, role and classes, and their label when the group is small), and the first of
each group is clicked.

Each one is clicked as a person would: scrolled into view, the page recorded once it has settled (cdp.Chrome.settled)
with the mouse resting over no control, then the mouse moved onto a point of the control that nothing covers, pressed
and released (hovering is part of it: a help button opens its tip on hover, and its click pins it). It does something
when, within about a second (the page settled again, and a full second gone before a "nothing happened" counts), any
of these happened:

  - something visible changed: an element appeared, went or moved, or its text, colour, background, border colour,
    opacity, transform, value or checked state changed (visible: Element.checkVisibility with opacity and visibility);
    on the control itself, its text, classes, value, checked state or an aria-pressed / -expanded / -checked /
    -selected / -current (its own :hover / :focus look doesn't count);
  - the address changed (a route), a dialog was asked for (alert, confirm: answered "cancel"), a window or a download
    was asked for, a form was submitted, or a request that changes something (POST, PUT, PATCH, DELETE, a beacon) was
    sent. Those requests never reach the server: the page's fetch, XMLHttpRequest and sendBeacon answer them with an
    empty 200 themselves, so the throwaway DB is never written to;
  - the focus moved to another element (a "Search" button that focuses its field).

Between two clicks the page is put back, and checked against its record (with its scroll positions put back, and a
toast still fading out not counted): after a route, history.back(); otherwise Escape (a sheet, a menu, a tip), then a
second click (a toggle). When it still differs, a fresh load with the localStorage and sessionStorage it started with.
The page is recorded by where each element is, not by the element, so a page that draws itself again, the same, is
the same page.

The matrix (reduced: each control is the same code on every DB and in both themes): the full and nocaps DBs (nocaps:
the budget suggestions, "Track a loan"), at 1440 and 390, on every page, dark. The app's shell (the sidebar, the top
bar, the phone tab bar: everything outside #page) is clicked on the Overview only; the other pages click their own.
A self-test first plants two dead buttons and a live one on the Overview, and the run stops if it misses either dead
one or flags the live one.

    python scripts/audit_controls.py BASE_URL [--label full] [--out DIR] [--widths 1440,390] [--pages overview,...]

writes <out>/controls-report.json and exits 1 on any dead control.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))
from audit_layout import HEIGHT, PAGES  # noqa: E402
from cdp import READY_NOW, Chrome, parked  # noqa: E402

KINDS = ("full", "nocaps")         # gate_dbs kinds the gate runs on (run_gates / tests/test_gates.py)
WIDTHS = (1440, 390)
ALL_PAGES = (*PAGES, "settings")
WORKERS = 4                        # headless Chromes at once (cdp.Chrome holds them to HF_CHROMES machine-wide as well)
WAIT_S = 1.0                       # how long a click has to show something
SETTLE_MS = 1500                   # settled()'s cap after a click (a slower page is reported as unsettled, not dead)

# Controls allowed to show nothing when clicked: {signature substring: why}. Normally empty: a control that does nothing
# you can see is a bug (fix it, or don't draw it as a control). An entry needs the reason it's right as it is.
ALLOW: dict[str, str] = {}

# Installed before any page script (Page.addScriptToEvaluateOnNewDocument): what a click asked for is written down in
# window.__hfc, and nothing it asks for leaves the page. A request that changes something is answered here with an
# empty 200; a dialog is answered "cancel"; a window, a download, a form submitted natively and a link to another
# origin are refused.
STUBS = r"""(() => {
  const rec = window.__hfc = {asked: []};
  const ask = (what) => { rec.asked.push(String(what).slice(0, 120)); };
  const SAFE = ['GET', 'HEAD', 'OPTIONS'];
  const realFetch = window.fetch;
  window.fetch = function (input, init) {
    const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    if (!SAFE.includes(method)) {
      ask(`${method} ${input && input.url ? input.url : input}`);
      return Promise.resolve(new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}}));
    }
    return realFetch.apply(this, arguments);
  };
  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) { this.__hfc = [String(method).toUpperCase(), url]; return open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    if (this.__hfc && !SAFE.includes(this.__hfc[0])) { ask(this.__hfc.join(' ')); return; }
    return send.apply(this, arguments);
  };
  navigator.sendBeacon = (url) => { ask(`BEACON ${url}`); return true; };
  window.open = (url) => { ask(`window.open ${url}`); return null; };
  window.alert = (m) => ask(`alert ${m}`);
  window.confirm = (m) => { ask(`confirm ${m}`); return false; };
  window.prompt = (m) => { ask(`prompt ${m}`); return null; };
  // on window, bubbling: after the page's own handlers, so only what they let through is stopped here
  addEventListener('submit', (e) => { if (!e.defaultPrevented) { e.preventDefault(); ask('form submitted'); } });
  addEventListener('click', (e) => {
    const a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a || e.defaultPrevented) return;
    if (a.hasAttribute('download') || a.protocol === 'blob:' || a.protocol === 'data:') { e.preventDefault(); ask(`download ${a.href}`); }
    else if (a.origin !== location.origin) { e.preventDefault(); ask(`link to ${a.href}`); }
  });
})();"""

# The page's controls (see the module docstring), one per signature, in document order; the elements are kept in
# window.__hfc.list for the clicks. `zone`: 'page' (inside #page), 'shell' (outside it) or 'all'.
ENUM = r"""((zone) => {
  const ok = (el) => {
    if (!el.isConnected || !el.checkVisibility({opacityProperty: true, visibilityProperty: true})) return false;
    if (el.closest('[hidden], template, [aria-hidden="true"], [inert], details:not([open]) > :not(summary)')) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    if (el.disabled || el.closest('fieldset[disabled]') || el.closest('[aria-disabled="true"]')) return false;
    return getComputedStyle(el).pointerEvents !== 'none';
  };
  const SEL = 'button, [role=button], [role=tab], [role=switch], [role=menuitem], [role=menuitemradio], [role=menuitemcheckbox], '
    + '[role=option], [role=checkbox], [role=radio], summary, input[type=checkbox], input[type=radio], input[type=button], '
    + 'input[type=submit], input[type=reset], a:not([href]), a[href="#"], a[href^="javascript:"]';
  const NATIVE = 'a[href]:not([href="#"]):not([href^="javascript:"]), select, textarea, '
    + 'input:not([type=checkbox]):not([type=radio]):not([type=button]):not([type=submit]):not([type=reset])';
  // the option already chosen in a set of options (a segmented control's pressed button, the selected tab, the
  // Transactions kind chip in use): a click on it changes nothing, by design (settings.js seg(): `if (k !== cur)`). A
  // set: a group (GROUP), or buttons side by side that each say whether they're pressed.
  const GROUP = '[role=group], [role=radiogroup], [role=tablist], .seg';
  const chosen = (el) => (el.getAttribute('aria-pressed') === 'true' || el.getAttribute('aria-selected') === 'true'
    || el.getAttribute('aria-checked') === 'true' || (el.type === 'radio' && el.checked)) && el.parentElement
    && (el.parentElement.matches(GROUP) || [...el.parentElement.children].some((o) => o !== el && o.tagName === el.tagName
      && o.getAttribute('aria-pressed') === 'false'));
  let current = 0;
  const inZone = (el) => zone === 'all' || (zone === 'page') === !!el.closest('#page');
  const found = new Set();
  for (const el of document.querySelectorAll(SEL)) {
    if (!inZone(el) || !ok(el)) continue;
    if (chosen(el)) current += 1; else found.add(el);
  }
  let native = 0;
  for (const el of document.body.querySelectorAll('*')) {   // a cursor: pointer region's outermost element
    if (found.has(el) || !inZone(el)) continue;
    if (getComputedStyle(el).cursor !== 'pointer') continue;
    const p = el.parentElement;
    if (p && getComputedStyle(p).cursor === 'pointer') continue;
    if (el.closest(SEL) || el.closest('label')) continue;     // inside a control (or a label: its input's)
    if (el.matches(NATIVE) || el.closest(NATIVE)) { native += 1; continue; }
    if (ok(el)) found.add(el);
  }
  const cls = (e) => [...e.classList].filter((c) => !/^(on|active|sel|selected|open|is-|has-|cur)/.test(c)).sort().join('.');
  const where = (e) => [e.parentElement, e.parentElement && e.parentElement.parentElement]
    .map((p) => (p ? `${p.tagName.toLowerCase()}.${cls(p)}` : '')).join('>');
  const label = (e) => (e.getAttribute('aria-label') || e.getAttribute('title') || e.textContent || '').trim()
    .replace(/\s+/g, ' ').replace(/[$€£]?\d[\d,.]*%?/g, '#').slice(0, 40);
  const items = [...found].sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1))
    .map((el) => ({el, kind: `${el.closest('#page') ? 'page' : 'shell'}|${where(el)}>${el.tagName.toLowerCase()}.${cls(el)}`
      + `[${el.getAttribute('role') || el.type || ''}]`}));
  const size = {};
  for (const it of items) size[it.kind] = (size[it.kind] || 0) + 1;
  const seen = new Set(), list = [], out = [];
  for (const it of items) {   // a big group (a row per transaction) is one control; a small one (a segmented control) isn't
    const sig = size[it.kind] > 5 ? `${it.kind}:*` : `${it.kind}:${label(it.el)}`;
    if (seen.has(sig)) continue;
    seen.add(sig);
    list.push(it.el);
    out.push({sig, label: label(it.el), group: size[it.kind]});
  }
  window.__hfc.list = list;
  return {controls: out, total: items.length, native, current};
})"""

# What the page looks like (window.__hfc.snap()) and how it differs from the last record (window.__hfc.diff()).
LOOK = r"""(() => {
  const rec = window.__hfc;
  if (rec.snap) return 1;
  const PROPS = ['color', 'background-color', 'border-top-color', 'border-bottom-color', 'opacity', 'transform', 'outline-style'];
  const ATTRS = ['class', 'aria-pressed', 'aria-expanded', 'aria-checked', 'aria-selected', 'aria-current'];
  const own = (e) => [...e.childNodes].filter((n) => n.nodeType === 3).map((n) => n.data).join('').trim();
  // keyed by where each element is (its path of child indexes), not by the element: a page that draws itself again,
  // the same, is the same page
  const pathOf = (el) => { const p = []; for (let e = el; e && e !== document.body; e = e.parentElement)
    p.unshift(e.tagName + [...e.parentElement.children].indexOf(e)); return '/' + p.join('/'); };
  rec.snap = (ctlPath) => {
    const m = new Map();
    const visit = (e, path) => {
      if (!e.checkVisibility()) return;   // not drawn: neither it nor anything in it
      const r = e.getBoundingClientRect();
      if (e.checkVisibility({opacityProperty: true, visibilityProperty: true}) && (r.width >= 1 || r.height >= 1)) {
        if (ctlPath && (path === ctlPath || path.startsWith(ctlPath + '/'))) {   // the control: what it says and its state, not its :hover look
          m.set(path, [[own(e), ...ATTRS.map((a) => e.getAttribute(a)), e.value, e.checked].join('|'), e]);
        } else {
          const cs = getComputedStyle(e);
          m.set(path, [[e.tagName, Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height), own(e),
                        ...PROPS.map((p) => cs.getPropertyValue(p)), e.value, e.checked].join('|'), e]);
        }
      }
      let i = 0;
      for (const k of e.children) visit(k, `${path}/${k.tagName}${i++}`);
    };
    let i = 0;
    for (const k of document.body.children) visit(k, `/${k.tagName}${i++}`);
    return m;
  };
  rec.record = (i) => {
    rec.ctl = i == null ? null : rec.list[i];
    rec.ctlPath = rec.ctl ? pathOf(rec.ctl) : null;
    rec.base = rec.snap(rec.ctlPath);
    rec.href = location.href;
    rec.focus = document.activeElement;
    rec.asked = [];
    // where the window and the control's scrolled boxes were (a page drawn again starts at the top)
    rec.scroll = [[null, scrollX, scrollY]];
    for (let e = rec.ctl && rec.ctl.parentElement; e && e !== document.body; e = e.parentElement)
      if (e.scrollTop || e.scrollLeft) rec.scroll.push([pathOf(e), e.scrollLeft, e.scrollTop]);
    return rec.base.size;
  };
  const who = (e) => `${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''}${typeof e.className === 'string' && e.className ? '.' + e.className.trim().split(/\s+/).join('.') : ''}`;
  // back: the page put back? Its scroll positions first, and a toast or a live message still fading out doesn't count
  const TRANSIENT = '.toast, [role=status], [role=alert], [aria-live]';
  const find = (path) => path.split('/').slice(1).reduce((e, step) => e && e.children[+step.replace(/^\D+/, '')], document.body);
  rec.diff = (back = false) => {
    if (back) for (const [path, x, y] of rec.scroll) {
      const e = path ? find(path) : null;
      if (!path) scrollTo(x, y); else if (e) { e.scrollLeft = x; e.scrollTop = y; }
    }
    const now = rec.snap(rec.ctlPath), changed = [];
    const counts = (e) => !back || !e.closest(TRANSIENT);
    for (const [k, [v, e]] of now) { const was = rec.base.get(k); if ((!was || was[0] !== v) && counts(e)) changed.push(who(e)); }
    for (const [k, [, e]] of rec.base) if (!now.has(k) && (!e.isConnected || counts(e))) changed.push('gone ' + who(e));
    const act = document.activeElement;
    const focus = act !== rec.focus && act !== document.body && !(rec.ctl && rec.ctl.contains(act))
      && !(rec.ctlPath && act && pathOf(act).startsWith(rec.ctlPath));
    return {changed: changed.length, sample: changed.slice(0, 4), href: location.href !== rec.href ? location.href : null,
            asked: rec.asked.slice(0, 4), focus: focus ? who(act) : null};
  };
  return 1;
})()"""

# A point of the window over no control, where the mouse rests while the page is recorded (null: none found).
NEUTRAL = r"""(() => {
  const CTL = 'button, a, [role], input, select, textarea, summary, label';
  for (const [x, y] of [[innerWidth - 2, innerHeight / 2], [innerWidth / 2, innerHeight - 2], [2, innerHeight - 2], [innerWidth - 2, 2],
                        [innerWidth / 2, 2], [2, innerHeight / 2]]) {
    const at = document.elementFromPoint(x, y);
    if (!at || at.closest(CTL)) continue;
    let p = at, pointer = false;
    for (; p; p = p.parentElement) if (getComputedStyle(p).cursor === 'pointer') { pointer = true; break; }
    if (!pointer) return {x, y};
  }
  return null;
})()"""


# Scroll control i into view and find a point of it that nothing covers (its centre first). null: no such point.
AIM = r"""((i) => {
  const el = window.__hfc.list[i];
  if (!el || !el.isConnected) return {gone: true};
  el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const r = el.getBoundingClientRect();
  const pts = [[0.5, 0.5], [0.15, 0.5], [0.85, 0.5], [0.5, 0.2], [0.5, 0.8], [0.1, 0.15], [0.9, 0.85]];
  for (const [fx, fy] of pts) {
    const x = r.left + r.width * fx, y = r.top + r.height * fy;
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    const at = document.elementFromPoint(x, y);
    if (!at || !(at === el || el.contains(at))) continue;
    const inner = at.closest('button, a, [role=button], [role=tab], input, select, textarea, summary, label');
    if (inner && inner !== el && el.contains(inner)) continue;   // another control inside this one
    return {x, y};
  }
  return {x: null, y: null};
})"""


def prepare(b: Chrome) -> None:
    """A Chrome for the gate: the stubs in every page it loads from now on, no download, no animation."""
    b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
    b.call("Page.addScriptToEvaluateOnNewDocument", source=STUBS)
    b.call("Page.setDownloadBehavior", behavior="deny")


def _escape(b: Chrome) -> None:
    for t in ("keyDown", "keyUp"):
        b.call("Input.dispatchKeyEvent", type=t, key="Escape", code="Escape", windowsVirtualKeyCode=27)


def _click(b: Chrome, x: float, y: float) -> None:
    for t in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", clickCount=1)


def _back(p: "_Page", rest: dict, undo) -> dict | None:
    """undo(), then the focus let go and the mouse at rest: how the page differs from its record (None: a new document)."""
    undo()
    p.b.js("(document.activeElement && document.activeElement !== document.body && document.activeElement.blur(), 1)")
    p.b.call("Input.dispatchMouseEvent", type="mouseMoved", x=rest["x"], y=rest["y"])
    p.settled("put back", 3000)
    return p.b.js("window.__hfc ? window.__hfc.diff(true) : null")


def _alive(d: dict) -> bool:
    return bool(d["changed"] or d["href"] or d["asked"] or d["focus"])


class _Page:
    """One page of one site in one Chrome: loads it as it starts (storage and all) and puts it back."""

    def __init__(self, b: Chrome, base: str, width: int, page: str):
        self.b, self.base, self.width, self.page = b, base, width, page
        self.unsettled: list[str] = []
        self.storage = None
        self.load()

    def settled(self, after: str, cap: int = 10000) -> None:
        why = self.b.settled(cap)
        if why:
            self.unsettled.append(f"{after}: {why}")

    def load(self) -> None:
        b = self.b
        if self.storage is not None:   # the storage the page started with (a click may have saved a setting there)
            b.js(f"(() => {{ const s = {json.dumps(self.storage)}; for (const [k, v] of [['l', localStorage], ['s', sessionStorage]]) "
                 f"{{ v.clear(); for (const [a, x] of Object.entries(s[k])) v.setItem(a, x); }} return 1; }})()")
        b.size(self.width, HEIGHT[self.width])
        b.goto(f"{self.base}/?controls={time.time():.3f}#/{self.page}", ready=False)
        b.js(READY_NOW)
        self.settled("load")
        b.js(LOOK)
        if self.storage is None:
            self.storage = b.js("({l: {...localStorage}, s: {...sessionStorage}})")
        b.call("Input.dispatchMouseEvent", type="mouseMoved", x=1, y=1)

    def controls(self, zone: str) -> dict:
        return self.b.js(f"{ENUM}({json.dumps(zone)})")


def probe(p: _Page, zone: str, only=None, skip: set[str] | None = None) -> dict:
    """Click each control of the page's `zone` once (see the module docstring). only(control) -> bool: just those (the
    self-test's plants); skip: signatures already clicked elsewhere. Returns {total, native, current, probed, alive,
    dead: [...], allowed, gone, covered, reloads, unsettled, sigs}."""
    b = p.b
    found = p.controls(zone)
    todo = [c for c in found["controls"] if (only is None or only(c)) and c["sig"] not in (skip or set())]
    out = {"total": found["total"], "native": found["native"], "current": found["current"], "probed": 0, "alive": 0,
           "dead": [], "allowed": [], "gone": [], "covered": [], "reloads": 0, "reloaded_after": [], "sigs": [c["sig"] for c in todo]}
    idx = {x["sig"]: i for i, x in enumerate(found["controls"])}
    fresh, dirty = True, False   # fresh: the page's list of controls was just made
    for c in todo:
        why = next((w for s, w in ALLOW.items() if s in c["sig"]), None)
        if why:
            out["allowed"].append({"sig": c["sig"], "why": why})
            continue
        if dirty:
            p.load()
            out["reloads"] += 1
            dirty, fresh = False, False
            idx = {x["sig"]: i for i, x in enumerate(p.controls(zone)["controls"])}
            fresh = True
        aim = b.js(f"{AIM}({idx[c['sig']]})") if c["sig"] in idx else {"gone": True}
        if aim.get("gone") and not fresh:   # redrawn since the list was made (a click before, put back): list it again
            idx = {x["sig"]: i for i, x in enumerate(p.controls(zone)["controls"])}
            fresh = True
            aim = b.js(f"{AIM}({idx[c['sig']]})") if c["sig"] in idx else {"gone": True}
        if aim.get("gone"):   # not on the page as it loads after all (drawn later, or by a click before)
            out["gone"].append(c["sig"])
            continue
        i = idx[c["sig"]]
        rest = b.js(NEUTRAL) or {"x": 1, "y": 1}
        b.call("Input.dispatchMouseEvent", type="mouseMoved", x=rest["x"], y=rest["y"])
        p.settled("mouse at rest", 3000)
        b.js(f"window.__hfc.record({i})")   # the page before the mouse reaches the control (hovering it may be what it does)
        t0 = time.monotonic()
        if aim["x"] is None:   # covered wherever it's aimed at (tap-covered's business): clicked in place, all the same
            out["covered"].append(c["sig"])
            b.js(f"(window.__hfc.list[{i}].click(), 1)")
        else:
            b.call("Input.dispatchMouseEvent", type="mouseMoved", x=aim["x"], y=aim["y"])
            _click(b, aim["x"], aim["y"])
        out["probed"] += 1
        fresh = False
        p.settled(f"click {c['label']!r}", SETTLE_MS)
        d = b.js("window.__hfc ? window.__hfc.diff() : null")
        if d is None:   # a new document: the click loaded one
            d = {"changed": 0, "sample": [], "href": "a new document", "asked": [], "focus": None}
        if not _alive(d):
            left = WAIT_S - (time.monotonic() - t0)
            if left > 0:
                time.sleep(left)
            d = b.js("window.__hfc.diff()")
        if _alive(d):
            out["alive"] += 1
            if d["href"]:   # a route: back to where it was (a new document, or another one: a fresh load)
                after = _back(p, rest, lambda: b.js(f"(history.back(), new Promise((r) => setTimeout(r, 50))).then(() => {READY_NOW})"))
                dirty = after is None or bool(after["changed"] or after["href"])
                if dirty:
                    out["reloaded_after"].append([c["label"], after and (after["href"] or after["sample"][:3])])
            else:   # put back: Escape (a sheet, a menu, a tip), then a second click (a toggle), then a fresh load. (What it
                # sent was answered here: only the page itself changed.)
                after = _back(p, rest, lambda: _escape(b))
                if after and after["changed"] and not after["href"] and aim["x"] is not None:
                    again = b.js(f"{AIM}({i})")
                    if not again.get("gone") and again["x"] is not None:
                        after = _back(p, rest, lambda: (b.call("Input.dispatchMouseEvent", type="mouseMoved", x=again["x"], y=again["y"]),
                                                        _click(b, again["x"], again["y"])))
                dirty = after is None or bool(after["changed"] or after["href"])
                if dirty:
                    out["reloaded_after"].append([c["label"], after and (after["href"] or after["sample"][:3])])
        else:
            out["dead"].append({"sig": c["sig"], "label": c["label"], "group": c["group"]})
            dirty = bool(d["changed"])   # (nothing changed: the page is as it was)
    out["unsettled"] = p.unsettled
    return out


# The self-test's plants, in the Overview's first card: two dead buttons (one with no handler; one, "See suggestions",
# whose handler toggles a class on a panel that stays display: none) and a live one (it shows a panel that was hidden).
PLANT_JS = r"""(() => {
  const c = document.querySelector('#page .card');
  const btn = (id, text) => Object.assign(document.createElement('button'), {id, type: 'button', className: 'btn', textContent: text});
  const panel = (id) => { const d = Object.assign(document.createElement('div'), {id, textContent: 'planted panel'}); d.style.cssText = 'display:none !important'; return d; };
  const nohandler = btn('plant-dead-nohandler', 'plantdeadnohandler');
  const sugg = btn('plant-dead-sugg', 'See suggestions plantdeadsugg'), sp = panel('plant-dead-sugg-panel');
  sugg.addEventListener('click', () => sp.classList.toggle('open'));
  const live = btn('plant-live', 'plantlive show'), lp = panel('plant-live-panel');
  live.addEventListener('click', () => { lp.style.cssText = 'display:block'; });
  c.append(nohandler, sugg, sp, live, lp);
  return 1;
})()"""


def selftest(b: Chrome, base: str) -> str:
    """The plants on the Overview at 1440: both dead buttons must be reported, the live one not. Raises AssertionError."""
    p = _Page(b, base, 1440, "overview")
    b.js(PLANT_JS)
    p.settled("plants")
    got = probe(p, "page", only=lambda c: "plant" in c["label"])
    dead = {d["label"] for d in got["dead"]}
    missed = [n for n in ("plantdeadnohandler", "plantdeadsugg") if not any(n in d for d in dead)]
    flagged = [d for d in dead if "plantlive" in d]
    assert got["probed"] == 3 and not missed and not flagged, (
        f"controls gate self-test: {got['probed']} of the 3 plants clicked; dead ones missed: {missed}; the live one "
        f"flagged: {flagged} ({got})")
    return f"SELF  1440 overview  the 2 planted dead controls were caught, the live one was not ({got['probed']} clicked)"


def plan(widths=WIDTHS, pages=ALL_PAGES) -> list[tuple[int, str, str]]:
    """(width, page, zone): the Overview's states first (their shell signatures are skipped on the other pages)."""
    first = [(w, "overview", "all") for w in widths if "overview" in pages]
    return first + [(w, pg, "page" if "overview" in pages else "all") for w in widths for pg in pages if pg != "overview"]


def audit_many(sites: dict[str, str], out: Path | None = None, widths=WIDTHS, pages=ALL_PAGES, logs: dict | None = None,
               workers: int = WORKERS, self_test=True) -> dict[str, dict]:
    """Every (site, width, page) of the plan, shared out over `workers` headless Chromes; each site's self-test first.
    Returns {label: {base_url, label, selftest, widths: {w: {page: result}}, pass, fail, probed, seconds}}."""
    logs = logs or {}
    emits = {k: logs.get(k) or (lambda line: print(line, flush=True)) for k in sites}
    reports = {k: {"base_url": u, "label": k, "selftest": None, "widths": {}, "pass": 0, "fail": 0, "probed": 0}
               for k, u in sites.items()}
    items = plan(widths, pages)
    firsts = [it for it in items if it[2] == "all"]
    queue = ([("self", k, None) for k in sites if self_test] + [("state", k, it) for it in firsts for k in sites]
             + [("state", k, it) for it in items if it not in firsts for k in sites])
    shell: dict[tuple[str, int], set[str]] = {}
    ready = {(k, w): threading.Event() for k in sites for w in widths}
    if "overview" not in pages:
        for e in ready.values():
            e.set()
    results: dict[tuple, dict] = {}
    lock, stop, nxt = threading.Lock(), threading.Event(), iter(range(len(queue)))
    t0 = time.monotonic()

    def take():
        with lock:
            return None if stop.is_set() else next(nxt, None)

    def run(b: Chrome, n: int) -> None:
        what, k, it = queue[n]
        try:
            if what == "self":
                reports[k]["selftest_line"] = selftest(b, sites[k])
                reports[k]["selftest"] = "ok"
                return
            w, page, zone = it
            if zone == "page":
                ready[(k, w)].wait()
            t = time.monotonic()
            try:
                r = probe(_Page(b, sites[k], w, page), zone, skip=shell.get((k, w)))
            finally:
                if zone == "all":
                    ready[(k, w)].set()
            if zone == "all":
                shell[(k, w)] = {s for s in r["sigs"] if s.startswith("shell|")}
            r["seconds"] = round(time.monotonic() - t, 1)
            r.pop("sigs")
            results[(k, w, page)] = r
        except BaseException:
            stop.set()
            for e in ready.values():
                e.set()
            raise

    def work() -> None:
        n = take()
        if n is None:
            return
        with Chrome() as b:
            prepare(b)
            while n is not None:
                run(b, n)
                n = take()

    n = max(1, min(workers, len(queue)))
    if n == 1:
        work()
    else:
        with parked(), ThreadPoolExecutor(n) as pool:   # (cdp.parked: this thread may hold a test's Chrome)
            for f in [pool.submit(work) for _ in range(n)]:
                f.result()
    for k, rep in reports.items():
        emit = emits[k]
        if rep.get("selftest_line"):
            emit(rep.pop("selftest_line"))
        for w in widths:
            for page in pages:
                r = results.get((k, w, page))
                if r is None:
                    continue
                rep["widths"].setdefault(str(w), {})[page] = r
                rep["probed"] += r["probed"]
                ok = not r["dead"]
                rep["pass" if ok else "fail"] += 1
                emit(f"{'PASS' if ok else 'FAIL'}  {w:<5} {page:<13} {r['probed']:>3} clicked of {r['total']} controls "
                     f"({r['alive']} did something, {r['current']} already chosen, {r['reloads']} reloads){'' if ok else ': dead ' + '; '.join(repr(d['label']) + ' ' + d['sig'] for d in r['dead'])}"
                     f"{'  unsettled: ' + str(len(r['unsettled'])) if r['unsettled'] else ''}  {r['seconds']}s")
        rep["seconds"] = round(time.monotonic() - t0, 1)
        emit(f"\n[{k}] controls: {rep['pass'] + rep['fail']} pages, {rep['probed']} controls clicked, {rep['fail']} page(s) "
             f"with a dead control")
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        path = out / "controls-report.json"
        merged = json.loads(path.read_text()) if path.exists() else {}
        merged.update(reports)
        path.write_text(json.dumps(merged, indent=1))
    return reports


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("--label", default="site")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--widths", default=",".join(map(str, WIDTHS)))
    ap.add_argument("--pages", default=",".join(ALL_PAGES))
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--no-self-test", action="store_true")
    a = ap.parse_args(argv)
    reps = audit_many({a.label: a.base_url}, a.out, tuple(int(w) for w in a.widths.split(",")), tuple(a.pages.split(",")),
                      workers=a.workers, self_test=not a.no_self_test)
    return 1 if any(r["fail"] for r in reps.values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
