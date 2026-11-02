"""`mirrors.yml`: publication targets, loaded and validated."""

from dataclasses import dataclass
from pathlib import Path

import yaml

from bootstrap.errors import CommandError
from bootstrap.manifest import ROOT

DEFAULT_PATH = ROOT / "mirrors.yml"
COMMON_FIELDS = ("name", "type", "public_url", "credentials")
TYPE_FIELDS = {
    "s3": ("endpoint", "region", "bucket", "prefix"),
    "git": ("push_url", "branch"),
}
MAY_BE_EMPTY = {"prefix"}  # an empty S3 prefix means the bucket root


@dataclass(frozen=True)
class Target:
    name: str
    type: str
    public_url: str
    credentials: str
    endpoint: str = ""
    region: str = ""
    bucket: str = ""
    prefix: str = ""
    push_url: str = ""
    branch: str = ""

    def public_file_url(self, env: str) -> str:
        return f"{self.public_url}{env}.jws"

    def object_key(self, env: str) -> str:
        return f"{self.prefix}{env}.jws"


def _bad(detail: str) -> CommandError:
    return CommandError("bad_config", detail)


def _target(index: int, raw: object) -> Target:
    if not isinstance(raw, dict):
        raise _bad(f"targets[{index}] is not a mapping")
    kind = raw.get("type")
    if kind not in TYPE_FIELDS:
        raise _bad(f"targets[{index}]: type must be one of {', '.join(TYPE_FIELDS)}")
    required = (*COMMON_FIELDS, *TYPE_FIELDS[kind])
    invalid = [
        f
        for f in required
        if not isinstance(raw.get(f), str) or (not raw[f] and f not in MAY_BE_EMPTY)
    ]
    label = f"targets[{index}] ({raw.get('name')})"
    if invalid:
        raise _bad(f"{label}: missing or empty {', '.join(invalid)}")
    if not raw["public_url"].endswith("/"):
        raise _bad(f"{label}: public_url must end with '/'")
    return Target(**{f: raw[f] for f in required})


def load(path: Path = DEFAULT_PATH) -> list[Target]:
    try:
        data = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as error:
        raise _bad(f"{path}: {error}") from error
    raw_targets = data.get("targets") if isinstance(data, dict) else None
    if not isinstance(raw_targets, list) or not raw_targets:
        raise _bad(f"{path}: `targets` must be a non-empty list")
    targets = [_target(i, raw) for i, raw in enumerate(raw_targets)]
    names = [t.name for t in targets]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise _bad(f"duplicate target names: {', '.join(duplicates)}")
    return targets
