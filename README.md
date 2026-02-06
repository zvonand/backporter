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
| `-t`, `--target` | Target in format `owner/repo:branch` |
| `-p`, `--pr` | URL of the PR to backport |
| `-C`, `--repo-dir` | Use existing cloned repo instead of cloning (avoids re-cloning large repos) |
| `--work-dir` | Directory to clone into when not using -C (default: temp dir, deleted after) |
| `--token` | GitHub token for API (default: GITHUB_TOKEN env var) |

## Using an existing clone

For large repos, reuse your existing clone instead of re-cloning:

```bash
python3 backporter.py -C /path/to/repo -t myorg/myrepo:main -p https://github.com/myorg/myrepo/pull/42
```

The script will fetch the latest target branch, create the backport branch, cherry-pick, and push. Use a clean working tree.

## Fork backports

When your target repo is a fork of the base repo (where the PR was merged), the script adds the base repo as an `upstream` remote and fetches from it. Cherry-pick uses `-m 1` for merge commits.

```bash
python3 backporter.py -C /path/to/your/fork -t myorg/repo:main -p https://github.com/upstream/repo/pull/100
```
