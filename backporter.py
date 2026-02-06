#!/usr/bin/env python3
"""
Backport a GitHub PR to a target branch.

Usage:
    python backporter.py --target owner/repo:branch --pr https://github.com/owner/repo/pull/123

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


def run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    return result


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
        epilog="Example: python backporter.py -t owner/repo:main -p https://github.com/owner/repo/pull/42",
    )
    parser.add_argument(
        "-t", "--target",
        required=True,
        help="Target in format owner/repo:branch (e.g. myorg/myrepo:main)",
    )
    parser.add_argument(
        "-p", "--pr",
        required=True,
        help="URL of the PR to backport (e.g. https://github.com/owner/repo/pull/123)",
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

    target_repo, target_branch = parse_target(args.target)
    pr_repo, pr_number = parse_pr_url(args.pr)

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

    try:
        if args.repo_dir:
            # Use existing repo: fetch and checkout target branch
            print(f"Fetching latest from {target_repo}...")
            run(["git", "fetch", "origin", target_branch], cwd=repo_dir)
            run(["git", "checkout", target_branch], cwd=repo_dir)
            run(["git", "pull", "origin", target_branch], cwd=repo_dir)
        else:
            # Clone the target repo
            print(f"Cloning {target_repo}...")
            run(["git", "clone", "--branch", target_branch, clone_url, repo_dir])

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
        run(["git", "cherry-pick", "-m", "1", merge_sha, "--no-edit"], cwd=repo_dir)

        # Push to target repo
        print(f"Pushing {backport_branch} to {target_repo}...")
        run(["git", "push", "origin", backport_branch], cwd=repo_dir)

        print(f"\nDone! Branch {backport_branch} has been pushed to {target_repo}.")

    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if cleanup_work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
