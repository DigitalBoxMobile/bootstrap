"""Read-only git queries against the repository working copy."""

import subprocess
from pathlib import Path


def blob(root: Path, ref: str, path: str) -> bytes | None:
    """Contents of `path` at `ref`, or None when it does not exist there."""
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{path}"], capture_output=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def is_commit(root: Path, ref: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0
