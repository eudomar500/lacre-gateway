/* Lacre page state: what survives a reload, and who is told when it moves.
 *
 * Five values, each with get, set and subscribe: key, jobId, account,
 * wallet and pendingTx. Where each one lives is decided here and nowhere
 * else:
 *
 *   key      sessionStorage, and only while "Remember for this session" is
 *            on. The tab forgets it when it closes. Never localStorage.
 *   jobId    the URL (?job=) and sessionStorage. The URL makes a job
 *            shareable and reloadable; the session copy covers a reload
 *            after something dropped the query.
 *   account  memory only: it is read again from /account on every load.
 *   wallet   the address in localStorage, as a durable choice. It is not
 *            trusted on load: reconnectWallet() asks the wallet with
 *            eth_accounts, which never prompts, and clears it if the wallet
 *            no longer grants it.
 *   pendingTx the wallet's attest_inline in flight, in localStorage: its
 *            EVM hash from the moment the wallet returns it, then its
 *            consensus tx id, and what the page read before sending (the
 *            Verifier, the fee, the sender's record count). A reload, or a
 *            visit the next day, follows it to FINALIZED from there. Only
 *            public chain data is kept: the headers were in the calldata.
 *
 * The remember switch is itself a durable choice and lives in localStorage.
 * Every storage call is wrapped: a private window or blocked storage costs
 * the persistence, never the page.
 */
(function () {
  'use strict';

  var KEY_SLOT = 'lacre.key';
  var JOB_SLOT = 'lacre.job';
  var WALLET_SLOT = 'lacre.wallet';
  var REMEMBER_SLOT = 'lacre.remember';
  var PENDING_SLOT = 'lacre.walletTx';
  var DURABLE = [WALLET_SLOT, REMEMBER_SLOT, PENDING_SLOT];
  var JOB_PARAM = 'job';
  var JOB_ID = /^[0-9a-f]{32}$/;
  var ADDRESS = /^0x[0-9a-fA-F]{40}$/;
  var HASH = /^0x[0-9a-fA-F]{64}$/;

  function session(slot, value) {
    try {
      if (value === undefined) { return sessionStorage.getItem(slot) || ''; }
      if (value) { sessionStorage.setItem(slot, value); } else { sessionStorage.removeItem(slot); }
    } catch (error) {
      // Storage refused: the value still holds for this page load.
    }
    return '';
  }

  // The durable slots are named here so nothing else can land in them.
  function durable(slot, value) {
    if (DURABLE.indexOf(slot) < 0) { throw new Error('not a durable slot: ' + slot); }
    try {
      if (value === undefined) { return localStorage.getItem(slot) || ''; }
      if (value) { localStorage.setItem(slot, value); } else { localStorage.removeItem(slot); }
    } catch (error) {
      // As above: a private window is not a reason to refuse the choice.
    }
    return '';
  }

  function jobFromUrl() {
    var raw = (new URLSearchParams(window.location.search).get(JOB_PARAM) || '').toLowerCase();
    return JOB_ID.test(raw) ? raw : '';
  }

  // Rewrites ?job= in place, keeping every other parameter and the anchor.
  // replaceState, not pushState: following a job is not a navigation, and
  // Back should leave the page rather than step through its jobs.
  function writeJobToUrl(id) {
    var url = new URL(window.location.href);
    if (id) { url.searchParams.set(JOB_PARAM, id); } else { url.searchParams.delete(JOB_PARAM); }
    var next = url.pathname + url.search + url.hash;
    if (next !== window.location.pathname + window.location.search + window.location.hash) {
      window.history.replaceState(window.history.state, '', next);
    }
  }

  // A stored transaction is taken back only in the shape the page writes:
  // a sender, the Verifier it went to, a fee, and an EVM hash or a tx id.
  function pendingFrom(text) {
    var tx;
    try { tx = JSON.parse(text || 'null'); } catch (error) { return null; }
    if (!tx || typeof tx !== 'object' || !ADDRESS.test(tx.from) || !ADDRESS.test(tx.verifier)) { return null; }
    if (!/^\d+$/.test(String(tx.fee)) || !/^\d+$/.test(String(tx.before))) { return null; }
    if (!HASH.test(tx.evm || '') && !HASH.test(tx.tx || '')) { return null; }
    return tx;
  }

  var remember = durable(REMEMBER_SLOT) !== '0';
  var storedWallet = durable(WALLET_SLOT);
  var values = {
    key: remember ? session(KEY_SLOT) : '',
    // The URL wins: a link someone opened names the job they meant.
    jobId: jobFromUrl() || (JOB_ID.test(session(JOB_SLOT)) ? session(JOB_SLOT) : ''),
    account: null,
    wallet: ADDRESS.test(storedWallet) ? storedWallet : null,
    pendingTx: pendingFrom(durable(PENDING_SLOT))
  };
  var listeners = { key: [], jobId: [], account: [], wallet: [], pendingTx: [] };

  function persist(name, value) {
    if (name === 'key') { session(KEY_SLOT, remember ? value : ''); }
    if (name === 'jobId') {
      session(JOB_SLOT, value);
      writeJobToUrl(value);
    }
    if (name === 'wallet') { durable(WALLET_SLOT, value || ''); }
    if (name === 'pendingTx') { durable(PENDING_SLOT, value ? JSON.stringify(value) : ''); }
  }

  function get(name) { return values[name]; }

  function set(name, value) {
    if (!(name in listeners)) { throw new Error('unknown state: ' + name); }
    persist(name, value);
    if (values[name] === value) { return; }
    values[name] = value;
    listeners[name].slice().forEach(function (fn) { fn(value); });
  }

  function subscribe(name, fn) {
    listeners[name].push(fn);
    return function () {
      var at = listeners[name].indexOf(fn);
      if (at >= 0) { listeners[name].splice(at, 1); }
    };
  }

  // Off drops the key from the tab at once; on stores the one in use.
  function setRemember(on) {
    remember = !!on;
    durable(REMEMBER_SLOT, remember ? '1' : '0');
    session(KEY_SLOT, remember ? values.key : '');
  }

  // ---- the wallet ------------------------------------------------------------

  function provider() { return window.ethereum && window.ethereum.request ? window.ethereum : null; }

  function firstAccount(accounts) {
    return Array.isArray(accounts) && ADDRESS.test(accounts[0]) ? accounts[0] : null;
  }

  var watching = false;

  // If the user switches accounts in the wallet, the page follows; if they
  // disconnect the site there, it forgets the address.
  function watchWallet() {
    var eth = provider();
    if (watching || !eth || !eth.on) { return; }
    watching = true;
    eth.on('accountsChanged', function (accounts) { set('wallet', firstAccount(accounts)); });
  }

  // On load, and only if a wallet was connected before: eth_accounts lists
  // what the wallet already grants this site without showing anything. An
  // empty answer, or no wallet at all, clears the stored address silently.
  function reconnectWallet() {
    if (!values.wallet) { return Promise.resolve(null); }
    var eth = provider();
    if (!eth) {
      set('wallet', null);
      return Promise.resolve(null);
    }
    return eth.request({ method: 'eth_accounts' }).then(function (accounts) {
      set('wallet', firstAccount(accounts));
      if (values.wallet) { watchWallet(); }
      return values.wallet;
    }, function () {
      set('wallet', null);
      return null;
    });
  }

  // The explicit connect, for a click only: this one may prompt.
  function connectWallet() {
    var eth = provider();
    if (!eth) { return Promise.reject(new Error('No wallet found in this browser')); }
    return eth.request({ method: 'eth_requestAccounts' }).then(function (accounts) {
      var address = firstAccount(accounts);
      if (!address) { throw new Error('The wallet returned no account'); }
      set('wallet', address);
      watchWallet();
      return address;
    });
  }

  // EIP-1193 has no disconnect: the page forgets the address and, where the
  // wallet supports it, gives the permission back so the next connect asks.
  function disconnectWallet() {
    var eth = provider();
    set('wallet', null);
    if (!eth) { return Promise.resolve(); }
    return eth.request({ method: 'wallet_revokePermissions', params: [{ eth_accounts: {} }] })
      .then(function () {}, function () {});
  }

  window.LacreState = {
    get: get,
    set: set,
    subscribe: subscribe,
    remember: function () { return remember; },
    setRemember: setRemember,
    reconnectWallet: reconnectWallet,
    connectWallet: connectWallet,
    disconnectWallet: disconnectWallet
  };
}());
