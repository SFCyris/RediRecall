# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measure the workflow fixes in a real browser and print one JSON object on stdout.

Everything here is a COMPUTED or OBSERVED value — a pixel width, the body a fetch was
actually called with, whether an element is still in the DOM after four seconds — never
a source-text match. The defects being guarded are all invisible to a grep: a progress
bar can be hard-coded to 50%, a Cancel button can POST the wrong URL, and a toast can
carry a dismiss button it never gets to use because it self-destructs first.

Run with an interpreter that has Playwright:  python3 ux_probe.py <index.html>
"""
import json
import pathlib
import sys

OUT: dict = {"ok": False}

# Every network call the page makes is answered from here. index.html is loaded over
# file://, so nothing reaches a server; without a stub, code paths that read a response
# throw before reaching the behaviour under test.
STUB_FETCH = r"""
window.__calls = [];
window.__routes = {};
window.fetch = async (url, opts) => {
  const u = String(url);
  window.__calls.push({url: u, method: (opts && opts.method) || 'GET',
                       body: opts && opts.body ? String(opts.body) : null});
  for (const [k, v] of Object.entries(window.__routes))
    if (u.includes(k)) return {ok: true, status: 200, json: async () => v,
                              blob: async () => new Blob([])};
  return {ok: true, status: 200, json: async () => ({}), blob: async () => new Blob([])};
};
"""


def main(index: pathlib.Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"skip": "playwright not installed"}))
        return

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1400, "height": 950})
        pg.add_init_script(STUB_FETCH)
        pg.goto(index.as_uri(), wait_until="domcontentloaded")
        pg.wait_for_timeout(900)
        # Over file:// every /api call is stubbed to {}, so the collections the settings
        # form iterates never arrive. Seed them, then open the panel where needed: the
        # crawl and ingest panes live inside it, and an element in a closed tab has no
        # box to measure.
        pg.evaluate(r"""() => {
          S.templates = []; S.ragInstances = []; S.models = [];
          S.config = Object.assign({rag:{}, cache:{}, ui:{}, crawl:{}, security:{},
                                    recrawl:{}, watch_folders:{folders:[]},
                                    pricing:{}, web_sources:[]}, S.config || {});
        }""")

        # ── 1 + 2: crawl progress is a measured ratio, not a hard-coded 50% ──────
        # Pixel width, not the style string: a bar can carry width:60% and still paint
        # nothing if its parent has no width, and the old code's 50% was a literal.
        pg.evaluate("openSettings('websources')")
        pg.wait_for_timeout(250)
        OUT["crawl_progress"] = pg.evaluate(r"""() => {
          const card = document.getElementById('crawl-progress-card');
          card.style.display = 'block';
          const bar = document.getElementById('crawl-bar');
          const wrap = bar.parentElement;
          // .progress-bar animates width over .3s, so reading the rect straight after the
          // call catches a frame of the transition — every case measured the same 5% until
          // this was here. Same trap as the focus ring that appeared to be 1px.
          bar.style.transition = 'none';
          const read = st => { _renderCrawlProgress(st);
            return {px: bar.getBoundingClientRect().width,
                    frac: bar.getBoundingClientRect().width / wrap.getBoundingClientRect().width,
                    note: document.getElementById('crawl-progress-note').textContent}; };
          return {
            // max_pages 0 is the shipped default — this is the case that used to pulse
            // at 2% for the whole crawl with no numbers anywhere.
            unlimited_early: read({pages_done: 3, discovered: 60, queued: 57, max_pages: 0}),
            unlimited_late:  read({pages_done: 45, discovered: 60, queued: 15, max_pages: 0}),
            capped:          read({pages_done: 25, discovered: 90, queued: 65, max_pages: 100}),
            nothing_yet:     read({pages_done: 0, discovered: 0, queued: 0, max_pages: 0}),
            paused:          read({pages_done: 5, discovered: 20, queued: 15, max_pages: 0,
                                   paused: true}),
          };
        }""")
        pg.evaluate("document.getElementById('crawl-bar').style.transition = ''")

        # ── 3: Pause and Cancel address the attached crawl, not the URL box ──────
        OUT["crawl_target"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          window.__routes = {'/api/crawl/active': [
            {url: 'https://a.example/docs', pages_done: 4, discovered: 30, queued: 26,
             max_pages: 0, chunks: 9, done: false, paused: false},
            {url: 'https://b.example/wiki', pages_done: 1, discovered: 5, queued: 4,
             max_pages: 0, done: false, paused: false}]};
          document.getElementById('crawl-url').value = 'https://typed-by-the-user.example/new';
          const bar = document.getElementById('crawl-bar');
          bar.style.transition = 'none';        // measure the settled width, not a frame
          await checkActiveCrawls();
          await wait(50);
          // 4 done of 30 discovered. The old code painted a literal 50% here.
          const barFrac = bar.getBoundingClientRect().width /
                          bar.parentElement.getBoundingClientRect().width;
          bar.style.transition = '';
          const urlBoxAfterAttach = document.getElementById('crawl-url').value;
          // Captured before the cancel below, which clears the attachment on purpose.
          const attachedShown = document.getElementById('crawl-attached').textContent;
          const otherCrawlOffered = !!document.querySelector('[data-crawl-attach]');
          window.__calls = [];
          await togglePauseCrawl();
          await cancelCrawl();
          const posts = window.__calls.filter(c => c.method === 'POST');
          _stopCrawlTimers();
          return {
            urlBoxAfterAttach, attachedShown, otherCrawlOffered, barFrac,
            clearedAfterCancel: document.getElementById('crawl-attached').textContent === '',
            pauseBody: (posts.find(c => c.url.includes('/pause')) || {}).body,
            cancelBody: (posts.find(c => c.url.includes('/cancel')) || {}).body,
          };
        }""")

        # ── 2b: a paused crawl reattaches as paused ─────────────────────────────
        OUT["reattach_paused"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          window.__routes = {'/api/crawl/active': [
            {url: 'https://p.example/', pages_done: 7, discovered: 40, queued: 33,
             max_pages: 0, done: false, paused: true}]};
          await checkActiveCrawls(); await wait(50);
          const out = {btn: document.getElementById('crawl-pause-btn').textContent,
                       note: document.getElementById('crawl-progress-note').textContent};
          _stopCrawlTimers();
          return out;
        }""")

        # ── 4: a file ingest can be cancelled, and names the job it cancels ─────
        pg.evaluate("openSettings('rag')")
        pg.wait_for_timeout(250)
        OUT["ingest_cancel"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          window.__routes = {'/api/ingest/active': [
            {job: 'job-abc123', instance: 'default', total: 5, index: 2,
             current: 'notes.pdf', done: false}]};
          await checkActiveIngests(); await wait(30);
          const btn = document.getElementById('ingest-cancel-btn');
          const visible = getComputedStyle(btn).display !== 'none' && btn.offsetParent !== null;
          window.__calls = [];
          await cancelIngest(); await wait(30);
          const post = window.__calls.find(c => c.url.includes('/api/ingest/cancel'));
          return {visible, body: post && post.body,
                  status: document.getElementById('ingest-status').textContent};
        }""")
        pg.evaluate("closeSettings(true)")
        pg.wait_for_timeout(150)

        # ── 5: closing Settings with staged edits asks first ────────────────────
        OUT["settings_dirty"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          const open = id => document.getElementById(id).classList.contains('open');
          openSettings('rag'); await wait(150);
          const cleanBadge = document.getElementById('settings-dirty').hidden;
          // Closing an untouched panel must NOT nag.
          closeSettings(); await wait(80);
          const closedWhenClean = !open('settings-overlay');

          openSettings('rag'); await wait(150);
          const f = document.getElementById('s-top-k');
          f.value = String((parseInt(f.value) || 5) + 3);
          f.dispatchEvent(new Event('input', {bubbles: true}));
          await wait(60);
          const dirtyBadge = !document.getElementById('settings-dirty').hidden;
          closeSettings(); await wait(120);
          const out = {cleanBadge, closedWhenClean, dirtyBadge,
                       stillOpen: open('settings-overlay'),
                       askedFirst: open('modal-overlay'),
                       prompt: document.getElementById('modal-title').textContent};
          closeModal(); await wait(60); closeSettings(true); await wait(60);
          return out;
        }""")

        # ── 6: with no model the welcome screen offers setup, not a dead end ────
        OUT["first_run"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          S.currentModel = '';
          clearChat(true); await wait(60);
          const setup = document.getElementById('welcome-setup');
          const chips = document.getElementById('welcome-chips');
          const unconfigured = {setupShown: !setup.hidden && setup.offsetParent !== null,
                                chipsShown: !chips.hidden && chips.offsetParent !== null,
                                routesToSettings: !!setup.querySelector('button')};
          S.currentModel = 'llama3';
          clearChat(true); await wait(60);
          const s2 = document.getElementById('welcome-setup');
          const c2 = document.getElementById('welcome-chips');
          return {unconfigured,
                  configured: {setupShown: !s2.hidden, chipsShown: !c2.hidden}};
        }""")

        # ── 7: the Status tab covers every provider, and the dots are painted ───
        OUT["status_tab"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          window.__routes = {
            '/api/status/redis':   {ok: true, version: '7.2', mode: 'standalone',
                                    memory_used: '1M', connected_clients: 1, uptime_days: 2,
                                    search_available: true},
            '/api/status/ollama':  {ok: true},
            '/api/status/claude':  {ok: true},
            '/api/status/openai':  {ok: false, error: 'bad key'},
            '/api/status/qwen':    {ok: false, configured: false, error: 'No API key configured'},
            '/api/status/mistral': {ok: false, configured: false, error: 'No API key configured'},
            '/api/status/groq':    {ok: false, configured: false, error: 'No API key configured'},
            '/api/status/gemini':  {ok: true},
            '/api/redis/memory':   {},
          };
          await loadStatus(); await wait(200);
          const txt = document.getElementById('status-content').textContent;
          const named = ['Ollama','Claude','OpenAI','Qwen','Mistral','Groq','Gemini']
                          .filter(n => txt.includes(n));
          const dot = id => {
            const el = document.getElementById(id + '-dot-cfg');
            return el ? getComputedStyle(el).backgroundColor : null;
          };
          return {named, deadButton: txt.includes('Update dots'),
                  offersSetUp: txt.includes('Set up'),
                  dots: {gemini: dot('gemini'), openai: dot('openai'), qwen: dot('qwen')}};
        }""")

        # ── 8: the scheduled re-crawl UI exists and drives the real endpoints ───
        OUT["recrawl"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          window.__routes = {'/api/recrawl/sources': [
            {url: 'https://docs.example/llms.txt', instance: 'default', depth: 1,
             last_crawled: 1700000000}]};
          await loadScheduledSources(); await wait(50);
          const table = document.getElementById('recrawl-table');
          const rendered = table.textContent;
          window.__calls = [];
          document.getElementById('crawl-url').value = 'https://new.example/docs';
          await addScheduledSource(); await wait(60);
          const post = window.__calls.find(c => c.url.includes('/api/recrawl/sources')
                                              && c.method === 'POST');
          window.__calls = [];
          await triggerRecrawl(); await wait(60);
          const trig = window.__calls.find(c => c.url.includes('/api/recrawl/trigger'));
          return {rendered, hasRow: rendered.includes('docs.example'),
                  hasRemove: !!table.querySelector('[data-recrawl-del]'),
                  addBody: post && post.body, triggered: !!trig,
                  toggleExists: !!document.getElementById('s-recrawl-enabled'),
                  inPayload: 'recrawl' in _collectSettings()};
        }""")

        # ── 9: an error toast survives, is dismissable, and is logged ───────────
        OUT["toasts"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          document.getElementById('toast-container').innerHTML = '';
          toast('disk on fire', 'error');
          toast('all good', 'success');
          await wait(4000);   // comfortably past the old blanket 3s expiry
          const c = document.getElementById('toast-container');
          const alive = [...c.querySelectorAll('.toast')].map(t => t.textContent.trim());
          const err = c.querySelector('.toast.error');
          const closeBtn = err && err.querySelector('.toast-close');
          const closable = !!closeBtn;
          if (closeBtn) closeBtn.click();       // must not throw when absent: a probe that
                                                // crashes skips the suite instead of failing it
          await wait(500);
          const afterDismiss = c.querySelectorAll('.toast.error').length;
          switchTab('logs'); renderToastLog();
          const log = document.getElementById('toast-log').textContent;
          return {alive, closable, afterDismiss,
                  loggedError: log.includes('disk on fire'),
                  loggedSuccess: log.includes('all good')};
        }""")

        OUT["toast_dedup"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          const c = document.getElementById('toast-container');
          c.innerHTML = '';
          // What a failing "search all conversations" does with Redis down.
          for (let i = 0; i < 12; i++) toast('Could not load that conversation', 'error');
          toast('a different problem', 'error');
          await wait(200);
          const toasts = [...c.querySelectorAll('.toast')];
          const out = {count: toasts.length,
                       badge: (c.querySelector('.toast-count') || {}).textContent,
                       texts: toasts.map(t => t.textContent.trim())};
          c.innerHTML = '';
          return out;
        }""")

        # ── 10: Cmd/Ctrl+F is left to the browser; Shift+Cmd+F is ours ─────────
        OUT["find_key"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          const press = (key, shift) => {
            const e = new KeyboardEvent('keydown',
              {key, shiftKey: !!shift, metaKey: true, ctrlKey: true,
               bubbles: true, cancelable: true});
            document.dispatchEvent(e);
            return e.defaultPrevented;
          };
          closeSearch();
          const plainPrevented = press('f', false);
          await wait(60);
          const openedOnPlain = document.getElementById('search-overlay').classList.contains('open');
          const shiftPrevented = press('F', true);
          await wait(60);
          const openedOnShift = document.getElementById('search-overlay').classList.contains('open');
          return {plainPrevented, openedOnPlain, shiftPrevented, openedOnShift};
        }""")

        # ── 10b: search reports a count and can reach other conversations ──────
        OUT["search"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          S.sessionId = 'sA';
          S.sessions = {
            sA: {title: 'Current', messages: [
                  {role: 'user', content: 'tell me about vector recall'},
                  {role: 'assistant', content: 'no match here', meta: {id: 'm1', chunks: [
                     {text: 'the recall knob lives in settings', source: 'notes.md'}]}}]},
            sB: {title: 'Older', messages: [
                  {role: 'assistant', content: 'recall is discussed here too', meta: {id: 'm2'}}]},
          };
          document.getElementById('search-all-sessions').checked = false;
          document.getElementById('search-chunks').checked = true;
          // searchChat is debounced; waiting past it is part of what is being checked.
          const settle = async () => { await wait(420); };
          searchChat('recall'); await settle();
          const oneSession = {count: document.getElementById('search-count').textContent,
                              hits: document.querySelectorAll('.search-hit').length};
          document.getElementById('search-all-sessions').checked = true;
          searchChat('recall'); await settle();
          const allSessions = {count: document.getElementById('search-count').textContent,
                               hits: document.querySelectorAll('.search-hit').length,
                               mentionsOther: document.getElementById('search-results')
                                                .textContent.includes('Older')};
          document.getElementById('search-chunks').checked = false;
          searchChat('recall'); await settle();
          const noChunks = document.querySelectorAll('.search-hit').length;
          return {oneSession, allSessions, noChunks};
        }""")

        # Everything a settings tab needs must load however the tab is reached — the
        # buttons are the route people actually take.
        OUT["tab_entry"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          window.__routes = {
            '/api/recrawl/sources': [{url: 'https://sched.example/x', instance: 'default',
                                      depth: 0, last_crawled: 0}],
            '/api/ingest/active':   [{job: 'jt1', instance: 'docs', total: 3, index: 0,
                                      current: 'a.pdf', done: false, started: true}],
            '/api/status/redis':    {ok: true, version: '7', mode: 'standalone',
                                     memory_used: '1M', connected_clients: 1,
                                     uptime_days: 1, search_available: true},
            '/api/status/gemini':   {ok: true},
            '/api/status/qwen':     {ok: false, configured: false, error: 'no key'},
            '/api/redis/memory':    {},
          };
          openSettings('general'); await wait(200);
          const click = t => document.querySelector(`.stab[data-tab="${t}"]`).click();

          click('websources'); await wait(400);
          const recrawl = document.getElementById('recrawl-table').textContent;

          click('providers'); await wait(700);
          const dot = getComputedStyle(document.getElementById('gemini-dot-cfg')).backgroundColor;

          click('rag'); await wait(400);
          const ingestVisible = document.getElementById('ingest-cancel-btn').offsetParent !== null;

          _stopIngestPoll(); _stopCrawlTimers(); closeSettings(true); await wait(150);
          return {recrawl, dot, ingestVisible};
        }""")

        # A click on "Use" changes what Save Settings would post; the input/change watch
        # cannot see it.
        OUT["provider_dirty"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          openSettings('providers'); await wait(250);
          S.provider = 'ollama';
          const before = _collectSettings().provider;
          setProvider('openai'); await wait(150);
          const out = {before, after: _collectSettings().provider,
                       dirty: _settingsDirty(),
                       badgeShown: !document.getElementById('settings-dirty').hidden};
          closeSettings(); await wait(150);
          out.askedFirst = document.getElementById('modal-overlay').classList.contains('open');
          closeModal(); await wait(80); closeSettings(true); await wait(80);
          S.provider = 'ollama';
          return out;
        }""")

        # Opening Settings must not park Enter on a destructive-by-accident control.
        OUT["settings_focus"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          openSettings('general'); await wait(250);
          const el = document.activeElement;
          const out = {label: (el.getAttribute('aria-label') || el.textContent || '').trim(),
                       inPanel: document.getElementById('settings-overlay').contains(el)};
          closeSettings(true); await wait(120);
          return out;
        }""")

        # A run of DIFFERENT never-expiring errors must stay bounded.
        OUT["toast_cap"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          const c = document.getElementById('toast-container');
          c.innerHTML = '';
          for (let i = 0; i < 20; i++) toast('failure number ' + i, 'error');
          await wait(200);
          const out = {visible: c.querySelectorAll('.toast').length,
                       // the newest survive; the log keeps all of them
                       keepsNewest: c.textContent.includes('failure number 19'),
                       logged: _toastLog.filter(m => m.msg.startsWith('failure number')).length};
          c.innerHTML = '';
          return out;
        }""")

        # A fragment in the seed URL: the server keys on the stripped form.
        OUT["crawl_fragment"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          window.__routes = {'/api/crawl/active': [
            {url: 'https://frag.example/docs', pages_done: 6, discovered: 20, queued: 14,
             resolved: 9, max_pages: 0, done: false, paused: false}]};
          _crawlUrl = null; _crawlStreamUrl = null;
          document.getElementById('crawl-progress-card').style.display = 'block';
          // Drive the REAL entry point with a fragment in the box. startCrawl sets both
          // _crawlUrl and _crawlStreamUrl from that value and then fails on the stubbed
          // stream (no .body), which is fine — the two variables are already set, and
          // setting them by hand would skip the very conversion under test.
          document.getElementById('crawl-url').value = 'https://frag.example/docs#install';
          try { await startCrawl(); } catch (e) {}
          await wait(80);
          const afterStart = {tracked: _crawlUrl, stream: _crawlStreamUrl};
          await checkActiveCrawls(); await wait(80);
          const out = {...afterStart,
                       tracked: _crawlUrl,
                       listedAsOther: !!document.querySelector('[data-crawl-attach]'),
                       // visible only when this tab is recognised as owning the stream,
                       // which is the symptom the fragment mismatch produced
                       rateShown: document.getElementById('crawl-rate-stat').style.display !== 'none',
                       note: document.getElementById('crawl-progress-note').textContent};
          _stopCrawlTimers(); _crawlAbort = null; _crawlStreamUrl = null;
          return out;
        }""")

        # Progress must count pages RESOLVED, not pages indexed.
        # The bar needs a laid-out parent to measure, so the pane has to be on screen.
        pg.evaluate("openSettings('websources')")
        pg.wait_for_timeout(300)
        OUT["crawl_resolved"] = pg.evaluate(r"""() => {
          const bar = document.getElementById('crawl-bar');
          document.getElementById('crawl-progress-card').style.display = 'block';
          bar.style.transition = 'none';
          const wrap = bar.parentElement;
          const read = st => { _renderCrawlProgress(st);
            return {frac: bar.getBoundingClientRect().width / wrap.getBoundingClientRect().width,
                    note: document.getElementById('crawl-progress-note').textContent}; };
          const out = {
            // an incremental re-crawl: 2 indexed, 38 skipped as already-indexed
            incremental: read({pages_done: 2, skipped: 38, blocked: 0, errors: 0,
                               resolved: 40, discovered: 50, queued: 10, max_pages: 0}),
            // a payload from before `resolved` existed — summed locally instead
            legacy:      read({pages_done: 2, skipped: 38, blocked: 0, errors: 0,
                               discovered: 50, queued: 10, max_pages: 0}),
          };
          bar.style.transition = '';
          return out;
        }""")
        pg.evaluate("_stopCrawlTimers(); _stopIngestPoll(); closeSettings(true)")
        pg.wait_for_timeout(200)

        # `hidden` on a display:flex element does nothing — origin beats specificity.
        OUT["chips_hidden"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          S.currentModel = ''; clearChat(true); await wait(80);
          const chips = document.getElementById('welcome-chips');
          const out = {attr: chips.hidden,
                       display: getComputedStyle(chips).display,
                       painted: chips.offsetParent !== null && chips.getBoundingClientRect().height > 0,
                       clickable: chips.getBoundingClientRect().height > 0};
          S.currentModel = 'llama3'; clearChat(true); await wait(80);
          const back = document.getElementById('welcome-chips');
          out.restored = getComputedStyle(back).display !== 'none'
                      && back.getBoundingClientRect().height > 0;
          return out;
        }""")

        # Cancel must not tear down a stream that belongs to a different crawl.
        OUT["cancel_stream"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          // this tab owns the stream for crawl A...
          _crawlAbort = new AbortController();
          _crawlStreamUrl = 'https://a.example/';
          let abortedA = false;
          _crawlAbort.signal.addEventListener('abort', () => { abortedA = true; });
          // ...while the panel is attached to crawl B, adopted from elsewhere
          _crawlUrl = 'https://b.example/';
          window.__calls = [];
          await cancelCrawl(); await wait(60);
          const post = window.__calls.find(c => c.url.includes('/api/crawl/cancel'));
          const out = {cancelledOnServer: JSON.parse(post.body).url, abortedLocalStream: abortedA};
          // ...and cancelling the crawl this tab DOES own must abort it
          _crawlAbort = new AbortController();
          _crawlStreamUrl = 'https://a.example/';
          let abortedOwn = false;
          _crawlAbort.signal.addEventListener('abort', () => { abortedOwn = true; });
          _crawlUrl = 'https://a.example/';
          await cancelCrawl(); await wait(60);
          out.abortedOwnStream = abortedOwn;
          _crawlAbort = null; _crawlStreamUrl = null; _crawlUrl = null;
          return out;
        }""")

        # The 1s poll must stop when there is nothing left to poll for.
        OUT["poll_stops"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          window.__routes = {'/api/crawl/active': [
            {url: 'https://gone.example/', pages_done: 1, discovered: 4, queued: 3,
             resolved: 1, max_pages: 0, done: false, paused: false}]};
          openSettings('websources'); await wait(400);
          const started = _crawlPollTimer !== null;
          // the crawl disappears from the listing — finished and reaped
          window.__routes = {'/api/crawl/active': []};
          await wait(1400);
          const stoppedWhenGone = _crawlPollTimer === null;

          // and closing the panel stops a poll that is still live
          window.__routes = {'/api/crawl/active': [
            {url: 'https://live.example/', pages_done: 1, discovered: 4, queued: 3,
             resolved: 1, max_pages: 0, done: false, paused: false}]};
          await checkActiveCrawls(); await wait(100);
          const runningAgain = _crawlPollTimer !== null;
          closeSettings(true); await wait(100);
          const stoppedOnClose = _crawlPollTimer === null;
          _stopCrawlTimers(); _crawlUrl = null;
          return {started, stoppedWhenGone, runningAgain, stoppedOnClose};
        }""")

        # An adopted ingest must keep up with the job, and report how it ended.
        OUT["ingest_poll"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          window.__routes = {'/api/ingest/active': [
            {job: 'jp', instance: 'docs', total: 4, index: 0, current: 'one.pdf',
             ok: 0, errors: 0, done: false, started: true},
            {job: 'jq', instance: 'other', total: 2, index: 0, current: 'x.txt',
             ok: 0, errors: 0, done: false, started: true}]};
          openSettings('rag'); await wait(400);
          const first = document.getElementById('ingest-status').textContent;
          const chooser = !!document.querySelector('[data-ingest-attach]');
          // the job advances
          window.__routes = {'/api/ingest/active': [
            {job: 'jp', instance: 'docs', total: 4, index: 2, current: 'three.pdf',
             ok: 2, errors: 0, done: false, started: true}]};
          await wait(1300);
          const advanced = document.getElementById('ingest-status').textContent;
          // and finishes
          window.__routes = {'/api/ingest/active': [
            {job: 'jp', instance: 'docs', total: 4, index: 3, current: 'four.pdf',
             ok: 3, errors: 1, done: true, cancelled: false, started: true}]};
          await wait(1300);
          const finished = document.getElementById('ingest-status').textContent;
          const btnGone = document.getElementById('ingest-cancel-btn').style.display === 'none';
          _stopIngestPoll(); closeSettings(true); await wait(120);
          return {first, chooser, advanced, finished, btnGone, pollStopped: true};
        }""")

        # A filename with an ampersand must not be double-encoded into textContent.
        OUT["ingest_name"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          window.__routes = {'/api/ingest/active': [
            {job: 'jn', instance: 'i', total: 1, index: 0, current: 'Q&A notes.pdf',
             ok: 0, errors: 0, done: false, started: true}]};
          await checkActiveIngests(); await wait(80);
          const t = document.getElementById('ingest-status').textContent;
          _stopIngestPoll();
          return {text: t};
        }""")

        # Typing must not re-fetch every conversation once per character.
        OUT["search_fetches"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          S.sessionId = 'k0';
          S.sessions = {k0: {title: 'cur', messages: [{role: 'user', content: 'recall'}]}};
          for (let i = 1; i <= 20; i++)
            S.sessions['k' + i] = {title: 'old ' + i, messages: [], _stub: true};
          window.__routes = {'/api/sessions/': {messages: [{role: 'assistant',
                                                            content: 'recall here'}]}};
          document.getElementById('search-all-sessions').checked = true;
          document.getElementById('search-chunks').checked = false;
          window.__calls = [];
          for (const q of ['r', 're', 'rec', 'reca', 'recal', 'recall'])
            searchChat(q);                       // six keystrokes, no awaits between them
          await wait(900);
          const debounced = window.__calls.filter(c => c.url.includes('/api/sessions/')).length;

          // ...and the case the debounce does NOT cover: two searches far enough apart to
          // both fire, with the first one's hydrate still in flight. Driving _runSearch
          // directly is the point — going through searchChat would collapse them again and
          // measure the debounce a second time instead of the in-flight guard.
          for (let i = 1; i <= 20; i++)
            S.sessions['k' + i] = {title: 'old ' + i, messages: [], _stub: true};
          window.__calls = [];
          const a = _runSearch('recall'), b = _runSearch('recall');
          await Promise.all([a, b]); await wait(200);
          const overlapping = window.__calls.filter(c => c.url.includes('/api/sessions/')).length;

          return {debounced, overlapping, stubs: 20,
                  count: document.getElementById('search-count').textContent};
        }""")

        # Keeping an answer: what the dialog offers, and what it actually posts.
        OUT["save_answer"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          S.sessionId = 'sv'; S.ragInstances = [{name: 'docs'}];
          const id = 'mSave';
          S.sessions = {sv: {title: 't', messages: [
            {role: 'user', content: 'how long does a cached answer live?'},
            {role: 'assistant', content: 'It lives for cache.ttl seconds [1].',
             meta: {id, chunks: [{n: 1, text: 'ttl is 3600', source: 'settings.md'},
                                 {n: 2, text: 'evicted after', source: 'cache.md'}]}}]}};
          clearChat(true);
          appendMessage('assistant', 'It lives for cache.ttl seconds [1].', {id});
          await wait(80);

          saveAnswerToKb(id); await wait(200);
          const prefilled = {
            title: document.getElementById('sv-title').value,
            question: document.getElementById('sv-question').value,
            answer: document.getElementById('sv-answer').value,
            instance: document.getElementById('sv-instance').value,
            // the dedicated instance is offered even though it does not exist yet
            options: [...document.getElementById('sv-instance').options].map(o => o.value),
            sourcesToggle: !!document.getElementById('sv-sources'),
          };
          // curate before saving — the whole point of the edit step
          document.getElementById('sv-answer').value = 'It lives for cache.ttl seconds.';
          window.__calls = [];
          closeModal();
          await _commitAnswerToKb(S.sessions.sv.messages[1].meta.chunks);
          await wait(150);

          const created = window.__calls.find(c => c.url.endsWith('/api/rag/instances')
                                                && c.method === 'POST');
          const posted = window.__calls.find(c => c.url.includes('/ingest/text'));
          return {prefilled,
                  createdBody: created && created.body,
                  postedTo: posted && posted.url,
                  postedBody: posted && posted.body};
        }""")

        # An answer saved into an instance that already exists must not re-create it.
        OUT["save_existing"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          // an instance that lives on a NAMED endpoint, not the default one
          S.ragInstances = [{name: 'saved-answers', redis_endpoint: 'archive'},
                            {name: 'docs'}];
          const id = 'mSave2';
          S.sessionId = 'sv2';
          S.sessions = {sv2: {title: 't', messages: [
            {role: 'user', content: 'q two'},
            {role: 'assistant', content: 'a two', meta: {id, chunks: []}}]}};
          clearChat(true);
          appendMessage('assistant', 'a two', {id});
          await wait(80);
          saveAnswerToKb(id); await wait(200);
          const noToggle = !document.getElementById('sv-sources');   // no chunks to offer
          window.__calls = [];
          closeModal();
          await _commitAnswerToKb([]);
          await wait(150);
          const post = window.__calls.find(c => c.url.includes('/ingest/text'));
          return {noToggle,
                  created: window.__calls.some(c => c.url.endsWith('/api/rag/instances')
                                                 && c.method === 'POST'),
                  posted: !!post,
                  postedUrl: post && post.url};
        }""")

        # The repair is only worth anything if the LANE applies it. mermaid itself is a
        # CDN script that never loads over file://, so its draw is stubbed and what the
        # timeline lane hands it is what gets inspected.
        OUT["timeline_lane"] = pg.evaluate(r"""async () => {
          const real = RICH_LANES.mermaid.draw;
          let handed = null;
          RICH_LANES.mermaid.draw = async (out, src) => { handed = src; };
          const host = document.createElement('div');
          document.body.appendChild(host);
          try {
            await RICH_LANES.timeline.draw(host, '2024-01-01 00:00 : Sunrise in New York', 'tl1');
            const withoutHeader = handed;
            await RICH_LANES.timeline.draw(host, 'timeline\n09:30 : Standup', 'tl2');
            return {withoutHeader, withHeader: handed};
          } finally { RICH_LANES.mermaid.draw = real; host.remove(); }
        }""")

        # ── 11: .btn-ghost is a defined rule, not an inherited default ─────────
        OUT["btn_ghost"] = pg.evaluate(r"""() => {
          const host = document.createElement('div');
          host.style.color = 'rgb(1, 2, 3)';       // a colour a real rule will override
          const ghost = document.createElement('button');
          ghost.className = 'btn btn-ghost';
          ghost.textContent = 'Cancel';
          host.appendChild(ghost);
          document.body.appendChild(host);
          const cs = getComputedStyle(ghost);
          const out = {color: cs.color, borderStyle: cs.borderStyle,
                       borderWidth: cs.borderWidth,
                       inheritedHostColour: cs.color === 'rgb(1, 2, 3)',
                       ruleExists: [...document.styleSheets].some(sh => {
                         let rs; try { rs = sh.cssRules; } catch (e) { return false; }
                         return [...(rs || [])].some(r => r.selectorText === '.btn-ghost');
                       })};
          host.remove();
          return out;
        }""")

        # ── 12: [n] in the answer reaches source #n in the inspector ───────────
        OUT["citations"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          S.sessionId = 'cite'; S.sessions = {cite: {title: 't', messages: []}};
          clearChat(true);
          appendMessage('assistant', 'Streams are append-only [2]. Keys expire [1].',
                        {id: 'mCite'});
          // Deliberately NOT in relevance order: this is the payload shape that made the
          // inspector's #n disagree with the answer's [n].
          updateRagContext('mCite', [
            {n: 1, text: 'keys expire after the ttl', source: 'ttl.md', relevance: 0.40},
            {n: 2, text: 'streams only ever append', source: 'streams.md', relevance: 0.90},
          ], {rag: 0.1}, 'streams');
          await wait(120);
          const el = document.getElementById('mCite');
          const labels = [...el.querySelectorAll('.rag-chunk')].map(c => ({
            n: c.dataset.citeN,
            badge: c.querySelector('.rag-chunk-score').textContent.trim(),
            text: c.querySelector('.rag-chunk-content').textContent.trim()}));
          const refs = [...el.querySelectorAll('.cite-ref')].map(r => r.textContent);
          const two = [...el.querySelectorAll('.cite-ref')].find(r => r.textContent === '[2]');
          if (two) two.click();
          await wait(120);
          const target = document.getElementById('cite-mCite-2');
          return {labels, refs,
                  clickedOpensRightChunk: !!(target &&
                     target.querySelector('.rag-chunk-content').classList.contains('open') &&
                     target.querySelector('.rag-chunk-content').textContent.includes('append')),
                  inspectorOpened: !!(target && target.closest('.rag-inspector')
                                        .classList.contains('open'))};
        }""")

        # The order a LIVE answer actually arrives in: rag_context first, then tokens.
        # The restore path above hands updateRagContext a finished bubble, which hides the
        # fact that on a real turn there is no [n] to link when the chunks arrive.
        OUT["citations_streamed"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          S.sessionId = 'cs'; S.sessions = {cs: {title: 't', messages: []}}; clearChat(true);
          const id = 'mStream';
          appendMessage('assistant', '', {id});
          // 1. chunks arrive BEFORE any token, exactly as handle_chat sends them
          updateRagContext(id, [
            {n: 1, text: 'keys expire after the ttl', source: 'ttl.md', relevance: 0.40},
            {n: 2, text: 'streams only ever append', source: 'streams.md', relevance: 0.90},
          ], {rag: 0.1}, 'streams');
          await wait(60);
          const linkedTooEarly = document.querySelectorAll('#' + id + ' .cite-ref').length;
          // 2. now the answer streams in, one token at a time
          for (const tok of ['Streams are ', 'append-only ', '[2]. ', 'Keys expire ', '[1].'])
            updateStreamingMsg(id, tok);
          await wait(250);
          finalizeStreamingMsg(id);
          await wait(200);
          const el = document.getElementById(id);
          const refs = [...el.querySelectorAll('.cite-ref')].map(r => r.textContent);
          // finalize runs twice on a real turn (token{done} then stream_end) — the guard
          // must stop the second pass nesting a button inside the first one's button.
          finalizeStreamingMsg(id);
          await wait(120);
          return {linkedTooEarly, refs,
                  afterSecondFinalize: el.querySelectorAll('.cite-ref').length,
                  nested: !!el.querySelector('.cite-ref .cite-ref')};
        }""")

        # Stepping between regenerated versions rewrites the bubble's innerHTML.
        OUT["citations_version_switch"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          const id = 'mVer';
          S.sessionId = 'cv';
          const versions = [{content: 'First take, cites [1].', provider: 'p', model: 'm'},
                            {content: 'Second take, cites [2] and [1].', provider: 'p', model: 'm'}];
          S.sessions = {cv: {title: 't', messages: [{role: 'assistant',
            content: versions[1].content,
            meta: {id, versions, chunks: [{n: 1, text: 'one', source: 'a.md'},
                                          {n: 2, text: 'two', source: 'b.md'}]}}]}};
          clearChat(true);
          appendMessage('assistant', versions[1].content, {id});
          updateRagContext(id, S.sessions.cv.messages[0].meta.chunks, {rag: 0.1}, '');
          await wait(120);
          const before = document.querySelectorAll('#' + id + ' .cite-ref').length;
          // finalizeStreamingMsg deletes the side-channel meta once the turn has landed, so
          // by the time anyone steps between versions the count has to come from the STORED
          // turn. Leaving the live meta in place hides that and leaves the fallback untested.
          delete _msgMeta[id];
          showVersion(id, versions, 0);   // step back to the first take
          await wait(150);
          const afterBack = [...document.querySelectorAll('#' + id + ' .cite-ref')]
                              .map(r => r.textContent);
          showVersion(id, versions, 1);   // and forward again
          await wait(150);
          const afterForward = [...document.querySelectorAll('#' + id + ' .cite-ref')]
                                 .map(r => r.textContent);
          return {before, afterBack, afterForward};
        }""")

        # _linkCitations must be safe to run twice over the same element: three call sites
        # now reach it, and a second pass that re-read its own buttons would nest them.
        OUT["citations_idempotent"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          const id = 'mIdem';
          S.sessionId = 'ci'; S.sessions = {ci: {title: 't', messages: []}}; clearChat(true);
          appendMessage('assistant', 'Cites [1] once.', {id});
          const el = document.getElementById(id);
          _linkCitations(el, id, 1);
          _linkCitations(el, id, 1);
          _linkCitations(el, id, 1);
          await wait(80);
          return {count: el.querySelectorAll('.cite-ref').length,
                  nested: !!el.querySelector('.cite-ref .cite-ref'),
                  text: el.querySelector('.msg-bubble').textContent.trim()};
        }""")

        # A bracketed number inside a code sample is not a citation.
        OUT["citations_in_code"] = pg.evaluate(r"""async () => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          S.sessionId = 'c2'; S.sessions = {c2: {title: 't', messages: []}};
          clearChat(true);
          appendMessage('assistant', 'Use `arr[1]` for the head [1].', {id: 'mCode'});
          updateRagContext('mCode', [{n: 1, text: 'arrays index from zero',
                                      source: 'a.md', relevance: 0.5}], {rag: 0.1}, '');
          await wait(120);
          const el = document.getElementById('mCode');
          return {refs: [...el.querySelectorAll('.cite-ref')].map(r => r.textContent),
                  codeUntouched: !el.querySelector('code .cite-ref')};
        }""")

        b.close()
    OUT["ok"] = True


if __name__ == "__main__":
    try:
        main(pathlib.Path(sys.argv[1]))
    except Exception as e:            # the harness reads OUT["error"]
        OUT["error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(OUT))
