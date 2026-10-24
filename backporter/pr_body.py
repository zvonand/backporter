"""
Body of a backport PR: the PR template of Altinity/ClickHouse's current default branch, whatever the target
branch, with only two regions filled from the source PR.

- Changelog category: the template's list of choices is replaced by the one category the source PR selected,
  copied verbatim (list marker and parenthetical included).
- Changelog entry: the template's placeholder is replaced by the source PR's entry, copied verbatim, with
  " (<source PR URL> by @<author>)" appended to its last line.

Everything else, CI/CD options and their checkbox states included, stays byte-for-byte as in the template.
When the source PR is unclear, the AI fills the two regions instead, and its result must pass the same check.

A bump PR's body is the same template with only the category filled: the template's own "Not for changelog" choice.
"""

import re

TEMPLATE_PATH = ".github/PULL_REQUEST_TEMPLATE.md"

_CATEGORY_RE = re.compile(r"^#{1,6}\s*Changelog category\b", re.IGNORECASE)
_ENTRY_RE = re.compile(r"^#{1,6}\s*Changelog entry\b", re.IGNORECASE)
_HEADER_RE = re.compile(r"^#{1,6}\s")
_ITEM_RE = re.compile(r"^\s*[-*]\s+\S")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_PLACEHOLDERS = {"", "...", "…"}


class Unclear(Exception):
    """The source PR doesn't determine the category or entry unambiguously."""


class TemplateError(Exception):
    """The PR template's category or entry region can't be identified."""


def _section(lines: list[str], header: re.Pattern, error: type[Exception]) -> tuple[int, int]:
    """(header line index, index of the next header or len(lines)); the header must occur exactly once."""
    found = [i for i, line in enumerate(lines) if header.match(line)]
    if len(found) != 1:
        raise error(f"expected one '{header.pattern}' heading, found {len(found)}")
    start = found[0]
    end = next((i for i in range(start + 1, len(lines)) if _HEADER_RE.match(lines[i])), len(lines))
    return start, end


def _trim(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return lines


def source_parts(body: str) -> tuple[str, list[str]]:
    """The selected category line and the entry lines of a source PR body."""
    lines = _COMMENT_RE.sub("", (body or "").replace("\r\n", "\n")).split("\n")
    start, end = _section(lines, _CATEGORY_RE, Unclear)
    selected = [line.strip() for line in lines[start + 1:end] if _ITEM_RE.match(line)]
    if len(selected) != 1:
        raise Unclear(f"the source PR selects {len(selected)} changelog categories")
    start, end = _section(lines, _ENTRY_RE, Unclear)
    region = _trim([line.rstrip() for line in lines[start + 1:end]])
    if "\n".join(region).strip() in _PLACEHOLDERS:
        raise Unclear("the source PR has no changelog entry")
    # The entry is its first paragraph; more text before the next heading (often the PR description,
    # when the author dropped the template's later headings) makes the boundary unclear.
    blank = next((i for i, line in enumerate(region) if not line.strip()), len(region))
    if blank < len(region):
        raise Unclear("the changelog entry is followed by more text with no heading in between")
    return selected[0], region


def attribute(entry: list[str], url: str, author: str) -> list[str]:
    """Append ' (<url> by @<author>)' to the entry's last line."""
    if url in "\n".join(entry):
        raise Unclear("the changelog entry already contains an attribution")
    last = entry[-1]
    if last.lstrip().startswith(("```", "|", ">", "<")):
        raise Unclear("the changelog entry ends with a code block, table, quote or HTML")
    return [*entry[:-1], f"{last} ({url} by @{author})"]


def _category_choices(lines: list[str]) -> list[int]:
    """Indexes of the template's changelog category choices."""
    start, end = _section(lines, _CATEGORY_RE, TemplateError)
    items = [i for i in range(start + 1, end) if _ITEM_RE.match(lines[i])]
    if not items or items != list(range(items[0], items[-1] + 1)):
        raise TemplateError("the template's changelog categories are not one contiguous list")
    return items


def fill(template: str, category: str, entry: list[str]) -> str:
    """The template with its category choices replaced by `category` and its entry placeholder by `entry`."""
    lines = template.split("\n")
    items = _category_choices(lines)
    lines[items[0]:items[-1] + 1] = [category]

    start, end = _section(lines, _ENTRY_RE, TemplateError)
    content = [i for i in range(start + 1, end) if lines[i].strip()]
    if len(content) != 1 or _COMMENT_RE.sub("", lines[content[0]]).strip() not in _PLACEHOLDERS:
        raise TemplateError("the template's changelog entry is not a single placeholder line")
    lines[content[0]:content[0] + 1] = entry
    return "\n".join(lines)


def build(template: str, source_body: str, url: str, author: str) -> str:
    """Deterministic body; raises Unclear if the source needs judgment, TemplateError if the template is unusable."""
    category, entry = source_parts(source_body)
    return fill(template, category, attribute(entry, url, author))


def not_for_changelog(template: str) -> str:
    """Bump body: the template with its category choices replaced by its own 'Not for changelog' choice."""
    lines = template.split("\n")
    items = _category_choices(lines)
    chosen = [lines[i] for i in items if "not for changelog" in lines[i].lower()]
    if len(chosen) != 1:
        raise TemplateError(f"expected one 'Not for changelog' category in the template, found {len(chosen)}")
    lines[items[0]:items[-1] + 1] = chosen
    return "\n".join(lines)


def check(template: str, body: str, url: str, author: str) -> None:
    """Raise Unclear unless body is the template with only the category and entry regions filled."""
    lines = body.replace("\r\n", "\n").split("\n")
    try:
        start, end = _section(lines, _CATEGORY_RE, Unclear)
        items = [line for line in lines[start + 1:end] if _ITEM_RE.match(line)]
        start, end = _section(lines, _ENTRY_RE, Unclear)
        entry = _trim(lines[start + 1:end])
    except Unclear as e:
        raise Unclear(f"generated body: {e}")
    if len(items) != 1:
        raise Unclear("generated body must select exactly one changelog category")
    if not entry or not entry[-1].endswith(f" ({url} by @{author})"):
        raise Unclear("generated changelog entry lacks the source attribution on its last line")
    if fill(template, items[0], entry) != "\n".join(lines):
        raise Unclear("generated body changes the template outside the changelog category and entry")


def prompt(template: str, source_body: str, url: str, author: str, title: str, problem: str) -> str:
    return f"""Fill in the body of a backport pull request in Altinity/ClickHouse.

Source PR: {url} by @{author}
Source PR title: {title}
Why this needs judgment: {problem}

Rules:
1. Start from the TEMPLATE below. Change only two regions and keep every other line byte-for-byte, including \
comments, blank lines, the documentation placeholder and every CI/CD option and checkbox state.
2. Changelog category: replace the template's list of choices with exactly one list line: the category the \
source PR selected, copied verbatim with its list marker and parenthetical. Don't reclassify it because this is \
a backport.
3. Changelog entry: replace the template's placeholder with the source PR's changelog entry, copied verbatim \
(text, Markdown, line breaks), and append " ({url} by @{author})" to its last line.
4. Never invent a category or an entry, and don't copy anything else from the source PR. If the source PR \
doesn't make its category or entry clear (none or several categories selected, an empty or placeholder entry, \
unclear section boundaries), don't guess.

Answer with either the complete body between <body> and </body>, or a single line \
`CANNOT: <reason>`.

<source_pr_body>
{source_body}
</source_pr_body>

<template>
{template}
</template>
"""


def parse_answer(answer: str) -> str:
    """The body from an AI answer; raises Unclear with the AI's reason if it declined."""
    match = re.search(r"<body>\n?(.*?)\n?</body>", answer, re.DOTALL)
    if match:
        return match.group(1)
    reason = re.search(r"CANNOT:\s*(.+)", answer)
    raise Unclear(f"AI: {reason.group(1).strip()}" if reason else "AI gave no body")
