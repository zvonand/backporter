"""
Committed config (target repo, project link), versions and local clone dirs.
"""

import os
import re
import sys
from dataclasses import dataclass

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.toml")
DIRS_PATH = os.path.expanduser("~/.config/backporter/dirs.toml")
# .env in the current directory, then in the repo root (editable install).
ENV_PATHS = (".env", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# The only repo the backporter pushes branches to and opens PRs in.
TARGET_REPO = "Altinity/ClickHouse"

FLAVORS = ("stable", "antalya")
_KEY_RE = re.compile(r"^(stable|antalya)-(\d+)\.(\d+)$")


@dataclass(frozen=True)
class Version:
    key: str
    flavor: str
    version: tuple[int, int]
    branch: str

    @classmethod
    def make(cls, flavor: str, version: str, branch: str) -> "Version":
        return cls.parse(f"{flavor}-{version}", branch)

    @classmethod
    def parse(cls, key: str, branch: str) -> "Version":
        """Build from a key like 'stable-25.8' and its branch."""
        match = _KEY_RE.match(key)
        if not match:
            raise ValueError(f"Invalid version key '{key}', expected <stable|antalya>-X.Y")
        flavor, major, minor = match.groups()
        return cls(key, flavor, (int(major), int(minor)), branch)

    @property
    def label(self) -> str:
        """Human-readable name used in PR titles, e.g. 'Antalya 26.3'."""
        return f"{self.flavor.capitalize()} {self.version[0]}.{self.version[1]}"


def find_version(versions: list[Version], key: str) -> Version:
    for v in versions:
        if v.key == key:
            return v
    known = ", ".join(v.key for v in versions) or "none; run `backporter add-version`"
    raise ValueError(f"Unknown version '{key}'. Known: {known}")


@dataclass
class Build:
    """A builds project that backport PRs of one flavor are added to (once)."""
    project: int
    # Text field that gets the version number, e.g. "26.3"
    version_field: str
    # Status for a newly added open PR; empty = leave unset
    status: str


@dataclass
class Config:
    project_owner: str
    project_number: int
    # "all", a flavor, or a version key -> labels for backport PRs
    labels: dict[str, list[str]]
    # version key -> branch in TARGET_REPO
    branches: dict[str, str]
    # AI conflict resolution in `backporter run`
    run_ai: bool
    # flavor -> builds project
    builds: dict[str, Build]

    def labels_for(self, version: Version) -> list[str]:
        """Labels for a backport PR into version: 'all', then its flavor's, then its own."""
        names = [*self.labels.get("all", []), *self.labels.get(version.flavor, []), *self.labels.get(version.key, [])]
        return list(dict.fromkeys(names))


def load_dotenv() -> None:
    """Set KEY=VALUE pairs from the first .env in ENV_PATHS; variables already in the environment win."""
    for path in ENV_PATHS:
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.removeprefix("export ").split("=", 1)
                value = value.strip().strip("'\"")
                if value:
                    os.environ.setdefault(key.strip(), value)
        return


def load_config(path: str = CONFIG_PATH) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    labels = data.get("labels", {})
    for key, names in labels.items():
        if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
            raise ValueError(f"{path}: labels.{key} must be a list of label names")
    branches = data.get("branches", {})
    for key, branch in branches.items():
        Version.parse(key, branch)
        if not isinstance(branch, str) or not branch:
            raise ValueError(f"{path}: branches.{key} must be a branch name")
    run_ai = data.get("run", {}).get("ai", True)
    if not isinstance(run_ai, bool):
        raise ValueError(f"{path}: run.ai must be true or false")
    builds = {}
    for flavor, b in data.get("builds", {}).items():
        if flavor not in FLAVORS or not isinstance(b.get("project"), int) or not b.get("version_field"):
            raise ValueError(f"{path}: builds.{flavor} needs a flavor key, an integer project and a version_field")
        builds[flavor] = Build(b["project"], b["version_field"], b.get("status", ""))
    return Config(data.get("project_owner", ""), data.get("project_number", 0), labels, branches, run_ai, builds)


def save_project(owner: str, number: int, path: str = CONFIG_PATH) -> None:
    """Rewrite project_owner and project_number in the config file."""
    with open(path) as f:
        text = f.read()
    text = re.sub(r"^project_owner = .*$", f'project_owner = "{owner}"', text, flags=re.MULTILINE)
    text = re.sub(r"^project_number = .*$", f"project_number = {number}", text, flags=re.MULTILINE)
    with open(path, "w") as f:
        f.write(text)


def parse_project_url(url: str) -> tuple[str, int]:
    """Parse https://github.com/(orgs|users)/<owner>/projects/<number>[/...] into (owner, number)."""
    match = re.search(r"github\.com/(?:orgs|users)/([^/]+)/projects/(\d+)", url)
    if not match:
        raise ValueError(f"Invalid project URL: {url}")
    return match.group(1), int(match.group(2))


def load_dirs(overrides: list[str], versions: list[Version]) -> dict[str, str]:
    """Return version key -> clone dir from DIRS_PATH, overridden by KEY=DIR strings."""
    dirs: dict[str, str] = {}
    if os.path.exists(DIRS_PATH):
        with open(DIRS_PATH, "rb") as f:
            dirs.update(tomllib.load(f))
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"-C expects KEY=DIR, got '{item}'")
        key, path = item.split("=", 1)
        dirs[key] = path
    for key in dirs:
        find_version(versions, key)
    return {k: os.path.abspath(os.path.expanduser(p)) for k, p in dirs.items()}
