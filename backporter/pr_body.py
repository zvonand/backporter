"""
Body of a backport PR: the PR template of Altinity/ClickHouse's current default branch, whatever the target
branch, with only two regions filled from the source PR.

- Changelog category: the template's list of choices is replaced by the one category the source PR selected,
  copied verbatim (list marker and parenthetical included). If it selected none, the template's choice for its
  upstream changelog label (_LABEL_CATEGORIES) is used.
- Changelog entry: the template's placeholder is replaced by the source PR's entry, copied verbatim, with
  " (<source PR URL> by @<author>)" appended to its last line. If the source PR has no entry, the placeholder stays
  when the category doesn't require one.

Everything else, CI/CD options and their checkbox states included, stays byte-for-byte as in the template.
When the source PR is unclear, or has no category or no entry the category requires, the AI fills the two regions
instead (picking the category or writing the entry when needed), and its result must pass the same check.

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
_NO_ENTRY = "changelog entry is not required"

# Upstream (ClickHouse/ClickHouse) changelog labels and the start of the template choice each one selects.
_LABEL_CATEGORIES = {
    "pr-feature": "New Feature",
    "pr-experimental": "Experimental Feature",
    "pr-improvement": "Improvement",
    "pr-performance": "Performance Improvement",
    "pr-backward-incompatible": "Backward Incompatible Change",
    "pr-build": "Build/Testing/Packaging Improvement",
    "pr-documentation": "Documentation",
    "pr-critical-bugfix": "Critical Bug Fix",
    "pr-bugfix": "Bug Fix",
    "pr-ci": "CI Fix or Improvement",
    "pr-not-for-changelog": "Not for changelog",
}


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


def _source_lines(body: str) -> list[str]:
    return _COMMENT_RE.sub("", (body or "").replace("\r\n", "\n")).split("\n")


def _source_section(lines: list[str], header: re.Pattern) -> tuple[int, int] | None:
    """Like _section, but None if the source PR body has no such heading."""
    if not any(header.match(line) for line in lines):
        return None
    return _section(lines, header, Unclear)


def _source_category(lines: list[str]) -> str | None:
    """The source PR's selected category line, or None if it selects none."""
    span = _source_section(lines, _CATEGORY_RE)
    if not span:
        return None
    selected = [line.strip() for line in lines[span[0] + 1:span[1]] if _ITEM_RE.match(line)]
    if len(selected) > 1:
        raise Unclear(f"the source PR selects {len(selected)} changelog categories")
    return selected[0] if selected else None


def _source_entry(lines: list[str]) -> list[str] | None:
    """The source PR's entry lines, or None if it has no entry."""
    span = _source_section(lines, _ENTRY_RE)
    if not span:
        return None
    region = _trim([line.rstrip() for line in lines[span[0] + 1:span[1]]])
    if "\n".join(region).strip() in _PLACEHOLDERS:
        return None
    # The entry is its first paragraph; more text before the next heading (often the PR description,
    # when the author dropped the template's later headings) makes the boundary unclear.
    blank = next((i for i, line in enumerate(region) if not line.strip()), len(region))
    if blank < len(region):
        raise Unclear("the changelog entry is followed by more text with no heading in between")
    return region


def source_parts(body: str) -> tuple[str, list[str]]:
    """The selected category line and the entry lines of a source PR body."""
    lines = _source_lines(body)
    category = _source_category(lines)
    if not category:
        raise Unclear("the source PR selects no changelog category")
    entry = _source_entry(lines)
    if not entry:
        raise Unclear("the source PR has no changelog entry")
    return category, entry


def needs_entry(category: str) -> bool:
    return _NO_ENTRY not in category.lower()


def label_category(template: str, labels: list[str]) -> str | None:
    """The template's choice for the source PR's one changelog label, or None if it has none or several."""
    names = {_LABEL_CATEGORIES[label] for label in labels if label in _LABEL_CATEGORIES}
    if len(names) != 1:
        return None
    name = names.pop()
    lines = template.split("\n")
    chosen = [
        lines[i].strip() for i in _category_choices(lines)
        if re.sub(r"^\s*[-*]\s+", "", lines[i]).startswith(name)
    ]
    return chosen[0] if len(chosen) == 1 else None


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


def fill(template: str, category: str, entry: list[str] | None) -> str:
    """
    The template with its category choices replaced by `category` and its entry placeholder by `entry`
    (kept if None).
    """
    lines = template.split("\n")
    items = _category_choices(lines)
    lines[items[0]:items[-1] + 1] = [category]

    start, end = _section(lines, _ENTRY_RE, TemplateError)
    content = [i for i in range(start + 1, end) if lines[i].strip()]
    if len(content) != 1 or _COMMENT_RE.sub("", lines[content[0]]).strip() not in _PLACEHOLDERS:
        raise TemplateError("the template's changelog entry is not a single placeholder line")
    if entry is not None:
        lines[content[0]:content[0] + 1] = entry
    return "\n".join(lines)


def build(template: str, source_body: str, url: str, author: str, labels: list[str]) -> str:
    """Deterministic body; raises Unclear if the source needs judgment, TemplateError if the template is unusable."""
    lines = _source_lines(source_body)
    category = _source_category(lines) or label_category(template, labels)
    if not category:
        raise Unclear("the source PR selects no changelog category and has no single changelog label")
    entry = _source_entry(lines)
    if entry:
        return fill(template, category, attribute(entry, url, author))
    if needs_entry(category):
        raise Unclear(f"the source PR has no changelog entry, but its category '{category}' requires one")
    return fill(template, category, None)


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
    if fill(template, items[0], None) == "\n".join(lines):
        if needs_entry(items[0]):
            raise Unclear(f"generated body has no changelog entry, but its category '{items[0]}' requires one")
        return
    if not entry or not entry[-1].endswith(f" ({url} by @{author})"):
        raise Unclear("generated changelog entry lacks the source attribution on its last line")
    if fill(template, items[0], entry) != "\n".join(lines):
        raise Unclear("generated body changes the template outside the changelog category and entry")


def prompt(template: str, source_body: str, url: str, author: str, title: str, labels: list[str], problem: str,
           diff: str, reverted: str) -> str:
    """
    `diff`: the source PR's changes; `reverted`: title and body of the PRs it reverts, directly or through reverts
    of reverts, each marked with whether the source PR undoes or re-applies it.
    """
    label_map = "\n".join(f"- {label}: {name}" for label, name in _LABEL_CATEGORIES.items())
    return f"""Fill in the body of a backport pull request in Altinity/ClickHouse.

Source PR: {url} by @{author}
Source PR title: {title}
Source PR labels: {", ".join(labels) or "none"}
Why this needs judgment: {problem}

Rules:
1. Start from the TEMPLATE below. Change only two regions and keep every other line byte-for-byte, including \
comments, blank lines, the documentation placeholder and every CI/CD option and checkbox state.
2. Changelog category: replace the template's list of choices with exactly one list line:
   - if the source PR selected one category: that category, copied verbatim with its list marker and \
parenthetical. Don't reclassify it because this is a backport;
   - if it selected none and exactly one of its labels is in the LABEL MAP below: the template's choice that \
starts with the mapped name, copied verbatim from the template;
   - otherwise: the template's choice that best fits the change, judging by the source PR's title, body, labels \
and diff, copied verbatim from the template.
3. Changelog entry:
   - if the source PR has a changelog entry: replace the template's placeholder with it, copied verbatim (text, \
Markdown, line breaks), and append " ({url} by @{author})" to its last line;
   - if it has none (no entry section, or an empty or placeholder entry) and the category's line contains \
"({_NO_ENTRY})": leave the template's placeholder line unchanged;
   - if it has none and the category requires one: replace the placeholder with a short, user-readable entry \
(one or two sentences, like a ClickHouse changelog entry) on what the change does for users, written from the \
source PR's title, body and diff and from the PRs it reverts (a chain of reverts: each one is marked with \
whether the source PR undoes or re-applies its change; describe the net effect), and append " ({url} by @{author})" to its last line.
4. Don't copy anything else from the source PR. If the source PR selects several categories or the boundaries \
of its entry are unclear, don't guess.

Answer with either the complete body between <body> and </body>, or a single line \
`CANNOT: <reason>`.

<label_map>
{label_map}
</label_map>

<source_pr_body>
{source_body}
</source_pr_body>

<reverted_prs>
{reverted or "none"}
</reverted_prs>

<source_pr_diff>
{diff}
</source_pr_diff>

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
