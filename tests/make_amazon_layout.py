"""Writes tests/fixtures/amazon_layout.eml, a synthetic message laid out the
way an Amazon order confirmation arrives: transport headers on top, two
DKIM-Signatures (the sender domain and the sending service), the signed
headers, then unsigned vendor headers and a multipart body.

Nothing in it is real mail. Addresses use example.org (RFC 2606), the order
number is zeros, and the b= values are placeholders that the tests replace
with signatures from a key generated at test time. bh= is the true hash of
the body below, so the body check can be tested. Run from the repo root:

    .venv/bin/python tests/make_amazon_layout.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lacre_gateway.vendor import dkimbody  # noqa: E402

BODY = (
    "------=_Part_000_1111.2222\r\n"
    "Content-Type: text/plain; charset=UTF-8\r\n"
    "Content-Transfer-Encoding: 7bit\r\n"
    "\r\n"
    "Hello,\r\n"
    "\r\n"
    "Thank you for your order. We will send a confirmation when your item ships.\r\n"
    "\r\n"
    "Order #000-0000000-0000000\r\n"
    "Arriving: Thursday, October 1\r\n"
    "\r\n"
    "------=_Part_000_1111.2222\r\n"
    "Content-Type: text/html; charset=UTF-8\r\n"
    "Content-Transfer-Encoding: 7bit\r\n"
    "\r\n"
    "<html><body><p>Thank you for your order.</p>"
    "<p>Order #000-0000000-0000000</p></body></html>\r\n"
    "\r\n"
    "------=_Part_000_1111.2222--\r\n"
).encode("ascii")


def main():
    bh = dkimbody.body_hash_b64(BODY, "simple")
    head = (
        "Return-Path: <0100000000000000-aaaaaaaa-0000-0000-0000-000000000000-000000@bounces.example.org>\r\n"
        "Delivered-To: customer@example.org\r\n"
        "Received: by 2002:a05:0000:0000:b0:000:0000:0000 with SMTP id 0000000000000;\r\n"
        "        Sun, 20 Sep 2026 05:12:03 -0700 (PDT)\r\n"
        "X-Received: by 2002:a05:0000:0000:b0:000:0000:0000 with SMTP id 0000000000000;\r\n"
        "        Sun, 20 Sep 2026 05:12:03 -0700 (PDT)\r\n"
        "ARC-Seal: i=1; a=rsa-sha256; t=1790251923; cv=none;\r\n"
        "        d=example.net; s=arc-20240605;\r\n"
        "        b=AAAAARCSEALPLACEHOLDERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\r\n"
        "ARC-Message-Signature: i=1; a=rsa-sha256; c=relaxed/relaxed; d=example.net; s=arc-20240605;\r\n"
        "        h=feedback-id:mime-version:subject:message-id:to:reply-to:from:date\r\n"
        "         :dkim-signature:dkim-signature;\r\n"
        "        bh=%(bh)s;\r\n"
        "        b=AAAAARCMSGPLACEHOLDERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\r\n"
        "ARC-Authentication-Results: i=1; mx.example.net;\r\n"
        "       dkim=pass header.i=@amazon.com header.s=synthsel2026a header.b=AAAAAAAA;\r\n"
        "       dkim=pass header.i=@amazonses.com header.s=synthsel2026b header.b=BBBBBBBB;\r\n"
        "       spf=pass smtp.mailfrom=bounces.example.org;\r\n"
        "       dmarc=pass (p=QUARANTINE sp=QUARANTINE dis=NONE) header.from=amazon.com\r\n"
        "Received: from a8-1.smtp-out.example.org (a8-1.smtp-out.example.org. [192.0.2.1])\r\n"
        "        by mx.example.net with ESMTPS id 0000000000000\r\n"
        "        for <customer@example.org>\r\n"
        "        (version=TLS1_3 cipher=TLS_AES_128_GCM_SHA256 bits=128/128);\r\n"
        "        Sun, 20 Sep 2026 05:12:03 -0700 (PDT)\r\n"
        "Received-SPF: pass (mx.example.net: domain of bounces.example.org designates 192.0.2.1 as permitted sender) client-ip=192.0.2.1;\r\n"
        "Authentication-Results: mx.example.net;\r\n"
        "       dkim=pass header.i=@amazon.com header.s=synthsel2026a header.b=AAAAAAAA;\r\n"
        "       dkim=pass header.i=@amazonses.com header.s=synthsel2026b header.b=BBBBBBBB;\r\n"
        "       spf=pass smtp.mailfrom=bounces.example.org;\r\n"
        "       dmarc=pass (p=QUARANTINE sp=QUARANTINE dis=NONE) header.from=amazon.com\r\n"
        "DKIM-Signature: v=1; a=rsa-sha256; q=dns/txt; c=relaxed/simple;\r\n"
        "\ts=synthsel2026a; d=amazon.com; t=1790251922;\r\n"
        "\th=Date:From:Reply-To:To:Message-ID:Subject:MIME-Version:Content-Type;\r\n"
        "\tbh=%(bh)s;\r\n"
        "\tb=SIGNATUREPLACEHOLDERAMAZONCOM\r\n"
        "DKIM-Signature: v=1; a=rsa-sha256; q=dns/txt; c=relaxed/simple;\r\n"
        "\ts=synthsel2026b; d=amazonses.com; t=1790251922;\r\n"
        "\th=Date:From:Reply-To:To:Message-ID:Subject:MIME-Version:Content-Type:Feedback-ID;\r\n"
        "\tbh=%(bh)s;\r\n"
        "\tb=SIGNATUREPLACEHOLDERAMAZONSES\r\n"
        "Date: Sun, 20 Sep 2026 12:12:02 +0000\r\n"
        "From: \"Amazon.com\" <auto-confirm@amazon.com>\r\n"
        "Reply-To: no-reply@example.org\r\n"
        "To: customer@example.org\r\n"
        "Message-ID: <0100000000000000-aaaaaaaa-0000-0000-0000-000000000000-000000@email.example.org>\r\n"
        "Subject: Your Amazon.com order #000-0000000-0000000\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/alternative; \r\n"
        "\tboundary=\"----=_Part_000_1111.2222\"\r\n"
        "X-AMAZON-MAIL-RELAY-TYPE: notification\r\n"
        "Bounces-to: 0100000000000000-aaaaaaaa@bounces.example.org\r\n"
        "X-AMAZON-RTE-VERSION: 2.0\r\n"
        "X-AMAZON-METADATA: CA=C0000000000000-CU=A0000000000000\r\n"
        "X-Original-MessageID: <urn.rtn.msg.00000000000000000000000000000000@1790251922000.rtn-svc-na-00000.us-east-1.example.org>\r\n"
        "Feedback-ID: ::1.us-east-1.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=:AmazonSES\r\n"
        "X-SES-Outgoing: 2026.09.20-192.0.2.1\r\n"
    ) % {"bh": bh}
    out = ROOT / "tests" / "fixtures" / "amazon_layout.eml"
    out.write_bytes(head.encode("ascii") + b"\r\n" + BODY)
    print("wrote %s, bh=%s" % (out.relative_to(ROOT), bh))


if __name__ == "__main__":
    main()
