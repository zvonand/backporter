"""
AI resolution of cherry-pick (backport) and merge (bump) conflicts.

Backend: the Anthropic API when ANTHROPIC_API_KEY is set, otherwise the `claude` CLI (Claude plan limits).
The AI works only in the clone dir and reports one outcome; the backporter does all git/GitHub work.
It runs with no GitHub credentials and with git network access disabled, so it cannot push, open PRs
or comment anywhere.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

from backporter import git_ops

RESOLVED = "RESOLVED"
ALREADY_PRESENT = "ALREADY_PRESENT"
NOT_APPLICABLE = "NOT_APPLICABLE"
UNRESOLVED = "UNRESOLVED"
OUTCOMES = (RESOLVED, ALREADY_PRESENT, NOT_APPLICABLE, UNRESOLVED)

TIMEOUT_SECONDS = 2 * 3600

_SECRET_VARS = ("GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "ANTHROPIC_API_KEY")

# Read-only git subcommands plus staging; `git checkout` only as `--ours`/`--theirs`.
GIT_SUBCOMMANDS = (
    "status", "diff", "log", "show", "grep", "blame", "ls-files", "merge-base", "rev-parse", "cat-file",
    "add", "rm", "checkout",
)
OTHER_COMMANDS = ("ls", "cat", "head", "tail", "grep", "rg", "wc")

_CLI_DENY = [
    "Bash(git push *)", "Bash(git commit *)", "Bash(git cherry-pick *)", "Bash(git reset *)", "Bash(git fetch *)",
    "Bash(git remote *)", "Bash(git config *)", "Bash(gh *)", "Bash(curl *)", "Bash(wget *)", "Bash(ssh *)",
    "WebFetch", "WebSearch",
    # Credential locations, should a path rule below ever match them.
    "Read(~/.config/**)", "Read(~/.ssh/**)", "Read(~/.claude/**)", "Read(~/.netrc)", "Read(~/.git-credentials)",
    "Read(~/.aws/**)", "Read(~/.docker/**)", "Read(~/.kube/**)", "Read(~/.gnupg/**)",
]


@dataclass
class Result:
    outcome: str
    note: str
    by: str = ""
    # A few Markdown lines on what conflicted and how it was resolved
    summary: str = ""


def unavailable() -> str | None:
    """Why AI can't run here, or None if it can."""
    if os.environ.get("ANTHROPIC_API_KEY") or shutil.which("claude"):
        return None
    return "neither the `claude` CLI nor ANTHROPIC_API_KEY is available"


def locked_env() -> dict[str, str]:
    """Environment for everything the AI runs: no GitHub/Anthropic credentials, no git network access."""
    env = {k: v for k, v in os.environ.items() if k not in _SECRET_VARS}
    env["GH_CONFIG_DIR"] = tempfile.mkdtemp(prefix="backporter-gh-")
    env["GIT_ALLOW_PROTOCOL"] = "file"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_EDITOR"] = "true"
    return env


def build_prompt(repo_dir: str, branch: str, backport_branch: str, pr_url: str, pr_title: str, pr_body: str,
                 merge_sha: str, conflicts: list[tuple[str, str]], conflict_sha: str) -> str:
    files = "\n".join(f"- {path} ({kind})" for path, kind in conflicts)
    return f"""You are resolving a git cherry-pick conflict for a ClickHouse backport.

Repository: {repo_dir} (your working directory)
Target branch: {branch} (an older ClickHouse release line)
Current branch: {backport_branch}, created from {branch}
Being cherry-picked: merge commit {merge_sha} of {pr_url}
Source PR title: {pr_title}

Source PR description:
<pr_description>
{pr_body or "(empty)"}
</pr_description>

The cherry-pick (`git cherry-pick -m 1 {merge_sha}`) had conflicts in:
{files}

The cherry-pick was committed as-is, conflict markers included, as HEAD ({conflict_sha}). See it with `git show HEAD`, and the original change with `git diff {merge_sha}^1 {merge_sha}`.

Your job: make this branch contain the source PR's change, adapted to {branch}. Then decide on exactly one outcome:
- RESOLVED: you resolved every conflict, keeping the intent of the source PR and the surrounding code of {branch}. Edit the files to remove all conflict markers and stage each one with `git add <path>` (or `git rm <path>`). The backporter commits your changes as a separate resolution commit.
- ALREADY_PRESENT: {branch} already contains this change (equivalent code is already there), so nothing needs backporting. If `git log` shows the commit or PR that brought it, give its PR URL.
- NOT_APPLICABLE: the change makes no sense for {branch}, e.g. the code or feature it fixes doesn't exist there. Explain why.
- UNRESOLVED: you cannot resolve it with confidence. Explain what blocks you.

Rules:
- Work only inside {repo_dir}. Change only files involved in this cherry-pick.
- Do not commit, switch branches, reset, fetch or push. The backporter does that.
- Use one plain command per Bash call, with no pipes, redirections or `&&`.
- You have no network or GitHub access, so don't try to use them.

End your answer with these lines, exactly:
OUTCOME: <RESOLVED|ALREADY_PRESENT|NOT_APPLICABLE|UNRESOLVED>
NOTE: <one line: what you adapted, or why>
BY: <PR URL that already brought the change; only for ALREADY_PRESENT, omit if unknown>
SUMMARY:
<2-5 short Markdown bullet points for reviewers: what conflicted in which file, why, and how you resolved it>
"""


def build_merge_prompt(repo_dir: str, branch: str, bump_branch: str, release_url: str, tag: str,
                       conflicts: list[tuple[str, str]], conflict_sha: str) -> str:
    files = "\n".join(f"- {path} ({kind})" for path, kind in conflicts)
    return f"""You are resolving git merge conflicts in a ClickHouse version bump.

Repository: {repo_dir} (your working directory)
Target branch: {branch}, Altinity's release line with its own changes on top of upstream ClickHouse
Current branch: {bump_branch}, created from {branch}
Being merged: upstream release tag {tag} ({release_url}), a newer release of the same version

The merge (`git merge --no-ff {tag}`) had conflicts in:
{files}

The merge was committed as-is, conflict markers included, as HEAD ({conflict_sha}). HEAD^1 is {branch}, HEAD^2 is {tag}.
See upstream's changes with `git diff HEAD^1...HEAD^2 -- <path>` and Altinity's with `git diff HEAD^2...HEAD^1 -- <path>`.

Your job: resolve every conflict so that the branch has upstream's changes from {tag} and keeps Altinity's changes from {branch}. Then decide on exactly one outcome:
- RESOLVED: you resolved every conflict. Edit the files to remove all conflict markers and stage each one with `git add <path>` (or `git rm <path>`). The backporter commits your changes as a separate resolution commit.
- UNRESOLVED: you cannot resolve it with confidence. Explain what blocks you.

Rules:
- Work only inside {repo_dir}. Change only files involved in this merge.
- Do not commit, switch branches, reset, fetch or push. The backporter does that.
- Use one plain command per Bash call, with no pipes, redirections or `&&`.
- You have no network or GitHub access, so don't try to use them.

End your answer with these lines, exactly:
OUTCOME: <RESOLVED|UNRESOLVED>
NOTE: <one line: what you resolved and how, or why you couldn't>
SUMMARY:
<2-5 short Markdown bullet points for reviewers: what conflicted in which file, why, and how you resolved it>
"""


def parse_result(text: str) -> Result:
    fields: dict[str, str] = {}
    lines = text.splitlines()
    summary_at = next((i for i, line in enumerate(lines) if re.match(r"^\s*SUMMARY:\s*$", line)), len(lines))
    for line in lines[:summary_at]:
        match = re.match(r"^\s*(OUTCOME|NOTE|BY):\s*(.*?)\s*$", line)
        if match:
            fields[match.group(1)] = match.group(2)
    summary = "\n".join(lines[summary_at + 1:]).strip()
    outcome = fields.get("OUTCOME", "").upper()
    if outcome not in OUTCOMES:
        return Result(UNRESOLVED, "AI did not report an outcome")
    by = fields.get("BY", "")
    try:
        git_ops.parse_pr_url(by)
    except ValueError:
        by = ""
    return Result(outcome, fields.get("NOTE", ""), by, summary)


def check_resolved(repo_dir: str, head_before: str, conflicts: list[tuple[str, str]]) -> str | None:
    """Why a RESOLVED report doesn't hold, or None if the working tree is ready for the resolution commit."""
    if git_ops.head_sha(repo_dir) != head_before:
        return "AI moved HEAD (committed or switched branches)"
    for path, _ in conflicts:
        full = os.path.join(repo_dir, path)
        if os.path.isfile(full):
            with open(full, errors="replace") as f:
                if re.search(r"^(<{7}|>{7})( |$)", f.read(), re.MULTILINE):
                    return f"conflict markers left in {path}"
    return None


def resolve(repo_dir: str, prompt: str, conflicts: list[tuple[str, str]]) -> Result:
    head_before = git_ops.run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()
    if os.environ.get("ANTHROPIC_API_KEY"):
        from backporter import ai_api

        print("AI: resolving with the Anthropic API...")
        text = ai_api.run_agent(repo_dir, prompt, os.environ["ANTHROPIC_API_KEY"], locked_env())
    else:
        print("AI: resolving with the claude CLI...")
        text = _run_cli(repo_dir, prompt, _resolve_tools())
    result = parse_result(text)
    if result.outcome == RESOLVED:
        problem = check_resolved(repo_dir, head_before, conflicts)
        if problem:
            return Result(UNRESOLVED, f"AI reported resolved, but {problem}")
    print(f"AI: {result.outcome}: {result.note}")
    return result


def complete(prompt: str) -> str:
    """A single text answer, without any tools (API if ANTHROPIC_API_KEY is set, else the claude CLI)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        from backporter import ai_api

        return ai_api.complete(prompt, os.environ["ANTHROPIC_API_KEY"])
    return _run_cli(tempfile.mkdtemp(prefix="backporter-ai-"), prompt, [])


def _resolve_tools() -> list[str]:
    # File tools only inside the repo (paths relative to the working directory).
    allowed = ["Read(./**)", "Edit(./**)", "Write(./**)", "Glob(./**)", "Grep(./**)"]
    allowed += [f"Bash(git {sub} *)" for sub in GIT_SUBCOMMANDS if sub != "checkout"]
    allowed += ["Bash(git checkout --ours *)", "Bash(git checkout --theirs *)"]
    allowed += [f"Bash({cmd} *)" for cmd in OTHER_COMMANDS]
    return allowed


def _run_cli(cwd: str, prompt: str, allowed: list[str]) -> str:
    """Run `claude -p` in cwd, sandboxed to the `allowed` tools (none if empty); returns its final text."""
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("`claude` CLI not found on PATH; install Claude Code or set ANTHROPIC_API_KEY")
    cmd = [
        claude, "-p", "--output-format", "stream-json", "--verbose",
        # No user/project settings, MCP servers, plugins or hooks: only the tools listed here are usable.
        "--safe-mode", "--strict-mcp-config", "--setting-sources", "",
        "--permission-mode", "dontAsk", "--permission-prompts", "none", "--no-session-persistence",
        *(["--allowedTools", *allowed] if allowed else []), "--disallowedTools", *_CLI_DENY,
    ]
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=locked_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    proc.stdin.write(prompt)
    proc.stdin.close()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    final = ""
    texts: list[str] = []
    for line in proc.stdout:
        if time.monotonic() > deadline:
            proc.kill()
            raise RuntimeError("AI timed out")
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            print(line.rstrip(), file=sys.stderr)
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    texts.append(block["text"])
                    print(f"  AI> {block['text'].strip()[:300]}")
        elif event.get("type") == "result":
            final = event.get("result") or ""
    proc.wait()
    if proc.returncode != 0 and not final:
        raise RuntimeError(f"claude CLI exited with code {proc.returncode}")
    return final or "\n".join(texts)
