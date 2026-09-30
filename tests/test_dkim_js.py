"""web/dkim.js against tools/headers_blob.py: the blob a wallet signs.

attest_inline takes the blob as its first argument and the Verifier hashes
it as sent, so the browser's cut has to be the Python tool's, byte for byte.
Each case runs the port under Node (tests/dkim_node.js) and the tool as a
command, from the Lacre checkout in support.LACRE_SOURCE.

The real message the port is held to, amazon-shipped.eml, is gitignored in
the Lacre repository and never enters this one. It is looked for in
LACRE_SAMPLE_EML, then under LACRE_SOURCE, then in a sibling ../lacre
checkout; the test fails, rather than skips, when none has it.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import support
from lacre_gateway import headers
from lacre_gateway.vendor import dkimcore

HARNESS = Path(__file__).resolve().parent / "dkim_node.js"
FIXTURE = support.FIXTURES / "amazon_layout.eml"
SAMPLE = Path("experiments") / "dkim-probe" / "samples" / "amazon-shipped.eml"
# What tools/headers_blob.py amazon-shipped.eml amazon.com --digest prints.
SAMPLE_DIGEST = (810, "e312657e687e473cce1bec47abd5ee57dde5427d9da321497f7241e3d8276e53")


def node():
    found = shutil.which("node")
    if not found:
        pytest.fail("node is not installed; web/dkim.js cannot be checked")
    return found


def tool():
    script = support.LACRE_SOURCE / "tools" / "headers_blob.py"
    if not script.is_file():
        pytest.fail("%s is missing: move vendor/lacre to a commit that has it, or point "
                    "LACRE_SOURCE at one" % (script,))
    return script


def sample():
    for candidate in (os.environ.get("LACRE_SAMPLE_EML"), support.LACRE_SOURCE / SAMPLE,
                      support.ROOT.parent / "lacre" / SAMPLE):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    pytest.fail("amazon-shipped.eml not found; set LACRE_SAMPLE_EML to its path")


def js(message, *args):
    answer = subprocess.run([node(), str(HARNESS), str(message)] + list(args),
                            capture_output=True, check=True)
    return json.loads(answer.stdout)


def python_blob(message, domain, selector=None):
    args = [sys.executable, str(tool()), str(message), domain] + ([selector] if selector else [])
    return subprocess.run(args, capture_output=True, check=True).stdout


def python_digest(message, domain, selector=None):
    args = [sys.executable, str(tool()), str(message), domain] + ([selector] if selector else [])
    out = subprocess.run(args + ["--digest"], capture_output=True, check=True, text=True).stdout
    lines = dict(line.split(":", 1) for line in out.splitlines())
    return int(lines["bytes        "]), lines["sha256       "].strip()


def python_error(message, domain, selector=None):
    args = [sys.executable, str(tool()), str(message), domain] + ([selector] if selector else [])
    answer = subprocess.run(args, capture_output=True, text=True)
    assert answer.returncode != 0
    return answer.stderr.strip()


def message(tmp_path, head, body=b"Hello.\r\n", name="m.eml"):
    path = tmp_path / name
    path.write_bytes(head.replace(b"\n", b"\r\n") + b"\r\n" + body)
    return path


SIGNATURE = (b"DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/simple; d=example.org;\n"
             b"\ts=sel1; t=1790000000; h=from:to:subject:date;\n"
             b"\tbh=47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=; b=AAAA\n")


# ---- the real message: the mandatory case ---------------------------------------

def test_the_blob_is_the_one_headers_blob_digests():
    eml = sample()
    assert python_digest(eml, "amazon.com") == SAMPLE_DIGEST
    cut = js(eml, "amazon.com")
    assert (cut["bytes"], cut["sha256"]) == SAMPLE_DIGEST
    assert base64.b64decode(cut["blob"]) == python_blob(eml, "amazon.com")
    assert cut["text_matches_bytes"] is True


def test_the_wallet_path_sends_that_blob_with_the_gateways_choice():
    eml = sample()
    cut = js(eml, "--inline")
    chosen = headers.select(eml.read_bytes())
    assert (cut["domain"], cut["selector"]) == (chosen.domain, chosen.selector)
    assert (cut["bytes"], cut["sha256"]) == SAMPLE_DIGEST
    assert base64.b64decode(cut["args"][0]) == python_blob(eml, chosen.domain, chosen.selector)
    assert cut["args"][1:] == ["amazon.com", "yg4mwqurec7fkhzutopddd3ytuaqrvuz"]
    assert cut["args_blob_is_blob"] is True


def test_the_signed_data_is_what_the_verifier_hashes():
    eml = sample()
    cut = js(eml, "--inline")
    blob = base64.b64decode(cut["blob"])
    fields = dkimcore.parse_headers(blob)
    index, tags = dkimcore.find_signature(fields)
    names = [n.strip() for n in tags["h"].split(":") if n.strip()]
    assert cut["canon"] == {"header": "relaxed", "body": "simple"}
    assert base64.b64decode(cut["signed"]) == dkimcore.signed_data(fields, index, names)


# ---- the committed synthetic message ------------------------------------------------

def test_the_fixture_cuts_alike():
    chosen = headers.select(FIXTURE.read_bytes())
    cut = js(FIXTURE, "--inline")
    assert (cut["domain"], cut["selector"]) == (chosen.domain, chosen.selector)
    expected = python_blob(FIXTURE, chosen.domain, chosen.selector)
    assert base64.b64decode(cut["blob"]) == expected
    assert (cut["bytes"], cut["sha256"]) == (len(expected), hashlib.sha256(expected).hexdigest())
    # The same fields as the gateway's blob, which puts the signature first.
    assert sorted(dkimcore.parse_headers(expected)) == sorted(dkimcore.parse_headers(chosen.blob))


# ---- the rules of the cut -------------------------------------------------------------

def test_a_repeated_name_is_taken_bottom_up(tmp_path):
    eml = message(tmp_path, b"Received: x\nSubject: first\nFrom: a@example.org\nTo: b@example.net\n"
                  b"Subject: second\nDate: Thu, 1 Jan 2026 00:00:00 +0000\n" + SIGNATURE)
    cut = js(eml, "example.org")
    blob = base64.b64decode(cut["blob"])
    assert blob == python_blob(eml, "example.org")
    assert b"Subject: second" in blob and b"Subject: first" not in blob
    assert b"Received" not in blob


def test_folded_and_lf_only_messages_cut_alike(tmp_path):
    head = (b"From: a@example.org\nTo: b@example.net,\n c@example.net\nSubject: folded\n\tline\n"
            b"Date: Thu, 1 Jan 2026 00:00:00 +0000\n" + SIGNATURE)
    crlf = message(tmp_path, head)
    lf = tmp_path / "lf.eml"
    lf.write_bytes(head + b"\nbody\n")
    for eml in (crlf, lf):
        cut = js(eml, "example.org", "sel1")
        assert base64.b64decode(cut["blob"]) == python_blob(eml, "example.org", "sel1")


def test_utf8_headers_are_carried_and_other_bytes_refused(tmp_path):
    ok = message(tmp_path, "Subject: caf\u00e9\nFrom: a@example.org\nTo: b@example.net\n"
                 "Date: x\n".encode("utf-8") + SIGNATURE, name="ok.eml")
    cut = js(ok, "example.org")
    assert base64.b64decode(cut["blob"]) == python_blob(ok, "example.org")
    assert cut["text_matches_bytes"] is True
    bad = message(tmp_path, b"Subject: caf\xe9\nFrom: a@example.org\nTo: b@example.net\nDate: x\n"
                  + SIGNATURE, name="bad.eml")
    refused = js(bad, "example.org")
    assert refused["blob_error"] is True
    assert refused["error"] == python_error(bad, "example.org")


def test_a_domain_with_two_selectors_needs_one(tmp_path):
    other = SIGNATURE.replace(b"s=sel1", b"s=sel2")
    eml = message(tmp_path, b"From: a@example.org\nTo: b@example.net\nSubject: s\nDate: x\n"
                  + SIGNATURE + other)
    refused = js(eml, "example.org")
    assert refused["error"] == python_error(eml, "example.org")
    for selector in ("sel1", "sel2"):
        cut = js(eml, "example.org", selector)
        assert base64.b64decode(cut["blob"]) == python_blob(eml, "example.org", selector)


def test_an_oversized_blob_is_refused(tmp_path):
    eml = message(tmp_path, b"From: a@example.org\nTo: b@example.net\nSubject: " + b"x" * 17000
                  + b"\nDate: x\n" + SIGNATURE)
    refused = js(eml, "example.org")
    assert refused["error"] == python_error(eml, "example.org")


@pytest.mark.parametrize("change,why", [
    ((b"c=relaxed/simple", b"c=simple/simple"), "unsupported header canonicalization simple"),
    ((b"t=1790000000;", b"t=1790000000; l=10;"), "body length limit not supported"),
    ((b"a=rsa-sha256", b"a=rsa-sha1"), "unsupported algorithm rsa-sha1"),
])
def test_the_wallet_path_refuses_what_could_only_buy_a_refusal(tmp_path, change, why):
    eml = message(tmp_path, b"From: a@example.org\nTo: b@example.net\nSubject: s\nDate: x\n"
                  + SIGNATURE.replace(*change))
    assert js(eml, "--inline")["error"] == why


def test_the_wallet_path_picks_the_from_domain_signature(tmp_path):
    esp = SIGNATURE.replace(b"d=example.org", b"d=mailer.test").replace(b"s=sel1", b"s=esp")
    eml = message(tmp_path, b"From: Shop <shop@example.org>\nTo: b@example.net\nSubject: s\nDate: x\n"
                  + esp + SIGNATURE)
    cut = js(eml, "--inline")
    chosen = headers.select(eml.read_bytes())
    assert (cut["domain"], cut["selector"]) == (chosen.domain, chosen.selector) == ("example.org", "sel1")
    assert base64.b64decode(cut["blob"]) == python_blob(eml, "example.org", "sel1")
