"""
Backport a GitHub PR to a target branch.

Usage:
    backporter --target owner/repo:branch --pr https://github.com/owner/repo/pull/123

The script will:
1. Use the target repo (-C for existing clone) and create branch <target_branch>/<PR_number>
2. Cherry-pick the PR merge commit into this branch
3. Push to the target repo
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

from github import Github

from backporter.changelog_extract import make_changelog_description


def run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
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


def get_conflicted_files(cwd: str) -> list[str]:
    """Return list of paths with merge conflicts (unmerged)."""
    return [path for path, _ in get_conflicted_entries(cwd)]


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
            label = _CONFLICT_TYPE_LABELS[code]
            entries.append((path, label))
        # Exclude other unmerged codes (e.g. UA = added by them, AU = added by us):
        # we only report actual conflicts (both modified, both added, both deleted,
        # or modify/delete), not "new file" or one-sided add.
    return entries


def branch_exists(cwd: str, branch: str) -> bool:
    """Return True if the given local branch exists."""
    result = run_no_check(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=cwd,
    )
    return result.returncode == 0


def is_unresolved_state(stderr: str) -> bool:
    """Return True if git stderr indicates unresolved conflicts or index state."""
    s = (stderr or "").lower()
    return "resolve" in s or "unmerged" in s or "index" in s


def is_cherry_pick_in_progress(cwd: str) -> bool:
    """Return True if a cherry-pick is in progress (e.g. after resolving conflicts)."""
    result = run_no_check(
        ["git", "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"],
        cwd=cwd,
    )
    return result.returncode == 0


def get_current_branch(cwd: str) -> str:
    """Return the current branch name."""
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=cwd, capture_output=True, text=True
    )
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


def parse_target(target: str) -> tuple[str, str]:
    """Parse 'owner/repo:branch' into (owner/repo, branch)."""
    if ":" not in target:
        raise ValueError("Target must be in format owner/repo:branch")
    repo, branch = target.rsplit(":", 1)
    if not repo or not branch:
        raise ValueError("Target must be in format owner/repo:branch")
    return repo.strip(), branch.strip()


def parse_pr_url(url: str) -> tuple[str, int]:
    """Parse PR URL into (owner/repo, pr_number)."""
    # Match: https://github.com/owner/repo/pull/123 or github.com/owner/repo/pull/123
    match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if not match:
        raise ValueError(f"Invalid PR URL: {url}")
    owner, repo, pr_num = match.groups()
    return f"{owner}/{repo}", int(pr_num)


def get_merge_commit_sha(pr_repo: str, pr_number: int, token: str | None) -> str:
    """Get the merge commit SHA for a PR via GitHub API."""
    gh = Github(token) if token else Github()
    repo = gh.get_repo(pr_repo)
    pr = repo.get_pull(pr_number)

    if not pr.merged:
        raise RuntimeError(f"PR #{pr_number} is not merged yet")

    merge_sha = pr.merge_commit_sha
    if not merge_sha:
        raise RuntimeError(f"PR #{pr_number} has no merge commit (merge strategy may not support backporting)")

    return merge_sha


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backport a GitHub PR to a target branch.",
        epilog="Example: backporter -t owner/repo:main -p https://github.com/owner/repo/pull/42",
    )
    parser.add_argument(
        "-t", "--target",
        help="Target in format owner/repo:branch (e.g. myorg/myrepo:main)",
    )
    parser.add_argument(
        "-p", "--pr",
        help="URL of the PR to backport (e.g. https://github.com/owner/repo/pull/123)",
    )
    parser.add_argument(
        "--make-description",
        action="store_true",
        help="Output changelog description (category + entry) from the PR body to stdout",
    )
    parser.add_argument(
        "--conflicts-resolved",
        action="store_true",
        dest="conflicts_resolved",
        help="Finish backport after resolving conflicts: git add ., cherry-pick --continue, push",
    )
    parser.add_argument(
        "-C", "--repo-dir",
        dest="repo_dir",
        help="Use existing repo directory instead of cloning",
    )
    parser.add_argument(
        "--work-dir",
        help="Directory to clone into when not using -C (default: temp dir)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token for API access (default: GITHUB_TOKEN env var)",
    )
    args = parser.parse_args()

    if args.conflicts_resolved:
        if not args.repo_dir or not args.target:
            parser.error("--conflicts-resolved requires -C and -t")
        target_repo, _ = parse_target(args.target)
        repo_dir = os.path.abspath(args.repo_dir)
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            print(f"Error: {repo_dir} is not a git repository", file=sys.stderr)
            sys.exit(1)
        if not is_cherry_pick_in_progress(repo_dir):
            print("Error: no cherry-pick in progress (CHERRY_PICK_HEAD not found).", file=sys.stderr)
            sys.exit(1)
        print("Staging all changes...")
        run(["git", "add", "."], cwd=repo_dir)
        print("Continuing cherry-pick...")
        env = os.environ.copy()
        env["GIT_EDITOR"] = "true"
        result = subprocess.run(
            ["git", "cherry-pick", "--continue"],
            cwd=repo_dir, env=env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(result.stderr or result.stdout, file=sys.stderr)
            sys.exit(1)
        current_branch = get_current_branch(repo_dir)
        print(f"Pushing {current_branch} to {target_repo}...")
        run(["git", "push", "origin", current_branch], cwd=repo_dir)
        print(f"\nDone! Branch {current_branch} has been pushed to {target_repo}.")
        sys.exit(0)

    if not args.pr:
        parser.error("--pr is required (unless --conflicts-resolved is used)")
    if not args.make_description and not args.target:
        parser.error("--target is required unless --make-description is used")

    pr_repo, pr_number = parse_pr_url(args.pr)

    # --make-description only: fetch PR body, output changelog description, exit
    if args.make_description and not args.target:
        gh = Github(args.token) if args.token else Github()
        repo = gh.get_repo(pr_repo)
        pr = repo.get_pull(pr_number)
        description = make_changelog_description(
            pr.body,
            pr_url=pr.html_url,
            pr_author=pr.user.login if pr.user else None,
        )
        print(description)
        return

    target_repo, target_branch = parse_target(args.target)

    backport_branch = f"backports/{target_branch}/{pr_number}"
    clone_url = f"https://github.com/{target_repo}.git"
    pr_fetch_url = f"https://github.com/{pr_repo}.git"

    # Get merge commit SHA via GitHub API
    print(f"Fetching PR #{pr_number} merge commit SHA...")
    merge_sha = get_merge_commit_sha(pr_repo, pr_number, args.token)

    if args.repo_dir:
        repo_dir = os.path.abspath(args.repo_dir)
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            print(f"Error: {repo_dir} is not a git repository", file=sys.stderr)
            sys.exit(1)
        cleanup_work_dir = False
    else:
        work_dir = args.work_dir or tempfile.mkdtemp(prefix="backporter-")
        repo_dir = os.path.join(work_dir, "repo")
        cleanup_work_dir = not args.work_dir

    def _print_description_and_exit(exit_code: int = 0) -> None:
        if args.make_description:
            gh = Github(args.token) if args.token else Github()
            repo_obj = gh.get_repo(pr_repo)
            pr = repo_obj.get_pull(pr_number)
            description = make_changelog_description(
                pr.body,
                pr_url=pr.html_url,
                pr_author=pr.user.login if pr.user else None,
            )
            if description:
                print("\n--- Changelog description ---\n")
                print(description)
        sys.exit(exit_code)

    try:
        if args.repo_dir:
            # Use existing repo: fetch and checkout target branch
            print(f"Fetching latest from {target_repo}...")
            run(["git", "fetch", "origin", target_branch], cwd=repo_dir)
            co_result = run_no_check(["git", "checkout", target_branch], cwd=repo_dir)
            if co_result.returncode != 0 and is_unresolved_state(co_result.stderr):
                print(co_result.stderr, file=sys.stderr)
                if not prompt_yes_no(
                    "Unresolved conflicts or dirty state. Recreate backport branch from clean target?"
                ):
                    print("Leaving repo as is.", file=sys.stderr)
                    _print_description_and_exit(0)
                run_no_check(["git", "cherry-pick", "--abort"], cwd=repo_dir)
                run(["git", "checkout", target_branch], cwd=repo_dir)
                run(["git", "pull", "origin", target_branch], cwd=repo_dir)
                run(["git", "branch", "-D", backport_branch], cwd=repo_dir)
            else:
                if co_result.returncode != 0:
                    print(co_result.stderr, file=sys.stderr)
                    raise RuntimeError(f"Command failed: git checkout {target_branch}")
                run(["git", "pull", "origin", target_branch], cwd=repo_dir)
        else:
            # Clone the target repo
            print(f"Cloning {target_repo}...")
            run(["git", "clone", "--branch", target_branch, clone_url, repo_dir])

        # If backport branch already exists, prompt
        if branch_exists(repo_dir, backport_branch):
            if not prompt_yes_no(
                f"Branch '{backport_branch}' already exists. Delete and recreate from clean target?"
            ):
                print("Aborted.", file=sys.stderr)
                _print_description_and_exit(0)
            run(["git", "branch", "-D", backport_branch], cwd=repo_dir)

        # Create the backport branch (checkout -b)
        print(f"Creating branch {backport_branch}...")
        run(["git", "checkout", "-b", backport_branch], cwd=repo_dir)

        # Fetch and cherry-pick the PR merge commit
        # Base repo (where PR was merged) is upstream of the target fork
        if target_repo == pr_repo:
            run(["git", "fetch", "origin", merge_sha], cwd=repo_dir)
        else:
            # Add upstream remote (or update URL if it exists)
            result = subprocess.run(
                ["git", "remote", "add", "upstream", pr_fetch_url],
                cwd=repo_dir, capture_output=True, text=True
            )
            if result.returncode != 0 and "already exists" in result.stderr:
                run(["git", "remote", "set-url", "upstream", pr_fetch_url], cwd=repo_dir)
            print(f"Fetching from upstream ({pr_repo})...")
            run(["git", "fetch", "upstream", merge_sha], cwd=repo_dir)

        print("Cherry-picking merge commit...")
        cp_result = run_no_check(
            ["git", "cherry-pick", "-m", "1", merge_sha, "--no-edit"],
            cwd=repo_dir,
        )

        def _print_description() -> None:
            if args.make_description:
                gh = Github(args.token) if args.token else Github()
                repo_obj = gh.get_repo(pr_repo)
                pr = repo_obj.get_pull(pr_number)
                description = make_changelog_description(
                    pr.body,
                    pr_url=pr.html_url,
                    pr_author=pr.user.login if pr.user else None,
                )
                if description:
                    print("\n--- Changelog description ---\n")
                    print(description)

        if cp_result.returncode != 0:
            # Cherry-pick failed; may be conflicts
            print(cp_result.stderr, file=sys.stderr)
            conflicted = get_conflicted_entries(repo_dir)
            if conflicted:
                print(
                    f"\nConflicts in {len(conflicted)} file(s); branch left with conflicts for manual resolve:",
                    file=sys.stderr,
                )
                for path, kind in conflicted:
                    print(f"  {path}  ({kind})", file=sys.stderr)
                print(
                    "\nResolve conflicts, then run: git add <paths> && git cherry-pick --continue",
                    file=sys.stderr,
                )
                print(
                    "Or after resolving: backporter --conflicts-resolved -C <repo-dir> -t <target>",
                    file=sys.stderr,
                )
                _print_description()
                sys.exit(1)
            raise RuntimeError(
                f"Command failed: git cherry-pick -m 1 {merge_sha} --no-edit"
            )

        # Push to target repo
        print(f"Pushing {backport_branch} to {target_repo}...")
        run(["git", "push", "origin", backport_branch], cwd=repo_dir)

        print(f"\nDone! Branch {backport_branch} has been pushed to {target_repo}.")
        _print_description()

    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if cleanup_work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
