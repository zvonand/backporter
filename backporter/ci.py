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
from collections import Counter
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
    pending = [r.name for r in runs if r.status != "completed"]
    conclusions = Counter(r.conclusion for r in runs if r.status == "completed")
    print(f"{len(runs)} check run(s): {len(pending)} not finished; "
          + ", ".join(f"{n} {c}" for c, n in conclusions.most_common()))
    if pending:
        print(f"Not finished: {', '.join(pending[:10])}{', ...' if len(pending) > 10 else ''}")
    if not runs or pending:
        return None
    failed = {r.name: r for r in runs if r.conclusion in FAILED_CONCLUSIONS}
    result = []
    for name, run in failed.items():
        print(f"Reading the report of '{name}'...")
        result.append(Failure(name, run.html_url, _details(pr_number, sha, run)))
    return result


def update_submodules(repo_dir: str) -> None:
    print("Updating submodules...")
    _drop_sparse_checkout(repo_dir)
    git_ops.run(["git", "submodule", "update", "--init"], cwd=repo_dir, stream=True)


def _drop_sparse_checkout(repo_dir: str) -> None:
    """
    Undo contrib/sparse-checkout/setup-sparse-checkout.sh of older branches' contrib/update-submodules.sh
    (submodule update commands in .git/config, sparse submodule work trees), if it was ever run in the clone.
    """
    out = git_ops.run_no_check(["git", "config", "--get-regexp", r"^submodule\..*\.update$"], cwd=repo_dir).stdout
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        if not value.startswith("!../sparse-checkout/"):
            continue
        git_ops.run(["git", "config", "--unset-all", key], cwd=repo_dir)
        path = os.path.join(repo_dir, key[len("submodule."):-len(".update")])
        if os.path.exists(os.path.join(path, ".git")):
            git_ops.run(["git", "sparse-checkout", "disable"], cwd=path)


def _tee(cmd: list[str], cwd: str, env: dict[str, str], log_path: str) -> int:
    """Run cmd, printing its output and writing it to log_path; returns the exit code."""
    print(f"$ {' '.join(cmd)}  (in {cwd}, log: {log_path})", flush=True)
    with open(log_path, "w") as log:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            print(line, end="")
            log.write(line)
        return proc.wait()


def build(repo_dir: str, build_dir: str, cmake: str) -> str | None:
    """
    Configure build_dir with the cmake command (if not done yet) and run ninja there, with the AI's locked
    environment (the build runs code the AI may have edited). None if it builds, else the errors.
    """
    env = ai.locked_env()
    if not os.path.exists(os.path.join(build_dir, "build.ninja")):
        os.makedirs(build_dir, exist_ok=True)
        print(f"Configuring {build_dir}...")
        log_path = os.path.join(build_dir, "backporter-cmake.log")
        if _tee(shlex.split(cmake), build_dir, env, log_path) != 0:
            with open(log_path, errors="replace") as log:
                raise RuntimeError(f"cmake failed in {build_dir}:\n{excerpt(log.read())}")
    log_path = os.path.join(build_dir, "backporter-build.log")
    print(f"Building in {build_dir}; this can take a while...")
    if _tee(["ninja"], build_dir, env, log_path) == 0:
        print("Build OK")
        return None
    with open(log_path, errors="replace") as log:
        errors = excerpt(log.read())
    print(f"Build failed; errors:\n{errors}")
    return errors
