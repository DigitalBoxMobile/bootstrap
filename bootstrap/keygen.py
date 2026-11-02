"""`bin/keygen`: generate an Ed25519 signing key, encrypt the private half with age."""

import argparse
import json
import sys
from pathlib import Path

from jwcrypto.jwk import JWK

from bootstrap import keys
from bootstrap.age import Age, AgeError
from bootstrap.errors import CommandError, fail
from bootstrap.manifest import ROOT


def generate(kid: str, *, root: Path, key_dir: Path, age: Age) -> tuple[Path, Path]:
    """Create the encrypted private key and `keys/<kid>.pub.json`; return both paths."""
    if not keys.KID_RE.fullmatch(kid):
        raise CommandError("invalid_kid", f"{kid!r} does not match {keys.KID_RE.pattern}")
    private_path = keys.private_key_path(kid, key_dir).resolve()
    if private_path.is_relative_to(root.resolve()):
        raise CommandError("key_dir_in_repo", f"{private_path} is inside the repository")
    public_path = root / "keys" / f"{kid}.pub.json"
    for path in (public_path, private_path):
        if path.exists():
            raise CommandError("key_exists", f"{path} already exists")

    key = JWK.generate(kty="OKP", crv="Ed25519", kid=kid)
    private_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        age.encrypt(private_path, key.export_private().encode())
    except AgeError as error:
        private_path.unlink(missing_ok=True)
        raise CommandError("age_failed", str(error)) from error
    public_path.write_text(json.dumps(keys.public_jwk(key, kid), indent=2) + "\n")
    return private_path, public_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="keygen", description=__doc__)
    parser.add_argument("kid")
    args = parser.parse_args(argv)

    try:
        private_path, public_path = generate(
            args.kid, root=ROOT, key_dir=keys.private_key_dir(), age=Age()
        )
    except CommandError as error:
        return fail(error)
    except OSError as error:
        return fail(CommandError("unreadable_input", str(error)))
    print(f"private (age-encrypted): {private_path}")
    print(f"public: {public_path}")
    print(
        "Keep copies of the private file: Passbolt and two offline copies (spare key separately)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
