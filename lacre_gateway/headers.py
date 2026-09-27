"""Choosing what of an email leaves this process: the signed headers only.

The Verifier fetches the headers from a URL that is public in calldata from
the moment the attest call is submitted, and anyone reading the chain can
fetch them for as long as they are served. So the blob holds only what the
chosen DKIM-Signature covers, which the signer already committed to, plus
that DKIM-Signature: Received, Return-Path, ARC, X- headers and everything
else is dropped, and the body never leaves this module. The body is used
for one thing, to check it against the signature's bh=, and is then
discarded with the rest of the upload.

Fields are read with lacre/dkimcore.parse_headers, the parser the Verifier
runs on chain, so the blob is split into fields exactly as the Verifier will
split it.
"""

import email.utils
import hashlib
from collections import Counter
from dataclasses import dataclass

from .vendor import attest, dkimbody, dkimcore

# The Verifier stores a paid, invalid "blob too large" record for a fetched
# blob over this size (contracts/verifier, MAX_BLOB).
MAX_BLOB = attest.MAX_BLOB
NO_L = "body length limit not supported"


class UnusableMail(ValueError):
    """The upload cannot become an attestation; nothing is stored or sent."""


@dataclass(frozen=True)
class Selection:
    blob: bytes
    domain: str
    selector: str
    bh: str
    body_canon: str
    # None when the signature names a body canonicalization that is neither
    # simple nor relaxed, which cannot be checked.
    body_hash_ok: bool | None
    headers_sha256: str
    kept: tuple


def split(raw):
    """(header block, body) of a message, at the first empty line."""
    ends = [i for i in (raw.find(b"\r\n\r\n"), raw.find(b"\n\n")) if i >= 0]
    if not ends:
        return raw, b""
    cut = min(ends)
    sep = 4 if raw[cut:cut + 4] == b"\r\n\r\n" else 2
    return raw[:cut], raw[cut + sep:]


def from_domain(fields):
    """The domain of the first From address, lowercased, or ""."""
    for name, value in fields:
        if dkimcore.field_name(name) == b"from":
            text = value.decode("latin-1").replace("\r\n", "")
            for _, address in email.utils.getaddresses([text]):
                if "@" in address:
                    return address.rpartition("@")[2].strip().strip(">").lower().rstrip(".")
            return ""
    return ""


def signatures(fields):
    """[(field index, tags)] for every DKIM-Signature, in order."""
    found = []
    for index, (name, value) in enumerate(fields):
        if dkimcore.field_name(name) == b"dkim-signature":
            found.append((index, dkimcore.parse_tags(value.decode("latin-1"))))
    return found


def choose(found, sender):
    """The signature to attest: d= equal to the From domain when one is,
    else one aligned with it, else the first. rsa-sha256 wins a tie because
    the Verifier checks nothing else."""
    def domain(tags):
        return tags.get("d", "").strip().lower().strip(".")

    def rank(item):
        d = domain(item[1])
        if sender and d == sender:
            match = 0
        elif sender and d and (sender.endswith("." + d) or d.endswith("." + sender)):
            match = 1
        else:
            match = 2
        return (match, 0 if item[1].get("a", "").strip() == "rsa-sha256" else 1)

    return min(found, key=rank)


def keep(fields, sig_index, names):
    """Indexes of the fields the signature covers, in their original order.

    RFC 6376 5.4.2: each h= entry takes the lowest unused instance of its
    name, so for a name listed k times the last k instances are the signed
    ones. Keeping more would trip the Verifier's "duplicate signed header"
    check, and fewer would break the signature.
    """
    wanted = Counter(names)
    kept = []
    for name, count in wanted.items():
        matches = [i for i, field in enumerate(fields)
                   if i != sig_index and dkimcore.field_name(field[0]).decode("latin-1") == name]
        kept.extend(matches[-count:] if count else [])
    return sorted(kept)


def select(raw):
    """The Selection for one .eml, or UnusableMail."""
    head, body = split(raw)
    fields = dkimcore.parse_headers(head)
    found = signatures(fields)
    if not found:
        raise UnusableMail("no DKIM-Signature in the message")
    sig_index, tags = choose(found, from_domain(fields))

    domain = attest.normalize(tags.get("d", ""), attest.MAX_DOMAIN)
    selector = attest.normalize(tags.get("s", ""), attest.MAX_LABEL)
    if not domain or not selector:
        raise UnusableMail("the DKIM-Signature has no usable d= or s=")
    if "l" in tags:
        # Verifier v1.2 refuses these; the headers would be published for a
        # call that cannot record.
        raise UnusableMail(NO_L)
    names = [n.strip().lower() for n in tags.get("h", "").split(":") if n.strip()]
    if not names:
        raise UnusableMail("the DKIM-Signature has an empty h= tag")

    indexes = keep(fields, sig_index, names)
    # The chosen signature goes first: the Verifier takes the first
    # DKIM-Signature whose d= and s= match, and h= may list another one.
    ordered = [sig_index] + indexes
    blob = b"".join(fields[i][0] + b":" + fields[i][1] + b"\r\n" for i in ordered)
    if len(blob) > MAX_BLOB:
        raise UnusableMail("the signed headers are over %d bytes" % (MAX_BLOB,))

    bh = dkimcore.unfold(tags.get("bh", ""))
    body_canon = tags.get("c", "simple/simple").partition("/")[2].strip().lower() or "simple"
    if body_canon in ("simple", "relaxed"):
        body_hash_ok = dkimbody.body_hash_b64(body, body_canon) == bh
    else:
        body_hash_ok = None
    return Selection(
        blob=blob,
        domain=domain,
        selector=selector,
        bh=bh,
        body_canon=body_canon,
        body_hash_ok=body_hash_ok,
        headers_sha256=hashlib.sha256(blob).hexdigest(),
        kept=tuple(dkimcore.field_name(fields[i][0]).decode("latin-1") for i in ordered),
    )
