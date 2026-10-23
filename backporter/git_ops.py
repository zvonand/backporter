"""
Git and GitHub PR helpers for cherry-picking a merged PR into a local clone.
"""

import os
import re
import subprocess
import sys

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


def cherry_pick(
    repo_dir: str, target_repo: str, target_branch: str, backport_branch: str, pr_repo: str, merge_sha: str
) -> tuple[str, list[tuple[str, str]]]:
    """
    Create backport_branch from the fresh target_branch in repo_dir and cherry-pick merge_sha.
    Returns (PICKED | EMPTY | CONFLICTS, conflicted entries). EMPTY means the change is already in
    target_branch. On CONFLICTS the cherry-pick is left in progress for resolving.
    """
    check_repo_dir(repo_dir)
    check_origin(repo_dir, target_repo)
    print(f"Fetching latest {target_branch} from {target_repo}...")
    run(["git", "fetch", "origin", target_branch], cwd=repo_dir, stream=True)
    co_result = run_no_check(["git", "checkout", target_branch], cwd=repo_dir)
    if co_result.returncode != 0 and is_unresolved_state(co_result.stderr):
        print(co_result.stderr, file=sys.stderr)
        if not prompt_yes_no("Unresolved conflicts or dirty state. Recreate backport branch from clean target?"):
            raise RuntimeError("Aborted; repo left as is")
        run_no_check(["git", "cherry-pick", "--abort"], cwd=repo_dir)
        run(["git", "checkout", target_branch], cwd=repo_dir)
        run(["git", "pull", "origin", target_branch], cwd=repo_dir, stream=True)
        run_no_check(["git", "branch", "-D", backport_branch], cwd=repo_dir)
    else:
        if co_result.returncode != 0:
            print(co_result.stderr, file=sys.stderr)
            raise RuntimeError(f"Command failed: git checkout {target_branch}")
        run(["git", "pull", "origin", target_branch], cwd=repo_dir, stream=True)

    if branch_exists(repo_dir, backport_branch):
        if not prompt_yes_no(f"Branch '{backport_branch}' already exists. Delete and recreate from clean target?"):
            raise RuntimeError("Aborted")
        run(["git", "branch", "-D", backport_branch], cwd=repo_dir)

    print(f"Creating branch {backport_branch}...")
    run(["git", "checkout", "-b", backport_branch], cwd=repo_dir)

    # The repo where the PR was merged is upstream of the target fork
    if target_repo == pr_repo:
        run(["git", "fetch", "origin", merge_sha], cwd=repo_dir, stream=True)
    else:
        pr_fetch_url = f"https://github.com/{pr_repo}.git"
        result = run_no_check(["git", "remote", "add", "upstream", pr_fetch_url], cwd=repo_dir)
        if result.returncode != 0 and "already exists" in result.stderr:
            run(["git", "remote", "set-url", "upstream", pr_fetch_url], cwd=repo_dir)
        print(f"Fetching from upstream ({pr_repo})...")
        run(["git", "fetch", "upstream", merge_sha], cwd=repo_dir, stream=True)

    print("Cherry-picking merge commit...")
    cp_result = run_no_check(["git", "cherry-pick", "-m", "1", merge_sha, "--no-edit"], cwd=repo_dir)
    if cp_result.returncode == 0:
        return PICKED, []
    conflicted = get_conflicted_entries(repo_dir)
    if conflicted:
        print(cp_result.stderr, file=sys.stderr)
        return CONFLICTS, conflicted
    if is_cherry_pick_in_progress(repo_dir) and index_matches_head(repo_dir):
        return EMPTY, []
    print(cp_result.stderr, file=sys.stderr)
    raise RuntimeError(f"Command failed: git cherry-pick -m 1 {merge_sha} --no-edit")


def index_matches_head(repo_dir: str) -> bool:
    """True if neither the index nor the working tree differ from HEAD (tracked files)."""
    staged = run_no_check(["git", "diff", "--cached", "--quiet", "HEAD"], cwd=repo_dir).returncode
    unstaged = run_no_check(["git", "diff", "--quiet"], cwd=repo_dir).returncode
    return staged == 0 and unstaged == 0


def drop_backport_branch(repo_dir: str, target_branch: str, backport_branch: str) -> None:
    """Abort any cherry-pick, go back to target_branch and delete the local backport branch."""
    if is_cherry_pick_in_progress(repo_dir):
        run(["git", "cherry-pick", "--abort"], cwd=repo_dir)
    run(["git", "checkout", target_branch], cwd=repo_dir)
    if branch_exists(repo_dir, backport_branch):
        run(["git", "branch", "-D", backport_branch], cwd=repo_dir)


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


def continue_cherry_pick(repo_dir: str, backport_branch: str) -> str:
    """
    Stage everything and finish an in-progress cherry-pick on backport_branch.
    Returns PICKED, or EMPTY if the resolution leaves no changes (the cherry-pick stays in progress).
    """
    check_repo_dir(repo_dir)
    if get_current_branch(repo_dir) != backport_branch:
        raise RuntimeError(f"{repo_dir} is not on {backport_branch}")
    if not is_cherry_pick_in_progress(repo_dir):
        raise RuntimeError("No cherry-pick in progress (CHERRY_PICK_HEAD not found)")
    print("Staging all changes...")
    run(["git", "add", "."], cwd=repo_dir)
    if index_matches_head(repo_dir):
        return EMPTY
    print("Continuing cherry-pick...")
    env = os.environ.copy()
    env["GIT_EDITOR"] = "true"
    result = subprocess.run(
        ["git", "cherry-pick", "--continue"], cwd=repo_dir, env=env, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        raise RuntimeError("git cherry-pick --continue failed")
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
    """Finish the in-progress cherry-pick by committing the conflicted files as they are (with markers)."""
    msg_path = os.path.join(repo_dir, run(["git", "rev-parse", "--git-path", "MERGE_MSG"], cwd=repo_dir).stdout.strip())
    message = open(msg_path).read() if os.path.exists(msg_path) else "Cherry-pick\n"
    listing = "\n".join(f"- {path} ({kind})" for path, kind in conflicts)
    message = f"{message.rstrip()}\n\nConflicts committed as-is; the next commit resolves them:\n{listing}\n"
    run(["git", "add", "-A", "--", *[path for path, _ in conflicts]], cwd=repo_dir)
    sha = _commit(repo_dir, message)
    if is_cherry_pick_in_progress(repo_dir):
        raise RuntimeError("cherry-pick still in progress after committing the conflicts")
    return sha


def commit_resolution(repo_dir: str, message: str) -> str:
    """Commit all changes to tracked files as the resolution of the previous (conflict) commit."""
    run(["git", "add", "-u"], cwd=repo_dir)
    return _commit(repo_dir, message, "--allow-empty")


def same_tree(repo_dir: str, ref: str) -> bool:
    """True if HEAD has the same content as ref (the backport changes nothing)."""
    return run_no_check(["git", "diff", "--quiet", ref, "HEAD"], cwd=repo_dir).returncode == 0


def redo_pick(repo_dir: str, merge_sha: str) -> list[tuple[str, str]]:
    """Drop the conflict commit and cherry-pick again, leaving the conflicts in progress for a human."""
    run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=repo_dir)
    run_no_check(["git", "cherry-pick", "-m", "1", merge_sha, "--no-edit"], cwd=repo_dir)
    return get_conflicted_entries(repo_dir)


def push(repo_dir: str, branch: str, target_repo: str) -> None:
    check_origin(repo_dir, target_repo)
    print(f"Pushing {branch} to {target_repo}...")
    run(["git", "push", "origin", f"refs/heads/{branch}:refs/heads/{branch}"], cwd=repo_dir, stream=True)
