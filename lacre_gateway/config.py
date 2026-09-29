"""Configuration, read from the environment only.

There is no config file and no default for anything that identifies a
deployment: the Router address and the API keys have to be given. The
signing key is never in the environment; only the path of the file that
holds it is (see signer.py and deploy/lacre-gateway.service).
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
# At least two labels: Email Routing works on a zone, never a bare host name.
DOMAIN = re.compile(r"^%s(\.%s)+$" % (_LABEL, _LABEL))
# Shorter than this and the HMAC over inbound mail is only as strong as a
# guessable password.
MIN_INBOUND_SECRET = 32
EXTRACT_MODES = ("none", "patterns", "llm", "auto")


class ConfigError(ValueError):
    pass


def _int(env, name, default):
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError("%s must be an integer" % (name,))
    if value < 0:
        raise ConfigError("%s cannot be negative" % (name,))
    return value


def body_base_from(blob_base_url):
    """LACRE_BODY_BASE_URL's default: the blob base with a last /h made /b.

    "" when the blob base does not end in /h, so there is nothing to derive.
    """
    base = blob_base_url.rstrip("/")
    return base[:-2] + "/b" if base.endswith("/h") else ""


@dataclass(frozen=True)
class Settings:
    network: str
    router: str
    api_keys: tuple = field(repr=False)
    blob_base_url: str
    data_dir: Path
    signing_key_file: Path | None = None
    # Seconds between worker passes.
    poll_s: int = 30
    # How long one attestation may wait for FINALIZED before the job stops.
    # tools/attest.py uses four hours; the stored status keeps being read
    # until then, and nothing is ever sent again while it is undecided.
    final_bound_s: int = 4 * 3600
    # Transactions one attestation may take (tools/attest.py --attempts).
    max_attempts: int = 3
    # Sends that failed before reaching the chain, tolerated per job.
    max_send_failures: int = 3
    # The KeyCache quarantine (QUARANTINE in contracts/keycache), plus a
    # margin so confirm_key is not sent against a runner clock a little
    # behind ours, which would revert with "the quarantine has not passed".
    key_quarantine_s: int = 24 * 3600
    confirm_margin_s: int = 600
    # A confirm_key that found a resolver down leaves the key pending; it is
    # tried again after this long.
    confirm_retry_s: int = 3600
    # /health reports the worker dead after this long without a pass.
    worker_stale_s: int = 300
    max_eml_bytes: int = 10 * 1024 * 1024
    # What POST /attest does when the request names no extract mode.
    extract_default: str = "auto"
    # Where /b/{name}.bin is reachable from the internet; "" means derived
    # from blob_base_url (body_url below).
    body_base_url: str = ""
    # The domain mailbox addresses live under: lacre-<id>@<mail_domain>.
    mail_domain: str = "in-sidr.xyz"
    # The key the Worker signs inbound mail with. "" turns POST /inbound
    # off: nothing can be delivered without it.
    inbound_secret: str = field(default="", repr=False)
    # What an account created for a LACRE_API_KEYS entry starts with. 0 makes
    # those accounts unlimited: a testnet convenience, to be set to a real
    # number (or the keys moved to admin-created accounts) before mainnet.
    bootstrap_credits: int = 0
    # Credits held when a job is created and charged when a step writes a
    # record: one Verifier record, one extraction record.
    price_attest: int = 1
    price_extract: int = 1
    # The X-Admin-Token of /admin/*. "" turns the admin API off.
    admin_token: str = field(default="", repr=False)

    @property
    def body_url(self):
        return self.body_base_url or body_base_from(self.blob_base_url)

    @property
    def db_path(self):
        return self.data_dir / "gateway.sqlite3"

    @property
    def blob_dir(self):
        return self.data_dir / "blobs"

    @property
    def body_dir(self):
        return self.data_dir / "bodies"


def load(env=None):
    env = os.environ if env is None else env
    network = env.get("LACRE_NETWORK", "bradbury").strip()
    router = env.get("LACRE_ROUTER", "").strip()
    if not ADDRESS.match(router):
        raise ConfigError("LACRE_ROUTER must be a 0x-prefixed 20 byte address")
    keys = tuple(k.strip() for k in env.get("LACRE_API_KEYS", "").split(",") if k.strip())
    if not keys:
        raise ConfigError("LACRE_API_KEYS must hold at least one key")
    if any(len(k) < 24 for k in keys):
        raise ConfigError("every API key must be at least 24 characters")
    base = env.get("LACRE_BLOB_BASE_URL", "").strip().rstrip("/")
    # The Verifier refuses any URL that does not start with https://.
    if not base.startswith("https://"):
        raise ConfigError("LACRE_BLOB_BASE_URL must start with https://")
    data_dir = env.get("LACRE_DATA_DIR", "").strip()
    if not data_dir:
        raise ConfigError("LACRE_DATA_DIR must be set")
    key_file = env.get("LACRE_SIGNING_KEY_FILE", "").strip()
    mode = env.get("LACRE_EXTRACT_DEFAULT", "").strip().lower() or "auto"
    if mode not in EXTRACT_MODES:
        raise ConfigError("LACRE_EXTRACT_DEFAULT must be one of %s" % (", ".join(EXTRACT_MODES),))
    body_base = env.get("LACRE_BODY_BASE_URL", "").strip().rstrip("/") or body_base_from(base)
    if not body_base:
        raise ConfigError("LACRE_BODY_BASE_URL must be set when LACRE_BLOB_BASE_URL does "
                          "not end in /h")
    # The Extractors refuse any body URL that does not start with https://.
    if not body_base.startswith("https://"):
        raise ConfigError("LACRE_BODY_BASE_URL must start with https://")
    mail_domain = env.get("LACRE_MAIL_DOMAIN", "").strip().lower().rstrip(".") or "in-sidr.xyz"
    if not DOMAIN.match(mail_domain):
        raise ConfigError("LACRE_MAIL_DOMAIN must be a domain name")
    inbound_secret = env.get("LACRE_INBOUND_SECRET", "").strip()
    if inbound_secret and len(inbound_secret) < MIN_INBOUND_SECRET:
        raise ConfigError("LACRE_INBOUND_SECRET must be at least %d characters"
                          % (MIN_INBOUND_SECRET,))
    admin_token = env.get("LACRE_ADMIN_TOKEN", "").strip()
    # The admin token mints credits, so it is held to the inbound secret's
    # length for the same reason.
    if admin_token and len(admin_token) < MIN_INBOUND_SECRET:
        raise ConfigError("LACRE_ADMIN_TOKEN must be at least %d characters"
                          % (MIN_INBOUND_SECRET,))
    return Settings(
        network=network,
        router=router,
        api_keys=keys,
        blob_base_url=base,
        data_dir=Path(data_dir),
        signing_key_file=Path(key_file) if key_file else None,
        poll_s=_int(env, "LACRE_POLL_S", 30),
        final_bound_s=_int(env, "LACRE_FINAL_BOUND_S", 4 * 3600),
        max_attempts=max(1, _int(env, "LACRE_MAX_ATTEMPTS", 3)),
        max_send_failures=max(1, _int(env, "LACRE_MAX_SEND_FAILURES", 3)),
        key_quarantine_s=_int(env, "LACRE_KEY_QUARANTINE_S", 24 * 3600),
        confirm_margin_s=_int(env, "LACRE_CONFIRM_MARGIN_S", 600),
        confirm_retry_s=_int(env, "LACRE_CONFIRM_RETRY_S", 3600),
        worker_stale_s=_int(env, "LACRE_WORKER_STALE_S", 300),
        max_eml_bytes=_int(env, "LACRE_MAX_EML_BYTES", 10 * 1024 * 1024),
        extract_default=mode,
        body_base_url=body_base,
        mail_domain=mail_domain,
        inbound_secret=inbound_secret,
        bootstrap_credits=_int(env, "LACRE_BOOTSTRAP_CREDITS", 0),
        price_attest=_int(env, "LACRE_PRICE_ATTEST", 1),
        price_extract=_int(env, "LACRE_PRICE_EXTRACT", 1),
        admin_token=admin_token,
    )
