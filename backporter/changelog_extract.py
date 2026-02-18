"""
Extract Changelog category and Changelog entry from GitHub PR bodies.
"""

import re

# Match "Changelog category (leave one):" with optional ### prefix
_CATEGORY_PATTERN = re.compile(
    r"^#{0,3}\s*Changelog category \(leave one\):",
    re.MULTILINE | re.IGNORECASE,
)
# Match "Changelog entry (a ... of the changes that goes into/to CHANGELOG.md):"
# Middle part can be plain text or markdown link e.g. [user-readable short description](url)
# Supports both "goes into" and "goes to"; . matches newline so header can wrap
_ENTRY_PATTERN = re.compile(
    r"Changelog entry \(a\s+.+?\s+of the changes that goes (?:into|to) CHANGELOG\.md\):",
    re.IGNORECASE | re.DOTALL,
)


def _find_line_start(body: str, pos: int) -> int:
    """Return the start of the line containing pos."""
    return body.rfind("\n", 0, pos) + 1 if pos > 0 else 0


def _find_section_end(body: str, after_pos: int, next_patterns: list[str]) -> int:
    """Find the end index of a section (before the next header or end of body)."""
    end = len(body)
    for pattern in next_patterns:
        match = re.search(pattern, body[after_pos:])
        if match:
            end = min(end, after_pos + match.start())
    return end


def make_changelog_description(
    pr_body: str | None,
    *,
    pr_url: str | None = None,
    pr_author: str | None = None,
) -> str:
    """
    Extract Changelog category and Changelog entry from a PR body and return
    them combined in markdown format.

    If pr_url and pr_author are provided, the changelog entry content is
    appended with " (<pr_url> by @<pr_author>)".
    """
    if not pr_body:
        return ""

    parts: list[str] = []
    next_section = [r"\n### ", r"\n## ", r"\n\n---"]
    entry_suffix = ""
    if pr_url and pr_author:
        entry_suffix = f" ({pr_url} by @{pr_author})"

    # Changelog category (leave one):
    cat_match = _CATEGORY_PATTERN.search(pr_body)
    if cat_match:
        start = _find_line_start(pr_body, cat_match.start())
        end = _find_section_end(pr_body, cat_match.end(), next_section)
        section = pr_body[start:end].strip()
        if section:
            parts.append(section)

    # Changelog entry (if present):
    entry_match = _ENTRY_PATTERN.search(pr_body)
    if entry_match:
        start = _find_line_start(pr_body, entry_match.start())
        end = _find_section_end(pr_body, entry_match.end(), next_section)
        section = pr_body[start:end].strip()
        # Take only the first non-empty line of content (skip blank lines after header; any newline is delimiter)
        first_nl = section.find("\n")
        if first_nl >= 0:
            header_line = section[: first_nl + 1]
            rest = section[first_nl + 1 :]
            first_content_line = next((ln for ln in rest.split("\n") if ln.strip()), "").strip()
            section = (header_line + first_content_line) if first_content_line else section
        if section and entry_suffix:
            # Append suffix to the entry content (after the header line)
            first_nl = section.find("\n")
            if first_nl >= 0:
                header = section[: first_nl + 1]
                content = section[first_nl + 1 :].rstrip()
                section = header + content + entry_suffix
        if section:
            parts.append(section)

    return "\n\n".join(parts) if parts else ""
