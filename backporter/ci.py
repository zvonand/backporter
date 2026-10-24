"""
CI results of a backport (or bump) PR, and local builds that check an AI fix before it is pushed.
"""

import gzip
import json
import os
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

from github.Repository import Repository

from backporter import ai, git_ops
from backporter.config import CI_REPORT_URL

FAILED_CONCLUSIONS = ("failure", "timed_out")
# Praktika result statuses of a failed job, step or test.
FAILED_STATUSES = ("FAIL", "ERROR", "failure", "error")
EXCERPT_LINES = 80
LINE_CHARS = 500
FAILED_RESULTS = 15


@dataclass
class Failure:
    """A failed check run with what is known about why it failed."""
    check: str
    url: str
    details: str


def excerpt(text: str) -> str:
    """Compiler errors and FAILED lines of a log if it has any, else its last lines; long lines are cut."""
    lines = [line[:LINE_CHARS] for line in text.splitlines()]
    errors = [line for line in lines if "error:" in line or line.startswith("FAILED:")]
    return "\n".join(errors[:EXCERPT_LINES] if errors else lines[-EXCERPT_LINES:])


def _job_name(check: str) -> str:
    """Praktika's file name for a check: 'Fast test' -> 'fast_test'."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", check.lower())).rstrip("_")


def _report(pr_number: int, sha: str, check: str) -> dict | None:
    """The praktika report of a check, or None if there is none."""
    url = CI_REPORT_URL.format(pr=pr_number, sha=sha, job=_job_name(check))
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise
    return json.loads(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)


def _failed_leaves(result: dict) -> list[dict]:
    """The innermost failed results (steps or tests) of a praktika result."""
    failed = [r for r in result.get("results") or [] if r.get("status") in FAILED_STATUSES]
    leaves = [leaf for r in failed for leaf in _failed_leaves(r)]
    return leaves or ([result] if result.get("status") in FAILED_STATUSES else [])


def _details(pr_number: int, sha: str, run) -> str:
    report = _report(pr_number, sha, run.name)
    if report is None:
        text = "\n".join(t for t in (run.output.summary, run.output.text) if t)
        return excerpt(text) if text else "(no details)"
    leaves = _failed_leaves(report)
    parts = [f"- {r.get('name')}: {r.get('status')}\n{excerpt(r.get('info') or '')}" for r in leaves[:FAILED_RESULTS]]
    if len(leaves) > FAILED_RESULTS:
        parts.append(f"... and {len(leaves) - FAILED_RESULTS} more failed")
    return "\n".join(parts) or excerpt(report.get("info") or "") or "(no details)"


def failures(repo: Repository, pr_number: int, sha: str) -> list[Failure] | None:
    """Failed checks of the commit, or None if CI hasn't finished (or started) on it."""
    runs = list(repo.get_commit(sha).get_check_runs())
    if not runs or any(r.status != "completed" for r in runs):
        return None
    failed = {r.name: r for r in runs if r.conclusion in FAILED_CONCLUSIONS}
    return [Failure(name, run.html_url, _details(pr_number, sha, run)) for name, run in failed.items()]


def update_submodules(repo_dir: str) -> None:
    print("Updating submodules...")
    git_ops.run(["sh", "contrib/update-submodules.sh"], cwd=repo_dir, stream=True)


def build(repo_dir: str, build_dir: str, cmake: str) -> str | None:
    """
    Configure build_dir with the cmake command (if not done yet) and run ninja there, with the AI's locked
    environment (the build runs code the AI may have edited). None if it builds, else the errors.
    """
    env = ai.locked_env()
    if not os.path.exists(os.path.join(build_dir, "build.ninja")):
        os.makedirs(build_dir, exist_ok=True)
        print(f"Configuring {build_dir}: {cmake}")
        result = subprocess.run(shlex.split(cmake), cwd=build_dir, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"cmake failed in {build_dir}:\n{excerpt(result.stdout + result.stderr)}")
    log_path = os.path.join(build_dir, "backporter-build.log")
    print(f"Building in {build_dir} (log: {log_path}); this can take a while...")
    with open(log_path, "w") as log:
        result = subprocess.run(["ninja"], cwd=build_dir, env=env, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode == 0:
        print("Build OK")
        return None
    with open(log_path, errors="replace") as log:
        errors = excerpt(log.read())
    print(f"Build failed:\n{errors[:3000]}")
    return errors
