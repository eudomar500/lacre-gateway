/* Lacre wallet path: attest_inline from the visitor's own wallet, on Bradbury.
 *
 * No account and no gateway job. The page cuts the headers blob itself
 * (dkim.js), this file resolves the Verifier through the Router, reads its
 * fee, has the wallet sign attest_inline with that fee as value, and follows
 * the transaction on the Bradbury RPC until it is FINALIZED. The record is
 * then found the way tools/attest.py finds it (docs/direct-use.md, "The
 * record is the proof"): records_of(sender) before and after, the new id is
 * the record, read with get(id) at LATEST_FINAL and kept only if the sender
 * is its requester.
 *
 * genlayer-js 1.2.0, the SDK major that speaks to Bradbury, is served from
 * /static like every other file and loaded only when a wallet is used. The
 * wallet signs; every read, the gas estimate and the receipt go to the RPC.
 *
 * Nothing here touches storage: what must survive a reload, the address and
 * the transaction in flight, is state.js's to keep.
 */
(function () {
  'use strict';

  var SDK_URL = '/static/genlayer-js-1.2.0.min.js';
  var CHAIN_ID = 4221;
  var CHAIN_HEX = '0x107d';
  var CHAIN_NAME = 'GenLayer Bradbury testnet';
  var RPC = 'https://rpc-bradbury.genlayer.com';
  // What the wallet is given when it adds the chain: the gateway's /rpc,
  // which forwards to RPC with every request id made an integer. Reads and
  // estimates here still go to RPC itself.
  var WALLET_RPC = 'https://lacre.in-sidr.xyz/rpc';
  var EXPLORER = 'https://explorer-bradbury.genlayer.com';
  var FAUCET = 'https://testnet-faucet.genlayer.foundation';
  var CURRENCY = { name: 'GEN Token', symbol: 'GEN', decimals: 18 };
  // 0.002 GEN in wei: less than this and a send cannot pay its gas.
  var GAS_FLOOR = 2000000000000000n;
  var FINAL = 'latest-final';
  var NONFINAL = 'latest-nonfinal';

  // tools/txstate.py: Bradbury's numbering of the stored consensus state.
  var STATUS = ['UNINITIALIZED', 'PENDING', 'PROPOSING', 'COMMITTING', 'REVEALING',
    'ACCEPTED', 'UNDETERMINED', 'FINALIZED', 'CANCELED', 'APPEAL_REVEALING', 'APPEAL_COMMITTING',
    'READY_TO_FINALIZE', 'VALIDATORS_TIMEOUT', 'LEADER_TIMEOUT'];
  var RESULT = ['IDLE', 'AGREE', 'DISAGREE', 'TIMEOUT', 'DETERMINISTIC_VIOLATION', 'NO_MAJORITY',
    'MAJORITY_AGREE', 'MAJORITY_DISAGREE'];
  var EXECUTION = ['NOT_VOTED', 'FINISHED_WITH_RETURN', 'FINISHED_WITH_ERROR'];
  // Stored states in which a transaction will not run again unless appealed.
  var DECIDED = ['ACCEPTED', 'UNDETERMINED', 'FINALIZED', 'CANCELED', 'LEADER_TIMEOUT', 'VALIDATORS_TIMEOUT'];
  // Stored states no later event changes.
  var ENDED = ['FINALIZED', 'CANCELED'];
  var AGREED = ['AGREE', 'MAJORITY_AGREE'];

  function named(table, number) {
    var n = Number(number);
    return n >= 0 && n < table.length ? table[n] : 'UNKNOWN_' + n;
  }

  function provider() { return window.ethereum && window.ethereum.request ? window.ethereum : null; }

  // MetaMask sends JSON-RPC ids as strings, which the Bradbury RPC refuses
  // (-32700, Request.id of type int). Added from here, the chain points at
  // WALLET_RPC, which renumbers them; a MetaMask that already had the chain
  // on RPC keeps it, and is told how to change it if a send fails. Rabby
  // also sets isMetaMask, and works on either.
  function isMetaMask() {
    var eth = provider();
    return !!eth && !!eth.isMetaMask && !eth.isRabby;
  }

  // ---- genlayer-js, on demand ---------------------------------------------------------

  var sdk = null;

  function loadSdk() {
    if (window.genlayer) { return Promise.resolve(window.genlayer); }
    if (sdk) { return sdk; }
    sdk = new Promise(function (resolve, reject) {
      var script = document.createElement('script');
      script.src = SDK_URL;
      script.async = true;
      script.onload = function () {
        if (window.genlayer) { resolve(window.genlayer); } else { reject(new Error('genlayer-js did not load')); }
      };
      script.onerror = function () {
        sdk = null;
        reject(new Error('genlayer-js could not be loaded'));
      };
      document.head.appendChild(script);
    });
    return sdk;
  }

  var reader = null;

  function readClient() {
    return loadSdk().then(function (gl) {
      if (!reader) { reader = gl.createClient({ chain: gl.testnetBradbury }); }
      return reader;
    });
  }

  function read(address, method, args, variant) {
    return readClient().then(function (client) {
      return client.readContract({
        address: address, functionName: method, args: args || [], transactionHashVariant: variant
      });
    });
  }

  function rpc(method, params) {
    return readClient().then(function (client) { return client.request({ method: method, params: params }); });
  }

  // ---- the chain in the wallet ------------------------------------------------------------

  // Whether a failed wallet_switchEthereumChain means "I do not have that
  // chain": 4902, MetaMask's nested 4902, or Rabby's -32603 whose message
  // names an unrecognized chain. Anything else, 4001 included, is rethrown.
  function isUnknownChain(error) {
    var e = error || {};
    if (e.code === 4902) { return true; }
    if (e.data && e.data.originalError && e.data.originalError.code === 4902) { return true; }
    if (e.code === -32603) {
      var message = String(e.message || (e.data && e.data.originalError && e.data.originalError.message) || '');
      return /unrecognized chain/i.test(message);
    }
    return false;
  }

  function addChainParams() {
    return {
      chainId: CHAIN_HEX, chainName: CHAIN_NAME, rpcUrls: [WALLET_RPC],
      nativeCurrency: CURRENCY, blockExplorerUrls: [EXPLORER]
    };
  }

  // Puts the wallet on Bradbury: switch, and when it does not know the
  // chain, add it. A wallet that already has the chain, on whichever RPC,
  // is only switched. A wallet that adds a chain should select it (EIP-3085)
  // but not every one does, so the result is read back and switched once
  // more if needed.
  function ensureChain() {
    var eth = provider();
    if (!eth) { return Promise.reject(new Error('No wallet found in this browser')); }
    function switchChain() {
      return eth.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: CHAIN_HEX }] });
    }
    return eth.request({ method: 'eth_chainId' }).then(function (current) {
      if (String(current).toLowerCase() === CHAIN_HEX) { return null; }
      return switchChain().then(function () { return null; }, function (error) {
        if (!isUnknownChain(error)) { throw error; }
        return eth.request({ method: 'wallet_addEthereumChain', params: [addChainParams()] })
          .then(function () { return eth.request({ method: 'eth_chainId' }); })
          .then(function (after) { return String(after).toLowerCase() === CHAIN_HEX ? null : switchChain(); });
      });
    });
  }

  function onBradbury() {
    var eth = provider();
    if (!eth) { return Promise.resolve(false); }
    return eth.request({ method: 'eth_chainId' }).then(function (id) {
      return String(id).toLowerCase() === CHAIN_HEX;
    }, function () { return false; });
  }

  // GEN on Bradbury, read from the RPC so it is Bradbury's whatever chain
  // the wallet shows. A string with at most four decimals.
  function balance(address) {
    return Promise.all([loadSdk(), rpc('eth_getBalance', [address, 'latest'])]).then(function (got) {
      var text = got[0].formatEther(BigInt(got[1]));
      var at = text.indexOf('.');
      return at < 0 ? text : text.slice(0, at + 5).replace(/\.?0+$/, '');
    });
  }

  // Whether the address holds the GEN floor for gas on Bradbury, read from
  // the RPC before the wallet is opened, so a wallet without gas is told
  // why instead of answering with a bare internal error.
  function hasGas(address) {
    return rpc('eth_getBalance', [address, 'latest']).then(function (wei) {
      return BigInt(wei) >= GAS_FLOOR;
    });
  }

  // The wallet's -32603, or a message that only says "internal error":
  // looked for down the cause chain, since the SDK wraps what the wallet said.
  function isInternalError(error) {
    for (var e = error, depth = 0; e && typeof e === 'object' && depth < 8; e = e.cause, depth += 1) {
      if (e.code === -32603) { return true; }
      if (e.data && e.data.originalError && e.data.originalError.code === -32603) { return true; }
      if (/internal error/i.test(String(e.shortMessage || '') + ' ' + String(e.message || ''))) { return true; }
    }
    return /internal error/i.test(String(error || ''));
  }

  // The Bradbury RPC refusing a string id: -32700, or its words for it,
  // looked for down the cause chain as in isInternalError.
  var ID_REFUSED = /Request\.id of type int|-32700/i;

  function isRpcIdError(error) {
    for (var e = error, depth = 0; e && typeof e === 'object' && depth < 8; e = e.cause, depth += 1) {
      if (e.code === -32700) { return true; }
      if (e.data && e.data.originalError && e.data.originalError.code === -32700) { return true; }
      if (ID_REFUSED.test(String(e.shortMessage || '') + ' ' + String(e.message || ''))) { return true; }
    }
    return ID_REFUSED.test(String(error || ''));
  }

  // ---- before sending: the Verifier's own refusals ------------------------------------------

  // tools/attest.py key_refusal: the refusal cached_key() gives, or null.
  function keyRefusal(status) {
    if (!status || typeof status !== 'object') { return 'the KeyCache could not be read'; }
    var state = String(status.state || '');
    if (state !== 'active') {
      return ['pending', 'rotated', 'retired'].indexOf(state) >= 0 ? 'key ' + state : 'key not registered';
    }
    var n = /^(0x)?[0-9a-fA-F]+$/.test(String(status.n_hex || '')) ? BigInt('0x' + String(status.n_hex).replace(/^0x/, '')) : 0n;
    var e = /^\d+$/.test(String(status.e || '').trim()) ? BigInt(String(status.e).trim()) : 0n;
    return n > 1n && e > 2n ? null : 'key not registered';
  }

  function list(value) { return Array.isArray(value) ? value.map(String) : []; }

  // Everything a send needs, read now: the Verifier and KeyCache the Router
  // names at LATEST_FINAL, the fee at LATEST_NONFINAL (the one in force when
  // the call executes, as tools/attest.py reads it), the key's state at
  // LATEST_FINAL (the one the Verifier reads), and the sender's records and
  // last refusal before the call, to tell the new record from the old.
  // Rejects with the reason the Verifier would refuse, so no fee is spent
  // on a refusal that can be seen coming.
  function prepare(router, from, cut) {
    return Promise.all([read(router, 'resolve', ['verifier'], FINAL), read(router, 'resolve', ['keycache'], FINAL)])
      .then(function (names) {
        var verifier = String(names[0] || '');
        var keycache = String(names[1] || '');
        if (!verifier) { throw new Error('The Router names no Verifier now'); }
        if (!keycache) { throw new Error('The Router names no KeyCache now'); }
        return Promise.all([
          read(verifier, 'fee', [], NONFINAL),
          read(keycache, 'key_status', [cut.domain, cut.selector], FINAL),
          read(verifier, 'records_of', [from], FINAL),
          read(verifier, 'last_refusal', [from], FINAL)
        ]).then(function (got) {
          var refused = keyRefusal(got[1]);
          if (refused) {
            throw new Error('The Verifier would refuse this call: ' + refused + ' for ' + cut.domain + ' / ' + cut.selector +
              '. The gateway registers new sender keys; a key is usable 24 h after it is first seen.');
          }
          return {
            from: from, verifier: verifier, fee: String(got[0]),
            domain: cut.domain, selector: cut.selector,
            before: list(got[2]).length, refusal: String(got[3] || '')
          };
        });
      });
  }

  // ---- the send ------------------------------------------------------------------------------

  // The EVM hash is handed to onSent the moment the wallet returns it, before
  // the receipt is mined, so a reload in that window can still find the
  // transaction; genlayer-js only returns the consensus tx id after it.
  function send(call, cut, onSent) {
    var eth = provider();
    if (!eth) { return Promise.reject(new Error('No wallet found in this browser')); }
    var tap = {
      request: function (args) {
        return eth.request(args).then(function (answer) {
          if (args && args.method === 'eth_sendTransaction' && typeof answer === 'string') { onSent({ evm: answer }); }
          return answer;
        });
      },
      on: eth.on ? eth.on.bind(eth) : undefined,
      removeListener: eth.removeListener ? eth.removeListener.bind(eth) : undefined
    };
    return Promise.all([loadSdk(), ensureChain()]).then(function (got) {
      var gl = got[0];
      var client = gl.createClient({ chain: gl.testnetBradbury, account: call.from, provider: tap });
      return client.writeContract({
        address: call.verifier,
        functionName: 'attest_inline',
        args: cut.args,
        value: BigInt(call.fee)
      });
    }).then(function (txId) {
      onSent({ tx: String(txId) });
      return String(txId);
    });
  }

  // ---- following it --------------------------------------------------------------------------

  // The consensus tx id of an EVM hash, from the NewTransaction event in its
  // receipt; null while the receipt is not mined.
  function txIdOf(evm) {
    return Promise.all([loadSdk(), rpc('eth_getTransactionReceipt', [evm])]).then(function (got) {
      var gl = got[0];
      var receipt = got[1];
      if (!receipt) { return null; }
      if (receipt.status === '0x0') { throw new Error('The transaction to the consensus contract reverted'); }
      var main = gl.testnetBradbury.consensusMainContract;
      var logs = (receipt.logs || []).filter(function (log) {
        return String(log.address).toLowerCase() === String(main.address).toLowerCase();
      });
      var events = gl.parseEventLogs({ abi: main.abi, eventName: 'NewTransaction', logs: logs });
      return events.length ? String(events[0].args.txId) : null;
    });
  }

  // The stored state from getTransactionAllData, which takes no timestamp:
  // the timestamped views report a queued transaction as CANCELED after
  // 1800 s while it can still run (tools/txstate.py).
  function stored(txId) {
    return loadSdk().then(function (gl) {
      var data = gl.testnetBradbury.consensusDataContract;
      var input = gl.encodeFunctionData({ abi: data.abi, functionName: 'getTransactionAllData', args: [txId] });
      return rpc('eth_call', [{ to: data.address, data: input }, 'latest']).then(function (raw) {
        var out = gl.decodeFunctionResult({ abi: data.abi, functionName: 'getTransactionAllData', data: raw });
        var tx = out[0];
        return {
          status: named(STATUS, tx.status),
          result: named(RESULT, tx.result),
          execution: named(EXECUTION, tx.txExecutionResult)
        };
      });
    });
  }

  function executed(state) {
    return (state.status === 'ACCEPTED' || state.status === 'FINALIZED') &&
      AGREED.indexOf(state.result) >= 0 && state.execution === 'FINISHED_WITH_RETURN';
  }

  // tools/attest.py ours(): a record this call could have written.
  function ours(record, call) {
    return !!record && typeof record === 'object' &&
      String(record.requester || '').toLowerCase() === call.from.toLowerCase() &&
      record.domain === call.domain && record.selector === call.selector &&
      record.source === 'inline' && String(record.fee_paid) === String(call.fee);
  }

  // What a FINALIZED call left, read at LATEST_FINAL:
  // {kind: 'recorded', id, record} | {kind: 'refused', reason} | {kind: 'nothing', why}.
  function outcome(call, state) {
    if (!executed(state)) {
      return Promise.resolve({ kind: 'nothing', why: 'FINALIZED with result ' + state.result + ' and execution ' +
        state.execution + ': nothing was written, and the fee is refunded' });
    }
    return Promise.all([read(call.verifier, 'records_of', [call.from], FINAL),
      read(call.verifier, 'last_refusal', [call.from], FINAL)]).then(function (got) {
      var fresh = list(got[0]).slice(call.before);
      var last = String(got[1] || '');
      return Promise.all(fresh.map(function (id) {
        return read(call.verifier, 'get', [id], FINAL).then(function (record) { return { id: id, record: record }; });
      })).then(function (found) {
        var mine = found.filter(function (f) { return ours(f.record, call); });
        if (mine.length) { return { kind: 'recorded', id: mine[mine.length - 1].id, record: mine[mine.length - 1].record }; }
        // Every refusal writes last_refusal, so a changed one is this call's.
        if (last && last !== call.refusal) { return { kind: 'refused', reason: last }; }
        return { kind: 'nothing', why: 'the call FINALIZED but no record it wrote can be read at LATEST_FINAL' };
      });
    });
  }

  function explorerTx(txId) { return EXPLORER + '/tx/' + txId; }

  window.LacreWallet = {
    CHAIN_ID: CHAIN_ID,
    WALLET_RPC: WALLET_RPC,
    EXPLORER: EXPLORER,
    FAUCET: FAUCET,
    DECIDED: DECIDED,
    ENDED: ENDED,
    provider: provider,
    isMetaMask: isMetaMask,
    loadSdk: loadSdk,
    ensureChain: ensureChain,
    onBradbury: onBradbury,
    isUnknownChain: isUnknownChain,
    balance: balance,
    hasGas: hasGas,
    isInternalError: isInternalError,
    isRpcIdError: isRpcIdError,
    prepare: prepare,
    send: send,
    txIdOf: txIdOf,
    stored: stored,
    outcome: outcome,
    explorerTx: explorerTx
  };
}());
