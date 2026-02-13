# backporter

A simple Python script to backport GitHub PRs to a target branch.

## Usage

```bash
python backporter.py --target owner/repo:branch --pr https://github.com/owner/repo/pull/123
```

Or with short flags:

```bash
python backporter.py -t myorg/myrepo:main -p https://github.com/myorg/myrepo/pull/42
```

## What it does

1. Clones the target repo and checks out the target branch
2. Creates a new branch named `<target_branch>/<PR_number>` (e.g. `main/42`)
3. Cherry-picks the PR merge commit into this branch
4. Pushes the branch to the target repo

## Requirements

- Python 3.10+
- Git
- [PyGithub](https://github.com/PyGithub/PyGithub): `pip install -r requirements.txt` (or use a venv)
- Network access to GitHub (HTTPS; use `git credential` for private repos)

A `GITHUB_TOKEN` env var (or `--token`) is recommended for private repos and to avoid API rate limits.

## Options

| Option | Description |
|--------|-------------|
| `-t`, `--target` | Target in format `owner/repo:branch` (required unless `--make-description` only) |
| `-p`, `--pr` | URL of the PR to backport |
| `--make-description` | Output changelog description (category + entry) from the PR body to stdout |
| `-C`, `--repo-dir` | Use existing cloned repo instead of cloning (avoids re-cloning large repos) |
| `--work-dir` | Directory to clone into when not using -C (default: temp dir, deleted after) |
| `--token` | GitHub token for API (default: GITHUB_TOKEN env var) |

## Using an existing clone

For large repos, reuse your existing clone instead of re-cloning:

```bash
python3 backporter.py -C /path/to/repo -t myorg/myrepo:main -p https://github.com/myorg/myrepo/pull/42
```

The script will fetch the latest target branch, create the backport branch, cherry-pick, and push. Use a clean working tree.

If the backport branch already exists locally, the script prompts: **Delete and recreate from clean target? [y/N]**  
- **No** (or Enter): aborts the backport; if `--make-description` was given, only the changelog description is printed.  
- **Yes**: the existing branch is deleted and recreated from the current target branch, then the cherry-pick runs as usual.

If the repo is in a conflicted or dirty state (e.g. you left a cherry-pick with conflicts), `git checkout` to the target branch will fail. The script then prompts: **Unresolved conflicts or dirty state. Recreate backport branch from clean target? [y/N]**  
- **No**: leaves the repo as is; if `--make-description` was given, the changelog description is printed.  
- **Yes**: runs `git cherry-pick --abort`, checks out the target branch, pulls, deletes the backport branch, then recreates it and runs the cherry-pick again.

## Conflicts

If the cherry-pick hits merge conflicts, the script does **not** abort the cherry-pick: the branch is left with conflicts for you to resolve manually. The script prints the list of conflicted files and exits with code 1. If `--make-description` was given, the changelog description is still printed. After resolving conflicts, run `git add <paths>` and `git cherry-pick --continue`, then push when ready.

## Changelog description

To output the changelog description (Changelog category + Changelog entry) from a PR body to stdout:

```bash
python backporter.py --make-description -p https://github.com/owner/repo/pull/123
```

With `--target`, the description is printed after a successful backport. Without `--target`, only the description is printed (no clone/cherry-pick/push).

## Fork backports

When your target repo is a fork of the base repo (where the PR was merged), the script adds the base repo as an `upstream` remote and fetches from it. Cherry-pick uses `-m 1` for merge commits.

```bash
python3 backporter.py -C /path/to/your/fork -t myorg/repo:main -p https://github.com/upstream/repo/pull/100
```
