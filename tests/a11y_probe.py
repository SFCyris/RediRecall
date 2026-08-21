# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measure accessibility properties of index.html in a real browser.

Prints one JSON object on stdout. Everything here is a COMPUTED value — contrast
ratios composited against the real backdrop, focus actually moving, Escape actually
closing one layer — never a source-text match, because the defects being guarded
(a 1.15:1 focus ring, a dialog with no keyboard exit) are all invisible to a grep.

Run with an interpreter that has Playwright:  python3 a11y_probe.py <index.html>
"""
import json
import pathlib
import sys

OUT: dict = {"ok": False}


def main(index: pathlib.Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"skip": "playwright not installed"}))
        return

    console: list = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
        pg.goto(index.as_uri(), wait_until="domcontentloaded")
        pg.wait_for_timeout(1200)

        OUT["console"] = [c for c in console
                          # A file:// page has no server and no CDN, so every fetch to
                          # /api/* and every CDN <script> fails. That is the harness, not
                          # the page under test.
                          if "Failed to load resource" not in c
                          and "net::" not in c
                          and 'URL scheme "file" is not supported' not in c]

        OUT["contrast"] = pg.evaluate(r"""() => {
          const L = c => { const f = v => { v/=255; return v<=.04045 ? v/12.92 : ((v+.055)/1.055)**2.4; };
                           return .2126*f(c[0])+.7152*f(c[1])+.0722*f(c[2]); };
          const ratio = (a,b) => { const x=L(a), y=L(b), hi=Math.max(x,y), lo=Math.min(x,y);
                                   return (hi+.05)/(lo+.05); };
          const hex = h => { h=h.replace('#',''); return [0,2,4].map(i=>parseInt(h.slice(i,i+2),16)); };
          const v = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
          const page = [255,255,255];   // light theme card surface
          const out = {};
          for (const n of ['--green','--yellow','--red','--blue','--accent','--text2'])
            out[n] = +ratio(hex(v(n)), page).toFixed(2);
          return out;
        }""")

        # White text sits on the accent gradient in .btn-primary and .msg-bubble.user, so
        # those stops are gated by the TEXT on them, not by the page behind them. Darkening
        # --accent for text contrast made the dark-theme fill WORSE (white on it fell to
        # 2.59:1) until the fills were split onto their own tokens.
        # Measured on REAL ELEMENTS, not on tokens. The first version of this check read
        # --accent-fill/--accent-fill2 and passed green while five actual controls still
        # painted white text on --accent at 2.59:1 — a test that could not fail for the
        # defect it named. Each entry is built, styled by the page's own CSS, measured, and
        # removed; the gradient stops are parsed out of the computed backgroundImage.
        OUT["fill_contrast"] = pg.evaluate(r"""() => {
          const L = c => { const f = v => { v/=255; return v<=.04045 ? v/12.92 : ((v+.055)/1.055)**2.4; };
                           return .2126*f(c[0])+.7152*f(c[1])+.0722*f(c[2]); };
          const ratio = (a,b) => { const x=L(a), y=L(b), hi=Math.max(x,y), lo=Math.min(x,y);
                                   return (hi+.05)/(lo+.05); };
          const nums = s => (s.match(/rgba?\([^)]+\)/g) || [])
                              .map(c => c.match(/[\d.]+/g).slice(0,3).map(Number));
          // Every surface that paints its own text on an accent fill.
          const SPECS = [
            ['btn-primary',      'button', 'btn btn-primary',            ''],
            ['new-chat-btn',     'button', 'new-chat-btn',               ''],
            ['send-btn',         'button', '',                           'send-btn'],
            ['seg-btn-active',   'button', 'seg-btn active',             ''],
            ['prov-use-active',  'button', 'prov-use-btn is-active',     ''],
            ['provider-active',  'button', 'active',                     '', 'provider-seg'],
            ['msg-bubble-user',  'div',    'msg-bubble user',            ''],
            ['msg-avatar-user',  'div',    'msg-avatar user-av',         ''],
          ];
          // A neutral host by default: parking everything inside .provider-seg let that
          // component's `background:transparent` base rule suppress .btn-primary's gradient,
          // so those rows measured as "no fill" instead of being checked at all.
          const host = document.createElement('div');
          host.style.cssText = 'position:fixed;left:-9999px;top:0';
          document.body.appendChild(host);
          const prev = document.documentElement.getAttribute('data-theme');
          const out = {};
          for (const theme of ['light','dark']) {
            document.documentElement.setAttribute('data-theme', theme);
            const rows = {};
            for (const [name, tag, cls, id, parentCls] of SPECS) {
              const el = document.createElement(tag);
              if (cls) el.className = cls;
              if (id) el.id = id;
              el.textContent = 'Xy';
              let mount = host;
              if (parentCls) { mount = document.createElement('div');
                               mount.className = parentCls; host.appendChild(mount); }
              mount.appendChild(el);
              const cs = getComputedStyle(el);
              const fg = nums(cs.color)[0] || [255,255,255];
              const stops = nums(cs.backgroundImage);
              const solid = nums(cs.backgroundColor);
              const bgs = stops.length ? stops
                        : (solid.length && cs.backgroundColor !== 'rgba(0, 0, 0, 0)' ? solid : []);
              rows[name] = bgs.length
                ? +Math.min(...bgs.map(b => ratio(fg, b))).toFixed(2)
                : null;   // no accent fill applied — nothing to gate
              el.remove();
              if (mount !== host) mount.remove();
            }
            out[theme] = rows;
          }
          if (prev) document.documentElement.setAttribute('data-theme', prev);
          else document.documentElement.removeAttribute('data-theme');
          host.remove();
          return out;
        }""")

        OUT["tokens_defined"] = pg.evaluate(r"""() => {
          const v = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
          const o = {};
          for (const n of ['--text3','--muted','--border-control','--accent-fill']) o[n] = v(n);
          return o;
        }""")

        OUT["aria"] = pg.evaluate(r"""() => ({
          liveRegions: document.querySelectorAll('[aria-live]').length,
          dialogs:     document.querySelectorAll('[role="dialog"][aria-modal]').length,
          h1:          !!document.querySelector('h1'),
          main:        !!document.querySelector('[role="main"]'),
          labelsFor:   document.querySelectorAll('label[for]').length,
          // The actual property. A `labelsFor >= N` floor stops guarding the moment the
          // real count drifts above N: with 49 links and a floor of 41, eight labels
          // could be removed unnoticed.
          unnamedControls: [...document.querySelectorAll('input,select,textarea')]
            .filter(el => el.type !== 'hidden')
            .filter(el => !(
              (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) ||
              el.closest('label') ||
              el.getAttribute('aria-label') ||
              el.getAttribute('aria-labelledby')))
            .map(el => el.id || el.name || el.outerHTML.slice(0, 60)),
          // Either mechanism gives the control an accessible name; aria-labelledby points
          // at existing visible text, aria-label supplies its own. Both satisfy 4.1.2.
          namedToggles: [...document.querySelectorAll('.toggle input[type=checkbox]')]
                          .filter(i => i.getAttribute('aria-labelledby') || i.getAttribute('aria-label')).length,
          totalToggles: document.querySelectorAll('.toggle input[type=checkbox]').length,
          // Enumerated, not a frozen id list. The old check named two ids and treated a
          // MISSING element as a pass, so renaming a button or adding a new unnamed one
          // was invisible — and ~15 icon-only buttons were never looked at.
          unnamedIconBtns: (() => {
            const emoji = /^(?:\p{Extended_Pictographic}|\p{So}|[\u2190-\u21FF\u2600-\u27BF\u2B00-\u2BFF\u00D7\u2715\u2716\u2717\u2718])+$/u;
            return [...document.querySelectorAll('button')]
              .filter(b => { const s = (b.textContent || '').trim();
                             return !s || emoji.test(s); })
              .filter(b => !(b.getAttribute('aria-label') || b.getAttribute('aria-labelledby')
                             || b.getAttribute('title') || '').trim())
              .map(b => b.id || b.className || b.outerHTML.slice(0, 60));
          })(),
        })""")

        # Focus indicator must be a real, visible outline — not a 12%-alpha shadow.
        # Measure a LIST of visible controls, one per element type: the previous version
        # targeted `input[type=text]`, whose first match sits in a closed Settings dialog
        # (offsetParent null), so .focus() was a no-op and the Tab dance always landed on
        # the same TEXTAREA — one element, and not the one the docstring named.
        # :focus-visible deliberately does not match programmatic .focus(), so each control
        # is reached the way a keyboard user reaches it.
        # Two passes. Every one of the page's text inputs lives inside the Settings
        # overlay, so with the panel shut the "input[type=text]" pass found nothing visible,
        # appended nothing, and its assertion skipped itself — the control type most likely
        # to carry `outline:none` was the one never measured. The panel cannot simply be
        # left open for the whole loop either: it is a focus trap, so the main-page controls
        # then refuse focus and every measurement skips instead.
        OUT["focus_rings"] = []
        PASSES = [
            (None,                 ("select", "textarea", "button")),
            ("redis",              ("#settings-overlay input[type=text]",
                                    "#settings-overlay button")),
        ]
        for tab, sels in PASSES:
          if tab:
              pg.evaluate("t => openSettings(t)", tab)
              pg.wait_for_timeout(200)
          for sel in sels:
              got = pg.evaluate(
                  """(sel) => {
                    const el = [...document.querySelectorAll(sel)].find(e => e.offsetParent);
                    if (!el) return null;
                    el.id = el.id || ('qa-focus-' + sel.replace(/\\W/g,''));
                    el.scrollIntoView();
                    return el.id;
                  }""", sel)
              if not got:
                  continue
              pg.focus(f"#{got}")
              pg.keyboard.press("Shift+Tab")
              pg.keyboard.press("Tab")
              OUT["focus_rings"].append(pg.evaluate(
                  """(id) => {
                    const el = document.getElementById(id);
                    const cs = getComputedStyle(el);
                    // Reported for diagnostics: when a control measures under 2px this says
                    // whether a rule was missing or merely not applied yet. (An earlier
                    // reading blamed a headless <select> quirk; the real cause was
                    // `transition: all` animating the outline, so the ring was caught
                    // mid-fade. It is excluded from the transition now and every control
                    // measures 2px immediately.)
                    let ruleMatches = false;
                    for (const sheet of document.styleSheets) {
                      let rules; try { rules = sheet.cssRules; } catch (e) { continue; }
                      for (const r of rules || []) {
                        if (!r.selectorText || !/focus-visible/.test(r.selectorText)) continue;
                        const w = r.style.outlineWidth || (r.style.outline || '').match(/\\d+px/)?.[0];
                        if (!w || parseFloat(w) < 2) continue;
                        for (const s of r.selectorText.split(',')) {
                          try { if (el.matches(s.trim())) { ruleMatches = true; } } catch (e) {}
                        }
                      }
                    }
                    return {sel: id, tag: el.tagName, focused: document.activeElement === el,
                            ruleMatches,
                            outlineWidth: cs.outlineWidth, outlineStyle: cs.outlineStyle};
                  }""", got))

        pg.evaluate("closeSettings(true)")   # force: the pass above focused fields, not edits
        pg.wait_for_timeout(200)

        # Escape must close only the innermost layer, Tab must not be captured by a
        # non-modal panel, and an inline handler must not close two layers at once.
        OUT["layering"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          const open = id => document.getElementById(id).classList.contains('open');
          const esc = el => (el || document).dispatchEvent(
            new KeyboardEvent('keydown', {key:'Escape', bubbles:true}));
          const out = {};

          document.getElementById('toggle-sidebar').focus();
          out.start = document.activeElement.id;
          openSettings('general'); await wait(120);
          out.focusInPanel = document.getElementById('settings-overlay').contains(document.activeElement);
          showModal('t','<p>b</p>',[{label:'Cancel',cls:'btn-secondary'}]); await wait(120);
          out.focusInModal = document.getElementById('modal-overlay').contains(document.activeElement);
          esc(); await wait(140);
          out.afterFirst = {modal: open('modal-overlay'), settings: open('settings-overlay')};
          esc(); await wait(160);
          out.settingsClosed = !open('settings-overlay');
          out.focusReturned = document.activeElement.id;

          // The destructive action must NOT be the one Enter would trigger.
          showModal('t','<p>b</p>',
            [{label:'Export & Delete', cls:'btn btn-primary'},
             {label:'Delete', cls:'btn btn-danger'},
             {label:'Cancel', cls:'btn btn-secondary'}]);
          await wait(140);
          out.autoFocusedLabel = (document.activeElement.textContent || '').trim();
          closeModal(); await wait(80);

          // A docked, non-modal panel must not capture Tab (WCAG 2.1.2 keyboard trap).
          if (!open('pinned-panel')) togglePinned();
          await wait(120);
          document.getElementById('msg-input').focus();
          let trapped = 0;
          for (let i = 0; i < 6; i++) {
            const ev = new KeyboardEvent('keydown', {key:'Tab', bubbles:true, cancelable:true});
            document.activeElement.dispatchEvent(ev);
            if (ev.defaultPrevented) trapped++;
            await wait(20);
          }
          out.pinnedTrapsTab = trapped > 0;
          if (open('pinned-panel')) togglePinned();
          await wait(100);

          // Escape order must follow the painted stacking order, not source order.
          if (!open('pinned-panel')) togglePinned();
          await wait(100);
          openSettings('general'); await wait(140);
          esc(); await wait(150);
          out.escWithPinnedBehindSettings = {pinned: open('pinned-panel'), settings: open('settings-overlay')};
          if (open('settings-overlay')) closeSettings();
          if (open('pinned-panel')) togglePinned();
          await wait(120);

          // The topmost overlays (z-index 9999) install their own Escape handler. Both
          // handlers live on `document`, so without an explicit stand-down the press
          // closed the overlay AND the layer underneath it.
          openSettings('general'); await wait(120);
          openLightbox('data:image/gif;base64,R0lGODlhAQABAAAAACw=');
          await wait(140);
          out.lightboxOpened = _isOpen(document.getElementById('img-lightbox'));
          esc(); await wait(160);
          out.escOverLightbox = {lightbox: _isOpen(document.getElementById('img-lightbox')),
                                settings: open('settings-overlay')};
          if (_isOpen(document.getElementById('img-lightbox'))) closeLightbox();
          if (open('settings-overlay')) closeSettings();
          await wait(100);

          // Escape from inside the search box must close ONE layer, not two.
          openSettings('general'); await wait(120);
          openSearch(); await wait(140);
          esc(document.getElementById('search-input')); await wait(160);
          out.escFromSearchInput = {search: open('search-overlay'), settings: open('settings-overlay')};
          if (open('search-overlay')) closeSearch();
          if (open('settings-overlay')) closeSettings();
          await wait(100);
          return out;
        }""")

        # Every destructive action must raise a dialog that names what is lost.
        OUT["confirms"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          const body = () => document.getElementById('modal-body').textContent;
          const isOpen = () => document.getElementById('modal-overlay').classList.contains('open');
          const res = {};
          S.sessions = S.sessions || {}; S.sessionId = S.sessionId || 'x';
          S.sessions[S.sessionId] = {messages:[{role:'user',content:'q'},{role:'assistant',content:'a'}]};
          confirmClearChat(); await wait(70);
          res.clearChat = isOpen() && /2 message/.test(body()); closeModal(); await wait(50);

          S.config = S.config || {}; S.config.web_sources = ['https://example.com/docs'];
          removeWebSource(0); await wait(70);
          res.webSource = isOpen() && /example\.com/.test(body()); closeModal(); await wait(50);

          S.templates = [{name:'Redis Expert'}];
          removeTemplate(0); await wait(70);
          res.template = isOpen() && /Redis Expert/.test(body()); closeModal(); await wait(50);

          resetRagStats(); await wait(70);
          res.ragStats = isOpen() && /queries|hit rate|counters/i.test(body());
          closeModal(); await wait(50);

          S.ragInstances = [{name:'docs',redis_endpoint:'edge'}];
          deleteRedisEndpoint('edge'); await wait(110);
          res.endpoint = isOpen() && /docs/.test(body()); closeModal(); await wait(50);

          S.config = {web_sources:['a','b'], watch_folders:{folders:['/x']}, claude:{api_key:'k'}};
          resetConfig(); await wait(70);
          res.resetConfig = isOpen() && /2 saved web sources/.test(body())
                            && /every stored API key/.test(body());
          closeModal();
          return res;
        }""")

        # prefers-reduced-motion (WCAG 2.2.2). Measured, not asserted from the CSS text:
        # the media block has to actually win over the per-element `animation:` shorthands.
        pg.emulate_media(reduced_motion="reduce")
        pg.wait_for_timeout(200)
        OUT["reduced_motion"] = pg.evaluate(r"""() => {
          const probe = document.createElement('div');
          probe.className = 'crawl-bar';
          probe.style.cssText = 'animation: crawlPulse 1.5s infinite alternate';
          document.body.appendChild(probe);
          const cs = getComputedStyle(probe);
          const out = {duration: cs.animationDuration, iterations: cs.animationIterationCount};
          probe.remove();
          // The streaming avatar animates via SMIL, which no media query can reach — the
          // preference has to be read in JS where the markup is built.
          // Count every <animate>/<animateTransform>, not just the indefinite ones:
          // repeatCount="1" still plays a full 2.8s colour cycle and two 360-degree spins.
          out.smilLoops = (typeof _streamingAvatarSVG === 'function')
            ? (_streamingAvatarSVG().match(/<animate(?:Transform)?\b/g) || []).length : -1;
          return out;
        }""")
        pg.emulate_media(reduced_motion="no-preference")
        pg.wait_for_timeout(150)
        OUT["motion_default"] = pg.evaluate(r"""() => {
          return (typeof _streamingAvatarSVG === 'function')
            ? (_streamingAvatarSVG().match(/<animate(?:Transform)?\b/g) || []).length : -1;
        }""")

        b.close()
    OUT["ok"] = True


if __name__ == "__main__":
    try:
        main(pathlib.Path(sys.argv[1]))
    except Exception as e:            # the harness reads OUT["error"]
        OUT["error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(OUT))
