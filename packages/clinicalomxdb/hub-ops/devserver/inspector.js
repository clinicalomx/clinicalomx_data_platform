// Dev client for the operator portal. Injected by hub-ops/devserver/serve.py as the page is
// served, so none of this exists in the committed file or on the VM.
//
// Three things: a state switcher, live reload, and an inspector.
//
// The inspector treats styles and content differently because the portal does. Its CSS is a
// static, selector-addressable block, so a style edit round-trips exactly into the source. Its
// DOM is almost entirely built from template literals inside render functions, so a text or
// structural edit cannot be mapped back mechanically; those are queued as notes for the agent to
// apply to the right template. Pretending both round-trip would be the wrong kind of convenient.
(function () {
  'use strict';

  // Opens the login card, and optionally presses Continue so the capture lands on the first
  // PROMPT rather than the username field. Without that second step the prompt states, which are
  // the ones worth reviewing, are unreachable as a URL.
  function autoOpenLogin(advance) {
    let tries = 0;
    const t = setInterval(() => {
      const btn = document.querySelector('#add-conn, [data-login]');
      if (btn) {
        btn.click();
        clearInterval(t);
        if (advance) {
          let more = 0;
          const t2 = setInterval(() => {
            const start = document.querySelector('#login-start');
            if (start) { start.click(); clearInterval(t2); return; }
            if (++more > 40) clearInterval(t2);
          }, 100);
        }
        return;
      }
      if (++tries > 40) clearInterval(t);
    }, 150);
  }
  const CFG = window.__dev || { state: 'healthy', states: [], login: 'password-prompt', open: '' };
  const $ = (sel, root) => (root || document).querySelector(sel);

  // Screenshot mode: no live-reload stream and no dev chrome. The stream is a request that never
  // completes, which stops headless Chrome ever deciding the page has loaded, and the toolbar
  // would sit on top of the UI being judged. Auto-open still runs, so an interaction state is
  // still reachable as a plain URL.
  if (CFG.shot) {
    if (CFG.open.startsWith('login')) autoOpenLogin(CFG.open === 'login-prompt');
    return;
  }

  // --- live reload ---------------------------------------------------------
  // A style-only change hot-swaps the stylesheet rather than reloading, so the state you are
  // inspecting (an open login card, a selected tab) survives the save. Most iterations here are
  // style tweaks, and losing that state every time would make the loop pointless.
  const es = new EventSource('/__dev/events');
  es.addEventListener('reload', () => location.reload());
  es.addEventListener('style', (ev) => {
    try {
      const css = JSON.parse(ev.data).data;
      const tag = document.querySelector('style');
      if (tag) tag.textContent = css;
      flash('styles reloaded');
    } catch (e) { location.reload(); }
  });

  // --- dev toolbar ---------------------------------------------------------
  const bar = document.createElement('div');
  bar.id = '__dev-bar';
  bar.innerHTML =
    '<span class="__dev-tag">dev</span>'
    + '<label>state <select id="__dev-state"></select></label>'
    + '<label>login <select id="__dev-login"></select></label>'
    + '<button id="__dev-inspect">inspect: off</button>'
    + '<span id="__dev-flash"></span>';
  document.body.appendChild(bar);

  const stateSel = $('#__dev-state');
  (CFG.states || []).forEach((s) => {
    const o = document.createElement('option');
    o.value = s; o.textContent = s; o.selected = s === CFG.state;
    stateSel.appendChild(o);
  });
  stateSel.addEventListener('change', () => setParam('state', stateSel.value));

  const loginSel = $('#__dev-login');
  ['password-prompt', 'duo-passcode', 'duo-menu', 'push-waiting', 'failed'].forEach((s) => {
    const o = document.createElement('option');
    o.value = s; o.textContent = s; o.selected = s === CFG.login;
    loginSel.appendChild(o);
  });
  loginSel.addEventListener('change', () => setParam('login', loginSel.value));

  function setParam(key, value) {
    const u = new URL(location.href);
    u.searchParams.set(key, value);
    location.href = u.toString();
  }

  let flashTimer = null;
  function flash(msg) {
    const el = $('#__dev-flash');
    if (!el) return;
    el.textContent = msg;
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { el.textContent = ''; }, 2500);
  }

  // --- inspector -----------------------------------------------------------
  let inspecting = false;
  let picked = null;
  const overrides = document.createElement('style');
  overrides.id = '__dev-overrides';
  document.head.appendChild(overrides);

  $('#__dev-inspect').addEventListener('click', () => toggle(!inspecting));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { toggle(false); closePanel(); }
    if (e.key === 'i' && e.altKey) toggle(!inspecting);
  });

  function toggle(on) {
    inspecting = on;
    $('#__dev-inspect').textContent = 'inspect: ' + (on ? 'on' : 'off');
    document.body.classList.toggle('__dev-inspecting', on);
  }

  // Capture phase, so a click lands on the inspector rather than on the portal's own handlers:
  // picking the Log in button should not start a login.
  document.addEventListener('click', (e) => {
    if (!inspecting) return;
    if (e.target.closest('#__dev-bar, #__dev-panel')) return;
    e.preventDefault();
    e.stopPropagation();
    picked = e.target;
    openPanel(picked);
  }, true);

  document.addEventListener('mouseover', (e) => {
    if (!inspecting || e.target.closest('#__dev-bar, #__dev-panel')) return;
    document.querySelectorAll('.__dev-hover').forEach((n) => n.classList.remove('__dev-hover'));
    e.target.classList.add('__dev-hover');
  });

  // Selector derivation, best effort. id wins, then a class chain, then a structural path. For
  // generated table cells with neither, the result is brittle and the panel says so rather than
  // implying a style edit will survive the next render change.
  function selectorFor(el) {
    if (el.id && !el.id.startsWith('__dev')) return '#' + el.id;
    const classes = Array.from(el.classList).filter((c) => !c.startsWith('__dev'));
    if (classes.length) return el.tagName.toLowerCase() + '.' + classes.join('.');
    const parent = el.parentElement;
    if (!parent || parent === document.body) return el.tagName.toLowerCase();
    const tag = el.tagName.toLowerCase();
    const sibs = Array.from(parent.children).filter((c) => c.tagName === el.tagName);
    const nth = sibs.length > 1 ? `:nth-of-type(${sibs.indexOf(el) + 1})` : '';
    return selectorFor(parent) + ' > ' + tag + nth;
  }

  function isBrittle(sel) {
    return sel.includes('nth-of-type') || sel.includes(' > ');
  }

  const FIELDS = [
    ['color', 'colour'],
    ['background-color', 'background'],
    ['padding', 'padding'],
    ['margin', 'margin'],
    ['font-size', 'font size'],
    ['font-weight', 'weight'],
    ['border', 'border'],
    ['border-radius', 'radius'],
    ['width', 'width'],
    ['max-width', 'max width'],
  ];

  function openPanel(el) {
    closePanel();
    const sel = selectorFor(el);
    const computed = getComputedStyle(el);
    const panel = document.createElement('div');
    panel.id = '__dev-panel';

    const rows = FIELDS.map(([prop, label]) =>
      `<label>${label}<input data-prop="${prop}" value="${escapeAttr(computed.getPropertyValue(prop).trim())}"></label>`
    ).join('');

    panel.innerHTML =
      `<h3>${escapeHtml(sel)}</h3>`
      + (isBrittle(sel)
        ? '<p class="__dev-warn">This element has no id or class, so the selector is positional '
          + 'and will break when the markup changes. Prefer a note.</p>'
        : '')
      + `<div class="__dev-fields">${rows}</div>`
      + '<div class="__dev-actions"><button id="__dev-save">save style</button>'
      + '<span class="__dev-hint">writes into the &lt;style&gt; block</span></div>'
      + '<hr>'
      + '<label class="__dev-notelabel">note for the agent'
      + '<textarea id="__dev-note" rows="3" placeholder="text and structure come from JS templates, so describe the change here"></textarea></label>'
      + '<div class="__dev-actions"><button id="__dev-queue">queue note</button>'
      + '<button id="__dev-close">close</button></div>';
    document.body.appendChild(panel);

    // Live preview on every keystroke, through a dev-only override sheet, so nothing is written
    // until save is pressed.
    panel.querySelectorAll('input[data-prop]').forEach((input) => {
      input.addEventListener('input', () => applyOverride(sel, panel));
    });

    $('#__dev-save').addEventListener('click', async () => {
      const decls = collect(panel);
      const res = await post('/__dev/style', { selector: sel, declarations: decls });
      flash(res.ok ? `saved: ${sel}` : `rejected: ${res.message}`);
    });
    $('#__dev-queue').addEventListener('click', async () => {
      const note = $('#__dev-note').value.trim();
      if (!note) { flash('note is empty'); return; }
      await post('/__dev/note', { selector: sel, state: CFG.state, note });
      $('#__dev-note').value = '';
      flash('note queued');
    });
    $('#__dev-close').addEventListener('click', closePanel);
  }

  function collect(panel) {
    return Array.from(panel.querySelectorAll('input[data-prop]'))
      .filter((i) => i.value.trim())
      .map((i) => `${i.dataset.prop}: ${i.value.trim()}`)
      .join('; ');
  }

  function applyOverride(sel, panel) {
    overrides.textContent = `${sel} { ${collect(panel)} }`;
  }

  function closePanel() {
    const p = $('#__dev-panel');
    if (p) p.remove();
    overrides.textContent = '';
  }

  async function post(url, body) {
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      return await r.json();
    } catch (e) {
      return { ok: false, message: String(e) };
    }
  }

  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const escapeAttr = escapeHtml;

  // --- auto-open, so interaction states are reachable as a plain URL -------
  // This is what lets shots.sh capture the login card without driving a browser.
  if (CFG.open.startsWith('login')) autoOpenLogin(CFG.open === 'login-prompt');

  // --- dev-only chrome -----------------------------------------------------
  const css = document.createElement('style');
  css.textContent = `
    #__dev-bar {
      position: fixed; bottom: 0; left: 0; right: 0; z-index: 2147483000;
      display: flex; gap: 1rem; align-items: center; padding: 0.4rem 0.8rem;
      font: 12px/1.4 system-ui, sans-serif; background: #1f2430; color: #e8e8e8;
      border-top: 2px solid #c86; box-shadow: 0 -2px 8px rgba(0,0,0,0.3);
    }
    #__dev-bar .__dev-tag {
      background: #c86; color: #1f2430; font-weight: 700; padding: 0.1rem 0.4rem; border-radius: 3px;
    }
    #__dev-bar label { display: flex; gap: 0.3rem; align-items: center; }
    #__dev-bar select, #__dev-bar button {
      font: inherit; background: #2b3140; color: #e8e8e8; border: 1px solid #444c5e;
      border-radius: 3px; padding: 0.15rem 0.4rem; cursor: pointer;
    }
    #__dev-flash { margin-left: auto; color: #8fd08f; }
    body.__dev-inspecting * { cursor: crosshair !important; }
    .__dev-hover { outline: 2px solid #c86 !important; outline-offset: -2px; }
    #__dev-panel {
      position: fixed; top: 1rem; right: 1rem; z-index: 2147483001; width: 22rem;
      max-height: 80vh; overflow: auto; padding: 0.8rem 1rem; border-radius: 6px;
      font: 12px/1.5 system-ui, sans-serif; background: #1f2430; color: #e8e8e8;
      border: 1px solid #444c5e; box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    }
    #__dev-panel h3 { margin: 0 0 0.5rem; font-size: 12px; font-family: ui-monospace, monospace; color: #c86; word-break: break-all; }
    #__dev-panel .__dev-warn { color: #e6b800; margin: 0 0 0.6rem; }
    #__dev-panel .__dev-fields { display: grid; grid-template-columns: 1fr; gap: 0.3rem; }
    #__dev-panel label { display: grid; grid-template-columns: 5.5rem 1fr; gap: 0.4rem; align-items: center; }
    #__dev-panel input, #__dev-panel textarea {
      font: inherit; background: #12151c; color: #e8e8e8; border: 1px solid #444c5e;
      border-radius: 3px; padding: 0.2rem 0.3rem; width: 100%;
    }
    #__dev-panel .__dev-notelabel { display: block; }
    #__dev-panel .__dev-actions { display: flex; gap: 0.5rem; align-items: center; margin-top: 0.6rem; }
    #__dev-panel button {
      font: inherit; background: #2b3140; color: #e8e8e8; border: 1px solid #444c5e;
      border-radius: 3px; padding: 0.25rem 0.6rem; cursor: pointer;
    }
    #__dev-panel .__dev-hint { color: #8a93a6; }
    #__dev-panel hr { border: none; border-top: 1px solid #444c5e; margin: 0.8rem 0; }
  `;
  document.head.appendChild(css);
})();
