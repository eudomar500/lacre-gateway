"""The public Lacre repository, pinned as the submodule vendor/lacre.

Everything the gateway knows about the chain comes from there: the network
table and the send path (tools/chain.py), the stored consensus state
(tools/txstate.py), the confirmation decisions (tools/attest.py), their
Extractor variant and the Extractors' own checks (tools/extract.py) and the
DKIM parsing the contracts themselves run (lacre/dkimcore.py). Nothing under
vendor/lacre is edited; the tools directory is put on sys.path because the
tools import each other by bare module name.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "vendor" / "lacre"
TOOLS = ROOT / "tools"

if not (TOOLS / "attest.py").is_file():
    raise ImportError("vendor/lacre is missing; run: git submodule update --init")

for path in (str(ROOT), str(TOOLS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import attest  # noqa: E402
import chain  # noqa: E402
import extract  # noqa: E402
import txstate  # noqa: E402
from lacre import dkimbody, dkimcore  # noqa: E402

__all__ = ["attest", "chain", "extract", "txstate", "dkimbody", "dkimcore", "ROOT"]
