"""Header selection against the Amazon layout (tests/fixtures/amazon_layout.eml).

The signatures are made at test time with a key generated here, over the
Verifier's own signed_data, and checked after selection with the Verifier's
own verify_headers: what the gateway serves has to verify exactly as the
full message does.
"""

import pytest

import support
from lacre_gateway import headers
from lacre_gateway.vendor import dkimcore

SIGNED = ["dkim-signature", "date", "from", "reply-to", "to", "message-id", "subject",
          "mime-version", "content-type"]


@pytest.fixture(scope="module")
def key():
    return support.rsa_key()


@pytest.fixture(scope="module")
def signed(key):
    raw = support.sign_eml(support.amazon_eml(), "amazon.com", key)
    return support.sign_eml(raw, "amazonses.com", key)


def test_the_signature_whose_d_is_the_from_domain_is_chosen(signed):
    chosen = headers.select(signed)
    assert (chosen.domain, chosen.selector) == ("amazon.com", "synthsel2026a")


def test_only_the_signed_headers_and_their_signature_are_kept(signed):
    chosen = headers.select(signed)
    assert list(chosen.kept) == SIGNED
    blob = chosen.blob.lower()
    for dropped in (b"received:", b"return-path:", b"delivered-to:", b"arc-", b"x-received:",
                    b"authentication-results:", b"received-spf:", b"x-amazon", b"bounces-to:",
                    b"x-original-messageid:", b"feedback-id:", b"x-ses-outgoing:",
                    b"d=amazonses.com"):
        assert dropped not in blob, dropped


def test_the_body_is_not_in_the_blob(signed):
    blob = headers.select(signed).blob
    assert b"Thank you for your order" not in blob
    assert b"Arriving" not in blob
    assert b"<html>" not in blob


def test_the_kept_headers_verify_like_the_whole_message(signed, key):
    n, e = support.public_numbers(key)
    head = signed.partition(b"\r\n\r\n")[0]
    fields = dkimcore.parse_headers(head)
    index = next(i for i, (name, value) in enumerate(fields)
                 if b"d=amazon.com" in value and dkimcore.field_name(name) == b"dkim-signature")
    whole = b"".join(n_ + b":" + v + b"\r\n" for n_, v in [fields[index]] + fields[:index]
                     + fields[index + 1:])
    assert dkimcore.verify_headers(whole, n, e)[0]
    ok, info = dkimcore.verify_headers(headers.select(signed).blob, n, e)
    assert ok, info["reason"]
    assert info["reason"] == "header signature verified"


def test_no_signed_name_appears_more_often_than_h_lists_it(signed):
    fields = dkimcore.parse_headers(headers.select(signed).blob)
    names = [dkimcore.field_name(f[0]).decode() for f in fields]
    for name in set(names) - {"dkim-signature"}:
        assert names.count(name) == 1


def test_the_body_hash_is_checked_and_not_stored(signed):
    chosen = headers.select(signed)
    assert chosen.body_hash_ok is True
    assert chosen.body_canon == "simple"
    tampered = signed.replace(b"Thursday, October 1", b"Friday, October 2")
    assert headers.select(tampered).body_hash_ok is False
    assert not hasattr(chosen, "body")


def test_a_repeated_header_keeps_only_the_instance_the_signature_covers(key):
    raw = support.amazon_eml().replace(
        b"Subject: Your Amazon.com order",
        b"Subject: an earlier subject nobody signed\r\nSubject: Your Amazon.com order", 1)
    raw = support.sign_eml(raw, "amazon.com", key)
    chosen = headers.select(raw)
    assert b"nobody signed" not in chosen.blob
    assert chosen.blob.count(b"\r\nSubject:") == 1
    n, e = support.public_numbers(key)
    assert dkimcore.verify_headers(chosen.blob, n, e)[0]


def test_an_lf_only_copy_still_verifies(signed, key):
    lf = signed.replace(b"\r\n", b"\n")
    n, e = support.public_numbers(key)
    assert dkimcore.verify_headers(headers.select(lf).blob, n, e)[0]


def test_without_a_matching_from_the_aligned_signature_wins(key):
    raw = support.amazon_eml().replace(b"<auto-confirm@amazon.com>",
                                       b"<ship-confirm@mail.amazonses.com>")
    assert headers.select(raw).domain == "amazonses.com"


def test_without_a_from_the_first_signature_is_used():
    raw = support.amazon_eml().replace(b"From: ", b"X-Was-From: ")
    assert headers.select(raw).domain == "amazon.com"


def test_no_signature_is_unusable():
    raw = support.amazon_eml().replace(b"DKIM-Signature:", b"X-Old-Signature:")
    with pytest.raises(headers.UnusableMail, match="no DKIM-Signature"):
        headers.select(raw)


def test_an_l_tag_is_unusable():
    raw = support.amazon_eml().replace(b"c=relaxed/simple;\r\n\ts=synthsel2026a",
                                       b"c=relaxed/simple; l=100;\r\n\ts=synthsel2026a")
    with pytest.raises(headers.UnusableMail, match="body length limit"):
        headers.select(raw)


def test_signed_headers_over_the_verifier_limit_are_unusable():
    raw = support.amazon_eml().replace(b"Subject: Your", b"Subject: " + b"x" * 17000 + b" Your")
    with pytest.raises(headers.UnusableMail, match="over 16384"):
        headers.select(raw)
