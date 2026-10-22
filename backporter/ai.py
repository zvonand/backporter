"""
AI resolution of cherry-pick conflicts.

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


def locked_env() -> dict[str, str]:
    """Environment for everything the AI runs: no GitHub/Anthropic credentials, no git network access."""
    env = {k: v for k, v in os.environ.items() if k not in _SECRET_VARS}
    env["GH_CONFIG_DIR"] = tempfile.mkdtemp(prefix="backporter-gh-")
    env["GIT_ALLOW_PROTOCOL"] = "file"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_EDITOR"] = "true"
    return env


def build_prompt(repo_dir: str, branch: str, backport_branch: str, pr_url: str, pr_title: str, pr_body: str,
                 merge_sha: str, conflicts: list[tuple[str, str]]) -> str:
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

The cherry-pick (`git cherry-pick -m 1 {merge_sha}`) stopped with conflicts in:
{files}

See the full change with `git diff {merge_sha}^1 {merge_sha}` and the conflict state with `git status` and `git diff`.

Your job: make this branch contain the source PR's change, adapted to {branch}. Then decide on exactly one outcome:
- RESOLVED: you resolved every conflict, keeping the intent of the source PR and the surrounding code of {branch}. Remove all conflict markers and stage each resolved file with `git add <path>` (or `git rm <path>`).
- ALREADY_PRESENT: {branch} already contains this change (equivalent code is already there), so nothing needs backporting. If `git log` shows the commit or PR that brought it, give its PR URL.
- NOT_APPLICABLE: the change makes no sense for {branch}, e.g. the code or feature it fixes doesn't exist there. Explain why.
- UNRESOLVED: you cannot resolve it with confidence. Explain what blocks you.

Rules:
- Work only inside {repo_dir}. Change only files involved in this cherry-pick.
- Do not commit, and do not run `git cherry-pick --continue`, `--abort` or `--skip`. Do not switch branches, reset, fetch or push. The backporter does that.
- Use one plain command per Bash call, with no pipes, redirections or `&&`.
- You have no network or GitHub access, so don't try to use them.

End your answer with these lines, exactly:
OUTCOME: <RESOLVED|ALREADY_PRESENT|NOT_APPLICABLE|UNRESOLVED>
NOTE: <one line: what you adapted, or why>
BY: <PR URL that already brought the change; only for ALREADY_PRESENT, omit if unknown>
"""


def parse_result(text: str) -> Result:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(OUTCOME|NOTE|BY):\s*(.*?)\s*$", line)
        if match:
            fields[match.group(1)] = match.group(2)
    outcome = fields.get("OUTCOME", "").upper()
    if outcome not in OUTCOMES:
        return Result(UNRESOLVED, "AI did not report an outcome")
    by = fields.get("BY", "")
    try:
        git_ops.parse_pr_url(by)
    except ValueError:
        by = ""
    return Result(outcome, fields.get("NOTE", ""), by)


def check_resolved(repo_dir: str, head_before: str, conflicts: list[tuple[str, str]]) -> str | None:
    """Why a RESOLVED report doesn't hold, or None if the tree is ready for `cherry-pick --continue`."""
    if git_ops.run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip() != head_before:
        return "AI moved HEAD (committed or switched branches)"
    if not git_ops.is_cherry_pick_in_progress(repo_dir):
        return "cherry-pick is no longer in progress"
    unmerged = git_ops.run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir).stdout.split()
    if unmerged:
        return f"still unmerged: {', '.join(unmerged)}"
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
        text = _run_cli(repo_dir, prompt)
    result = parse_result(text)
    if result.outcome == RESOLVED:
        problem = check_resolved(repo_dir, head_before, conflicts)
        if problem:
            return Result(UNRESOLVED, f"AI reported resolved, but {problem}")
    print(f"AI: {result.outcome}: {result.note}")
    return result


def _run_cli(repo_dir: str, prompt: str) -> str:
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("`claude` CLI not found on PATH; install Claude Code or set ANTHROPIC_API_KEY")
    # File tools only inside the repo (paths relative to the working directory).
    allowed = ["Read(./**)", "Edit(./**)", "Write(./**)", "Glob(./**)", "Grep(./**)"]
    allowed += [f"Bash(git {sub} *)" for sub in GIT_SUBCOMMANDS if sub != "checkout"]
    allowed += ["Bash(git checkout --ours *)", "Bash(git checkout --theirs *)"]
    allowed += [f"Bash({cmd} *)" for cmd in OTHER_COMMANDS]
    cmd = [
        claude, "-p", "--output-format", "stream-json", "--verbose",
        # No user/project settings, MCP servers, plugins or hooks: only the tools listed here are usable.
        "--safe-mode", "--strict-mcp-config", "--setting-sources", "",
        "--permission-mode", "dontAsk", "--permission-prompts", "none", "--no-session-persistence",
        "--allowedTools", *allowed, "--disallowedTools", *_CLI_DENY,
    ]
    proc = subprocess.Popen(
        cmd, cwd=repo_dir, env=locked_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
