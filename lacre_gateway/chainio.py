"""The chain, through the public repo's tools and nothing else.

Reads go through chain.read (the gen_call path with its decoding fallback),
stored consensus state through txstate.stored, and every send through
attest.Session.send, which repeats a broadcast the node refused only with the
first nonce pinned and only after every earlier try is confirmed absent.

Two differences from the tools, both about the key. tools/chain.py reads the
signing key from PROBE_PK; the gateway never has it in its environment and
reads it from a file (load_account), then builds the same client connect()
builds and applies the same checks before anything is signed. And the tools
print what they do: the gateway sends their stdout nowhere (main.py), so no
address or response body they print can reach a log.
"""

import os
import re
import threading

from .vendor import attest, chain, txstate

FINAL = attest.FINAL
NONFINAL = attest.NONFINAL
KEY = re.compile(r"^0x[0-9a-fA-F]{64}$")


class ChainUnavailable(RuntimeError):
    """A read or a connect got no usable answer."""


# What a send can end in besides a tx id, from tools/attest.py.
NothingSent = attest.NothingSent
Stop = attest.Stop


def load_account(path):
    """The signing account from a key file; the key text is not kept.

    Outside the systemd credentials directory the file must carry no group
    or other permission bits, so 600 and 400 pass. Error messages never
    carry any of its content.
    """
    from genlayer_py import create_account

    # systemd LoadCredential with User= hands the key over as mode 440 and
    # grants the service user read through an ACL, so the group bits are set
    # although no group member can reach it: $CREDENTIALS_DIRECTORY is
    # private to the service and managed by systemd. Only there are the
    # mode bits not checked; every other path keeps the owner only rule.
    if not _in_credentials_directory(path):
        info = os.stat(path)
        if info.st_mode & 0o077:
            raise PermissionError("the signing key file must be owner only (mode 600 or 400), "
                                  "with no group or other access")
    with open(path, "r", encoding="ascii") as handle:
        text = handle.read().strip()
    if not KEY.match(text):
        text = None
        raise ValueError("the signing key file must hold one 0x-prefixed 32 byte hex key")
    try:
        return create_account(text)
    finally:
        text = None


def _in_credentials_directory(path):
    """True when path resolves to a file inside $CREDENTIALS_DIRECTORY.

    Both sides are resolved first, so a symlink or .. that leads out of the
    directory does not count as inside it.
    """
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return False
    directory = os.path.realpath(directory)
    target = os.path.realpath(path)
    return target != directory and os.path.commonpath([directory, target]) == directory


class Chain:
    """The live chain. Connects lazily, so the service starts while the RPC
    is down and /health can say so."""

    def __init__(self, network, key_file=None):
        if network not in chain.NETWORKS:
            raise ValueError("unknown network %r" % (network,))
        self.network = network
        self.net = chain.NETWORKS[network]
        self.key_file = key_file
        self._client = None
        self._account = None
        self._lock = threading.Lock()

    # ---- connection ------------------------------------------------------

    def _connect(self):
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                if self.key_file:
                    self._account = load_account(self.key_file)
                    self._client = self._signing_client(self._account)
                else:
                    self._client, _ = txstate.connect_readonly(self.network)
            except SystemExit:
                raise ChainUnavailable("could not connect to %s" % (self.network,))
            except (OSError, ValueError) as error:
                if isinstance(error, PermissionError) or "signing key" in str(error):
                    raise
                raise ChainUnavailable("could not connect to %s (%s)"
                                       % (self.network, type(error).__name__))
            return self._client

    def _signing_client(self, account):
        # tools/chain.connect() with the key from a file instead of PROBE_PK:
        # same client, same refusals before anything is signed.
        from genlayer_py import create_client

        net = self.net
        if net["mode"] != "l2":
            raise ChainUnavailable("%s is not an l2 network" % (self.network,))
        net["chain"].rpc_urls["default"]["http"] = [net["rpc_url"]]
        client = chain.resilient(lambda: create_client(chain=net["chain"], account=account),
                                 "connect")
        chain.resilient(client.initialize_consensus_smart_contract,
                        "reading the consensus contracts")
        if int(client.chain.id) != net["chain_id"]:
            raise ChainUnavailable("the SDK chain id does not match the network table")
        configured = client.chain.consensus_main_contract["address"]
        if int(configured, 16) == 0 or configured.lower() != net["consensus_main"].lower():
            raise ChainUnavailable("the SDK ConsensusMain does not match the network table")
        return client

    @property
    def can_sign(self):
        return bool(self.key_file)

    @property
    def requester(self):
        """The address attestations are sent from, or None without a key."""
        self._connect()
        return self._account.address if self._account else None

    # ---- reads -------------------------------------------------------------

    def ping(self):
        """Whether the RPC answers, and for the chain id the table names."""
        try:
            answer = chain.rpc(self.net, "eth_chainId", [])
            return int(str(answer.get("result", "0x0")), 16) == self.net["chain_id"]
        except Exception:
            return False

    def view(self, address, method, args, final=True):
        client = self._connect()
        try:
            value = chain.read(self.net, client, address, method, list(args),
                               FINAL if final else NONFINAL)
        except SystemExit:
            value = chain.UNKNOWN
        except Exception as error:
            raise ChainUnavailable("%s() failed (%s)" % (method, type(error).__name__))
        if value is chain.UNKNOWN:
            raise ChainUnavailable("%s() returned no readable result" % (method,))
        return value

    def stored(self, tx_id):
        client = self._connect()
        try:
            return txstate.stored(client, tx_id)
        except SystemExit:
            raise ChainUnavailable("the stored state of a transaction could not be read")
        except Exception as error:
            raise ChainUnavailable("getTransactionAllData failed (%s)" % (type(error).__name__,))

    def messages(self, tx_id):
        client = self._connect()
        try:
            return txstate.messages(client, tx_id)
        except BaseException as error:
            if isinstance(error, KeyboardInterrupt):
                raise
            raise ChainUnavailable("getTransactionData failed (%s)" % (type(error).__name__,))

    def explorer(self, tx_id):
        return "%s/tx/%s" % (self.net["explorer"], tx_id)

    # ---- writes ------------------------------------------------------------

    def send(self, address, method, args, value=0):
        """One transaction through attest.Session.send: its consensus tx id.

        Raises NothingSent when nothing reached the chain, Stop when the
        outcome of a broadcast is unknown. Neither is retried here.
        """
        if not self.can_sign:
            raise NothingSent("no signing key is configured")
        client = self._connect()
        try:
            encoded = chain.write_calldata(client, self._account, address, method, list(args))
        except SystemExit:
            raise NothingSent("the calldata could not be built")
        session = attest.Session(self.net, client, self._account, encoded,
                                 {"value": int(value)})
        try:
            return session.send()
        except SystemExit:
            # chain.send dies on an L2 revert or on a receipt without a
            # creation event: something reached the chain, so the outcome is
            # unknown and nothing may be sent again blindly.
            raise Stop("the send ended after the broadcast without a consensus tx id")
