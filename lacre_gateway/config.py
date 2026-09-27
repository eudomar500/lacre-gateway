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

    @property
    def db_path(self):
        return self.data_dir / "gateway.sqlite3"

    @property
    def blob_dir(self):
        return self.data_dir / "blobs"


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
    )
