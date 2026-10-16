/* l2only.js -- Scanner Studio's "L2 only" view (2026-10-16)
 *
 * The Level 2 tile, full screen: symbol, price and the day's change on
 * top, then the ladder (or wheels) and the tape. For a phone or tablet on
 * a desk at work, so the position and P&L are hidden.
 *
 *   enter   the "L2 only" button in the Level 2 tile's title bar, or an
 *           address ending in ?l2only (or ?l2only=TSLA)
 *   leave   the "Full app" button
 *
 * It is remembered: a home-screen app has no address bar, so the choice
 * lives in localStorage and reopening the app goes straight back to it.
 *
 * Nothing is rebuilt. The live tile focus.js made is MOVED into a full-
 * screen panel, so l2pie.js keeps drawing into the same elements and the
 * data, polling and IBKR depth are exactly what the Focus page does.
 * Display only: like the rest of Scanner Studio, nothing here trades.
 *
 * Install: save beside focus.js and add, after focus.js's script tag,
 *   <script src="l2only.js?v=1"></script>
 */
(function () {
  'use strict';

  const KEY = 'ss-l2only-v1';                 // { on: true, symbol: 'SPCX' }
  const DEFAULT_SYMBOL = 'SPCX';

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY)) || null; } catch (e) { return null; }
  }
  function save(v) {
    try { v ? localStorage.setItem(KEY, JSON.stringify(v)) : localStorage.removeItem(KEY); } catch (e) { /* private mode */ }
  }

  const css = `
html.l2only, html.l2only body { overflow: hidden !important; }
#l2o { display: none; }
html.l2only #l2o {
  display: flex; flex-direction: column; gap: 6px;
  position: fixed; inset: 0; z-index: 2147483000; overflow: auto;
  background: var(--bg, #2B3648); color: var(--text, #D7E2F2);
  padding: calc(env(safe-area-inset-top, 0px) + 6px) 8px calc(env(safe-area-inset-bottom, 0px) + 6px);
}
#l2o .l2o-bar { display: flex; align-items: center; gap: 8px; }
#l2o .l2o-bar .fx-head { flex: 1 1 auto; margin: 0; min-width: 0; }
#l2o .fx-pos, #l2o .fx-hint { display: none !important; }
#l2o .fx-l2tile {
  flex: 1 0 auto; margin: 0;
  height: auto !important; max-height: none !important;
  min-height: calc(100vh - 90px);
  display: flex; flex-direction: column;
}
#l2o .fx-l2stage { flex: 1 1 auto; min-height: 0; }
#l2o .l2o-wait { margin: auto; color: var(--muted, #9CADC8); }
.l2o-btn {
  font: inherit; font-size: 12px; line-height: 1; cursor: pointer;
  padding: 6px 9px; border-radius: 8px; white-space: nowrap;
  color: var(--text, #D7E2F2); background: rgba(0,0,0,.25);
  border: 1px solid var(--muted, #9CADC8);
}
.l2o-enter { margin-left: auto; }
html.l2only .l2o-enter { display: none; }
`;

  const params = new URLSearchParams(location.search);
  let saved = load();
  if (params.has('l2only')) {
    saved = { on: true, symbol: (params.get('l2only') || '').trim().toUpperCase()
                               || (saved && saved.symbol) || DEFAULT_SYMBOL };
    save(saved);
  }
  const on = !!(saved && saved.on);
  // Before the app paints, so the full layout never flashes up first.
  if (on) document.documentElement.classList.add('l2only');

  function currentSymbol() {
    const input = document.querySelector('.fx-head input');
    const typed = input && (input.value || '').trim().toUpperCase();
    return typed || (load() || {}).symbol || DEFAULT_SYMBOL;
  }

  let panel, bar, waiting;

  function buildPanel() {
    const style = document.createElement('style');
    style.id = 'l2o-style';
    style.textContent = css;
    document.head.append(style);

    panel = document.createElement('div');
    panel.id = 'l2o';
    bar = document.createElement('div');
    bar.className = 'l2o-bar';
    const exit = document.createElement('button');
    exit.type = 'button';
    exit.className = 'l2o-btn l2o-exit';
    exit.textContent = '\u2715 Full app';
    exit.title = 'Back to the whole of Scanner Studio';
    exit.onclick = leave;
    bar.append(exit);
    waiting = document.createElement('div');
    waiting.className = 'l2o-wait';
    waiting.textContent = 'Opening Level 2\u2026';
    panel.append(bar, waiting);
    document.body.append(panel);
  }

  /* Move the newest Focus header and Level 2 tile into the panel. Run
     whenever the page changes: Focus rebuilds both when the symbol does. */
  function adopt() {
    if (!document.documentElement.classList.contains('l2only')) return;
    const tile = [...document.querySelectorAll('.fx-l2tile')].find(t => !panel.contains(t));
    if (!tile) return;
    const head = [...document.querySelectorAll('.fx-head')].find(t => !panel.contains(t));
    panel.querySelectorAll('.fx-l2tile, .fx-head').forEach(old => old.remove());
    waiting.remove();
    if (head) bar.prepend(head);
    panel.append(tile);
    save({ on: true, symbol: currentSymbol() });
  }

  /* The way in: a button in each Level 2 tile's title bar. */
  function addEnterButtons() {
    document.querySelectorAll('.fx-l2tile > header').forEach(header => {
      if (header.querySelector('.l2o-enter')) return;
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'l2o-btn l2o-enter';
      b.textContent = '\u2922 L2 only';
      b.title = 'Show only Level 2, full screen. Remembered until "Full app".';
      b.onclick = enter;
      header.append(b);
    });
  }

  function enter() {
    save({ on: true, symbol: currentSymbol() });
    document.documentElement.classList.add('l2only');
    adopt();
  }

  function leave() {
    save({ on: false, symbol: currentSymbol() });
    // A reload puts every tile back where focus.js built it -- simpler and
    // safer than moving them home one by one.
    location.replace(location.pathname);
  }

  /* Opening Focus on the remembered symbol, once the app has loaded. The
     app may still be settling on its start page, so check it took. */
  function openRemembered() {
    const symbol = (load() || {}).symbol || DEFAULT_SYMBOL;
    let tries = 0;
    const timer = setInterval(() => {
      if (document.querySelector('#l2o .fx-l2tile')) { clearInterval(timer); return; }
      if (typeof window.openFocus === 'function' && tries % 10 === 0) window.openFocus(symbol);
      if (++tries > 100) {                       // 20 seconds
        clearInterval(timer);
        waiting.textContent = 'Level 2 did not open. Tap "Full app" and open ' + symbol + ' from Focus.';
      }
    }, 200);
  }

  function start() {
    buildPanel();
    new MutationObserver(() => { addEnterButtons(); adopt(); })
      .observe(document.body, { childList: true, subtree: true });
    addEnterButtons();
    if (on) { adopt(); openRemembered(); }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
