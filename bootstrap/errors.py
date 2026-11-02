"""Command failure with a machine-readable reason code."""

import sys


class CommandError(Exception):
    """Printed as `error: <reason_code>: <details>`; exit status 1."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def fail(error: CommandError) -> int:
    print(f"error: {error}", file=sys.stderr)
    return 1
