/* Lacre web app: one script for every page. No framework, no build step.
 *
 * The API key lives in state.key below and nowhere else: not in storage,
 * not in a cookie, not in the URL, so a reload asks for it again. Every
 * call goes to this origin; the pages carry no address of their own.
 */
(function () {
  'use strict';

  var MDASH = '\u2014';
  var DOT = '\u00b7';
  var APPROX = '\u2248';
  var BULLET = '\u2022';
  var INFINITY = '\u221e';
  var POLL_MS = 20000;
  var FLASH_MS = 1400;
  var MOBILE = window.matchMedia('(max-width: 1023.98px)');
  var JOB_ID = /^[0-9a-f]{32}$/;
  var EXPLORER = 'https://explorer-bradbury.genlayer.com/';

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
    { n: 'Recorded', m: 0, eta: MDASH }
  ];
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

  function show(el, on) { el.hidden = !on; }

  function setLight(el, state) {
    el.className = (el.getAttribute('data-base') || 'lt') + ' ' + state;
  }

  function initApp() {
    var state = {
      key: '', account: null, health: null, keyMsg: '',
      mailboxes: [], newMailbox: '', mailMsg: '', mailBusy: false,
      tab: 'file', file: null, paste: '', mode: 'auto',
      source: '', jobId: '', job: null, record: null, jobMode: 'auto', busy: false, timer: 0,
      plugOpen: false, folded: false, leftOpen: window.innerWidth >= 1280, mInfo: false,
      msg: '', msgErr: false
    };
    var disk = new Disk($('disk'));
    var origin = window.location.origin;
    var mailSig = '';
    var exSig = '';

    function effectiveTab() { return MOBILE.matches ? 'file' : state.tab; }

    function modeOfJob(job) {
      var x = job.extraction;
      if (!x || x.status === 'skipped') { return 'none'; }
      return x.requested || 'auto';
    }

    function progress() {
      if (!state.jobId) { return { phase: 'idle', stage: -1, stages: stagesFor(state.mode) }; }
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
      var name = state.health && state.health.network ? String(state.health.network) : 'bradbury';
      return name.charAt(0).toUpperCase() + name.slice(1);
    }

    function readout(p) {
      var n = p.stages.length;
      var job = state.job;
      if (p.phase === 'idle') { return ['00 / ' + pad(n), 'Idle', 'Drop a signed .eml']; }
      if (p.phase === 'final') { return [pad(n) + ' / ' + pad(n), 'Recorded', 'Finalized on ' + networkName()]; }
      if (p.phase === 'provisional') {
        return [pad(n) + ' / ' + pad(n), 'Recorded', 'Recorded ' + DOT + ' waiting for FINALIZED'];
      }
      if (p.phase === 'refused') {
        var why = job.refusal_reason || job.error || 'no reason given';
        return [pad(p.stage + 1) + ' / ' + pad(n), job.status === 'failed' ? 'Failed' : 'Refused',
          why + ' ' + DOT + ' nothing recorded'];
      }
      var cur = p.stages[p.stage];
      var eta;
      if (job && job.stage === 'sender in verification') {
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
      var map = { 'Queued': [0], 'Attesting': [1, 2], 'Finalizing': p.stage >= 5 ? ex : [2], 'Serving body': [0], 'Extracting': ex };
      return map[name] || [];
    }

    function layerState(index, busy) {
      if (busy.indexOf(index) >= 0) { return ['running', 'BUSY']; }
      var layers = state.health && state.health.layers;
      if (!layers || layers[LAYERS[index]] === undefined) { return ['off', MDASH]; }
      if (layers[LAYERS[index]]) { return ['final', 'LIVE']; }
      // An Extractor the Router does not name is off, not broken.
      return index >= 3 ? ['off', 'OFF'] : ['refused', 'DOWN'];
    }

    function canAttest() {
      if (!state.key || state.busy || state.jobId) { return false; }
      if (effectiveTab() === 'paste') { return state.paste.trim().length > 20; }
      return !!state.file;
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
      var net = !state.health ? 'off' : state.health.chain ? 'final' : 'refused';
      all('[data-net]').forEach(function (light) { setLight(light, net); });
      all('[data-net-name]').forEach(function (el) { el.textContent = networkName(); });
      all('[data-net-upper]').forEach(function (el) { el.textContent = networkName().toUpperCase(); });

      var account = state.account;
      var prices = account ? account.prices : null;
      all('[data-price]').forEach(function (el) {
        el.textContent = prices ? String(prices[el.getAttribute('data-price')]) : MDASH;
      });
      all('[data-count]').forEach(function (el) {
        var v = account && account.counts ? account.counts[el.getAttribute('data-count')] : undefined;
        el.textContent = v === undefined ? MDASH : groups(v);
      });
      var balance = account ? (account.unlimited ? INFINITY : groups(account.credits)) : '';
      show($('balance'), !!account);
      show($('balanceNone'), !account);
      $('balanceN').textContent = balance;
      $('mBal').textContent = (account ? balance : MDASH) + ' cr';
      var mp = $('mPrices').children;
      mp[0].textContent = 'attest ' + (prices ? prices.attest : MDASH) + ' cr';
      mp[1].textContent = 'extract ' + (prices ? prices.extract : MDASH) + ' cr';

      $('side').classList.toggle('collapsed', !state.leftOpen);
      $('sideToggle').setAttribute('aria-expanded', String(state.leftOpen));
      $('sideToggle').title = state.leftOpen ? 'Collapse' : 'Expand';
      $('mstrip').setAttribute('aria-expanded', String(state.mInfo));
      $('minfo').classList.toggle('open', state.mInfo);
    }

    function renderStage(p) {
      var text = readout(p);
      $('stageNum').textContent = text[0];
      $('stageName').textContent = text[1];
      $('stageEta').textContent = text[2];
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
    }

    function renderInput(p) {
      var idle = p.phase === 'idle';
      var tab = effectiveTab();
      setLight($('inLt'), idle ? 'idle' : p.phase);
      var label = state.source === 'paste' ? 'pasted message'
        : state.source === 'file' && state.file ? state.file.name
        : 'job ' + state.jobId.slice(0, 8);
      var mode = state.job ? modeOfJob(state.job) : state.jobMode;
      $('inSum').textContent = idle ? '' : label + ' ' + DOT + ' ' + mode;
      show($('btnNew'), !idle);
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
      // Only the header shows while a job runs; the card folds up to it.
      $('cardIn').style.height = idle ? (52 + body.scrollHeight) + 'px' : '52px';
    }

    function renderOutput(p) {
      var job = state.job;
      var shown = !!job && (p.phase === 'final' || p.phase === 'provisional' || (p.phase === 'running' && p.stage >= 2));
      $('cardOut').classList.toggle('shown', shown);
      if (!job) { return; }
      var final = p.phase === 'final';
      show($('outLt'), final);
      show($('outProv'), !final);
      show($('outFinal'), final);
      $('oJob').textContent = job.id;
      $('oDom').textContent = job.sender ? job.sender.domain : MDASH;
      var hasRecord = job.record_id !== undefined && job.record_id !== null;
      $('oRec').textContent = hasRecord ? String(job.record_id) : MDASH;
      $('oTx').textContent = job.consensus_tx ? shortTx(job.consensus_tx) : MDASH;
      document.querySelector('[data-copy-ref="rec"]').disabled = !hasRecord;
      document.querySelector('[data-copy-ref="tx"]').disabled = !job.consensus_tx;
      var link = $('oTxLink');
      // Only a link into the explorer is followed, whatever the answer holds.
      var explorer = typeof job.explorer === 'string' && job.explorer.indexOf(EXPLORER) === 0 ? job.explorer : '';
      show(link, !!explorer);
      if (explorer) { link.href = explorer; }

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

      var x = job.extraction;
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

      if (MOBILE.matches) {
        plug.style.setProperty('--plug-h', ($('plugInner').scrollHeight) + 'px');
      } else {
        plug.style.setProperty('--plug-h', Math.max(300, Math.min(712, $('stagearea').clientHeight - 40)) + 'px');
      }
    }

    function render() {
      var p = progress();
      renderReadouts(p);
      renderStage(p);
      renderInput(p);
      renderOutput(p);
      renderPlugin(p);
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
        } else {
          state.account = null;
          state.keyMsg = failure(res);
        }
        render();
      });
    }

    function refreshHealth() {
      var key = state.key;
      return call('GET', '/health', { key: key }).then(function (res) {
        if (state.key !== key) { return; }
        // 503 still carries the checks; only a body without them is ignored.
        if (res.data && typeof res.data === 'object' && 'chain' in res.data) { state.health = res.data; }
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
      state.account = null;
      state.health = null;
      state.mailboxes = [];
      state.newMailbox = '';
      state.mailMsg = '';
      state.keyMsg = '';
      render();
      if (!key) { return; }
      refreshAccount().then(function () {
        if (state.key !== key || !state.account) { return; }
        refreshHealth();
        refreshMailboxes();
        if (state.jobId && !state.timer && !isTerminal()) { startPolling(); }
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
      state.job = null;
      state.record = null;
      state.jobMode = mode;
      state.source = source;
      state.folded = false;
      exSig = '';
      if (source !== 'resume') { state.plugOpen = true; }
      setMsg('');
      render();
      startPolling();
    }

    function poll() {
      var id = state.jobId;
      var key = state.key;
      if (!id || !key) { return; }
      call('GET', '/jobs/' + encodeURIComponent(id), { key: key }).then(function (res) {
        if (state.jobId !== id || state.key !== key) { return; }
        if (res.status === 200 && res.data) {
          var was = state.job ? state.job.status : '';
          state.job = res.data;
          setMsg('');
          var p = progress();
          // The plugin folds away once the job waits on chain, as in the design.
          if (!state.folded && p.stage >= 2) {
            state.folded = true;
            state.plugOpen = false;
          }
          if (isTerminal()) {
            stopPolling();
            if (was !== state.job.status) { refreshAccount(); }
            if (p.phase === 'final') { loadRecord(); }
          }
        } else if (res.status === 404) {
          stopPolling();
          state.jobId = '';
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

    function attest() {
      if (!canAttest()) { return; }
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
      state.jobId = '';
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
    copyRefs.dom = function () { return state.job && state.job.sender ? state.job.sender.domain : ''; };
    copyRefs.rec = function () { return state.job && state.job.record_id != null ? String(state.job.record_id) : ''; };
    copyRefs.tx = function () { return state.job && state.job.consensus_tx ? state.job.consensus_tx : ''; };
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
    $('paste').addEventListener('input', function (event) { state.paste = event.target.value; render(); });
    var keyInput = $('key');
    keyInput.addEventListener('change', function () { setKey(keyInput.value.trim()); });
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
    $('follow').addEventListener('click', resume);
    $('resume').addEventListener('keydown', function (event) { if (event.key === 'Enter') { resume(); } });
    $('plugClosed').addEventListener('click', function () {
      state.plugOpen = true;
      render();
      if (state.key && state.account) { refreshMailboxes(); }
    });
    $('plugFold').addEventListener('click', function () { state.plugOpen = false; render(); });
    $('mailCreate').addEventListener('click', createMailbox);

    var resizeTimer = 0;
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(render, 120);
    });
    MOBILE.addEventListener('change', render);
    if (document.fonts && document.fonts.ready) { document.fonts.ready.then(render); }
    // The MCP page links here to create a mailbox; the plugin opens for it.
    if (window.location.hash === '#plugin') { state.plugOpen = true; }
    render();
  }

  var ICON_COPY = '<svg class="ic ic-copy" viewBox="0 0 256 256" aria-hidden="true">' +
    '<polyline points="168 168 216 168 216 40 88 40 88 88"/><rect x="40" y="88" width="128" height="128" rx="8"/></svg>';
  var ICON_OK = '<svg class="ic ic-ok" viewBox="0 0 256 256" aria-hidden="true"><polyline points="40 144 96 200 224 72"/></svg>';

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
    if (page === 'access') { initAccess(); }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
