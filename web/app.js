/* Lacre web app: one script for every page. No framework, no build step.
 *
 * What must survive a reload (the key, the job followed, the wallet and its
 * transaction in flight) is kept by state.js, which decides where each value
 * lives; this file never touches storage itself. Every call to the API goes
 * to this origin. The wallet path (wallet.js) talks to the Bradbury RPC and
 * to the wallet, never to the API: it creates no job.
 */
(function () {
  'use strict';

  var NONE = '-';
  var DOT = '\u00b7';
  var APPROX = '\u2248';
  var BULLET = '\u2022';
  var INFINITY = '\u221e';
  var POLL_MS = 20000;
  var PRIMITIVES_MS = 600000;
  var JOBS_PAGE = 8;
  var PULSE_BACK_MS = 6000;
  var FLASH_MS = 1400;
  var MOBILE = window.matchMedia('(max-width: 1023.98px)');
  var JOB_ID = /^[0-9a-f]{32}$/;
  var ADDRESS = /^0x[0-9a-fA-F]{40}$/;
  var EXPLORER = 'https://explorer-bradbury.genlayer.com/';
  var KEY_SETTLE_MS = 400;

  function $(id) { return document.getElementById(id); }
  function all(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }

  // ---- copy ----------------------------------------------------------------

  // Controls whose text is only known at run time (the key among it) name a
  // function here instead of carrying the text in the DOM.
  var copyRefs = {};

  function writeClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    // A page served without TLS, as on a LAN test box, has no clipboard API.
    return new Promise(function (resolve, reject) {
      var area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.className = 'sr';
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (error) { ok = false; }
      area.remove();
      if (ok) { resolve(); } else { reject(new Error('copy failed')); }
    });
  }

  function flash(el) {
    el.classList.add('flash');
    clearTimeout(el.flashTimer);
    el.flashTimer = setTimeout(function () { el.classList.remove('flash'); }, FLASH_MS);
  }

  document.addEventListener('click', function (event) {
    var el = event.target.closest('[data-copy], [data-copy-ref], [data-copy-from]');
    if (!el || el.disabled) { return; }
    var text = '';
    if (el.hasAttribute('data-copy')) {
      text = el.getAttribute('data-copy');
    } else if (el.hasAttribute('data-copy-from')) {
      var from = $(el.getAttribute('data-copy-from'));
      text = from ? from.textContent : '';
    } else {
      var source = copyRefs[el.getAttribute('data-copy-ref')];
      text = source ? source() : '';
    }
    if (!text) { return; }
    event.preventDefault();
    writeClipboard(text).then(function () { flash(el); }, function () {});
  });

  // ---- nav -------------------------------------------------------------------

  function initNav() {
    var nav = document.querySelector('.nav');
    var button = nav && nav.querySelector('.nav-menu');
    if (!button) { return; }
    button.addEventListener('click', function () {
      var open = nav.classList.toggle('menu-open');
      button.setAttribute('aria-expanded', String(open));
    });
  }

  // ---- the API ------------------------------------------------------------------

  function call(method, path, options) {
    options = options || {};
    var headers = {};
    var body;
    if (options.key) { headers['X-API-Key'] = options.key; }
    if (options.form) { body = options.form; }
    if (options.json !== undefined) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(options.json);
    }
    return fetch(path, {
      method: method, headers: headers, body: body,
      cache: 'no-store', credentials: 'omit', redirect: 'error'
    }).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (data) {
        return { status: response.status, data: data };
      });
    }, function () {
      return { status: 0, data: null };
    });
  }

  function detailText(data) {
    if (!data || data.detail === undefined) { return ''; }
    if (typeof data.detail === 'string') { return data.detail; }
    // FastAPI answers a validation error with a list of {loc, msg}.
    if (Array.isArray(data.detail) && data.detail.length) {
      var first = data.detail[0];
      var where = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : '';
      return (where ? where + ': ' : '') + (first.msg || 'invalid');
    }
    return '';
  }

  function failure(res) {
    if (res.status === 0) { return 'The gateway did not answer'; }
    if (res.status === 401) { return 'Unknown API key'; }
    if (res.status === 403) { return 'This account is disabled'; }
    if (res.status === 402 && res.data) {
      return 'Not enough credits: ' + res.data.credits + ' left, ' + res.data.needed + ' needed';
    }
    if (res.status === 429) { return 'Too many requests, try again later'; }
    return detailText(res.data) || 'HTTP ' + res.status;
  }

  // ---- the disk --------------------------------------------------------------------

  var HUB = { idle: '#f2f6ff', running: '#2f7bf5', provisional: '#e9a23b', final: '#22b06b', refused: '#e0484e' };
  var HUB_GLOW = {
    idle: 'rgba(140,175,235,.9)', running: 'rgba(47,123,245,.7)', provisional: 'rgba(233,162,59,.7)',
    final: 'rgba(34,176,107,.65)', refused: 'rgba(224,72,78,.7)'
  };
  var RING = { done: 'rgba(47,123,245,.5)', active: '#2f7bf5', final: 'rgba(34,176,107,.7)', refused: '#e0484e' };
  var RING_GLOW = {
    done: 'rgba(47,123,245,.25)', active: 'rgba(47,123,245,.55)', final: 'rgba(34,176,107,.3)',
    refused: 'rgba(224,72,78,.5)'
  };
  var SWEEP_MASK = 'radial-gradient(farthest-side, transparent calc(100% - 6px), #000 calc(100% - 5px), ' +
    '#000 calc(100% - 1px), transparent 100%)';
  var PULSE = 'lacrePulse 2.4s ease-in-out infinite';

  function div(style, children) {
    var el = document.createElement('div');
    Object.keys(style).forEach(function (name) { el.style[name] = style[name]; });
    (children || []).forEach(function (child) { el.appendChild(child); });
    return el;
  }

  function merge(a, b) {
    var out = {};
    Object.keys(a).forEach(function (k) { out[k] = a[k]; });
    Object.keys(b).forEach(function (k) { out[k] = b[k]; });
    return out;
  }

  // A stack of discs seen at an angle: the body is sixteen layers under the
  // face, the hub eight above it. The structure is built once per size and
  // ring count; a state change restyles it in place, so the rotation and
  // the pulse do not restart on every poll.
  function Disk(host) {
    this.host = host;
    this.size = 0;
    this.count = 0;
    this.status = '';
    this.stage = -2;
    this.sweep = null;
    this.sweepStage = -1;
  }

  Disk.prototype.set = function (size, status, stage, count) {
    if (size !== this.size || count !== this.count) {
      this.build(size, count);
      this.status = '';
    }
    if (status !== this.status || stage !== this.stage) {
      this.update(status, stage);
      this.status = status;
      this.stage = stage;
    }
  };

  Disk.prototype.build = function (s, n) {
    this.size = s;
    this.count = n;
    var H = Math.round(s * 0.74);
    var round = { position: 'absolute', inset: '0', borderRadius: '50%' };
    var plane = [];
    for (var i = 16; i >= 1; i--) {
      var k = i / 16;
      var v = i === 6 ? [150, 155, 162]
        : [Math.round(222 - k * 46), Math.round(225 - k * 46), Math.round(230 - k * 46)];
      plane.push(div(merge(round, { transform: 'translateZ(' + (-i * 1.5) + 'px)', background: 'rgb(' + v.join(',') + ')' })));
    }
    var spokes = [0, 60, 120].map(function (a) {
      return div({
        position: 'absolute', left: '50%', top: '2.5%', bottom: '2.5%', width: '1px', marginLeft: '-0.5px',
        background: 'linear-gradient(180deg, rgba(36,39,44,0), rgba(36,39,44,.10) 12%, rgba(36,39,44,.10) 88%, rgba(36,39,44,0))',
        transform: 'rotate(' + a + 'deg)'
      });
    });
    spokes.push(div({ position: 'absolute', left: '50%', top: '1.4%', width: '2.4%', height: '3.6%', marginLeft: '-1.2%', borderRadius: '2px', background: '#33373d' }));
    spokes.push(div({ position: 'absolute', left: '50%', bottom: '1.8%', width: '9%', height: '1.6%', marginLeft: '-4.5%', borderRadius: '3px', background: 'rgba(36,39,44,.08)', boxShadow: '0 1px 0 rgba(255,255,255,.9)' }));
    this.rot = div(merge(round, {}), spokes);
    var face = [
      div(merge(round, { background: 'radial-gradient(circle at 38% 28%, #ffffff 0%, #f6f7f8 42%, #e3e6e9 100%)', boxShadow: 'inset 0 0 0 1px rgba(36,39,44,.10)' })),
      this.rot
    ];
    this.rings = [];
    for (var j = 0; j < n; j++) {
      var r = 0.92 - j * (0.92 - 0.38) / Math.max(1, n - 1);
      var ring = div({
        position: 'absolute', left: ((1 - r) * 50) + '%', top: ((1 - r) * 50) + '%',
        width: (r * 100) + '%', height: (r * 100) + '%', borderRadius: '50%', boxSizing: 'border-box'
      });
      this.rings.push({ el: ring, r: r });
      face.push(ring);
    }
    this.gloss = div(merge(round, { background: 'radial-gradient(ellipse 60% 42% at 36% 22%, rgba(255,255,255,.85), rgba(255,255,255,0) 70%)', pointerEvents: 'none' }));
    face.push(this.gloss);
    this.face = div(merge(round, {}), face);
    plane.push(this.face);
    var hub = { position: 'absolute', left: '35%', top: '35%', width: '30%', height: '30%', borderRadius: '50%' };
    for (var h = 1; h <= 8; h++) {
      plane.push(div(merge(hub, { transform: 'translateZ(' + (h * 1.4) + 'px)', background: 'rgb(' + (196 + h * 3) + ',' + (199 + h * 3) + ',' + (204 + h * 3) + ')' })));
    }
    var dot = Math.round(s * 0.07);
    this.dot = div({ width: dot + 'px', height: dot + 'px', borderRadius: '50%' });
    plane.push(div(merge(hub, {
      transform: 'translateZ(13px)', background: 'radial-gradient(circle at 40% 30%, #ffffff, #f0f2f4 55%, #dde0e4)',
      boxShadow: 'inset 0 1px 0 #fff, inset 0 0 0 1px rgba(36,39,44,.08)', display: 'flex', alignItems: 'center', justifyContent: 'center'
    }), [
      div({ position: 'absolute', inset: '19%', borderRadius: '50%', border: '1px solid rgba(36,39,44,.16)', boxShadow: '0 1px 0 rgba(255,255,255,.9)' }),
      this.dot
    ]));
    var shadow = div({
      position: 'absolute', left: '1%', width: '98%', top: (H / 2 - s * 0.29 + s * 0.07) + 'px', height: (s * 0.6) + 'px',
      borderRadius: '50%', background: 'radial-gradient(closest-side, rgba(28,32,38,.28), rgba(28,32,38,0))',
      filter: 'blur(' + Math.round(s * 0.02) + 'px)'
    });
    var planeEl = div({
      position: 'absolute', left: '0', top: ((H - s) / 2) + 'px', width: s + 'px', height: s + 'px',
      transformStyle: 'preserve-3d', transform: 'rotateX(56deg)'
    }, plane);
    var root = div({ position: 'relative', width: s + 'px', height: H + 'px', perspective: (s * 3) + 'px', perspectiveOrigin: '50% 20%' }, [shadow, planeEl]);
    this.host.replaceChildren(root);
    this.sweep = null;
    this.sweepStage = -1;
  };

  Disk.prototype.update = function (status, stage) {
    var s = this.size;
    var spin = status === 'running';
    this.rot.style.animation = spin ? 'lacreSpin 16s linear infinite' : 'none';
    for (var j = 0; j < this.rings.length; j++) {
      var mode = 'plain';
      if (status === 'final') { mode = 'final'; }
      else if (status === 'running') { mode = j < stage ? 'done' : j === stage ? 'active' : 'plain'; }
      else if (status === 'refused') { mode = j < stage ? 'done' : j === stage ? 'refused' : 'plain'; }
      var st = this.rings[j].el.style;
      if (mode === 'plain') {
        st.border = '1px solid rgba(36,39,44,.14)';
        st.boxShadow = '0 1px 0 rgba(255,255,255,.95), inset 0 1px 0 rgba(255,255,255,.95)';
        st.animation = 'none';
      } else {
        st.border = '1.5px solid ' + RING[mode];
        st.boxShadow = '0 0 8px ' + RING_GLOW[mode] + ', inset 0 0 6px ' + RING_GLOW[mode];
        st.animation = mode === 'active' ? PULSE : 'none';
      }
    }
    var sweeping = spin && stage >= 0 && stage < this.rings.length;
    if (this.sweep && (!sweeping || this.sweepStage !== stage)) {
      this.sweep.remove();
      this.sweep = null;
    }
    if (sweeping && !this.sweep) {
      // A new sweep per stage, so each stage starts its arc from the notch.
      var r = this.rings[stage].r;
      var sweep = div({
        position: 'absolute', borderRadius: '50%', boxSizing: 'border-box',
        left: 'calc(' + ((1 - r) * 50) + '% - 3px)', top: 'calc(' + ((1 - r) * 50) + '% - 3px)',
        width: 'calc(' + (r * 100) + '% + 6px)', height: 'calc(' + (r * 100) + '% + 6px)',
        background: 'conic-gradient(from 0deg, rgba(47,123,245,0) 0turn, rgba(47,123,245,0) .6turn, rgba(47,123,245,.95) 1turn)',
        animation: 'lacreSpin 2.8s linear infinite'
      });
      sweep.style.setProperty('-webkit-mask', SWEEP_MASK);
      sweep.style.setProperty('mask', SWEEP_MASK);
      this.face.insertBefore(sweep, this.gloss);
      this.sweep = sweep;
      this.sweepStage = stage;
    }
    var hub = HUB[status] ? status : 'idle';
    this.dot.style.background = HUB[hub];
    this.dot.style.boxShadow = '0 0 0 1.5px rgba(36,39,44,.18), 0 0 ' + Math.round(s * 0.05) + 'px ' +
      Math.round(s * 0.014) + 'px ' + HUB_GLOW[hub];
    this.dot.style.animation = spin ? PULSE : 'none';
  };

  // ---- the validators ----------------------------------------------------------------------

  // Five pieces drop in on Attest and orbit the disk at their own heights
  // while it verifies, snap to its edge with a green light once the
  // validators agree, turn slowly while it finalizes and lift away when it
  // is recorded. Three more drift over the page until the job ends. One
  // frame loop, transforms and opacity only, none of it under reduced motion.
  var CALM = window.matchMedia('(prefers-reduced-motion: reduce)');
  var TURN = Math.PI * 2 / 5;

  function Validators(field, swarm) {
    this.field = field;
    this.swarm = swarm;
    this.mode = 'off';
    this.pieces = [];
    this.frame = 0;
    this.c = { x: 0, y: 0, r: 0 };
  }

  Validators.prototype.set = function (mode, c) {
    this.c = c;
    if (CALM.matches || (this.mode === 'off' && mode === 'retract')) { return; }
    if (this.mode === 'off') {
      for (var i = 0; i < 8; i++) {
        var el = document.createElement('span');
        el.className = 'vpiece';
        (i < 5 ? this.field : this.swarm).appendChild(el);
        this.pieces.push({ el: el, i: i % 5, drift: i >= 5, x: i < 5 ? c.x : window.innerWidth * (i - 4) / 4, y: -window.innerHeight, o: 1 });
      }
    }
    this.mode = mode;
    if (!this.frame) { this.frame = requestAnimationFrame(this.tick.bind(this)); }
  };

  Validators.prototype.tick = function (now) {
    var t = now / 1000, c = this.c, mode = this.mode, live = false;
    this.pieces.forEach(function (p) {
      var a = t * (mode === 'snap' ? 0.25 : 0.55 + p.i * 0.12) + p.i * TURN, x = p.x, y = -120, o = 0;
      if (p.drift && mode !== 'retract') {
        x = window.innerWidth * (0.5 + 0.44 * Math.sin(t * (0.09 + p.i * 0.04) + p.i * 2));
        y = window.innerHeight * (0.5 + 0.42 * Math.sin(t * (0.13 + p.i * 0.03) + p.i)) + 5 * Math.sin(t * 11 + p.i);
        o = 0.55;
      } else if (mode === 'snap') {
        x = c.x + c.r * Math.cos(a);
        y = c.y + c.r * 0.56 * Math.sin(a);
        o = 1;
      } else if (mode === 'orbit') {
        var r = c.r * (1.1 + p.i * 0.06);
        x = c.x + r * Math.cos(a);
        y = c.y + r * 0.56 * Math.sin(a) - c.r * (0.12 + p.i * 0.1);
        o = Math.sin(a) < 0 ? 0.5 : 1;
      }
      p.x += (x - p.x) * 0.06;
      p.y += (y - p.y) * 0.06;
      p.o += (o - p.o) * 0.08;
      p.el.style.transform = 'translate(' + p.x.toFixed(1) + 'px,' + p.y.toFixed(1) + 'px)';
      p.el.style.opacity = p.o.toFixed(2);
      p.el.classList.toggle('ok', mode === 'snap' && !p.drift);
      live = live || p.o > 0.02;
    });
    if (mode !== 'retract' || live) {
      this.frame = requestAnimationFrame(this.tick.bind(this));
      return;
    }
    this.pieces.forEach(function (p) { p.el.remove(); });
    this.pieces = [];
    this.mode = 'off';
    this.frame = 0;
  };

  // ---- the cables -----------------------------------------------------------------------------

  // A cable from the wallet chip, or the plugin, into the disk's entry port.
  // It is drawn in and retracted by stroke-dashoffset alone; the pulse on it
  // is a dash pattern that CSS moves along the same path, toward the disk
  // ('in') or back toward the card ('out'). The path is set, not animated.
  function Cable(line, pulse) {
    this.line = line;
    this.pulse = pulse;
    this.on = false;
    this.len = 0;
    this.timer = 0;
  }

  function cablePath(from, to) {
    var dx = Math.max(40, Math.abs(from.x - to.x) * 0.45);
    var sx = from.x > to.x ? -1 : 1;
    return 'M' + from.x.toFixed(1) + ' ' + from.y.toFixed(1) +
      'C' + (from.x + sx * dx).toFixed(1) + ' ' + from.y.toFixed(1) + ' ' +
      (to.x - sx * dx).toFixed(1) + ' ' + to.y.toFixed(1) + ' ' + to.x.toFixed(1) + ' ' + to.y.toFixed(1);
  }

  Cable.prototype.set = function (ends, flow) {
    var line = this.line;
    var pulse = this.pulse;
    var self = this;
    if (!ends) {
      show(pulse, false);
      if (!this.on) { return; }
      this.on = false;
      line.style.strokeDashoffset = String(this.len);
      clearTimeout(this.timer);
      this.timer = setTimeout(function () { if (!self.on) { show(line, false); } }, CALM.matches ? 0 : 900);
      return;
    }
    show(line, true);
    var d = cablePath(ends.from, ends.to);
    if (line.getAttribute('d') !== d) {
      line.setAttribute('d', d);
      pulse.setAttribute('d', d);
      this.len = Math.ceil(line.getTotalLength()) + 2;
      line.style.strokeDasharray = this.len + ' ' + this.len;
    }
    if (!this.on) {
      this.on = true;
      clearTimeout(this.timer);
      // Drawn in from the far end: fully offset first, then settled to 0.
      line.style.transition = 'none';
      line.style.strokeDashoffset = String(this.len);
      line.getBoundingClientRect();
      line.style.transition = '';
      line.style.strokeDashoffset = '0';
    }
    show(pulse, !!flow);
    pulse.classList.toggle('back', flow === 'out');
  };

  // ---- the main screen ------------------------------------------------------------------

  // The stages the disk shows and the honest time each takes: an on-chain
  // step waits for FINALIZED, about 35 minutes on Bradbury.
  var ALL = [
    { n: 'Queued', m: 0.5, eta: '< 1 min' },
    { n: 'Attesting', m: 2, eta: APPROX + ' 2 min' },
    { n: 'Finalizing', m: 35, eta: APPROX + ' 35 min' },
    { n: 'Serving body', m: 1, eta: APPROX + ' 1 min' },
    { n: 'Extracting', m: 3, eta: APPROX + ' 3 min' },
    { n: 'Finalizing', m: 35, eta: APPROX + ' 35 min' },
    { n: 'Recorded', m: 0, eta: NONE }
  ];
  // attest_inline from a wallet: signed, sent, decided, then FINALIZED.
  var WALLET_STAGES = [{ n: 'Signing', m: 0.5, eta: '< 1 min' }, ALL[1], ALL[2], ALL[6]];
  var JOB_STATUS_TEXT = { pending: 'queued', finalized: 'recorded', refused: 'refused', failed: 'failed' };
  // The worker's stage names while a job is extracting, to the disk's.
  var EXTRACT_STAGE = {
    'recorded': 3, 'waiting for the Extractor': 3, 'serving body': 3,
    'extracting': 4, 'waiting for extraction FINALIZED': 5
  };
  var TERMINAL = ['finalized', 'refused', 'failed'];
  var EXTRACT_ENDED = ['skipped', 'refused', 'failed', 'no match', 'not run'];
  var LAYERS = ['router', 'keycache', 'verifier', 'extractor_patterns', 'extractor_llm'];
  var FIELDS = [
    ['Match', 'match'], ['Shipped', 'shipped'], ['Delivery day', 'eta_day'], ['Delivery date', 'eta_date'],
    ['Order found', 'order_id_found'], ['Flagged', 'flagged'], ['Method', 'method']
  ];

  function stagesFor(mode) {
    return mode === 'none' ? [ALL[0], ALL[1], ALL[2], ALL[6]] : ALL;
  }

  function pad(x) { return String(x).padStart(2, '0'); }

  function groups(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }

  function mask(key) {
    if (key.length <= 12) { return BULLET.repeat(8); }
    return key.slice(0, 4) + BULLET.repeat(6) + key.slice(-4);
  }

  function shortTx(tx) { return tx.slice(0, 10) + '\u2026' + tx.slice(-6); }

  function utc(iso) { return String(iso).replace('T', ' ').replace(/:\d\dZ$/, ' UTC'); }

  function kb(bytes) { return (bytes / 1024).toFixed(1) + ' KB'; }

  // An SVG element has no hidden property, only the attribute.
  function show(el, on) {
    if (el instanceof SVGElement) { el.toggleAttribute('hidden', !on); } else { el.hidden = !on; }
  }

  function setLight(el, state) {
    el.className = (el.getAttribute('data-base') || 'lt') + ' ' + state;
  }

  function initApp() {
    var store = window.LacreState;
    var wallet = window.LacreWallet;
    var state = {
      key: '', account: null, primitives: null, keyMsg: '',
      mailboxes: [], newMailbox: '', mailMsg: '', mailBusy: false,
      jobs: [], jobsNext: null, jobFilter: '',
      tab: 'file', file: null, paste: '', mode: 'auto',
      source: '', jobId: '', job: null, record: null, jobMode: 'auto', busy: false, timer: 0,
      plugOpen: false, folded: false, leftOpen: window.innerWidth >= 1280, mInfo: false,
      msg: '', msgErr: false, confirm: false, resumeOpen: false,
      // The wallet: its address, GEN on Bradbury, and whether it is there.
      walletAddr: store.get('wallet'), walletBal: '', walletChain: true, walletMsg: '', walletBusy: false,
      keyOpen: false,
      // The wallet's attest_inline in flight: what state.js keeps, the stored
      // consensus state last read, and the outcome once FINALIZED.
      wtx: store.get('pendingTx'), wstate: null, wresult: null, wfail: '', wtimer: 0, wticking: false,
      pulseBackUntil: 0
    };
    var disk = new Disk($('disk'));
    var validators = new Validators($('vfield'), $('vswarm'));
    var cables = {
      wallet: new Cable($('cableWallet'), $('cableWalletPulse')),
      plug: new Cable($('cablePlug'), $('cablePlugPulse'))
    };
    var origin = window.location.origin;
    var mailSig = '';
    var exSig = '';
    var txSig = '';
    var jobSig = '';
    var jobsSig = '';
    var inputHeight = 52;

    function effectiveTab() { return MOBILE.matches ? 'file' : state.tab; }

    // A connected wallet pays when no key is set and the key field is not
    // asked for; with a key, the gateway path is taken as before.
    function walletMode() { return !!state.walletAddr && !state.key && !state.keyOpen; }

    function walletEnded() { return !!state.wresult || !!state.wfail; }

    function modeOfJob(job) {
      var x = job.extraction;
      if (!x || x.status === 'skipped') { return 'none'; }
      return x.requested || 'auto';
    }

    // The wallet's transaction on the same four stages as a job without an
    // extraction: signing until the consensus tx id is known, attesting
    // until the stored state is decided, finalizing until FINALIZED.
    function walletProgress() {
      var stages = WALLET_STAGES;
      if (state.wresult) {
        return state.wresult.kind === 'recorded' ? { phase: 'final', stage: 3, stages: stages }
          : { phase: 'refused', stage: 2, stages: stages };
      }
      if (state.wfail) { return { phase: 'refused', stage: state.wtx.tx ? 1 : 0, stages: stages }; }
      if (!state.wtx.tx) { return { phase: 'running', stage: 0, stages: stages }; }
      var decided = state.wstate && wallet.DECIDED.indexOf(state.wstate.status) >= 0;
      return { phase: 'running', stage: decided ? 2 : 1, stages: stages };
    }

    function progress() {
      if (state.wtx) { return walletProgress(); }
      if (!state.jobId) { return { phase: 'idle', stage: -1, stages: walletMode() ? WALLET_STAGES : stagesFor(state.mode) }; }
      var job = state.job;
      if (!job) { return { phase: 'running', stage: 0, stages: stagesFor(state.jobMode) }; }
      var stages = stagesFor(modeOfJob(job));
      var last = stages.length - 1;
      switch (job.status) {
        case 'pending':
          return { phase: 'running', stage: 0, stages: stages };
        case 'attesting':
          return { phase: 'running', stage: job.stage === 'waiting for FINALIZED' ? 2 : 1, stages: stages };
        case 'extracting':
          var at = EXTRACT_STAGE[job.stage];
          return { phase: 'running', stage: Math.min(last, at === undefined ? 3 : at), stages: stages };
        case 'finalized':
          return { phase: job.tx_status === 'FINALIZED' ? 'final' : 'provisional', stage: last, stages: stages };
        default:
          // refused or failed: the ring where it stopped, from what was sent.
          return { phase: 'refused', stage: job.consensus_tx ? 2 : job.status === 'refused' ? 1 : 0, stages: stages };
      }
    }

    function networkName() {
      var name = state.primitives && state.primitives.network ? String(state.primitives.network) : 'bradbury';
      return name.charAt(0).toUpperCase() + name.slice(1);
    }

    function readout(p) {
      var n = p.stages.length;
      var job = state.job;
      if (p.phase === 'idle') {
        return ['00 / ' + pad(n), 'Idle', walletMode() ? 'Drop a signed .eml ' + DOT + ' your wallet pays the fee' : 'Drop a signed .eml'];
      }
      if (p.phase === 'final') {
        return [pad(n) + ' / ' + pad(n), 'Recorded', 'Finalized on ' + networkName() + (state.wtx ? ' ' + DOT + ' requester is your address' : '')];
      }
      if (p.phase === 'provisional') {
        return [pad(n) + ' / ' + pad(n), 'Recorded', 'Recorded ' + DOT + ' waiting for FINALIZED'];
      }
      if (p.phase === 'refused') {
        if (state.wtx) {
          var said = state.wfail || (state.wresult.kind === 'refused' ? 'the Verifier refused: ' + state.wresult.reason : state.wresult.why);
          return [pad(p.stage + 1) + ' / ' + pad(n), state.wfail ? 'Failed' : 'Refused', said + ' ' + DOT + ' nothing recorded'];
        }
        var why = job.refusal_reason || job.error || 'no reason given';
        return [pad(p.stage + 1) + ' / ' + pad(n), job.status === 'failed' ? 'Failed' : 'Refused',
          why + ' ' + DOT + ' nothing recorded'];
      }
      var cur = p.stages[p.stage];
      var eta;
      if (state.wtx && p.stage === 0) {
        eta = state.wtx.evm ? 'Sent from your wallet ' + DOT + ' waiting for its receipt' : 'Confirm attest_inline in your wallet';
      } else if (state.wtx && state.wstate && state.wstate.status.indexOf('APPEAL') === 0) {
        eta = 'An appeal is in progress ' + DOT + ' ' + state.wstate.status;
      } else if (job && job.stage === 'sender in verification') {
        // A sender key seen for the first time waits out the KeyCache quarantine.
        eta = 'Sender in verification ' + DOT + ' up to 24 h the first time a domain is seen';
        if (job.sender_confirm_after) { eta += ' ' + DOT + ' confirm after ' + utc(job.sender_confirm_after); }
      } else if (job && job.stage === 'waiting for the Verifier') {
        eta = 'Waiting for the Verifier ' + DOT + ' one call at a time';
      } else {
        var rem = p.stages.slice(p.stage).reduce(function (a, b) { return a + b.m; }, 0);
        eta = cur.eta + ' this stage ' + DOT + ' ' + APPROX + ' ' + Math.max(1, Math.round(rem)) + ' min to record';
      }
      return [pad(p.stage + 1) + ' / ' + pad(n), cur.n, eta];
    }

    function busyLayers(p) {
      if (p.phase !== 'running') { return []; }
      var mode = state.job ? modeOfJob(state.job) : state.jobMode;
      var lane = state.job && state.job.extraction ? state.job.extraction.lane : null;
      var ex = lane === 'patterns' || mode === 'patterns' ? [3] : lane === 'llm' || mode === 'llm' ? [4] : [3, 4];
      var name = p.stages[p.stage].n;
      var map = { 'Queued': [0], 'Signing': [0], 'Attesting': [1, 2], 'Finalizing': p.stage >= 5 ? ex : [2], 'Serving body': [0], 'Extracting': ex };
      return map[name] || [];
    }

    function layerState(index, busy) {
      if (busy.indexOf(index) >= 0) { return ['running', 'BUSY']; }
      var layers = state.primitives && state.primitives.layers;
      if (!layers || layers[LAYERS[index]] === undefined) { return ['off', NONE]; }
      if (layers[LAYERS[index]]) { return ['final', 'LIVE']; }
      // An Extractor the Router does not name is off, not broken.
      return index >= 3 ? ['off', 'OFF'] : ['refused', 'DOWN'];
    }

    function canAttest() {
      if (state.busy || state.jobId || state.wtx) { return false; }
      if (!state.key && !walletMode()) { return false; }
      if (effectiveTab() === 'paste') { return state.paste.trim().length > 20; }
      return !!state.file;
    }

    // A job is in flight from the moment it is followed until it ends, and
    // so is the wallet's transaction.
    function inFlight() {
      if (state.wtx) { return !walletEnded(); }
      return !!state.jobId && !isTerminal();
    }

    function setMsg(text, err) {
      state.msg = text || '';
      state.msgErr = !!err;
    }

    // ---- render -------------------------------------------------------------------------

    function renderReadouts(p) {
      var busy = busyLayers(p);
      LAYERS.forEach(function (name, index) {
        var ls = layerState(index, busy);
        all('[data-layer="' + name + '"]').forEach(function (row) {
          var light = row.querySelector('.lt');
          if (light) { setLight(light, ls[0]); }
          row.querySelector('.layer-st').textContent = ls[1];
        });
        all('[data-layer-lt="' + name + '"]').forEach(function (light) { setLight(light, ls[0]); });
      });
      var net = !state.primitives ? 'off' : state.primitives.layers.router ? 'final' : 'refused';
      all('[data-net]').forEach(function (light) { setLight(light, net); });
      all('[data-net-name]').forEach(function (el) { el.textContent = networkName(); });
      all('[data-net-upper]').forEach(function (el) { el.textContent = networkName().toUpperCase(); });

      var account = state.account;
      var prices = account ? account.prices : null;
      all('[data-price]').forEach(function (el) {
        el.textContent = prices ? String(prices[el.getAttribute('data-price')]) : NONE;
      });
      all('[data-count]').forEach(function (el) {
        var v = account && account.counts ? account.counts[el.getAttribute('data-count')] : undefined;
        el.textContent = v === undefined ? NONE : groups(v);
      });
      var balance = account ? (account.unlimited ? INFINITY : groups(account.credits)) : '';
      show($('balance'), !!account);
      show($('balanceNone'), !account);
      $('balanceN').textContent = balance;
      $('mBal').textContent = (account ? balance : NONE) + ' cr';
      var mp = $('mPrices').children;
      mp[0].textContent = 'attest ' + (prices ? prices.attest : NONE) + ' cr';
      mp[1].textContent = 'extract ' + (prices ? prices.extract : NONE) + ' cr';

      renderJobs();
      renderPrimitives();

      $('side').classList.toggle('collapsed', !state.leftOpen);
      $('sideToggle').setAttribute('aria-expanded', String(state.leftOpen));
      $('sideToggle').title = state.leftOpen ? 'Collapse' : 'Expand';
      $('mstrip').setAttribute('aria-expanded', String(state.mInfo));
      $('minfo').classList.toggle('open', state.mInfo);
    }

    function shortAddress(a) { return a.slice(0, 8) + '\u2026' + a.slice(-6); }
    function navAddress(a) { return a.slice(0, 6) + '...' + a.slice(-4); }

    function age(iso) {
      var s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
      if (s < 60) { return Math.floor(s) + 's'; }
      if (s < 3600) { return Math.floor(s / 60) + 'm'; }
      if (s < 86400) { return Math.floor(s / 3600) + 'h'; }
      return Math.floor(s / 86400) + 'd';
    }

    // The account's jobs, newest first; a click follows one, as Resume does.
    function renderJobs() {
      all('[data-filter]').forEach(function (row) {
        row.setAttribute('aria-pressed', String(row.getAttribute('data-filter') === state.jobFilter));
        row.disabled = !state.account;
      });
      var list = $('jobs');
      var rows = state.account ? state.jobs : [];
      // Rebuilt only when something shown moved, so a hover or a focus
      // on the list survives the renders in between.
      var sig = JSON.stringify([state.jobId, rows.map(function (j) { return [j.id, j.status, j.stage, age(j.created_at)]; })]);
      show($('jobsMore'), !!state.account && !!state.jobsNext);
      if (sig === jobsSig) { return; }
      jobsSig = sig;
      list.replaceChildren.apply(list, rows.map(function (j) {
        var li = document.createElement('button');
        li.type = 'button';
        li.className = 'job-li' + (j.id === state.jobId ? ' cur' : '');
        li.setAttribute('data-job', j.id);
        li.title = j.id + ' ' + DOT + ' ' + j.status;
        var id = document.createElement('span');
        id.className = 'job-id';
        id.textContent = j.id.slice(0, 8);
        var st = document.createElement('span');
        st.className = 'job-st';
        st.textContent = j.status === 'finalized' || j.status === 'refused' || j.status === 'failed'
          ? JOB_STATUS_TEXT[j.status] : j.stage || JOB_STATUS_TEXT[j.status] || j.status;
        var when = document.createElement('span');
        when.className = 'job-age';
        when.textContent = age(j.created_at);
        li.appendChild(id);
        li.appendChild(st);
        li.appendChild(when);
        return li;
      }));
    }

    // The addresses come from /primitives, which reads them off the Router at
    // FINALIZED at most ten minutes ago, for every visitor with or without a
    // key, so the section never shows one the Router moved away from for long.
    function renderPrimitives() {
      var addresses = state.primitives && state.primitives.addresses;
      all('[data-prim]').forEach(function (card) {
        var a = addresses && ADDRESS.test(addresses[card.getAttribute('data-prim')] || '')
          ? addresses[card.getAttribute('data-prim')] : '';
        setLight(card.querySelector('.lt'), a ? 'final' : 'off');
        var v = card.querySelector('.prim-v');
        v.textContent = a ? shortAddress(a) : addresses ? 'not named' : NONE;
        v.title = a;
        var copy = card.querySelector('button');
        copy.disabled = !a;
        if (a) { copy.setAttribute('data-copy', a); } else { copy.removeAttribute('data-copy'); }
        var link = card.querySelector('a');
        show(link, !!a);
        if (a) { link.href = EXPLORER + 'address/' + a; }
      });
      var readable = !!(state.primitives && state.primitives.layers.router);
      $('primNote').firstChild.textContent = state.primitives && !readable
        ? 'The Router could not be read just now; it is asked again in a moment. '
        : 'Read from the Router on ';
      $('primNote').lastChild.textContent = readable || !state.primitives ? ' at FINALIZED, at most ten minutes ago.' : '';
      all('[data-net-name]', $('primNote')).forEach(function (el) { show(el, readable || !state.primitives); });
    }

    function validatorMode(p) {
      if (p.phase !== 'running') { return 'retract'; }
      return p.stages[p.stage].n === 'Finalizing' ? 'snap' : 'orbit';
    }

    function renderStage(p) {
      var text = readout(p);
      $('stageNum').textContent = text[0];
      $('stageName').textContent = text[1];
      $('stageEta').textContent = text[2];
      show($('hero'), p.phase === 'idle');
      var ticks = $('ticks');
      while (ticks.children.length < p.stages.length) { ticks.appendChild(document.createElement('span')); }
      while (ticks.children.length > p.stages.length) { ticks.lastChild.remove(); }
      Array.prototype.forEach.call(ticks.children, function (tick, i) {
        var cls = '';
        if (p.phase === 'final' || p.phase === 'provisional') { cls = p.phase === 'final' ? 'ok' : 'done'; }
        else if (p.phase === 'refused' && i === p.stage) { cls = 'bad'; }
        else if (p.phase !== 'idle' && i < p.stage) { cls = 'done'; }
        else if (p.phase === 'running' && i === p.stage) { cls = 'cur'; }
        var name = 'tick' + (cls ? ' ' + cls : '');
        if (tick.className !== name) { tick.className = name; }
      });
      disk.set(diskSize(), p.phase, p.stage, p.stages.length);
      var el = $('disk');
      validators.set(validatorMode(p), {
        x: el.offsetLeft + el.offsetWidth / 2, y: el.offsetTop + el.offsetHeight / 2, r: el.offsetWidth / 2
      });
    }

    function renderInput(p) {
      var idle = p.phase === 'idle';
      var tab = effectiveTab();
      setLight($('inLt'), idle ? 'idle' : p.phase);
      var label = state.wtx ? 'your wallet'
        : state.source === 'paste' ? 'pasted message'
        : state.source === 'file' && state.file ? state.file.name
        : 'job ' + state.jobId.slice(0, 8);
      var mode = state.wtx ? 'attest_inline' : state.job ? modeOfJob(state.job) : state.jobMode;
      $('inSum').textContent = idle ? '' : label + ' ' + DOT + ' ' + mode;
      var flying = inFlight();
      if (!flying) { state.confirm = false; }
      // While a job runs there is nothing new to start from here: the header
      // says which job is followed, and starting another takes a confirmation.
      show($('btnNew'), !idle && !flying);
      show($('following'), flying);
      $('followingId').textContent = state.wtx ? (state.wtx.tx || state.wtx.evm || '').slice(0, 10) : state.jobId.slice(0, 8);
      show($('cardFollow'), flying);
      show($('another'), !state.confirm);
      show($('confirm'), state.confirm);
      var paying = walletMode();
      show($('resumeLink'), idle && !paying);

      // A connected wallet shows as a chip. Without a key it pays, and the
      // key field folds away behind "Use an API key"; with one, the key pays.
      show($('walletChip'), !!state.walletAddr);
      if (state.walletAddr) {
        var t = $('walletChipT');
        t.replaceChildren(document.createTextNode(paying ? 'Paying with wallet ' : 'Wallet '));
        var addr = document.createElement('span');
        addr.className = 'mono';
        addr.textContent = shortAddress(state.walletAddr);
        t.appendChild(addr);
        if (state.key) { t.appendChild(document.createTextNode(', the API key pays')); }
        t.title = state.walletAddr;
        $('useKey').textContent = paying ? 'Use an API key' : 'Pay with the wallet';
        show($('useKey'), idle && (paying || (!state.key && state.keyOpen)));
        setLight($('walletChipLt'), state.walletChain ? 'final' : 'provisional');
      }
      show($('keyField'), !paying);
      show($('modeField'), !paying);
      $('attestT').textContent = paying ? 'ATTEST ' + DOT + ' WALLET' : 'ATTEST';
      show($('resumeBox'), idle && state.resumeOpen);
      $('resumeLink').setAttribute('aria-expanded', String(state.resumeOpen));
      $('tabFile').setAttribute('aria-selected', String(tab === 'file'));
      $('tabPaste').setAttribute('aria-selected', String(tab === 'paste'));
      show($('drop'), tab === 'file');
      show($('paste'), tab === 'paste');
      show($('dropEmpty'), !state.file);
      show($('dropHas'), !!state.file);
      $('drop').classList.toggle('has', !!state.file);
      $('emlName').textContent = state.file ? state.file.name : '';
      $('emlSize').textContent = state.file ? kb(state.file.size) : '';
      all('.pill').forEach(function (pill) {
        pill.setAttribute('aria-pressed', String(pill.getAttribute('data-mode') === state.mode));
      });
      $('attest').disabled = !canAttest();
      $('follow').disabled = !state.key || state.busy;
      var msg = $('inMsg');
      msg.textContent = state.msg || state.keyMsg;
      msg.classList.toggle('err', state.msg ? state.msgErr : !!state.keyMsg);
      var body = $('cardBody');
      // Only the header, and the follow line under it, show while a job runs;
      // the card folds up to them. A job restored without a key keeps the
      // card open, since the key field is what it waits on.
      var open = idle || (!state.account && !state.wtx);
      inputHeight = 52 + (flying ? $('cardFollow').offsetHeight : 0) + (open ? body.scrollHeight : 0);
      $('cardIn').style.height = inputHeight + 'px';
      // Focus inside a folded card scrolls it; the header stays on top.
      $('cardIn').scrollTop = 0;
    }

    function outRows(walletCard) {
      show($('oReqRow'), walletCard);
      show($('oJobRow'), !walletCard);
      show($('oXtxRow'), !walletCard);
      show($('exWallet'), walletCard);
      if (walletCard) { earlierTxRows([], []); }
    }

    // The two tx rows hold the latest attestation and extraction attempts;
    // a retry leaves the earlier ones in rows of their own above them, so
    // every transaction a job sent stays reachable, oldest first.
    function earlierTxRows(attest, extract) {
      $('oTxRow').querySelector('.orow-k').textContent = txLabel('Attest tx', attest.length);
      $('oXtxRow').querySelector('.orow-k').textContent = txLabel('Extract tx', extract.length);
      var sig = JSON.stringify([attest, extract]);
      if (sig === txSig) { return; }
      txSig = sig;
      all('.orow.tx.earlier').forEach(function (el) { el.remove(); });
      attest.slice(0, -1).forEach(function (t, i) {
        $('oTxRow').before(earlierTxRow(txLabel('Attest tx', i + 1), t));
      });
      extract.slice(0, -1).forEach(function (t, i) {
        $('oXtxRow').before(earlierTxRow(txLabel('Extract tx', i + 1), t));
      });
    }

    function txLabel(name, n) { return n > 1 ? name + ' ' + n : name; }

    function earlierTxRow(label, t) {
      var el = document.createElement('div');
      el.className = 'orow tx earlier';
      var k = document.createElement('span');
      k.className = 'orow-k';
      k.textContent = label;
      var v = document.createElement('span');
      v.className = 'orow-v';
      v.textContent = shortTx(t.tx);
      el.appendChild(k);
      el.appendChild(v);
      el.appendChild(copyButton('cbtn', t.tx));
      if (typeof t.explorer === 'string' && t.explorer.indexOf(EXPLORER) === 0) {
        var link = document.createElement('a');
        link.className = 'cbtn';
        link.href = t.explorer;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.title = 'Explorer';
        link.insertAdjacentHTML('beforeend', ICON_LINK);
        el.appendChild(link);
      }
      return el;
    }

    // The wallet's attestation: the requester is the wallet, the record is
    // the one records_of(wallet) gained, read at LATEST_FINAL, and there is
    // no job and no extraction.
    function renderWalletOutput(p) {
      var w = state.wtx;
      var r = state.wresult && state.wresult.kind === 'recorded' ? state.wresult : null;
      $('cardOut').classList.toggle('shown', !!w.tx && (p.phase !== 'running' || p.stage >= 2));
      outRows(true);
      var final = p.phase === 'final';
      show($('outLt'), final);
      show($('outProv'), !final);
      show($('outFinal'), final);
      $('oReq').textContent = r ? String(r.record.requester) : w.from;
      $('oReq').title = r ? String(r.record.requester) : w.from;
      $('oDom').textContent = w.domain || NONE;
      $('oRec').textContent = r ? r.id : NONE;
      document.querySelector('[data-copy-ref="rec"]').disabled = !r;
      txRow('tx', 'oTx', w.tx, w.tx ? wallet.explorerTx(w.tx) : '', NONE);
      $('oTxRow').classList.toggle('cur', p.phase === 'running' && p.stage >= 1);
      setLight($('ckValid'), r ? (r.record.valid ? 'final' : 'refused') : 'provisional');
      setLight($('ckAligned'), r ? (r.record.aligned ? 'final' : 'refused') : 'provisional');
      show($('ex'), false);
    }

    function renderOutput(p) {
      if (state.wtx) {
        renderWalletOutput(p);
        return;
      }
      var job = state.job;
      var shown = !!job && (p.phase === 'final' || p.phase === 'provisional' || (p.phase === 'running' && p.stage >= 2));
      $('cardOut').classList.toggle('shown', shown);
      if (!job) { return; }
      outRows(false);
      var final = p.phase === 'final';
      show($('outLt'), final);
      show($('outProv'), !final);
      show($('outFinal'), final);
      $('oJob').textContent = job.id;
      $('oDom').textContent = job.sender ? job.sender.domain : NONE;
      var hasRecord = job.record_id !== undefined && job.record_id !== null;
      $('oRec').textContent = hasRecord ? String(job.record_id) : NONE;
      document.querySelector('[data-copy-ref="rec"]').disabled = !hasRecord;
      var x = job.extraction;
      var xTxs = x && Array.isArray(x.consensus_txs) ? x.consensus_txs : [];
      earlierTxRows(Array.isArray(job.consensus_txs) ? job.consensus_txs : [], xTxs);
      txRow('tx', 'oTx', job.consensus_tx, job.explorer, NONE);
      txRow('xtx', 'oXtx', x && x.consensus_tx, xTxs.length ? xTxs[xTxs.length - 1].explorer : '',
        x ? NONE : 'not requested');
      // The row of the transaction the current stage waits on is lit:
      // stages 2 and 3 are the attestation's, 5 and 6 the extraction's.
      var running = p.phase === 'running';
      $('oTxRow').classList.toggle('cur', running && (p.stage === 1 || p.stage === 2));
      $('oXtxRow').classList.toggle('cur', running && !!x && (p.stage === 4 || p.stage === 5));

      var valid = 'provisional';
      var aligned = 'provisional';
      if (final && hasRecord) {
        if (state.record) {
          valid = state.record.valid ? 'final' : 'refused';
          aligned = state.record.aligned ? 'final' : 'refused';
        } else {
          valid = aligned = job.valid_and_aligned ? 'final' : 'refused';
        }
      }
      setLight($('ckValid'), valid);
      setLight($('ckAligned'), aligned);

      show($('ex'), !!x);
      if (!x) { return; }
      var rows = [];
      var note = '';
      if (x.record_id !== undefined && x.record_id !== null) {
        if (x.lane) { rows.push(['Lane', x.lane]); }
        FIELDS.forEach(function (f) {
          var v = x[f[1]];
          if (v === undefined || v === null || v === '') { return; }
          rows.push([f[0], typeof v === 'boolean' ? (v ? 'yes' : 'no') : String(v)]);
        });
      } else if (EXTRACT_ENDED.indexOf(x.status) >= 0) {
        var reason = x.skipped_reason || x.refusal_reason || x.error || '';
        note = x.status + (reason ? ': ' + reason : '');
      } else {
        note = 'pending';
      }
      $('exNote').textContent = note;
      var sig = JSON.stringify(rows);
      if (sig !== exSig) {
        exSig = sig;
        $('exRows').replaceChildren.apply($('exRows'), rows.map(function (row) {
          var el = document.createElement('div');
          el.className = 'orow';
          var k = document.createElement('span');
          k.className = 'orow-k';
          k.textContent = row[0];
          var v = document.createElement('span');
          v.className = 'orow-v';
          v.textContent = row[1];
          el.appendChild(k);
          el.appendChild(v);
          el.appendChild(copyButton('cbtn', row[1]));
          return el;
        }));
      }
    }

    function txRow(ref, id, tx, explorer, none) {
      $(id).textContent = tx ? shortTx(tx) : none;
      document.querySelector('[data-copy-ref="' + ref + '"]').disabled = !tx;
      var link = $(id + 'Link');
      // Only a link into the explorer is followed, whatever the answer holds.
      var url = tx && typeof explorer === 'string' && explorer.indexOf(EXPLORER) === 0 ? explorer : '';
      show(link, !!url);
      if (url) { link.href = url; }
    }

    function curlText(key) {
      return 'curl -X POST ' + origin + '/attest \\\n  -H "X-API-Key: ' + key + '" \\\n  -F "eml=@message.eml" \\\n' +
        '  -F "extract=' + state.mode + '"';
    }

    function configText(key) {
      return '{\n  "mcpServers": {\n    "lacre": {\n      "type": "http",\n      "url": "' + origin + '/mcp",\n' +
        '      "headers": { "X-API-Key": "' + key + '" }\n    }\n  }\n}';
    }

    function renderPlugin(p) {
      var plug = $('plug');
      plug.classList.toggle('open', state.plugOpen);
      $('plugClosed').setAttribute('aria-expanded', String(state.plugOpen));
      var lt = p.phase === 'running' ? 'running' : p.phase === 'final' ? 'final' : 'idle';
      all('.plug-lt').forEach(function (el) { setLight(el, lt); });
      $('apiEpV').textContent = origin + '/attest';
      $('curl').textContent = curlText(state.key ? mask(state.key) : '$LACRE_KEY');
      $('mcpUrlV').textContent = origin + '/mcp';
      $('cfg').textContent = configText(state.key ? mask(state.key) : 'YOUR_KEY');

      var sig = JSON.stringify(state.mailboxes.map(function (m) { return [m.id, m.enabled]; })) + state.newMailbox;
      if (sig !== mailSig) {
        mailSig = sig;
        $('mlist').replaceChildren.apply($('mlist'), state.mailboxes.map(function (m) {
          var line = copyButton('cline' + (m.enabled ? '' : ' off'), m.address);
          var light = document.createElement('span');
          light.className = 'lt ' + (m.id === state.newMailbox ? 'final' : m.enabled ? 'idle' : 'off');
          var text = document.createElement('span');
          text.className = 'cline-v';
          text.textContent = m.address;
          line.insertBefore(text, line.firstChild);
          line.insertBefore(light, line.firstChild);
          line.title = m.enabled ? 'Copy' : 'Disabled';
          return line;
        }));
      }
      var anyOn = state.mailboxes.some(function (m) { return m.enabled; });
      setLight($('mailLt'), anyOn ? 'final' : 'idle');
      $('mailCreate').disabled = !state.key || !state.account || state.mailBusy;
      $('mailMsg').textContent = state.mailMsg || (state.key ? '' : 'Paste your API key to create one');
      $('mailMsg').classList.toggle('err', !!state.mailMsg);

      var slot = plug.parentNode;
      var below = state.plugOpen && !MOBILE.matches;
      if (MOBILE.matches) {
        plug.style.setProperty('--plug-h', ($('plugInner').scrollHeight) + 'px');
      } else if (below) {
        // Open, it would cover the input card, which sits in the same column:
        // it drops to just under the card instead and takes the room left.
        // With less than 240 left it runs past the stage, never taller than
        // the viewport under the nav, and scrolls inside.
        var top = 28 + inputHeight + 12;
        var room = $('stagearea').clientHeight - top - 20;
        slot.style.top = top + 'px';
        plug.style.setProperty('--plug-h', Math.min(712, window.innerHeight - 64 - 40, Math.max(240, room)) + 'px');
      } else {
        plug.style.setProperty('--plug-h', Math.max(300, Math.min(712, $('stagearea').clientHeight - 40)) + 'px');
      }
      if (!below) { slot.style.top = ''; }
      slot.classList.toggle('below', below);
    }

    // ---- the cables ----------------------------------------------------------------------------

    function within(el) {
      var a = $('stagearea').getBoundingClientRect();
      var r = el.getBoundingClientRect();
      return { left: r.left - a.left, top: r.top - a.top, right: r.right - a.left, bottom: r.bottom - a.top, width: r.width, height: r.height };
    }

    // The entry port sits on the disk's rim, on the side the cards are.
    function portPoint() {
      var d = within($('disk'));
      return { x: d.right - d.width * 0.015, y: d.top + d.height * 0.5 };
    }

    // The chip's row on the input card's edge, or the card's head while the
    // card is folded over the chip.
    function chipPoint() {
      var card = within($('cardIn'));
      var chip = $('walletChip');
      var y = card.top + 26;
      if (!chip.hidden) {
        var c = within(chip);
        if (c.bottom <= card.bottom) { y = c.top + c.height / 2; }
      }
      return { x: card.left, y: y };
    }

    function plugPoint() {
      var plug = within($('plug'));
      return { x: plug.left, y: plug.top + Math.min(plug.height / 2, 150) };
    }

    // On connect a cable runs from the wallet chip to the disk. While the
    // wallet's transaction is in flight it pulses toward the disk, and when
    // it is recorded, back toward the card for a moment. A job sent through
    // the API or a mailbox joins the plugin to the disk with a gray one.
    function renderCables(p) {
      var port = $('diskPort');
      if (MOBILE.matches) {
        cables.wallet.set(null);
        cables.plug.set(null);
        show(port, false);
        return;
      }
      var to = portPoint();
      var flow = '';
      if (state.wtx && p.phase === 'running') { flow = 'in'; }
      if (state.wtx && p.phase === 'final' && Date.now() < state.pulseBackUntil) { flow = 'out'; }
      cables.wallet.set(state.walletAddr ? { from: chipPoint(), to: to } : null, flow);
      var viaGateway = !state.wtx && !!state.jobId && p.phase === 'running';
      cables.plug.set(viaGateway ? { from: plugPoint(), to: to } : null, viaGateway ? 'in' : '');
      show(port, !!state.walletAddr || viaGateway);
      port.setAttribute('transform', 'translate(' + to.x.toFixed(1) + ' ' + to.y.toFixed(1) + ')');
    }

    function render() {
      var p = progress();
      renderReadouts(p);
      renderStage(p);
      renderInput(p);
      renderOutput(p);
      renderPlugin(p);
      renderCables(p);
      renderWallet();
    }

    function diskSize() {
      if (MOBILE.matches) { return Math.max(220, Math.min(320, window.innerWidth - 40)); }
      var area = $('stagearea');
      var fit = Math.min(540, area.clientWidth - 56 - 452 - 24, (area.clientHeight - 150) / 0.74);
      return Math.round(Math.max(260, fit));
    }

    // ---- the key and what it reads -------------------------------------------------------------

    function refreshAccount() {
      var key = state.key;
      return call('GET', '/account', { key: key }).then(function (res) {
        if (state.key !== key) { return; }
        if (res.status === 200 && res.data) {
          state.account = res.data;
          state.keyMsg = '';
          store.set('key', key);
          // Whatever moved the account moved a job: the list is read again.
          refreshJobs();
        } else {
          state.account = null;
          state.keyMsg = failure(res);
          if (res.status === 401 || res.status === 403) { store.set('key', ''); }
        }
        store.set('account', state.account);
        render();
      });
    }

    // Public: which contract the Router names for each layer, and where. No
    // key is sent, and the gateway answers everyone from one reading.
    function refreshPrimitives() {
      return call('GET', '/primitives').then(function (res) {
        var data = res.data;
        if (res.status === 200 && data && data.layers && data.addresses) { state.primitives = data; }
        render();
      });
    }

    function jobsPath(offset) {
      return '/jobs?' + (state.jobFilter ? 'status=' + state.jobFilter + '&' : '') + 'limit=' + JOBS_PAGE +
        (offset ? '&offset=' + offset : '');
    }

    // The first page of the account's jobs; "More" appends the next.
    function refreshJobs(more) {
      var key = state.key;
      var filter = state.jobFilter;
      var path = more && state.jobsNext ? state.jobsNext : jobsPath(0);
      if (!key || !/^\/jobs\?[a-z0-9=&]+$/.test(path)) { return Promise.resolve(); }
      return call('GET', path, { key: key }).then(function (res) {
        if (state.key !== key || state.jobFilter !== filter) { return; }
        if (res.status === 200 && res.data && Array.isArray(res.data.jobs)) {
          var fresh = res.data.jobs.filter(function (j) { return j && JOB_ID.test(j.id); });
          state.jobs = more ? state.jobs.concat(fresh) : fresh;
          state.jobsNext = typeof res.data.next === 'string' ? res.data.next : null;
        }
        render();
      });
    }

    function refreshMailboxes() {
      var key = state.key;
      return call('GET', '/mailboxes', { key: key }).then(function (res) {
        if (state.key !== key) { return; }
        if (res.status === 200 && res.data && Array.isArray(res.data.mailboxes)) {
          state.mailboxes = res.data.mailboxes;
        }
        render();
      });
    }

    function setKey(key) {
      if (key === state.key) { return; }
      state.key = key;
      // A new key is remembered only once /account has taken it, so a typo
      // is not kept; the one restored on load stays through a gateway outage.
      if (store.get('key') !== key) { store.set('key', ''); }
      state.account = null;
      store.set('account', null);
      state.mailboxes = [];
      state.newMailbox = '';
      state.mailMsg = '';
      state.keyMsg = '';
      state.jobs = [];
      state.jobsNext = null;
      render();
      if (!key) { return; }
      refreshAccount().then(function () {
        if (state.key !== key || !state.account) { return; }
        refreshMailboxes();
        // A job restored from the URL waited for the key; it is read now.
        if (state.jobId && !isTerminal()) { startPolling(); }
      });
    }

    // ---- jobs --------------------------------------------------------------------------------------

    function isTerminal() { return !!state.job && TERMINAL.indexOf(state.job.status) >= 0; }

    function stopPolling() {
      clearInterval(state.timer);
      state.timer = 0;
    }

    function startPolling() {
      stopPolling();
      poll();
      state.timer = setInterval(poll, POLL_MS);
    }

    function follow(id, mode, source) {
      state.jobId = id;
      store.set('jobId', id);
      state.confirm = false;
      jobSig = '';
      state.job = null;
      state.record = null;
      state.jobMode = mode;
      state.source = source;
      state.folded = false;
      exSig = '';
      if (source !== 'resume') { state.plugOpen = true; }
      setMsg(state.key ? '' : 'Paste your API key to follow job ' + id.slice(0, 8), !state.key);
      render();
      if (state.key) { startPolling(); }
    }

    function poll() {
      var id = state.jobId;
      var key = state.key;
      if (!id || !key) { return; }
      call('GET', '/jobs/' + encodeURIComponent(id), { key: key }).then(function (res) {
        if (state.jobId !== id || state.key !== key) { return; }
        if (res.status === 200 && res.data) {
          state.job = res.data;
          setMsg('');
          // Every move of the job can move the account: what it holds, what
          // it was charged and the counts. It is read again on each one.
          var x = res.data.extraction || {};
          var sig = [res.data.status, res.data.stage, res.data.tx_status, x.status, x.tx_status].join('|');
          if (sig !== jobSig) {
            if (jobSig) { refreshAccount(); }
            jobSig = sig;
          }
          var p = progress();
          // The plugin folds away once the job waits on chain, as in the design.
          if (!state.folded && p.stage >= 2) {
            state.folded = true;
            state.plugOpen = false;
          }
          if (isTerminal()) {
            stopPolling();
            if (p.phase === 'final') { loadRecord(); }
          }
        } else if (res.status === 404) {
          stopPolling();
          state.jobId = '';
          store.set('jobId', '');
          state.job = null;
          setMsg('No such job for this key', true);
        } else if (res.status === 401 || res.status === 403) {
          stopPolling();
          setMsg(failure(res), true);
        } else if (res.status === 0) {
          setMsg('The gateway did not answer, trying again in 20 s', true);
        } else {
          setMsg(failure(res), true);
        }
        render();
      });
    }

    function loadRecord() {
      var job = state.job;
      // The job names its record by id and Verifier; only that path is read.
      if (!job || typeof job.record !== 'string' || !/^\/records\/\d+\?verifier=0x[0-9a-fA-F]{40}$/.test(job.record)) { return; }
      var id = job.id;
      call('GET', job.record, { key: state.key }).then(function (res) {
        if (state.jobId !== id) { return; }
        if (res.status === 200 && res.data && res.data.record) {
          state.record = res.data.record;
          render();
        }
      });
    }

    // ---- the wallet's attestation -----------------------------------------------------------------

    // Words for a wallet or chain error: a rejected prompt is not a failure.
    function walletError(error) {
      var e = error || {};
      if (e.code === 4001 || /user rejected|denied/i.test(String(e.message || ''))) { return 'You declined in the wallet'; }
      return String(e.shortMessage || e.message || e).split('\n')[0].slice(0, 240);
    }

    // The message as bytes: the file as it is, a paste as CRLF lines in
    // UTF-8, the same bytes the gateway path uploads.
    function inputBytes() {
      if (effectiveTab() === 'paste') {
        return Promise.resolve(new TextEncoder().encode(state.paste.replace(/\r?\n/g, '\r\n')));
      }
      return state.file.arrayBuffer().then(function (buffer) { return new Uint8Array(buffer); });
    }

    function pendingCall(w) {
      return { from: w.from, verifier: w.verifier, fee: String(w.fee), domain: w.domain, selector: w.selector,
        before: Number(w.before), refusal: w.refusal || '' };
    }

    function keepPending(w) {
      state.wtx = w;
      store.set('pendingTx', w);
    }

    function attestWithWallet() {
      var router = state.primitives && state.primitives.addresses && state.primitives.addresses.router;
      if (!router || !ADDRESS.test(router)) {
        setMsg('The Router could not be read just now; try again in a moment', true);
        render();
        return;
      }
      var from = state.walletAddr;
      state.busy = true;
      setMsg('Cutting the signed headers');
      render();
      inputBytes().then(function (bytes) {
        var cut = window.LacreDkim.forInline(bytes);
        setMsg('Reading the Verifier, its fee and the key of ' + cut.domain);
        render();
        return wallet.ensureChain().then(function () {
          state.walletChain = true;
          return wallet.prepare(router, from, cut);
        }).then(function (ready) {
          setMsg('Confirm attest_inline in your wallet ' + DOT + ' fee ' + ready.fee + ' wei, ' + cut.bytes.length +
            ' bytes of headers into public calldata');
          render();
          var w = { from: from, verifier: ready.verifier, fee: ready.fee, domain: cut.domain, selector: cut.selector,
            before: ready.before, refusal: ready.refusal, sentAt: new Date().toISOString() };
          return wallet.send(ready, cut, function (sent) {
            if (sent.evm && !w.evm) {
              w.evm = sent.evm;
              keepPending(w);
              state.busy = false;
              setMsg('');
              followWallet();
            }
            if (sent.tx) {
              w.tx = sent.tx;
              keepPending(w);
              render();
            }
          });
        });
      }).then(function () {
        state.busy = false;
        render();
      }, function (error) {
        state.busy = false;
        // Once the wallet has sent, the chain decides; a later error in the
        // SDK's own wait does not stop the follow, which reads the receipt.
        if (!state.wtx) { setMsg(error instanceof window.LacreDkim.BlobError ? error.message : walletError(error), true); }
        render();
      });
    }

    function stopWallet() {
      clearInterval(state.wtimer);
      state.wtimer = 0;
    }

    function followWallet() {
      stopWallet();
      state.wresult = null;
      state.wfail = '';
      state.wstate = null;
      render();
      walletTick();
      state.wtimer = setInterval(walletTick, POLL_MS);
    }

    // One reading: the tx id from the receipt if it is not known yet, the
    // stored consensus state, and at FINALIZED the record, read at
    // LATEST_FINAL. Nothing is final before that.
    function walletTick() {
      var w = state.wtx;
      if (!w || walletEnded() || state.wticking) { return; }
      state.wticking = true;
      var txId = w.tx ? Promise.resolve(w.tx) : wallet.txIdOf(w.evm).then(function (id) {
        if (id && state.wtx === w) {
          w.tx = id;
          keepPending(w);
        }
        return id;
      });
      txId.then(function (id) {
        if (!id) { return null; }
        return wallet.stored(id).then(function (st) {
          if (state.wtx !== w) { return null; }
          state.wstate = st;
          if (st.status === 'CANCELED') {
            state.wfail = 'The transaction was CANCELED';
            return null;
          }
          if (st.status !== 'FINALIZED') { return null; }
          return wallet.outcome(pendingCall(w), st).then(function (result) {
            if (state.wtx !== w) { return; }
            state.wresult = result;
            if (result.kind === 'recorded') { state.pulseBackUntil = Date.now() + PULSE_BACK_MS; setTimeout(render, PULSE_BACK_MS + 50); }
            refreshBalance();
          });
        });
      }).then(function () {
        state.wticking = false;
        if (state.wtx === w && walletEnded()) { stopWallet(); }
        if (state.wtx === w && !walletEnded()) { setMsg(''); }
        render();
      }, function (error) {
        state.wticking = false;
        if (state.wtx !== w) { return; }
        if (/reverted/i.test(String(error && error.message))) {
          state.wfail = String(error.message);
          stopWallet();
        } else {
          setMsg('The Bradbury RPC did not answer, trying again in 20 s', true);
        }
        render();
      });
    }

    function attest() {
      if (!canAttest()) { return; }
      if (walletMode()) {
        attestWithWallet();
        return;
      }
      var form = new FormData();
      var tab = effectiveTab();
      if (tab === 'paste') {
        // A textarea hands back bare LF; mail on the wire is CRLF, and the
        // body hash is taken over CRLF lines.
        var raw = state.paste.replace(/\r?\n/g, '\r\n');
        form.append('eml', new Blob([raw], { type: 'message/rfc822' }), 'pasted.eml');
      } else {
        form.append('eml', state.file, state.file.name);
      }
      form.append('extract', state.mode);
      state.busy = true;
      setMsg('');
      render();
      var mode = state.mode;
      call('POST', '/attest', { key: state.key, form: form }).then(function (res) {
        state.busy = false;
        if (res.status === 202 && res.data && JOB_ID.test(res.data.job_id)) {
          follow(res.data.job_id, res.data.extract || mode, tab);
          refreshAccount();
          return;
        }
        setMsg(failure(res), true);
        render();
      });
    }

    function reset() {
      stopPolling();
      // The wallet's transaction runs on regardless; the page only stops
      // following it, and a reload no longer picks it up.
      stopWallet();
      state.wtx = null;
      state.wstate = null;
      state.wresult = null;
      state.wfail = '';
      store.set('pendingTx', null);
      state.jobId = '';
      store.set('jobId', '');
      state.confirm = false;
      state.job = null;
      state.record = null;
      state.source = '';
      exSig = '';
      setMsg('');
      render();
    }

    function resume() {
      var id = $('resume').value.trim().toLowerCase();
      if (!JOB_ID.test(id)) {
        setMsg('A job id is 32 hex characters', true);
        render();
        return;
      }
      if (!state.key) {
        setMsg('Paste your API key first', true);
        render();
        return;
      }
      $('resume').value = '';
      state.resumeOpen = false;
      follow(id, 'auto', 'resume');
    }

    function createMailbox() {
      if (!state.key || state.mailBusy) { return; }
      var form = new FormData();
      form.append('extract', state.mode);
      state.mailBusy = true;
      state.mailMsg = '';
      render();
      call('POST', '/mailboxes', { key: state.key, form: form }).then(function (res) {
        state.mailBusy = false;
        if (res.status === 201 && res.data && res.data.address) {
          state.mailboxes = state.mailboxes.concat([res.data]);
          state.newMailbox = res.data.id;
          refreshAccount();
        } else {
          state.mailMsg = failure(res);
        }
        render();
      });
    }

    function copyButton(className, text) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = className;
      button.title = 'Copy';
      button.setAttribute('data-copy', text);
      button.insertAdjacentHTML('beforeend', ICON_COPY + ICON_OK);
      return button;
    }

    function takeFile(file) {
      if (!file) { return; }
      state.file = file;
      state.tab = 'file';
      setMsg('');
      render();
    }

    // ---- wiring ----------------------------------------------------------------------------------------

    copyRefs.job = function () { return state.job ? state.job.id : state.jobId; };
    copyRefs.req = function () {
      var r = state.wresult && state.wresult.kind === 'recorded' ? state.wresult.record : null;
      return r ? String(r.requester) : state.wtx ? state.wtx.from : '';
    };
    copyRefs.dom = function () { return state.job && state.job.sender ? state.job.sender.domain : ''; };
    copyRefs.rec = function () {
      if (state.wtx) { return state.wresult && state.wresult.kind === 'recorded' ? String(state.wresult.id) : ''; }
      return state.job && state.job.record_id != null ? String(state.job.record_id) : '';
    };
    copyRefs.tx = function () {
      if (state.wtx) { return state.wtx.tx || ''; }
      return state.job && state.job.consensus_tx ? state.job.consensus_tx : '';
    };
    copyRefs.xtx = function () {
      var x = state.job && state.job.extraction;
      return x && x.consensus_tx ? x.consensus_tx : '';
    };
    copyRefs.endpoint = function () { return origin + '/attest'; };
    copyRefs.mcpUrl = function () { return origin + '/mcp'; };
    // The key shows masked; what is copied is ready to paste and run.
    copyRefs.curl = function () { return curlText(state.key || '$LACRE_KEY'); };
    copyRefs.cfg = function () { return configText(state.key || 'YOUR_KEY'); };

    $('sideToggle').addEventListener('click', function () {
      state.leftOpen = !state.leftOpen;
      render();
      // The disk is sized to the room beside the panel once it has moved.
      setTimeout(render, 850);
    });
    $('mstrip').addEventListener('click', function () { state.mInfo = !state.mInfo; render(); });
    all('.tab').forEach(function (tab) {
      tab.addEventListener('click', function () { state.tab = tab.getAttribute('data-tab'); render(); });
    });
    var drop = $('drop');
    drop.addEventListener('click', function () { $('file').click(); });
    drop.addEventListener('dragover', function (event) { event.preventDefault(); drop.classList.add('over'); });
    drop.addEventListener('dragleave', function () { drop.classList.remove('over'); });
    drop.addEventListener('drop', function (event) {
      event.preventDefault();
      drop.classList.remove('over');
      takeFile(event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0]);
    });
    $('file').addEventListener('change', function (event) {
      takeFile(event.target.files && event.target.files[0]);
      event.target.value = '';
    });
    // The sample goes in as a File, so everything after reads it exactly as
    // a dropped one.
    $('sample').addEventListener('click', function () {
      fetch('/static/sample.eml').then(function (res) {
        if (!res.ok) { throw new Error('HTTP ' + res.status); }
        return res.blob();
      }).then(function (blob) {
        takeFile(new File([blob], 'sample.eml', { type: 'message/rfc822' }));
      }).catch(function () {
        setMsg('The sample could not be loaded', true);
        render();
      });
    });
    $('paste').addEventListener('input', function (event) { state.paste = event.target.value; render(); });
    var keyInput = $('key');
    var keyTimer = 0;
    keyInput.addEventListener('change', function () { setKey(keyInput.value.trim()); });
    // Typed, or filled in by a password manager, the key is read once it
    // settles, without waiting for the field to lose focus.
    keyInput.addEventListener('input', function () {
      clearTimeout(keyTimer);
      keyTimer = setTimeout(function () { setKey(keyInput.value.trim()); }, KEY_SETTLE_MS);
    });
    $('remember').checked = store.remember();
    $('remember').addEventListener('change', function () { store.setRemember($('remember').checked); });
    keyInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { setKey(keyInput.value.trim()); }
    });
    $('pasteKey').addEventListener('click', function () {
      if (!navigator.clipboard || !navigator.clipboard.readText) { keyInput.focus(); return; }
      navigator.clipboard.readText().then(function (text) {
        keyInput.value = (text || '').trim();
        setKey(keyInput.value);
      }, function () { keyInput.focus(); });
    });
    all('.pill').forEach(function (pill) {
      pill.addEventListener('click', function () { state.mode = pill.getAttribute('data-mode'); render(); });
    });
    $('attest').addEventListener('click', attest);
    $('btnNew').addEventListener('click', reset);
    $('another').addEventListener('click', function () { state.confirm = true; render(); });
    $('anotherNo').addEventListener('click', function () { state.confirm = false; render(); });
    $('anotherYes').addEventListener('click', reset);
    $('resumeLink').addEventListener('click', function () {
      state.resumeOpen = !state.resumeOpen;
      render();
      if (state.resumeOpen) { $('resume').focus(); }
    });
    $('follow').addEventListener('click', resume);
    $('resume').addEventListener('keydown', function (event) { if (event.key === 'Enter') { resume(); } });
    $('plugClosed').addEventListener('click', function () {
      state.plugOpen = true;
      render();
      if (state.key && state.account) { refreshMailboxes(); }
    });
    $('plugFold').addEventListener('click', function () { state.plugOpen = false; render(); });
    $('mailCreate').addEventListener('click', createMailbox);
    $('jobs').addEventListener('click', function (event) {
      var li = event.target.closest('[data-job]');
      if (!li || !state.key) { return; }
      if (state.wtx) {
        if (!walletEnded()) {
          setMsg('Your wallet attestation is still in flight', true);
          render();
          return;
        }
        reset();
      }
      follow(li.getAttribute('data-job'), 'auto', 'resume');
    });
    all('[data-filter]').forEach(function (row) {
      row.addEventListener('click', function () {
        var f = row.getAttribute('data-filter');
        state.jobFilter = state.jobFilter === f ? '' : f;
        state.jobs = [];
        state.jobsNext = null;
        render();
        refreshJobs();
      });
    });
    $('jobsMore').addEventListener('click', function () { refreshJobs(true); });
    // The card folds and unfolds over .9 s; the cable follows it once done.
    $('cardIn').addEventListener('transitionend', function (event) {
      if (event.target === $('cardIn')) { render(); }
    });

    var resizeTimer = 0;
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(render, 120);
    });
    MOBILE.addEventListener('change', render);
    if (document.fonts && document.fonts.ready) { document.fonts.ready.then(render); }
    // The MCP section links here to create a mailbox; the plugin opens for
    // it, back at the top of the page where it lives.
    function openPluginFromHash() {
      if (window.location.hash !== '#plugin') { return; }
      state.plugOpen = true;
      window.scrollTo(0, 0);
      render();
      if (state.key && state.account) { refreshMailboxes(); }
    }
    window.addEventListener('hashchange', openPluginFromHash);
    openPluginFromHash();

    // ---- the wallet card ------------------------------------------------------------------------

    // The nav item it was opened from, or null. One element serves both navs.
    var pop = $('walletPop');
    var popItem = null;

    function openPop(item) {
      popItem = item;
      // Right after the item, so Tab goes from the item into it.
      item.after(pop);
      if (item.closest('.nav-drop')) {
        pop.style.top = pop.style.right = '';
      } else {
        var r = item.getBoundingClientRect();
        pop.style.top = (r.bottom + 10) + 'px';
        pop.style.right = (document.documentElement.clientWidth - r.right) + 'px';
      }
      pop.hidden = false;
      item.setAttribute('aria-expanded', 'true');
    }

    function closePop(refocus) {
      if (!popItem) { return; }
      var item = popItem;
      popItem = null;
      pop.hidden = true;
      item.setAttribute('aria-expanded', 'false');
      if (refocus) { item.focus(); }
    }

    function renderWallet() {
      var address = state.walletAddr;
      var present = !!wallet.provider();
      setLight($('walletLt'), !address ? 'off' : state.walletChain ? 'final' : 'provisional');
      $('walletTag').textContent = address && !state.walletChain ? 'NOT ON BRADBURY' : 'BRADBURY';
      show($('walletInfo'), !!address);
      if (address) {
        $('walletAddr').textContent = shortAddress(address);
        $('walletAddr').title = address;
        $('walletAddr').setAttribute('data-copy', address);
        $('walletBal').textContent = state.walletBal || NONE;
        $('walletPopAddr').textContent = address;
        $('walletPopGen').textContent = state.walletBal || NONE;
        show($('walletPopBal'), state.walletChain);
        show($('walletPopChain'), !state.walletChain);
      } else {
        closePop(false);
      }
      var btn = $('walletBtn');
      btn.disabled = state.walletBusy || (!address && !present);
      btn.textContent = !present && !address ? 'NO WALLET FOUND'
        : !address ? (state.walletBusy ? 'CONNECTING' : 'CONNECT WALLET')
        : !state.walletChain ? 'SWITCH TO BRADBURY' : 'DROP A SIGNED .EML';
      $('walletMsg').textContent = state.walletMsg ||
        (!present && !address ? 'No wallet in this browser. Any wallet that injects window.ethereum works.' : '');
      $('walletMsg').classList.toggle('err', !!state.walletMsg);
      // The nav item is the same wallet seen from the top of the page.
      all('.nav-wallet').forEach(function (item) {
        item.disabled = state.walletBusy;
        if (!address) {
          item.textContent = state.walletBusy ? 'CONNECTING' : 'CONNECT WALLET';
          item.removeAttribute('title');
          item.removeAttribute('aria-expanded');
          return;
        }
        item.setAttribute('aria-expanded', String(item === popItem));
        var lt = document.createElement('span');
        lt.className = 'lt lt-7 ' + (state.walletChain ? 'final' : 'provisional');
        var text = document.createElement('span');
        text.className = 'mono';
        text.textContent = navAddress(address);
        item.replaceChildren(lt, text);
        item.title = address;
      });
    }

    function refreshBalance() {
      var address = state.walletAddr;
      if (!address) { return Promise.resolve(); }
      return wallet.balance(address).then(function (gen) {
        if (state.walletAddr === address) { state.walletBal = gen; render(); }
      }, function () {});
    }

    function checkChain() {
      return wallet.onBradbury().then(function (on) {
        state.walletChain = on;
        render();
      });
    }

    function connect() {
      state.walletBusy = true;
      state.walletMsg = '';
      render();
      store.connectWallet().then(function () {
        return wallet.ensureChain();
      }).then(function () {
        state.walletChain = true;
        state.walletBusy = false;
        refreshBalance();
        render();
      }, function (error) {
        state.walletBusy = false;
        state.walletMsg = walletError(error);
        if (state.walletAddr) { checkChain(); }
        render();
      });
    }

    $('walletBtn').addEventListener('click', function () {
      if (!state.walletAddr) { connect(); return; }
      if (!state.walletChain) {
        wallet.ensureChain().then(checkChain, function (error) { state.walletMsg = walletError(error); render(); });
        return;
      }
      // Connected: the way in is the drop zone at the top.
      window.scrollTo(0, 0);
      state.keyOpen = false;
      render();
      $('drop').focus();
    });
    // Connected, the item opens the wallet under it. Without a wallet to
    // connect, the card says why there is none.
    all('.nav-wallet').forEach(function (item) {
      item.addEventListener('click', function () {
        if (state.walletAddr) {
          var again = popItem === item;
          closePop(false);
          if (!again) { openPop(item); }
          return;
        }
        if (wallet.provider()) { connect(); return; }
        var nav = document.querySelector('.nav');
        nav.classList.remove('menu-open');
        nav.querySelector('.nav-menu').setAttribute('aria-expanded', 'false');
        $('walletCard').scrollIntoView();
      });
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { closePop(true); }
    });
    // The item's own click runs first and has already toggled the popover.
    document.addEventListener('click', function (event) {
      if (popItem && !pop.contains(event.target) && !popItem.contains(event.target)) { closePop(false); }
    });
    // A floating popover is placed once; the inline one moves with the drawer.
    window.addEventListener('resize', function () {
      if (popItem && !popItem.closest('.nav-drop')) { closePop(false); }
    });
    function disconnect() {
      state.walletMsg = '';
      store.disconnectWallet();
    }
    $('walletOff').addEventListener('click', disconnect);
    $('walletPopOff').addEventListener('click', function () {
      closePop(true);
      disconnect();
    });
    $('useKey').addEventListener('click', function () {
      state.keyOpen = !state.keyOpen;
      render();
      if (state.keyOpen) { $('key').focus(); }
    });

    store.subscribe('wallet', function (address) {
      state.walletAddr = address;
      state.walletBal = '';
      if (!address) { state.keyOpen = false; }
      if (address) {
        refreshBalance();
        checkChain();
      }
      render();
    });
    var eth = wallet.provider();
    if (eth && eth.on) { eth.on('chainChanged', function () { if (state.walletAddr) { checkChain(); } }); }
    store.reconnectWallet().then(function (address) {
      if (address) {
        refreshBalance();
        checkChain();
      }
    });

    // What the last visit left: the job in the URL or the session, and the
    // key if it was remembered. No click is needed to pick either up.
    var savedKey = store.get('key');
    var savedJob = store.get('jobId');
    if (savedKey) {
      keyInput.value = savedKey;
      setKey(savedKey);
    }
    // A wallet transaction in flight wins over a job: it is followed on the
    // chain, with or without the wallet, and needs no key.
    if (state.wtx) {
      followWallet();
    } else if (savedJob) {
      follow(savedJob, 'auto', 'resume');
    }
    refreshPrimitives();
    setInterval(refreshPrimitives, PRIMITIVES_MS);
    render();
  }

  var ICON_COPY = '<svg class="ic ic-copy" viewBox="0 0 256 256" aria-hidden="true">' +
    '<polyline points="168 168 216 168 216 40 88 40 88 88"/><rect x="40" y="88" width="128" height="128" rx="8"/></svg>';
  var ICON_OK = '<svg class="ic ic-ok" viewBox="0 0 256 256" aria-hidden="true"><polyline points="40 144 96 200 224 72"/></svg>';
  var ICON_LINK = '<svg class="ic" viewBox="0 0 256 256" aria-hidden="true"><polyline points="216 104 216 40 152 40"/>' +
    '<line x1="144" y1="112" x2="216" y2="40"/><path d="M184 144v64a8 8 0 0 1-8 8H48a8 8 0 0 1-8-8V80a8 8 0 0 1 8-8h64"/></svg>';

  // ---- the access form -------------------------------------------------------------------------------

  function initAccess() {
    var form = $('accForm');
    var name = $('accName');
    var email = $('accEmail');
    var what = $('accWhat');
    var send = $('accSend');
    var busy = false;

    function ready() { return name.value.trim() !== '' && email.value.indexOf('@') > 0; }
    function sync() { send.disabled = busy || !ready(); }

    form.addEventListener('input', function () {
      $('accOk').hidden = true;
      sync();
    });
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      if (busy || !ready()) { return; }
      busy = true;
      sync();
      $('accMsg').textContent = '';
      call('POST', '/access-request', {
        json: { name: name.value.trim(), email: email.value.trim(), what: what.value.trim() }
      }).then(function (res) {
        busy = false;
        if (res.status === 201) {
          $('accOk').hidden = false;
        } else {
          $('accMsg').textContent = res.status === 429 ? 'Too many requests from here, try again later' : failure(res);
        }
        sync();
      });
    });
    sync();
  }

  function init() {
    initNav();
    var page = document.body.getAttribute('data-page');
    if (page === 'app') { initApp(); }
    if ($('accForm')) { initAccess(); }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
