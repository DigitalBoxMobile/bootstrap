"""Adapter over the `age` CLI. The passphrase is read by age itself from the TTY."""

import subprocess
from pathlib import Path


class AgeError(Exception):
    """`age` is missing or exited with an error."""


class Age:
    def encrypt(self, path: Path, data: bytes) -> None:
        """Encrypt `data` with a passphrase into `path` (`age -p -o`)."""
        self._run(["age", "-p", "-o", str(path)], input=data)

    def decrypt(self, path: Path) -> bytes:
        """Decrypt `path` (`age -d`); the plaintext stays in memory."""
        return self._run(["age", "-d", str(path)], stdout=subprocess.PIPE)

    @staticmethod
    def _run(command: list[str], **kwargs) -> bytes:
        try:
            result = subprocess.run(command, check=False, **kwargs)
        except FileNotFoundError as error:
            raise AgeError("age is not installed (brew install age)") from error
        if result.returncode != 0:
            raise AgeError(f"`{' '.join(command[:2])}` exited with {result.returncode}")
        return result.stdout or b""
