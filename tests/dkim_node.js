// Runs web/dkim.js under Node for tests/test_dkim_js.py.
//
//   node tests/dkim_node.js MESSAGE DOMAIN [SELECTOR]   the cut of build()
//   node tests/dkim_node.js MESSAGE --inline            the cut of forInline()
//
// Prints one JSON object: the blob and its signed data as base64, the
// blob's length and SHA-256, and for --inline the arguments attest_inline
// would be sent. An error the port raises is printed as {"error": ...}.
'use strict';

var crypto = require('crypto');
var fs = require('fs');
var path = require('path');
var dkim = require(path.join(__dirname, '..', 'web', 'dkim.js'));

var argv = process.argv.slice(2);
var raw = new Uint8Array(fs.readFileSync(argv[0]));
var out;
try {
  var cut = argv[1] === '--inline' ? dkim.forInline(raw) : dkim.build(raw, argv[1], argv[2]);
  out = {
    bytes: cut.bytes.length,
    sha256: crypto.createHash('sha256').update(cut.bytes).digest('hex'),
    blob: Buffer.from(cut.bytes).toString('base64'),
    text_matches_bytes: Buffer.from(cut.blob, 'utf8').equals(Buffer.from(cut.bytes)),
    signed: Buffer.from(cut.signed, 'latin1').toString('base64'),
    canon: cut.canon
  };
  if (cut.args) {
    out.domain = cut.domain;
    out.selector = cut.selector;
    out.args = [out.blob, cut.args[1], cut.args[2]];
    out.args_blob_is_blob = cut.args[0] === cut.blob;
  }
} catch (error) {
  out = { error: String(error && error.message || error), blob_error: error instanceof dkim.BlobError };
}
process.stdout.write(JSON.stringify(out));
