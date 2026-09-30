/* The headers blob attest_inline takes, cut from one .eml in the browser.
 *
 * A port of tools/headers_blob.py in the Lacre repository, which cuts the
 * blob with the functions the Verifier itself runs (lacre/dkimcore.py): the
 * fields are split as parse_headers splits them, the signature is the first
 * DKIM-Signature whose d= and s= match, and each name in its h= takes the
 * lowest unused instance of that name, so a repeated name is taken bottom
 * up. The blob holds that signature and exactly those fields, in the
 * message's own order, with CRLF line endings; nothing else of the message,
 * the body included. tests/test_dkim_js.py checks it byte for byte against
 * the Python tool.
 *
 * Bytes are handled as binary strings, one character per byte, the way the
 * Python side handles bytes, so no step can re-encode a header on the way.
 * Only the finished blob is read as UTF-8, because attest_inline takes a
 * string and the calldata carries it UTF-8 encoded.
 *
 * Which signature to cut is the gateway's choice (lacre_gateway/headers.py,
 * choose): d= equal to the From domain, else aligned with it, else the
 * first; rsa-sha256 wins a tie. So a message goes to the same domain and
 * selector whether it is sent through the gateway or from a wallet.
 *
 * Loaded by the page as window.LacreDkim, and by Node as a module.
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) { module.exports = api; } else { root.LacreDkim = api; }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // attest_inline refuses a blob over this many bytes as encoded in UTF-8.
  var MAX_BLOB = 16384;
  // The Verifier's normalize() limits (tools/attest.py MAX_DOMAIN, MAX_LABEL).
  var MAX_DOMAIN = 253;
  var MAX_LABEL = 63;
  // What str.strip() removes from text decoded as latin-1.
  var PY_SPACE = '\t\n\u000b\u000c\r\u001c\u001d\u001e\u001f \u0085\u00a0';

  function BlobError(message) {
    this.name = 'BlobError';
    this.message = message;
  }
  BlobError.prototype = Object.create(Error.prototype);
  BlobError.prototype.constructor = BlobError;

  function binary(bytes) {
    var out = '';
    for (var i = 0; i < bytes.length; i += 8192) {
      out += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
    }
    return out;
  }

  function toBytes(text) {
    var out = new Uint8Array(text.length);
    for (var i = 0; i < text.length; i++) { out[i] = text.charCodeAt(i); }
    return out;
  }

  function strip(text, chars) {
    var start = 0;
    var end = text.length;
    while (start < end && chars.indexOf(text.charAt(start)) >= 0) { start++; }
    while (end > start && chars.indexOf(text.charAt(end - 1)) >= 0) { end--; }
    return text.slice(start, end);
  }

  function pyStrip(text) { return strip(text, PY_SPACE); }

  // bytes.lower(): A to Z only, whatever else a header holds.
  function bytesLower(text) {
    return text.replace(/[A-Z]+/g, function (s) { return s.toLowerCase(); });
  }

  function fieldName(name) { return bytesLower(strip(name, ' \t')); }

  // The bytes before the first empty line, whichever line ending it uses,
  // with that line's ending kept.
  function headerBlock(raw) {
    var found = [[raw.indexOf('\r\n\r\n'), 4], [raw.indexOf('\n\n'), 2]].filter(function (f) { return f[0] >= 0; });
    if (!found.length) { return raw; }
    found.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    return raw.slice(0, found[0][0] + found[0][1] / 2);
  }

  // dkimcore.parse_headers: a continuation line belongs to the field above
  // it, and folds are re-emitted as CRLF.
  function parseHeaders(block) {
    var fields = [];
    block.split('\n').forEach(function (line) {
      if (line.charAt(line.length - 1) === '\r') { line = line.slice(0, -1); }
      var first = line.charAt(0);
      if (first === ' ' || first === '\t') {
        if (fields.length) { fields[fields.length - 1][1] += '\r\n' + line; }
        return;
      }
      var at = line.indexOf(':');
      if (at >= 0) { fields.push([line.slice(0, at), line.slice(at + 1)]); }
    });
    return fields;
  }

  // dkimcore.parse_tags, over text decoded as latin-1.
  function parseTags(text) {
    var tags = {};
    text.split(';').forEach(function (spec) {
      var at = spec.indexOf('=');
      if (at < 0) { return; }
      var name = pyStrip(spec.slice(0, at));
      if (name) { tags[name] = pyStrip(spec.slice(at + 1)); }
    });
    return tags;
  }

  function has(tags, name) { return Object.prototype.hasOwnProperty.call(tags, name); }

  function tag(tags, name) { return has(tags, name) ? tags[name] : ''; }

  // The Verifier's normalize(): trimmed, lowercased, outer dots removed,
  // "" when over the limit.
  function normalize(value, limit) {
    var text = strip(pyStrip(String(value)).toLowerCase(), '.');
    return text.length <= limit ? text : '';
  }

  function signatures(fields) {
    var found = [];
    fields.forEach(function (field, index) {
      if (fieldName(field[0]) === 'dkim-signature') { found.push({ index: index, tags: parseTags(field[1]) }); }
    });
    return found;
  }

  // The domain of the first From address, lowercased, or "".
  function fromDomain(fields) {
    for (var i = 0; i < fields.length; i++) {
      if (fieldName(fields[i][0]) !== 'from') { continue; }
      var text = fields[i][1].replace(/\r\n/g, '');
      var angle = /<([^<>]*@[^<>]*)>/.exec(text);
      var bare = angle ? null : /[^\s<>(),;:"]+@[^\s<>(),;:"]+/.exec(text);
      var address = angle ? angle[1] : bare ? bare[0] : '';
      if (address.indexOf('@') < 0) { return ''; }
      return strip(pyStrip(address.slice(address.lastIndexOf('@') + 1)), '>').toLowerCase().replace(/\.+$/, '');
    }
    return '';
  }

  // headers.choose: d= equal to the From domain, else aligned, else first.
  function choose(found, sender) {
    function rank(item) {
      var d = strip(pyStrip(tag(item.tags, 'd')).toLowerCase(), '.');
      var match = 2;
      if (sender && d === sender) { match = 0; }
      else if (sender && d && (sender.slice(-d.length - 1) === '.' + d || d.slice(-sender.length - 1) === '.' + sender)) { match = 1; }
      return [match, pyStrip(tag(item.tags, 'a')) === 'rsa-sha256' ? 0 : 1];
    }
    var best = null;
    var bestRank = null;
    found.forEach(function (item) {
      var r = rank(item);
      if (!best || r[0] < bestRank[0] || (r[0] === bestRank[0] && r[1] < bestRank[1])) {
        best = item;
        bestRank = r;
      }
    });
    return best;
  }

  // headers_blob.select: the first DKIM-Signature whose d= and s= equal
  // the domain and selector, as the Verifier picks it.
  function select(fields, domain, selector) {
    domain = strip(pyStrip(domain).toLowerCase(), '.');
    var wanted = selector === undefined || selector === null ? null : strip(pyStrip(selector).toLowerCase(), '.');
    var matches = signatures(fields).filter(function (item) {
      if (tag(item.tags, 'd').toLowerCase() !== domain) { return false; }
      return wanted === null || tag(item.tags, 's').toLowerCase() === wanted;
    });
    if (!matches.length) {
      throw new BlobError('no DKIM-Signature with d=' + domain + (wanted === null ? '' : ' and s=' + wanted) + ' in this message');
    }
    if (wanted === null) {
      var selectors = {};
      matches.forEach(function (item) { selectors[tag(item.tags, 's').toLowerCase()] = true; });
      if (Object.keys(selectors).length > 1) { throw new BlobError('more than one selector signs for d=' + domain + '; pass SELECTOR'); }
    }
    return matches[0];
  }

  // headers_blob.covered: indexes of the fields the signature covers, as
  // signed_data picks them, in the message's order.
  function covered(fields, sigIndex, names) {
    var pool = {};
    fields.forEach(function (field, index) {
      if (index === sigIndex) { return; }
      var key = fieldName(field[0]);
      (pool[key] = pool[key] || []).push(index);
    });
    var chosen = [sigIndex];
    names.forEach(function (name) {
      var found = pool[pyStrip(name).toLowerCase()];
      if (found && found.length) { chosen.push(found.pop()); }
    });
    chosen.sort(function (a, b) { return a - b; });
    return chosen.filter(function (index, at) { return at === 0 || chosen[at - 1] !== index; });
  }

  // ---- canonicalization, as the Verifier hashes the blob ------------------------

  // RFC 6376 3.4.2 relaxed: lowercase name, unfold, collapse WSP runs, trim.
  function canonRelaxed(name, value) {
    var body = strip(value.replace(/\r\n/g, '').replace(/[ \t]+/g, ' '), ' ');
    return fieldName(name) + ':' + body + '\r\n';
  }

  // RFC 6376 3.4.1 simple: the field as it arrived.
  function canonSimple(name, value) { return name + ':' + value + '\r\n'; }

  // dkimcore.strip_b: empty the b= value and keep the tag.
  function stripB(value) {
    return value.split(';').map(function (part) {
      var at = part.indexOf('=');
      if (at >= 0 && bytesLower(strip(part.slice(0, at), ' \t\r\n')) === 'b') { return part.slice(0, at + 1); }
      return part;
    }).join(';');
  }

  // dkimcore.signed_data, with the header canonicalization the c= tag names.
  function signedData(fields, sigIndex, names, canon) {
    var pool = {};
    fields.forEach(function (field, index) {
      if (index === sigIndex) { return; }
      var key = fieldName(field[0]);
      (pool[key] = pool[key] || []).push(field);
    });
    var chunks = [];
    names.forEach(function (name) {
      var found = pool[pyStrip(name).toLowerCase()];
      if (found && found.length) {
        var field = found.pop();
        chunks.push(canon(field[0], field[1]));
      }
    });
    var sig = fields[sigIndex];
    // The signature closes the input with b= empty and no trailing CRLF.
    chunks.push(canon(sig[0], stripB(sig[1])).slice(0, -2));
    return chunks.join('');
  }

  function canonOf(tags) {
    var parts = (has(tags, 'c') ? tags.c : 'simple/simple').split('/');
    return {
      header: pyStrip(parts[0]).toLowerCase() || 'simple',
      body: parts.length > 1 ? pyStrip(parts[1]).toLowerCase() || 'simple' : 'simple'
    };
  }

  function utf8(bytes) {
    try {
      return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    } catch (error) {
      // A header byte that is not UTF-8 would be re-encoded on its way into
      // calldata, and the signature would no longer cover what arrives.
      throw new BlobError('the signed headers are not UTF-8 text; attest_inline cannot carry them');
    }
  }

  // headers_blob.build: the blob for domain (and selector), with what the
  // caller needs to send it and to check it.
  function build(raw, domain, selector) {
    var text = typeof raw === 'string' ? raw : binary(raw);
    var fields = parseHeaders(headerBlock(text));
    var sig = select(fields, domain, selector);
    var names = tag(sig.tags, 'h').split(':').filter(function (n) { return pyStrip(n); });
    if (!names.length) { throw new BlobError('the signature has an empty h= tag'); }
    var blob = covered(fields, sig.index, names).map(function (i) {
      return fields[i][0] + ':' + fields[i][1] + '\r\n';
    }).join('');
    var bytes = toBytes(blob);
    var decoded = utf8(bytes);
    if (bytes.length > MAX_BLOB) {
      throw new BlobError('the blob is ' + bytes.length + ' bytes, over the ' + MAX_BLOB + ' attest_inline takes');
    }
    var canon = canonOf(sig.tags);
    // The Verifier splits the blob into fields again and hashes those, so the
    // signed data is taken from the blob, not from the message.
    var inBlob = parseHeaders(blob);
    var at = signatures(inBlob).filter(function (item) {
      return tag(item.tags, 'd') === tag(sig.tags, 'd') && tag(item.tags, 's') === tag(sig.tags, 's');
    })[0].index;
    return {
      blob: decoded,
      bytes: bytes,
      tags: sig.tags,
      canon: canon,
      signed: signedData(inBlob, at, names, canon.header === 'relaxed' ? canonRelaxed : canonSimple)
    };
  }

  // Everything the wallet path needs from one .eml: the signature the
  // gateway would pick, its blob, and the three arguments of attest_inline.
  // Throws BlobError for a message that could only buy a refusal or a
  // record with valid false.
  function forInline(raw) {
    var text = typeof raw === 'string' ? raw : binary(raw);
    var fields = parseHeaders(headerBlock(text));
    var found = signatures(fields);
    if (!found.length) { throw new BlobError('no DKIM-Signature in the message'); }
    var tags = choose(found, fromDomain(fields)).tags;
    var domain = normalize(tag(tags, 'd'), MAX_DOMAIN);
    var selector = normalize(tag(tags, 's'), MAX_LABEL);
    if (!domain || !selector) { throw new BlobError('the DKIM-Signature has no usable d= or s='); }
    // Verifier v1.2 refuses these, and v1.2 checks nothing but rsa-sha256
    // under relaxed header canonicalization; anything else is a paid record
    // with valid false.
    if (has(tags, 'l')) { throw new BlobError('body length limit not supported'); }
    if (pyStrip(tag(tags, 'a')) !== 'rsa-sha256') { throw new BlobError('unsupported algorithm ' + (tag(tags, 'a') || '(none)')); }
    if (canonOf(tags).header !== 'relaxed') { throw new BlobError('unsupported header canonicalization ' + canonOf(tags).header); }
    var cut = build(text, domain, selector);
    cut.domain = domain;
    cut.selector = selector;
    // attest_inline(headers_blob, domain, selector), in that order.
    cut.args = [cut.blob, domain, selector];
    return cut;
  }

  return {
    MAX_BLOB: MAX_BLOB,
    BlobError: BlobError,
    headerBlock: headerBlock,
    parseHeaders: parseHeaders,
    parseTags: parseTags,
    fromDomain: fromDomain,
    build: build,
    forInline: forInline,
    binary: binary
  };
}));
