import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jwcrypto.jwk import JWK

from bootstrap import gen_testdata, keys, sign
from bootstrap.age import AgeError

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


class FakeAge:
    """In-memory stand-in for the `age` adapter: no binary, no TTY."""

    def __init__(self):
        self.secrets: dict[Path, bytes] = {}
        self.broken = False

    def encrypt(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"age-encrypted")
        self.secrets[path] = data

    def decrypt(self, path: Path) -> bytes:
        if self.broken or path not in self.secrets:
            raise AgeError("decryption failed")
        return self.secrets[path]


class Repo:
    """A temporary git repository laid out like the real one."""

    def __init__(self, root: Path, key_dir: Path):
        self.root = root
        self.key_dir = key_dir
        self.age = FakeAge()
        self.privates: dict[str, JWK] = {}
        for directory in ("keys", "dist", "manifests"):
            (root / directory).mkdir()
        self.git("init", "-q", "-b", "main")
        for kid in ("prod-2026a", "prod-2026b", "stage-2026a"):
            self.add_key(kid)
        for env in ("production", "staging"):
            self.write_manifest(env, gen_testdata.base_manifest(env))

    def git(self, *args: str) -> str:
        identity = [
            *("-c", "user.name=t", "-c", "user.email=t@example.com"),
            *("-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"),
        ]
        result = subprocess.run(
            ["git", "-C", str(self.root), *identity, *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def commit(self, message: str = "snapshot") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def add_key(self, kid: str, *, public: bool = True) -> JWK:
        key = JWK.generate(kty="OKP", crv="Ed25519")
        self.privates[kid] = key
        private = json.loads(key.export_private())
        private["kid"] = kid
        self.age.encrypt(keys.private_key_path(kid, self.key_dir), json.dumps(private).encode())
        if public:
            self.write_public(kid, keys.public_jwk(key, kid))
        return key

    def write_public(self, kid: str, jwk: dict) -> None:
        (self.root / "keys" / f"{kid}.pub.json").write_text(json.dumps(jwk, indent=2) + "\n")

    def manifest_path(self, env: str) -> Path:
        return self.root / "manifests" / f"{env}.json"

    def dist_path(self, env: str) -> Path:
        return self.root / "dist" / f"{env}.jws"

    def read_manifest(self, env: str) -> dict:
        return json.loads(self.manifest_path(env).read_text())

    def write_manifest(self, env: str, manifest: dict) -> None:
        self.manifest_path(env).write_text(json.dumps(manifest, indent=2) + "\n")

    def sign(self, env: str, kids: list[str], now: datetime = NOW) -> dict:
        return sign.sign(env, kids, root=self.root, key_dir=self.key_dir, age=self.age, now=now)

    def sign_all(self) -> None:
        self.sign("production", ["prod-2026a"])
        self.sign("staging", ["stage-2026a"])


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    root = tmp_path / "repo"
    root.mkdir()
    return Repo(root, tmp_path / "private-keys")
