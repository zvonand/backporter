"""
Git and GitHub helpers for cherry-picking a merged PR, or merging an upstream release tag, into a local clone.
"""

import os
import re
import shutil
import subprocess
import sys
import time

from github.PullRequest import PullRequest


def run(cmd: list[str], cwd: str | None = None, check: bool = True, stream: bool = False) -> subprocess.CompletedProcess:
    """
    Run a command and return the result. With stream, print the command and let its output (e.g. git's
    progress) go straight to the terminal; for long operations like fetch and push.
    """
    if stream:
        print(f"$ {' '.join(cmd)}", flush=True)
        result = subprocess.run(cmd, cwd=cwd, text=True)
        if check and result.returncode != 0:
            raise RuntimeError(f"Command failed: {' '.join(cmd)}")
        return result
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    return result


_RETRY_ATTEMPTS = 3
_RETRY_SECONDS = 10


def run_with_retry(cmd: list[str], cwd: str | None = None) -> None:
    """Run a streamed command that may fail transiently (e.g. on network errors), retrying it a few times."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        if run(cmd, cwd=cwd, check=False, stream=True).returncode == 0:
            return
        if attempt < _RETRY_ATTEMPTS:
            print(f"Command failed (attempt {attempt}/{_RETRY_ATTEMPTS}); retrying in {_RETRY_SECONDS}s...",
                  file=sys.stderr, flush=True)
            time.sleep(_RETRY_SECONDS)
    raise RuntimeError(f"Command failed: {' '.join(cmd)}")


def run_no_check(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a command and return the result without raising on non-zero exit."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


# Git status --porcelain: first two chars = index + work tree; unmerged codes:
# UU = both modified, DU = deleted by us/updated by them, UD = updated by us/deleted by them,
# DD = both deleted, AA = both added
_CONFLICT_TYPE_LABELS = {
    "UU": "both modified",
    "AA": "both added",
    "DD": "both deleted",
    "DU": "modify/delete (deleted by us, changed by them)",
    "UD": "modify/delete (changed by us, deleted by them)",
}


def get_conflicted_entries(cwd: str) -> list[tuple[str, str]]:
    """Return list of (path, conflict_type_label) for unmerged paths."""
    result = subprocess.run(
        ["git", "status", "--porcelain", "-u"],
        cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    entries: list[tuple[str, str]] = []
    for line in result.stdout.strip().splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        rest = line[3:].strip()
        # Handle "old -> new" renames
        path = rest.split(" -> ")[-1].strip() if " -> " in rest else rest
        if code in _CONFLICT_TYPE_LABELS:
            entries.append((path, _CONFLICT_TYPE_LABELS[code]))
        # Other unmerged codes (UA = added by them, AU = added by us) are one-sided, not conflicts.
    return entries


def branch_exists(cwd: str, branch: str) -> bool:
    """Return True if the given local branch exists."""
    result = run_no_check(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], cwd=cwd)
    return result.returncode == 0


def is_unresolved_state(stderr: str) -> bool:
    """Return True if git stderr indicates unresolved conflicts or index state."""
    s = (stderr or "").lower()
    return "resolve" in s or "unmerged" in s or "index" in s


def is_cherry_pick_in_progress(cwd: str) -> bool:
    """Return True if a cherry-pick is in progress (e.g. after resolving conflicts)."""
    result = run_no_check(["git", "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"], cwd=cwd)
    return result.returncode == 0


def is_merge_in_progress(cwd: str) -> bool:
    result = run_no_check(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=cwd)
    return result.returncode == 0


def abort_in_progress(cwd: str) -> None:
    """Abort a cherry-pick or merge in progress, if any."""
    if is_cherry_pick_in_progress(cwd):
        run(["git", "cherry-pick", "--abort"], cwd=cwd)
    if is_merge_in_progress(cwd):
        run(["git", "merge", "--abort"], cwd=cwd)


def get_current_branch(cwd: str) -> str:
    """Return the current branch name."""
    result = subprocess.run(["git", "branch", "--show-current"], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("Could not determine current branch")
    return result.stdout.strip()


def prompt_yes_no(question: str, default_no: bool = True) -> bool:
    """Prompt with question; return True for yes, False for no."""
    suffix = " [y/N]: " if default_no else " [Y/n]: "
    try:
        answer = input(question + suffix).strip().lower()
    except EOFError:
        answer = ""
    if not answer:
        return not default_no
    return answer in ("y", "yes")


def parse_pr_url(url: str) -> tuple[str, int]:
    """Parse PR URL into (owner/repo, pr_number)."""
    match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if not match:
        raise ValueError(f"Invalid PR URL: {url}")
    owner, repo, pr_num = match.groups()
    return f"{owner}/{repo}", int(pr_num)


def parse_release_url(url: str) -> tuple[str, str]:
    """Parse release URL (https://github.com/<owner>/<repo>/releases/tag/<tag>) into (owner/repo, tag)."""
    match = re.search(r"github\.com/([^/\s]+)/([^/\s]+)/releases/tag/([^/?#\s]+)", url)
    if not match:
        raise ValueError(f"Invalid release URL: {url}")
    owner, repo, tag = match.groups()
    return f"{owner}/{repo}", tag


def release_number(tag: str) -> tuple[int, int, int]:
    """'v26.8.34.13-lts' -> (26, 8, 34)"""
    match = re.match(r"^v(\d+)\.(\d+)\.(\d+)\b", tag)
    if not match:
        raise ValueError(f"Release tag '{tag}' doesn't look like vX.Y.Z...")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def get_merge_commit_sha(pr: PullRequest) -> str:
    """Return the merge commit SHA of a merged PR."""
    if not pr.merged:
        raise RuntimeError(f"PR #{pr.number} is not merged yet")
    if not pr.merge_commit_sha:
        raise RuntimeError(f"PR #{pr.number} has no merge commit (merge strategy may not support backporting)")
    return pr.merge_commit_sha


def check_repo_dir(repo_dir: str) -> None:
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        raise RuntimeError(f"{repo_dir} is not a git repository")


def check_origin(repo_dir: str, target_repo: str) -> None:
    """Refuse unless every push URL of origin (after insteadOf rewrites) is target_repo on GitHub."""
    urls = run(["git", "remote", "get-url", "--push", "--all", "origin"], cwd=repo_dir).stdout.split()
    for url in urls or [""]:
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
        if not match or match.group(1).lower() != target_repo.lower():
            raise RuntimeError(f"Refusing: origin in {repo_dir} pushes to '{url}', not {target_repo}")


PICKED = "picked"
EMPTY = "empty"
CONFLICTS = "conflicts"


def pick_command(merge_sha: str) -> list[str]:
    return ["git", "cherry-pick", "-m", "1", merge_sha, "--no-edit"]


def merge_command(tag: str) -> list[str]:
    return ["git", "merge", "--no-ff", "--no-edit", tag]


def _start_branch(repo_dir: str, target_repo: str, target_branch: str, backport_branch: str, replace: bool) -> None:
    """
    Create backport_branch from the fresh target_branch in repo_dir (asking before discarding local state).
    With replace, an existing local backport_branch is recreated without asking.
    """
    check_repo_dir(repo_dir)
    check_origin(repo_dir, target_repo)
    print(f"Fetching latest {target_branch} from {target_repo}...")
    run_with_retry(["git", "fetch", "origin", target_branch], cwd=repo_dir)
    co_result = run_no_check(["git", "checkout", target_branch], cwd=repo_dir)
    if co_result.returncode != 0 and is_unresolved_state(co_result.stderr):
        print(co_result.stderr, file=sys.stderr)
        if not prompt_yes_no("Unresolved conflicts or dirty state. Recreate backport branch from clean target?"):
            raise RuntimeError("Aborted; repo left as is")
        abort_in_progress(repo_dir)
        run(["git", "checkout", target_branch], cwd=repo_dir)
        run_with_retry(["git", "pull", "origin", target_branch], cwd=repo_dir)
        run_no_check(["git", "branch", "-D", backport_branch], cwd=repo_dir)
    else:
        if co_result.returncode != 0:
            print(co_result.stderr, file=sys.stderr)
            raise RuntimeError(f"Command failed: git checkout {target_branch}")
        run_with_retry(["git", "pull", "origin", target_branch], cwd=repo_dir)

    if branch_exists(repo_dir, backport_branch):
        question = f"Branch '{backport_branch}' already exists. Delete and recreate from clean target?"
        if not replace and not prompt_yes_no(question):
            raise RuntimeError("Aborted")
        run(["git", "branch", "-D", backport_branch], cwd=repo_dir)

    print(f"Creating branch {backport_branch}...")
    run(["git", "checkout", "-b", backport_branch], cwd=repo_dir)


def _fetch_source(repo_dir: str, target_repo: str, source_repo: str, *fetch_args: str) -> None:
    """`git fetch <remote> *fetch_args` from origin if source_repo is target_repo, else from `upstream` (added/updated)."""
    if target_repo == source_repo:
        run_with_retry(["git", "fetch", "origin", *fetch_args], cwd=repo_dir)
        return
    fetch_url = f"https://github.com/{source_repo}.git"
    result = run_no_check(["git", "remote", "add", "upstream", fetch_url], cwd=repo_dir)
    if result.returncode != 0 and "already exists" in result.stderr:
        run(["git", "remote", "set-url", "upstream", fetch_url], cwd=repo_dir)
    print(f"Fetching from upstream ({source_repo})...")
    run_with_retry(["git", "fetch", "upstream", *fetch_args], cwd=repo_dir)


def _conflicts_or_fail(repo_dir: str, result: subprocess.CompletedProcess, cmd: list[str]) -> list[tuple[str, str]]:
    """Conflicted entries after a failed cmd; raises if the failure wasn't conflicts."""
    print(result.stderr or result.stdout, file=sys.stderr)
    conflicted = get_conflicted_entries(repo_dir)
    if not conflicted:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    return conflicted


def cherry_pick(
    repo_dir: str, target_repo: str, target_branch: str, backport_branch: str, pr_repo: str, merge_sha: str,
    replace: bool,
) -> tuple[str, list[tuple[str, str]]]:
    """
    Create backport_branch from the fresh target_branch in repo_dir and cherry-pick merge_sha.
    Returns (PICKED | EMPTY | CONFLICTS, conflicted entries). EMPTY means the change is already in
    target_branch. On CONFLICTS the cherry-pick is left in progress for resolving.
    """
    _start_branch(repo_dir, target_repo, target_branch, backport_branch, replace)
    _fetch_source(repo_dir, target_repo, pr_repo, merge_sha)

    print("Cherry-picking merge commit...")
    cmd = pick_command(merge_sha)
    cp_result = run_no_check(cmd, cwd=repo_dir)
    if cp_result.returncode == 0:
        return PICKED, []
    if not get_conflicted_entries(repo_dir) and is_cherry_pick_in_progress(repo_dir) and index_matches_head(repo_dir):
        return EMPTY, []
    return CONFLICTS, _conflicts_or_fail(repo_dir, cp_result, cmd)


def merge_tag(
    repo_dir: str, target_repo: str, target_branch: str, bump_branch: str, source_repo: str, tag: str, replace: bool
) -> tuple[str, list[tuple[str, str]]]:
    """
    Create bump_branch from the fresh target_branch in repo_dir and merge source_repo's tag into it (--no-ff).
    Returns (PICKED | EMPTY | CONFLICTS, conflicted entries). EMPTY means the tag is already in target_branch.
    On CONFLICTS the merge is left in progress for resolving.
    """
    _start_branch(repo_dir, target_repo, target_branch, bump_branch, replace)
    # Only this tag: tags of upstream's whole history would otherwise come along.
    _fetch_source(repo_dir, target_repo, source_repo, "--no-tags", f"+refs/tags/{tag}:refs/tags/{tag}")
    if is_ancestor(repo_dir, f"refs/tags/{tag}", "HEAD"):
        return EMPTY, []

    print(f"Merging {tag}...")
    cmd = merge_command(tag)
    result = run_no_check(cmd, cwd=repo_dir)
    if result.returncode == 0:
        return PICKED, []
    return CONFLICTS, _conflicts_or_fail(repo_dir, result, cmd)


def index_matches_head(repo_dir: str) -> bool:
    """True if neither the index nor the working tree differ from HEAD (tracked files; submodule work trees ignored)."""
    staged = run_no_check(["git", "diff", "--cached", "--quiet", "HEAD"], cwd=repo_dir).returncode
    unstaged = run_no_check(["git", "diff", "--quiet", "--ignore-submodules"], cwd=repo_dir).returncode
    return staged == 0 and unstaged == 0


def drop_backport_branch(repo_dir: str, target_branch: str, backport_branch: str) -> None:
    """Abort any cherry-pick or merge, go back to target_branch and delete the local backport branch."""
    abort_in_progress(repo_dir)
    run(["git", "checkout", target_branch], cwd=repo_dir)
    if branch_exists(repo_dir, backport_branch):
        run(["git", "branch", "-D", backport_branch], cwd=repo_dir)


# Set on a backport branch that `backporter check` kept; its value is the comment for the PR ('' for none).
# `git branch -D` removes it along with the branch.
_CHECKED_KEY = "backporterChecked"


def keep_checked(repo_dir: str, branch: str, comment: str) -> None:
    """Mark branch as kept by `backporter check`, with the comment its PR gets ('' for none)."""
    run(["git", "config", f"branch.{branch}.{_CHECKED_KEY}", comment], cwd=repo_dir)


def checked_comment(repo_dir: str, branch: str) -> str | None:
    """The PR comment stored by keep_checked on branch, or None if `backporter check` didn't keep it."""
    if not branch_exists(repo_dir, branch):
        return None
    result = run_no_check(["git", "config", "--get", f"branch.{branch}.{_CHECKED_KEY}"], cwd=repo_dir)
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def is_ancestor(repo_dir: str, commit: str, ref: str) -> bool:
    return run_no_check(["git", "merge-base", "--is-ancestor", commit, ref], cwd=repo_dir).returncode == 0


def commits_mentioning(repo_dir: str, ref: str, number: int, title: str, merge_sha: str) -> list[str]:
    """Commits in ref whose message names PR #number, its title, or cherry-picks merge_sha; newest first."""
    out = run(
        ["git", "log", ref, "-n", "50", "--format=%H%x00%B%x01", "-F",
         f"--grep=#{number}", f"--grep={title}", f"--grep={merge_sha}"],
        cwd=repo_dir,
    ).stdout
    number_re = re.compile(rf"#{number}(?!\d)")
    shas = []
    for record in out.split("\x01"):
        if "\x00" not in record:
            continue
        sha, message = record.strip().split("\x00", 1)
        if number_re.search(message) or title in message or merge_sha in message:
            shas.append(sha)
    return shas


def continue_in_progress(repo_dir: str, backport_branch: str) -> str:
    """
    Stage everything and finish an in-progress cherry-pick or merge on backport_branch.
    Returns PICKED, or EMPTY if a cherry-pick's resolution leaves no changes (it then stays in progress).
    A merge is always committed: it records the merged tag even when it changes no files.
    """
    check_repo_dir(repo_dir)
    if get_current_branch(repo_dir) != backport_branch:
        raise RuntimeError(f"{repo_dir} is not on {backport_branch}")
    merge = is_merge_in_progress(repo_dir)
    if not merge and not is_cherry_pick_in_progress(repo_dir):
        raise RuntimeError("No cherry-pick or merge in progress (CHERRY_PICK_HEAD/MERGE_HEAD not found)")
    print("Staging all changes...")
    run(["git", "add", "."], cwd=repo_dir)
    if not merge and index_matches_head(repo_dir):
        return EMPTY
    operation = "merge" if merge else "cherry-pick"
    print(f"Continuing {operation}...")
    env = os.environ.copy()
    env["GIT_EDITOR"] = "true"
    result = subprocess.run(
        ["git", operation, "--continue"], cwd=repo_dir, env=env, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        raise RuntimeError(f"git {operation} --continue failed")
    return PICKED


def head_sha(repo_dir: str) -> str:
    return run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()


def _commit(repo_dir: str, message: str, *extra: str) -> str:
    """Commit the index with message (comment lines stripped); returns the new HEAD sha."""
    result = subprocess.run(
        ["git", "commit", "-q", "--cleanup=strip", "-F", "-", *extra], cwd=repo_dir, input=message,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        raise RuntimeError("git commit failed")
    return head_sha(repo_dir)


def commit_conflicts(repo_dir: str, conflicts: list[tuple[str, str]]) -> str:
    """Finish the in-progress cherry-pick or merge by committing the conflicted files as they are (with markers)."""
    msg_path = os.path.join(repo_dir, run(["git", "rev-parse", "--git-path", "MERGE_MSG"], cwd=repo_dir).stdout.strip())
    message = open(msg_path).read() if os.path.exists(msg_path) else "Cherry-pick\n"
    listing = "\n".join(f"- {path} ({kind})" for path, kind in conflicts)
    message = f"{message.rstrip()}\n\nConflicts committed as-is; the next commit resolves them:\n{listing}\n"
    run(["git", "add", "-A", "--", *[path for path, _ in conflicts]], cwd=repo_dir)
    sha = _commit(repo_dir, message)
    if is_cherry_pick_in_progress(repo_dir) or is_merge_in_progress(repo_dir):
        raise RuntimeError("cherry-pick or merge still in progress after committing the conflicts")
    return sha


def commit_resolution(repo_dir: str, message: str) -> str:
    """Commit all changes to tracked files as the resolution of the previous (conflict) commit."""
    run(["git", "add", "-u"], cwd=repo_dir)
    return _commit(repo_dir, message, "--allow-empty")


def same_tree(repo_dir: str, ref: str) -> bool:
    """True if HEAD has the same content as ref (the backport changes nothing)."""
    return run_no_check(["git", "diff", "--quiet", ref, "HEAD"], cwd=repo_dir).returncode == 0


def redo(repo_dir: str, command: list[str]) -> list[tuple[str, str]]:
    """Drop the conflict commit and run command (the cherry-pick or merge) again, leaving the conflicts in progress."""
    run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=repo_dir)
    run_no_check(command, cwd=repo_dir)
    return get_conflicted_entries(repo_dir)


def checkout_pr_branch(repo_dir: str, target_repo: str, target_branch: str, branch: str, sha: str) -> str:
    """
    Check out branch at sha, which must be its head on origin (asking before resetting a local branch that differs).
    Refuses if the clone has local changes. Returns the merge base with target_branch.
    """
    check_repo_dir(repo_dir)
    check_origin(repo_dir, target_repo)
    dirty = run(["git", "status", "--porcelain", "--untracked-files=no", "--ignore-submodules"], cwd=repo_dir).stdout
    if dirty.strip() or is_cherry_pick_in_progress(repo_dir) or is_merge_in_progress(repo_dir):
        raise RuntimeError(f"{repo_dir} has local changes or a cherry-pick/merge in progress; clean it first")
    run_with_retry(
        ["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
         f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}"],
        cwd=repo_dir,
    )
    remote = run(["git", "rev-parse", f"refs/remotes/origin/{branch}"], cwd=repo_dir).stdout.strip()
    if remote != sha:
        raise RuntimeError(f"origin/{branch} is at {remote[:12]}, not at the PR's head {sha[:12]}")
    if branch_exists(repo_dir, branch):
        local = run(["git", "rev-parse", f"refs/heads/{branch}"], cwd=repo_dir).stdout.strip()
        if local != sha and not prompt_yes_no(f"Local branch '{branch}' differs from origin. Reset it to origin?"):
            raise RuntimeError("Aborted")
    remove_colliding_submodules(repo_dir, sha)
    run(["git", "checkout", "-B", branch, sha], cwd=repo_dir)
    return run(["git", "merge-base", f"refs/remotes/origin/{target_branch}", "HEAD"], cwd=repo_dir).stdout.strip()


def remove_colliding_submodules(repo_dir: str, sha: str) -> None:
    """
    Deinit submodules left in the clone (no longer submodules at sha) whose path sha has files at: their work
    tree and .git/config section go, their repository in .git/modules stays. Other leftovers are kept.
    """
    out = run_no_check(["git", "config", "--get-regexp", r"^submodule\..*\.url$"], cwd=repo_dir).stdout
    for line in out.splitlines():
        name = line.split(" ", 1)[0][len("submodule."):-len(".url")]
        entry = run(["git", "ls-tree", sha, "--", name], cwd=repo_dir).stdout
        if not entry or entry.startswith("160000 "):
            continue
        print(f"Removing submodule {name}: {sha[:12]} has files there")
        if os.path.exists(os.path.join(repo_dir, name, ".git")):
            shutil.rmtree(os.path.join(repo_dir, name))
        run(["git", "config", "--remove-section", f"submodule.{name}"], cwd=repo_dir)


def discard_changes(repo_dir: str) -> None:
    """Drop uncommitted changes to tracked files."""
    run(["git", "reset", "-q", "--hard", "HEAD"], cwd=repo_dir)


def push(repo_dir: str, branch: str, target_repo: str) -> None:
    check_origin(repo_dir, target_repo)
    print(f"Pushing {branch} to {target_repo}...")
    run_with_retry(["git", "push", "origin", f"refs/heads/{branch}:refs/heads/{branch}"], cwd=repo_dir)
