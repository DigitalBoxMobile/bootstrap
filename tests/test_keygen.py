import json

import pytest
from jwcrypto.jwk import JWK

from bootstrap import keygen, keys
from bootstrap.errors import CommandError


def generate(repo, kid: str = "prod-2026z", key_dir=None):
    return keygen.generate(kid, root=repo.root, key_dir=key_dir or repo.key_dir, age=repo.age)


def test_keygen_encrypts_private_jwk_and_writes_public_file(repo):
    private_path, public_path = generate(repo)

    assert private_path == repo.key_dir / "prod-2026z.jwk.age"
    private = JWK(**json.loads(repo.age.secrets[private_path]))
    assert private.has_private
    public = json.loads(public_path.read_text())
    assert public == keys.public_jwk(private, "prod-2026z")
    assert "d" not in public
    assert public_path == repo.root / "keys" / "prod-2026z.pub.json"


@pytest.mark.parametrize("kid", ["prod-26", "dev-2026a", "prod-2026A"])
def test_keygen_rejects_malformed_kid(repo, kid):
    with pytest.raises(CommandError) as raised:
        generate(repo, kid)
    assert raised.value.code == "invalid_kid"


def test_keygen_refuses_private_directory_inside_repository(repo):
    with pytest.raises(CommandError) as raised:
        generate(repo, key_dir=repo.root / "secrets")

    assert raised.value.code == "key_dir_in_repo"
    assert repo.age.secrets.keys().isdisjoint({repo.root / "secrets" / "prod-2026z.jwk.age"})
    assert not (repo.root / "secrets").exists()
    assert not (repo.root / "keys" / "prod-2026z.pub.json").exists()


def test_keygen_refuses_to_overwrite_existing_public_key(repo):
    with pytest.raises(CommandError) as raised:
        generate(repo, "prod-2026a")
    assert raised.value.code == "key_exists"


def test_keygen_refuses_to_overwrite_existing_private_key(repo):
    repo.add_key("prod-2026z", public=False)

    with pytest.raises(CommandError) as raised:
        generate(repo, "prod-2026z")
    assert raised.value.code == "key_exists"
