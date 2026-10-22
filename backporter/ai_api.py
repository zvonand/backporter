"""
Conflict-resolution agent on the Anthropic API, with locally executed, restricted tools.

Bash runs a single command without a shell, and only allowlisted commands (see ai.GIT_SUBCOMMANDS,
ai.OTHER_COMMANDS). File tools are confined to the repo dir.
"""

import os
import shlex
import subprocess
import time

import anthropic

from backporter import ai

MODEL = "claude-opus-5-5"
MAX_TURNS = 200
OUTPUT_LIMIT = 30000

SYSTEM = (
    "You are an autonomous, headless agent resolving a git conflict. Never ask for confirmation. "
    "Follow the task's output contract exactly: end with the OUTCOME/NOTE/BY lines."
)

TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run one command in the repo (no shell: no pipes, redirections, `&&`, `;` or `$()`). "
            f"Allowed: git {', git '.join(ai.GIT_SUBCOMMANDS)} (checkout only with --ours/--theirs), "
            f"and {', '.join(ai.OTHER_COMMANDS)}."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file in the repo, with line numbers. Optional 1-based offset and line limit.",
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exactly one occurrence of old_string with new_string in a repo file.",
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "write_file",
        "description": "Write the full content of a repo file.",
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
]
_SCHEMAS = {t["name"]: t["input_schema"] for t in TOOLS}


class ToolError(Exception):
    pass


def _validate(name: str, args: object) -> None:
    """Schema check for tool input; with eager streaming the API does not validate it."""
    schema = _SCHEMAS.get(name)
    if schema is None:
        raise ToolError(f"unknown tool {name}")
    if not isinstance(args, dict):
        raise ToolError("input must be an object")
    for key in schema["required"]:
        if key not in args:
            raise ToolError(f"missing '{key}'")
    for key, value in args.items():
        expected = schema["properties"].get(key, {}).get("type")
        if expected == "string" and not isinstance(value, str) or expected == "integer" and not isinstance(value, int):
            raise ToolError(f"'{key}' must be a {expected}")


def _path(repo_dir: str, path: str) -> str:
    full = os.path.realpath(os.path.join(repo_dir, path))
    root = os.path.realpath(repo_dir)
    if full != root and not full.startswith(root + os.sep) or f"{os.sep}.git{os.sep}" in full + os.sep:
        raise ToolError(f"path outside the repo working tree: {path}")
    return full


def _bash(repo_dir: str, command: str, env: dict[str, str]) -> str:
    if any(ch in command for ch in "\n;&|<>`$"):
        raise ToolError("one plain command only: no pipes, redirections, `;`, `&&`, `$` or backticks")
    argv = shlex.split(command)
    if not argv:
        raise ToolError("empty command")
    if argv[0] == "git":
        sub = argv[1] if len(argv) > 1 else ""
        if sub not in ai.GIT_SUBCOMMANDS:
            raise ToolError(f"git subcommand not allowed: {' '.join(argv[:2])}")
        if sub == "checkout" and argv[2:3] not in (["--ours"], ["--theirs"]):
            raise ToolError("only `git checkout --ours|--theirs <paths>` is allowed")
        # --output writes a file anywhere; -O/--open-files-in-pager runs a command.
        if any(a.startswith(("--output", "-O", "--open-files-in-pager", "--ext-diff", "--textconv")) for a in argv[2:]):
            raise ToolError("option not allowed")
    elif argv[0] in ai.OTHER_COMMANDS:
        for arg in argv[1:]:
            if not arg.startswith("-"):
                _path(repo_dir, arg)
    else:
        raise ToolError(f"command not allowed: {argv[0]}")
    result = subprocess.run(argv, cwd=repo_dir, env=env, capture_output=True, text=True, timeout=600)
    out = (result.stdout + result.stderr)[:OUTPUT_LIMIT]
    return f"{out}\n[exit code {result.returncode}]"


def _read(repo_dir: str, args: dict) -> str:
    with open(_path(repo_dir, args["path"]), errors="replace") as f:
        lines = f.readlines()
    start = max(args.get("offset", 1), 1) - 1
    end = start + args["limit"] if args.get("limit") else len(lines)
    return "".join(f"{i + 1}\t{line}" for i, line in enumerate(lines[start:end], start))[:OUTPUT_LIMIT]


def _edit(repo_dir: str, args: dict) -> str:
    path = _path(repo_dir, args["path"])
    with open(path) as f:
        text = f.read()
    count = text.count(args["old_string"])
    if count != 1:
        raise ToolError(f"old_string found {count} times; it must match exactly once")
    with open(path, "w") as f:
        f.write(text.replace(args["old_string"], args["new_string"], 1))
    return "ok"


def _write(repo_dir: str, args: dict) -> str:
    with open(_path(repo_dir, args["path"]), "w") as f:
        f.write(args["content"])
    return "ok"


def _execute(repo_dir: str, name: str, args: dict, env: dict[str, str]) -> str:
    if name == "bash":
        return _bash(repo_dir, args["command"], env)
    if name == "read_file":
        return _read(repo_dir, args)
    if name == "edit_file":
        return _edit(repo_dir, args)
    return _write(repo_dir, args)


def run_agent(repo_dir: str, prompt: str, api_key: str, env: dict[str, str]) -> str:
    """Run the agent loop and return its final text."""
    client = anthropic.Anthropic(api_key=api_key)
    messages: list[dict] = [{"role": "user", "content": prompt}]
    deadline = time.monotonic() + ai.TIMEOUT_SECONDS
    bad_json = 0
    for _ in range(MAX_TURNS):
        if time.monotonic() > deadline:
            raise RuntimeError("AI timed out")
        try:
            with client.beta.messages.stream(
                model=MODEL,
                max_tokens=64000,
                system=SYSTEM,
                messages=messages,
                tools=TOOLS,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                cache_control={"type": "ephemeral"},
                # On a safety decline, the API re-runs the request on its recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                response = stream.get_final_message()
        except ValueError:
            # Tool input the SDK could not parse at all (eager input streaming); ask again.
            bad_json += 1
            if bad_json > 3:
                raise RuntimeError("AI kept producing unparsable tool input")
            continue

        text = "".join(b.text for b in response.content if b.type == "text")
        if text.strip():
            print(f"  AI> {text.strip()[:300]}")
        if response.stop_reason == "refusal":
            return "OUTCOME: UNRESOLVED\nNOTE: the model declined the request"
        if response.stop_reason == "max_tokens":
            return "OUTCOME: UNRESOLVED\nNOTE: the model hit max_tokens"
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return text
        results = []
        for block in tool_uses:
            try:
                _validate(block.name, block.input)
                content, is_error = _execute(repo_dir, block.name, block.input, env), False
            except (ToolError, OSError, subprocess.TimeoutExpired, ValueError) as e:
                content, is_error = f"Error: {e}", True
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return "OUTCOME: UNRESOLVED\nNOTE: the AI ran out of turns"
