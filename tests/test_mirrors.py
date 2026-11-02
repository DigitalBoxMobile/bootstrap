import pytest
import yaml

from bootstrap import mirrors
from bootstrap.errors import CommandError

S3 = {
    "name": "a",
    "type": "s3",
    "endpoint": "https://e",
    "region": "r",
    "bucket": "b",
    "prefix": "v1/",
    "public_url": "https://p/v1/",
    "credentials": "A",
}
GIT = {
    "name": "g",
    "type": "git",
    "push_url": "git@h:o/r.git",
    "branch": "master",
    "public_url": "https://p/dist/",
    "credentials": "G",
}


def load(tmp_path, targets):
    path = tmp_path / "mirrors.yml"
    path.write_text(yaml.safe_dump({"targets": targets}))
    return mirrors.load(path)


def test_repository_config_is_valid():
    names = [t.name for t in mirrors.load()]

    assert names == ["yandex", "selectel", "github"]


def test_loads_both_types(tmp_path):
    s3, git = load(tmp_path, [S3, GIT])

    assert s3.object_key("production") == "v1/production.jws"
    assert git.public_file_url("staging") == "https://p/dist/staging.jws"


def test_empty_prefix_is_allowed(tmp_path):
    (target,) = load(tmp_path, [{**S3, "prefix": ""}])

    assert target.object_key("staging") == "staging.jws"


@pytest.mark.parametrize(
    "targets, fragment",
    [
        ([S3, {**GIT, "name": "a"}], "duplicate target names: a"),
        ([{**S3, "type": "ftp"}], "type must be one of"),
        ([{k: v for k, v in S3.items() if k != "bucket"}], "bucket"),
        ([{k: v for k, v in GIT.items() if k != "push_url"}], "push_url"),
        ([{**S3, "public_url": "https://p/v1"}], "must end with '/'"),
        ([], "non-empty list"),
    ],
)
def test_invalid_config(tmp_path, targets, fragment):
    with pytest.raises(CommandError) as raised:
        load(tmp_path, targets)

    assert raised.value.code == "bad_config"
    assert fragment in raised.value.detail
