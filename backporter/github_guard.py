"""
Allowlist for every GitHub API request the backporter makes.

Reads (GET/HEAD) are allowed. The only allowed writes are:
- opening a PR in TARGET_REPO (POST /repos/<TARGET_REPO>/pulls);
- the GraphQL mutations used to maintain the project.
Everything else is refused before it is sent, in particular any comment, review, issue or
edit anywhere, and any write to ClickHouse/ClickHouse.
"""

import json
import re
import urllib.parse

from github import Auth, Github
from github.Requester import Requester

from backporter.config import TARGET_REPO

ALLOWED_MUTATIONS = {
    "createProjectV2Field",
    "updateProjectV2Field",
    "createProjectV2View",
    "updateProjectV2View",
    "addProjectV2DraftIssue",
    "updateProjectV2ItemFieldValue",
    "deleteProjectV2Item",
}

# All PyGithub HTTP requests (including redirects) go through this private method.
_RAW = "_Requester__requestRaw"


class ForbiddenRequest(RuntimeError):
    pass


def _mutation_fields(query: str) -> set[str]:
    """Words at the top level of the mutation's selection set (field names and any aliases)."""
    start = query.find("{", re.search(r"\bmutation\b", query).end())
    if start < 0:
        return set()
    depth = 0
    top: list[str] = []
    for ch in query[start:]:
        if ch in "{(":
            depth += 1
        elif ch in "})":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1:
            top.append(ch)
    return set(re.findall(r"\w+", "".join(top)))


def check_request(verb: str, url: str, body: object) -> None:
    """Raise ForbiddenRequest unless the request is on the allowlist."""
    if verb in ("GET", "HEAD"):
        return
    path = urllib.parse.urlparse(url).path.rstrip("/")
    if verb == "POST" and path.lower() == f"/repos/{TARGET_REPO}/pulls".lower():
        return
    if verb == "POST" and path.endswith("/graphql"):
        query = json.loads(body)["query"] if isinstance(body, (str, bytes)) else ""
        if not re.search(r"\bmutation\b", query):
            return
        names = _mutation_fields(query)
        if names and names <= ALLOWED_MUTATIONS and len(re.findall(r"\bmutation\b", query)) == 1:
            return
        raise ForbiddenRequest(f"Refusing GraphQL mutation {sorted(names) or query[:80]!r}")
    raise ForbiddenRequest(f"Refusing {verb} {path}")


def guarded_github(token: str | None) -> Github:
    """A Github client whose requests (from any Requester in this process) pass check_request."""
    raw = getattr(Requester, _RAW, None)
    if not callable(raw):
        raise RuntimeError("Installed PyGithub has no request hook to guard; refusing to run")
    if not getattr(raw, "_backporter_guarded", False):
        def guarded(self, cnx, verb, url, requestHeaders, input, *args, **kwargs):
            check_request(verb, url, input)
            return raw(self, cnx, verb, url, requestHeaders, input, *args, **kwargs)

        guarded._backporter_guarded = True
        setattr(Requester, _RAW, guarded)
    return Github(auth=Auth.Token(token)) if token else Github()
