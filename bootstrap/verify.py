"""`bin/verify`: reference check of a .jws file against the client contract."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from bootstrap import contract, keys
from bootstrap.errors import CommandError, fail
from bootstrap.manifest import parse_now


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify", description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--env", required=True, choices=keys.ENVS)
    parser.add_argument("--keys", type=Path, default=keys.DEFAULT_DIR)
    parser.add_argument("--min-version", type=int, default=0)
    parser.add_argument("--now", type=parse_now, default=None)
    args = parser.parse_args(argv)

    try:
        data = args.file.read_bytes()
        trusted = keys.trusted_for_env(keys.load_dir(args.keys), args.env)
    except CommandError as error:
        return fail(error)
    except OSError as error:
        return fail(CommandError("unreadable_input", str(error)))

    result = contract.evaluate(
        data,
        env=args.env,
        trusted_keys=trusted,
        stored_version=args.min_version,
        now=args.now or datetime.now(UTC),
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, separators=(",", ":")))
    if not result.accepted:
        print(f"error: {result.reason}: {args.file}", file=sys.stderr)
    return 0 if result.accepted else 1


if __name__ == "__main__":
    sys.exit(main())
