"""
Backport tracker backed by a GitHub Project.

    backporter init <project-URL>             format an empty project and save its link to config
    backporter add-version X.Y --flavor F     add a version (stable, antalya or both) to the project
    backporter add <PR-URL> <version>         track a backport to version and every version it implies
    backporter remove <PR-URL> <version>      stop tracking version and what only it implied
    backporter scan [--version KEY]           process web UI requests, refresh statuses, list pending
    backporter start <PR-URL> [version ...]   cherry-pick, push, open backport PRs (--ai: AI resolves conflicts)
    backporter run [--no-ai]                  scan, then backport everything with Decision Approved
    backporter continue <PR-URL> <version>    finish after resolving conflicts (--ai: let AI resolve them)
    backporter mark <PR-URL> <version> present|not-applicable [--by URL] [--note TEXT]
    backporter describe <PR-URL>              print a PR's changelog category and entry (as in backport PRs)
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import date

from github import Github, GithubException
from github.PullRequest import PullRequest
from github.Repository import Repository

from backporter import ai, git_ops, github_guard, pr_body
from backporter.github_guard import guarded_github
from backporter.config import (
    CONFIG_PATH, FLAVORS, TARGET_REPO, Config, Version, find_version, load_config, load_dirs, load_dotenv,
    parse_project_url, save_project,
)
from backporter.project import (
    CLOSED, CONFLICTS, EXPLICIT, F_BACKPORT_PR, F_FLAVOR, F_NOTE, F_REQUESTED, F_SOURCE_PR, F_STATUS, F_VERSION,
    APPROVED, DISCARDED, F_DECISION, F_NOT_NEEDED_FROM, F_REQUESTED_BY, FINAL_STATUSES, IMPLIED, MERGED, NOT_APPLICABLE, NOT_STARTED, PR_OPEN, PRESENT, Item, Project,
)
from backporter.rules import closure, plan_remove, sort_versions


_VERSION_KEY_RE = re.compile(r"\b(?:stable|antalya)-\d+\.\d+\b")


def source_url(pr_repo: str, number: int) -> str:
    return f"https://github.com/{pr_repo}/pull/{number}"


def backport_branch(version: Version, number: int) -> str:
    return f"backports/{version.branch}/{number}"


def open_project(gh: Github, config: Config) -> Project:
    if not config.project_number:
        raise RuntimeError(f"No project set in {CONFIG_PATH}; run `backporter init <project-URL>` first")
    project = Project.open(gh.requester, config.project_owner, config.project_number)
    if not project.is_formatted():
        raise RuntimeError("Project is not formatted; run `backporter init <project-URL>` first")
    return project


def tracked_items(project: Project) -> list[Item]:
    """Items created by the backporter (not requests, not processed source PR rows)."""
    return [i for i in project.items() if i.requested in (EXPLICIT, IMPLIED)]


def project_versions(project: Project, config: Config) -> list[Version]:
    """The project's versions with their branches from the config."""
    keys = project.version_keys()
    missing = [k for k in keys if k not in config.branches]
    if missing:
        raise RuntimeError(f"No branch for {', '.join(missing)}; add it to [branches] in {CONFIG_PATH}")
    return [Version.parse(k, config.branches[k]) for k in keys]


def pr_items(project: Project, url: str) -> dict[str, Item]:
    """Version key -> item for one source PR."""
    return {i.version: i for i in tracked_items(project) if i.source_pr == url}


def create_item(
    project: Project, version: Version, url: str, title: str, requested: str, requested_by: str,
    status: str = NOT_STARTED, note: str = "", not_needed_from: str = "",
) -> Item:
    """Add an item (Not started unless given); title is '<owner/repo>#<N> <source PR title>'."""
    values = {
        F_SOURCE_PR: url,
        F_VERSION: version.key,
        F_FLAVOR: version.flavor,
        F_REQUESTED: requested,
        F_REQUESTED_BY: requested_by,
        F_STATUS: status,
    }
    if note:
        values[F_NOTE] = note
    if not_needed_from:
        values[F_NOT_NEEDED_FROM] = not_needed_from
    item_id = project.add_item(title=title, body=url, values=values)
    print(f"{version.key}: added {url} ({requested}{', ' + status if status != NOT_STARTED else ''})")
    return Item(
        id=item_id, title=title, body=url, content_url="", source_pr=url, version=version.key, flavor=version.flavor,
        requested=requested, status=status, backport_pr="", note=note, decision="", requested_by=requested_by,
        not_needed_from=not_needed_from, creator="",
    )


def promote(project: Project, item: Item, backport: PullRequest) -> None:
    """
    Replace a draft row by the backport PR itself, with the same field values, so the row's Title is the PR
    (clickable, with its state). Only PRs in TARGET_REPO are ever added to the project.
    """
    if item.content_url:
        return
    if backport.base.repo.full_name.lower() != TARGET_REPO.lower():
        raise RuntimeError(f"Refusing to add {backport.html_url} to the project: not in {TARGET_REPO}")
    github_guard.allow_project_content(backport.node_id)
    new_id = project.add_existing_item(backport.node_id)
    values = {
        F_SOURCE_PR: item.source_pr, F_VERSION: item.version, F_FLAVOR: item.flavor, F_REQUESTED: item.requested,
        F_REQUESTED_BY: item.requested_by, F_NOT_NEEDED_FROM: item.not_needed_from, F_DECISION: item.decision,
        F_NOTE: item.note, F_STATUS: item.status,
    }
    for field, value in values.items():
        if value:
            project.set_value(new_id, field, value)
    project.delete_item(item.id)
    item.id, item.content_url, item.title = new_id, backport.html_url, backport.title
    print(f"    the row is now {backport.html_url}")


def version_number(number: str) -> tuple[int, int]:
    """'26.3' -> (26, 3)"""
    major, minor = number.split(".")
    return int(major), int(minor)


def not_needed_note(bound: str, requested_by: str) -> str:
    return f"Not needed from {bound} (per request by @{requested_by})"


def backport_body(repo_dir: str, version: Version, pr: PullRequest) -> str:
    """
    The target branch's PR template with the source PR's changelog category and entry (see pr_body).
    The AI fills them only when the source is unclear. Raises pr_body.Unclear/TemplateError if a human is needed.
    """
    if not pr.user:
        raise pr_body.Unclear("the source PR has no author")
    try:
        template = git_ops.show_file(repo_dir, version.branch, pr_body.TEMPLATE_PATH)
    except RuntimeError:
        raise pr_body.TemplateError(f"{pr_body.TEMPLATE_PATH} not found on {version.branch}")
    try:
        return pr_body.build(template, pr.body or "", pr.html_url, pr.user.login)
    except pr_body.Unclear as e:
        print(f"PR body: {e}; asking the AI...")
        prompt = pr_body.prompt(template, pr.body or "", pr.html_url, pr.user.login, pr.title, str(e))
        try:
            answer = ai.complete(prompt)
        except RuntimeError as ai_error:
            raise pr_body.Unclear(f"{e}; AI unavailable: {ai_error}")
        body = pr_body.parse_answer(answer)
        pr_body.check(template, body, pr.html_url, pr.user.login)
        return body


def cmd_init(args, gh: Github, config: Config) -> None:
    owner, number = parse_project_url(args.url)
    project = Project.open(gh.requester, owner, number)
    project.ensure_schema()
    save_project(owner, number)
    print(f"Project {args.url} is ready; saved to {CONFIG_PATH}")


def cmd_add_version(args, gh: Github, config: Config) -> None:
    project = open_project(gh, config)
    existing = set(project.version_keys())
    repo = gh.get_repo(TARGET_REPO)
    new: list[Version] = []
    for flavor in FLAVORS if args.flavor == "both" else (args.flavor,):
        key = f"{flavor}-{args.version}"
        if key not in config.branches:
            raise RuntimeError(f"No branch for {key}; add it to [branches] in {CONFIG_PATH} first")
        version = Version.make(flavor, args.version, config.branches[key])
        if version.key in existing:
            raise RuntimeError(f"Version {version.key} already exists")
        try:
            repo.get_branch(version.branch)
        except GithubException:
            raise RuntimeError(f"Branch {version.branch} not found in {TARGET_REPO}")
        new.append(version)
    for version in new:
        project.add_version(version)
        print(f"Added version {version.key} (branch {version.branch})")

    # Backfill: tracked PRs whose explicit requests now imply a new version.
    versions = project_versions(project, config)
    keys = {v.key for v in versions}
    by_pr: dict[str, list[Item]] = defaultdict(list)
    for item in tracked_items(project):
        by_pr[item.source_pr].append(item)
    for url, items in by_pr.items():
        present = {i.version for i in items}
        needed = {
            v.key
            for i in items if i.requested == EXPLICIT and i.version in keys
            for v in closure(find_version(versions, i.version), versions)
        }
        explicit = [i for i in items if i.requested == EXPLICIT and i.version in keys]
        title = None
        for version in new:
            if version.key not in needed or version.key in present:
                continue
            if title is None:
                pr_repo, number = git_ops.parse_pr_url(url)
                title = f"{pr_repo}#{number} {gh.get_repo(pr_repo).get_pull(number).title}"
            implied_by = [i for i in explicit if version in closure(find_version(versions, i.version), versions)]
            active = [
                i for i in implied_by
                if not i.not_needed_from or version.version < version_number(i.not_needed_from)
            ]
            if active:
                create_item(project, version, url, title, IMPLIED, active[0].requested_by)
            else:
                # Every request implying it said the backport isn't needed from some version below this one.
                limit = implied_by[0]
                create_item(
                    project, version, url, title, IMPLIED, limit.requested_by, NOT_APPLICABLE,
                    not_needed_note(limit.not_needed_from, limit.requested_by),
                )


def track(
    gh: Github, project: Project, config: Config, pr_url: str, version_key: str, requested_by: str,
    created: list[Item] | None = None, not_needed_from: str = "",
) -> None:
    """
    Track a backport of pr_url to version_key (explicit) and every version it implies, for `requested_by`.
    Implied versions >= not_needed_from (a version number like '26.3') get new rows marked Not applicable;
    existing rows are left as they are, since other requests may need them.
    New items are appended to `created`; items already in it count as existing, since GitHub's
    item list can lag behind for a while after items are added.
    """
    created = [] if created is None else created
    pr_repo, number = git_ops.parse_pr_url(pr_url)
    url = source_url(pr_repo, number)
    versions = project_versions(project, config)
    target = find_version(versions, version_key)
    bound = version_number(not_needed_from) if not_needed_from else None
    if bound and target.version >= bound:
        raise ValueError(f"{target.key} is not below 'Not needed from' {not_needed_from}")
    pr = gh.get_repo(pr_repo).get_pull(number)
    existing = {**pr_items(project, url), **{i.version: i for i in created if i.source_pr == url}}
    for v in closure(target, versions):
        requested = EXPLICIT if v == target else IMPLIED
        item = existing.get(v.key)
        if item and v == target and not_needed_from and item.not_needed_from != not_needed_from:
            project.set_value(item.id, F_NOT_NEEDED_FROM, not_needed_from)
            item.not_needed_from = not_needed_from
        if item:
            if requested == EXPLICIT and item.decision == DISCARDED:
                # A new request overrides an earlier discard; the Note keeps that history visible.
                project.clear_value(item.id, F_DECISION)
                project.set_value(item.id, F_REQUESTED, EXPLICIT)
                project.set_value(item.id, F_REQUESTED_BY, requested_by)
                project.set_value(
                    item.id, F_NOTE, f"Previously discarded; requested again by @{requested_by} on {date.today()}"
                )
                item.decision, item.requested, item.requested_by = "", EXPLICIT, requested_by
                print(f"{v.key}: was discarded; requested again, Decision cleared")
            elif requested == EXPLICIT and item.requested != EXPLICIT:
                project.set_value(item.id, F_REQUESTED, EXPLICIT)
                project.set_value(item.id, F_REQUESTED_BY, requested_by)
                item.requested, item.requested_by = EXPLICIT, requested_by
                print(f"{v.key}: marked explicit")
            elif requested == EXPLICIT:
                print(f"{v.key}: already requested ({item.status})")
            continue
        title = f"{pr_repo}#{number} {pr.title}"
        if bound and v.version >= bound:
            created.append(create_item(
                project, v, url, title, requested, requested_by, NOT_APPLICABLE,
                not_needed_note(not_needed_from, requested_by),
            ))
        else:
            created.append(create_item(
                project, v, url, title, requested, requested_by,
                not_needed_from=not_needed_from if v == target else "",
            ))


def cmd_add(args, gh: Github, config: Config) -> None:
    track(gh, open_project(gh, config), config, args.pr, args.version, gh.get_user().login,
          not_needed_from=args.not_needed_from or "")


def process_requests(gh: Github, project: Project, config: Config) -> list[Item]:
    """
    Turn items added by hand in the web UI into tracked items, then delete them.
    The PR URL comes from the Source PR field, the item's own PR, the title or the body; versions come from
    the Version field and from version keys in the title, body or Note. Whoever added the request row
    becomes "Requested by".
    """
    keys = set(project.version_keys())
    created: list[Item] = []
    for item in project.items():
        if item.requested:
            continue
        text = " ".join((item.source_pr, item.content_url, item.title, item.body))
        try:
            pr_repo, number = git_ops.parse_pr_url(text)
        except ValueError:
            print(f"Request '{item.title}': no PR URL found; skipped", file=sys.stderr)
            continue
        requested = ([item.version] if item.version else []) + _VERSION_KEY_RE.findall(
            f"{item.title} {item.body} {item.note}"
        )
        unknown = [k for k in requested if k not in keys]
        if not requested or unknown:
            print(
                f"Request '{item.title}': {'unknown versions ' + ', '.join(unknown) if unknown else 'no version'}; "
                f"skipped", file=sys.stderr,
            )
            continue
        bound = item.not_needed_from
        too_high = [k for k in requested if bound and version_number(k.split("-", 1)[1]) >= version_number(bound)]
        if too_high:
            print(
                f"Request '{item.title}': {', '.join(too_high)} not below 'Not needed from' {bound}; skipped",
                file=sys.stderr,
            )
            continue
        url = source_url(pr_repo, number)
        limit = f" (not needed from {bound})" if bound else ""
        print(f"Request '{item.title}': {url} -> {', '.join(dict.fromkeys(requested))}{limit}")
        for key in dict.fromkeys(requested):
            track(gh, project, config, url, key, item.creator, created, bound)
        project.delete_item(item.id)
    return created


def cmd_remove(args, gh: Github, config: Config) -> None:
    pr_repo, number = git_ops.parse_pr_url(args.pr)
    url = source_url(pr_repo, number)
    project = open_project(gh, config)
    versions = project_versions(project, config)
    target = find_version(versions, args.version)
    items = pr_items(project, url)
    other_explicit = [
        find_version(versions, k) for k, i in items.items() if i.requested == EXPLICIT and k != target.key
    ]
    for v in plan_remove(target, other_explicit, versions):
        item = items.get(v.key)
        if item:
            project.delete_item(item.id)
            print(f"{v.key}: removed (was {item.status})")


def find_backport_pr(repo: Repository, item: Item, version: Version) -> PullRequest | None:
    if item.backport_pr:
        _, number = git_ops.parse_pr_url(item.backport_pr)
        return repo.get_pull(number)
    _, number = git_ops.parse_pr_url(item.source_pr)
    owner = repo.full_name.split("/")[0]
    pulls = list(repo.get_pulls(state="all", head=f"{owner}:{backport_branch(version, number)}"))
    return pulls[0] if pulls else None


def cmd_scan(args, gh: Github, config: Config) -> None:
    scan(gh, config, args.version)


def scan(gh: Github, config: Config, only_version: str | None) -> None:
    """Process web requests, refresh statuses from GitHub, report pending and missing items."""
    print(f"Reading project {config.project_owner}#{config.project_number}...", flush=True)
    project = open_project(gh, config)
    print("Processing requests...", flush=True)
    created = process_requests(gh, project, config)
    version_list = project_versions(project, config)
    if only_version:
        find_version(version_list, only_version)
    versions = {v.key: v for v in version_list}
    repo = gh.get_repo(TARGET_REPO)
    items = tracked_items(project)
    listed = {i.id for i in items}
    items += [i for i in created if i.id not in listed]

    to_refresh = [
        i for i in items
        if i.status not in FINAL_STATUSES and i.decision != DISCARDED and i.version in versions
        and (not only_version or i.version == only_version)
    ]
    print(f"Refreshing {len(to_refresh)} item(s) from GitHub...", flush=True)
    for n, item in enumerate(to_refresh, 1):
        version = versions[item.version]
        print(f"  [{n}/{len(to_refresh)}] {item.version} {item.source_pr}", flush=True)
        pr = find_backport_pr(repo, item, version)
        if pr is None:
            continue
        status = MERGED if pr.merged_at else CLOSED if pr.state == "closed" else PR_OPEN
        promote(project, item, pr)
        if pr.html_url != item.backport_pr:
            project.set_value(item.id, F_BACKPORT_PR, pr.html_url)
            item.backport_pr = pr.html_url
        if status != item.status:
            project.set_value(item.id, F_STATUS, status)
            print(f"    {item.status} -> {status} ({pr.html_url})", flush=True)
            item.status = status

    pending: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        if item.status in (NOT_STARTED, CONFLICTS) and item.decision != DISCARDED and (
            not only_version or item.version == only_version
        ):
            pending[item.version].append(item)
    if not pending:
        print("No pending backports.")
    for v in sort_versions(version_list):
        if pending.get(v.key):
            print(f"\n{v.key} ({v.branch}):")
            for item in pending[v.key]:
                print(f"  {item.status:<12} {item.decision or 'undecided':<10} {item.source_pr}  {item.title}")

    by_pr: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        by_pr[item.source_pr].append(item)
    for url, its in by_pr.items():
        present = {i.version for i in its}
        for i in its:
            if i.requested != EXPLICIT or i.version not in versions:
                continue
            missing = [v.key for v in closure(versions[i.version], version_list) if v.key not in present]
            if missing:
                print(f"\n{url}: missing {', '.join(missing)}; run `backporter add {url} {i.version}`")


def finish_backport(
    gh: Github, project: Project, config: Config, item: Item, version: Version, pr: PullRequest, repo_dir: str,
    comment: str = "",
) -> bool:
    """
    Push the backport branch, open (or reuse) the backport PR, update the project item.
    False, with nothing pushed, if the PR body needs a human.
    """
    branch = backport_branch(version, pr.number)
    try:
        body = backport_body(repo_dir, version, pr)
    except (pr_body.Unclear, pr_body.TemplateError) as e:
        project.set_value(item.id, F_NOTE, f"PR body needs a human: {e}")
        project.set_value(item.id, F_STATUS, CONFLICTS)
        print(f"PR body needs a human: {e}. Nothing pushed; the branch {branch} stays in {repo_dir}.", file=sys.stderr)
        return False
    git_ops.push(repo_dir, branch, TARGET_REPO)
    repo = gh.get_repo(TARGET_REPO)
    # get_repo follows renames/redirects; never open a PR anywhere but TARGET_REPO.
    if repo.full_name.lower() != TARGET_REPO.lower():
        raise RuntimeError(f"Refusing to open a PR in {repo.full_name}; only {TARGET_REPO} is allowed")
    owner = TARGET_REPO.split("/")[0]
    existing = list(repo.get_pulls(state="open", head=f"{owner}:{branch}"))
    if existing:
        backport = existing[0]
        print(f"Reusing open PR {backport.html_url}")
    else:
        print(f"Opening PR {branch} -> {version.branch} in {TARGET_REPO}...")
        backport = repo.create_pull(
            base=version.branch,
            head=branch,
            title=f"{version.label} Backport of #{pr.number} - {pr.title}",
            body=body,
        )
    labels = config.labels_for(version)
    if labels:
        print(f"Adding labels: {', '.join(labels)}")
        backport.add_to_labels(*labels)
    if comment:
        print("Commenting on the PR with a summary of the conflicts...")
        github_guard.allow_comment(backport.number)
        backport.create_issue_comment(comment)
    print("Updating the project...")
    promote(project, item, backport)
    project.set_value(item.id, F_BACKPORT_PR, backport.html_url)
    project.set_value(item.id, F_STATUS, PR_OPEN)
    print(f"{version.key}: {backport.html_url}")
    return True


def set_outcome(project: Project, item: Item, status: str, note: str, by: str | None = None) -> None:
    """Record a final outcome on the project item; nothing is written outside the project."""
    if by:
        project.set_value(item.id, F_BACKPORT_PR, by)
    project.set_value(item.id, F_NOTE, note)
    project.set_value(item.id, F_STATUS, status)
    print(f"{item.version}: {status}: {note}")


def find_existing(gh: Github, repo_dir: str, version: Version, pr: PullRequest, pr_repo: str) -> str | None:
    """URL of a merged PR that already brought pr's change into version.branch, if one can be found."""
    if pr.merge_commit_sha and git_ops.is_ancestor(repo_dir, pr.merge_commit_sha, version.branch):
        return pr.html_url
    commits = git_ops.commits_mentioning(repo_dir, version.branch, pr.number, pr.title, pr.merge_commit_sha or "")
    for sha in commits:
        for repo_name in dict.fromkeys((TARGET_REPO, pr_repo)):
            try:
                pulls = [p for p in gh.get_repo(repo_name).get_commit(sha).get_pulls() if p.merged_at]
            except GithubException:
                continue
            if pulls:
                return pulls[0].html_url
    return None


def record_present(
    gh: Github, project: Project, item: Item, version: Version, pr: PullRequest, pr_repo: str, repo_dir: str,
    note: str | None = None, by: str | None = None,
) -> None:
    """Drop the local backport branch and mark the item Already present, naming the PR that brought it."""
    git_ops.drop_backport_branch(repo_dir, version.branch, backport_branch(version, pr.number))
    by = by or find_existing(gh, repo_dir, version, pr, pr_repo)
    found = f"via {by}" if by else "introducing PR not found"
    set_outcome(project, item, PRESENT, f"{note}; {found}" if note else f"Already in {version.branch}; {found}", by)


def resolve_with_ai(
    gh: Github, project: Project, config: Config, item: Item, version: Version, pr: PullRequest, pr_repo: str,
    repo_dir: str, conflicts: list[tuple[str, str]],
) -> bool:
    """
    Commit the cherry-pick with its conflicts as-is, let the AI resolve them, commit the resolution separately,
    and act on the AI's outcome. False if it stayed unresolved (the conflicts are then left in progress again).
    """
    branch = backport_branch(version, pr.number)
    conflict_sha = git_ops.commit_conflicts(repo_dir, conflicts)
    print(f"Committed the cherry-pick with its conflicts as-is: {conflict_sha[:12]}")
    prompt = ai.build_prompt(
        repo_dir, version.branch, branch, pr.html_url, pr.title, pr.body or "", pr.merge_commit_sha, conflicts,
        conflict_sha,
    )
    result = ai.resolve(repo_dir, prompt, conflicts)
    if result.outcome == ai.RESOLVED:
        resolution_sha = git_ops.commit_resolution(
            repo_dir, f"Resolve conflicts in backport of #{pr.number}\n\n{result.note}\n"
        )
        print(f"Committed the resolution: {resolution_sha[:12]}")
        if git_ops.same_tree(repo_dir, version.branch):
            record_present(gh, project, item, version, pr, pr_repo, repo_dir, f"AI: {result.note}")
            return True
        project.set_value(item.id, F_NOTE, f"AI resolved conflicts: {result.note}")
        comment = conflict_comment(conflicts, conflict_sha, resolution_sha, result)
        return finish_backport(gh, project, config, item, version, pr, repo_dir, comment)
    if result.outcome == ai.ALREADY_PRESENT:
        record_present(gh, project, item, version, pr, pr_repo, repo_dir, f"AI: {result.note}", result.by or None)
        return True
    if result.outcome == ai.NOT_APPLICABLE:
        git_ops.drop_backport_branch(repo_dir, version.branch, branch)
        set_outcome(project, item, NOT_APPLICABLE, f"AI: {result.note}")
        return True
    git_ops.redo_pick(repo_dir, pr.merge_commit_sha)
    project.set_value(item.id, F_NOTE, f"AI could not resolve: {result.note}")
    project.set_value(item.id, F_STATUS, CONFLICTS)
    return False


def conflict_comment(conflicts: list[tuple[str, str]], conflict_sha: str, resolution_sha: str, result: ai.Result) -> str:
    """Comment for the backport PR: what conflicted and how the AI resolved it."""
    files = "\n".join(f"- `{path}` ({kind})" for path, kind in conflicts)
    return (
        "**Backporter: cherry-pick conflicts resolved by AI**\n\n"
        f"Conflicted files:\n{files}\n\n"
        f"Commits:\n- {conflict_sha[:12]}: cherry-pick with the conflicts committed as-is\n"
        f"- {resolution_sha[:12]}: resolution\n\n"
        f"{result.summary or result.note}\n"
    )


def check_labels(gh: Github, config: Config, versions: list[Version]) -> None:
    """Fail early if a configured label does not exist in TARGET_REPO (labels are never created)."""
    repo = gh.get_repo(TARGET_REPO)
    names = dict.fromkeys(n for v in versions for n in config.labels_for(v))
    missing = []
    for name in names:
        try:
            repo.get_label(name)
        except GithubException:
            missing.append(name)
    if missing:
        raise RuntimeError(f"Labels not found in {TARGET_REPO}: {', '.join(missing)}; fix [labels] in {CONFIG_PATH}")


def backport(
    gh: Github, project: Project, config: Config, pr: PullRequest, pr_repo: str, items: dict[str, Item],
    targets: list[Version], dirs: dict[str, str], use_ai: bool,
) -> bool:
    """Backport pr into targets (each has a clone dir), oldest first. False if stopped at unresolved conflicts."""
    merge_sha = git_ops.get_merge_commit_sha(pr)
    for v in targets:
        git_ops.check_repo_dir(dirs[v.key])
        git_ops.check_origin(dirs[v.key], TARGET_REPO)
    check_labels(gh, config, targets)

    for v in sort_versions(targets):
        item = items[v.key]
        print(f"\n=== {v.key} ===")
        outcome, conflicts = git_ops.cherry_pick(
            dirs[v.key], TARGET_REPO, v.branch, backport_branch(v, pr.number), pr_repo, merge_sha
        )
        if outcome == git_ops.EMPTY:
            record_present(gh, project, item, v, pr, pr_repo, dirs[v.key])
            continue
        if outcome == git_ops.CONFLICTS:
            if use_ai and resolve_with_ai(gh, project, config, item, v, pr, pr_repo, dirs[v.key], conflicts):
                continue
            project.set_value(item.id, F_STATUS, CONFLICTS)
            print(f"\nConflicts in {len(conflicts)} file(s) in {dirs[v.key]}:", file=sys.stderr)
            for path, kind in conflicts:
                print(f"  {path}  ({kind})", file=sys.stderr)
            print(f"\nResolve them, then run: backporter continue {pr.html_url} {v.key}", file=sys.stderr)
            return False
        if not finish_backport(gh, project, config, item, v, pr, dirs[v.key]):
            return False
    return True


def cmd_start(args, gh: Github, config: Config) -> None:
    pr_repo, number = git_ops.parse_pr_url(args.pr)
    url = source_url(pr_repo, number)
    pr = gh.get_repo(pr_repo).get_pull(number)
    project = open_project(gh, config)
    versions = project_versions(project, config)
    items = pr_items(project, url)
    if args.versions:
        targets = [find_version(versions, k) for k in args.versions]
        untracked = [v.key for v in targets if v.key not in items]
        if untracked:
            raise RuntimeError(f"Not tracked: {', '.join(untracked)}; run `backporter add` first")
    else:
        targets = [
            v for v in versions
            if v.key in items and items[v.key].status == NOT_STARTED and items[v.key].decision != DISCARDED
        ]
    if not targets:
        print("Nothing to start.")
        return
    dirs = load_dirs(args.dirs, versions)
    no_dir = [v.key for v in targets if v.key not in dirs]
    if no_dir:
        raise RuntimeError(f"No clone dir for {', '.join(no_dir)}; pass -C KEY=DIR or set it in the dirs file")
    if not backport(gh, project, config, pr, pr_repo, items, targets, dirs, args.ai):
        sys.exit(1)


def cmd_run(args, gh: Github, config: Config) -> None:
    """scan, then backport every Not started item with Decision Approved that has a clone dir."""
    scan(gh, config, None)
    project = open_project(gh, config)
    versions = project_versions(project, config)
    dirs = load_dirs(args.dirs, versions)
    use_ai = config.run_ai and not args.no_ai
    approved: dict[str, list[Item]] = defaultdict(list)
    for item in tracked_items(project):
        if item.status == NOT_STARTED and item.decision == APPROVED:
            approved[item.source_pr].append(item)
    if not approved:
        print("\nNothing approved to run.")
        return
    count = sum(len(its) for its in approved.values())
    print(f"\nApproved: {count} item(s) in {len(approved)} PR(s); AI for conflicts: {'on' if use_ai else 'off'}")
    failed = False
    for n, (url, its) in enumerate(approved.items(), 1):
        print(f"\n##### [{n}/{len(approved)}] {url}: {', '.join(i.version for i in its)}")
        targets = [v for v in versions if v.key in {i.version for i in its}]
        no_dir = [v.key for v in targets if v.key not in dirs]
        if no_dir:
            print(f"No clone dir for {', '.join(no_dir)}; skipped", file=sys.stderr)
        targets = [v for v in targets if v.key in dirs]
        if not targets:
            continue
        pr_repo, number = git_ops.parse_pr_url(url)
        items = pr_items(project, url)
        try:
            pr = gh.get_repo(pr_repo).get_pull(number)
            if not pr.merged:
                print("Source PR is not merged yet; skipped", file=sys.stderr)
                continue
            if not backport(gh, project, config, pr, pr_repo, items, targets, dirs, use_ai):
                failed = True
        except (RuntimeError, ValueError, GithubException) as e:
            print(f"Error: {e}", file=sys.stderr)
            for v in targets:
                project.set_value(items[v.key].id, F_NOTE, f"backporter run failed: {e}")
            failed = True
    if failed:
        sys.exit(1)


def cmd_continue(args, gh: Github, config: Config) -> None:
    pr_repo, number = git_ops.parse_pr_url(args.pr)
    url = source_url(pr_repo, number)
    project = open_project(gh, config)
    versions = project_versions(project, config)
    version = find_version(versions, args.version)
    item = pr_items(project, url).get(version.key)
    if item is None:
        raise RuntimeError(f"{url} is not tracked for {version.key}")
    dirs = load_dirs(args.dirs, versions)
    if version.key not in dirs:
        raise RuntimeError(f"No clone dir for {version.key}; pass -C KEY=DIR or set it in the dirs file")
    pr = gh.get_repo(pr_repo).get_pull(number)
    repo_dir = dirs[version.key]
    check_labels(gh, config, [version])
    if args.ai:
        git_ops.check_repo_dir(repo_dir)
        git_ops.check_origin(repo_dir, TARGET_REPO)
        if git_ops.get_current_branch(repo_dir) != backport_branch(version, number):
            raise RuntimeError(f"{repo_dir} is not on {backport_branch(version, number)}")
        conflicts = git_ops.get_conflicted_entries(repo_dir)
        if conflicts:
            if not resolve_with_ai(gh, project, config, item, version, pr, pr_repo, repo_dir, conflicts):
                sys.exit(1)
            return
    if git_ops.continue_cherry_pick(dirs[version.key], backport_branch(version, number)) == git_ops.EMPTY:
        record_present(gh, project, item, version, pr, pr_repo, dirs[version.key])
        return
    if not finish_backport(gh, project, config, item, version, pr, dirs[version.key]):
        sys.exit(1)


def cmd_mark(args, gh: Github, config: Config) -> None:
    pr_repo, number = git_ops.parse_pr_url(args.pr)
    url = source_url(pr_repo, number)
    project = open_project(gh, config)
    versions = project_versions(project, config)
    version = find_version(versions, args.version)
    item = pr_items(project, url).get(version.key)
    if item is None:
        raise RuntimeError(f"{url} is not tracked for {version.key}")
    if args.outcome == "not-applicable" and not args.note:
        raise ValueError("not-applicable requires --note with the reason")
    repo_dir = load_dirs(args.dirs, versions).get(version.key)
    if args.outcome == "present" and repo_dir:
        pr = gh.get_repo(pr_repo).get_pull(number)
        record_present(gh, project, item, version, pr, pr_repo, repo_dir, args.note, args.by)
        return
    if repo_dir and git_ops.branch_exists(repo_dir, backport_branch(version, number)):
        git_ops.drop_backport_branch(repo_dir, version.branch, backport_branch(version, number))
    if args.outcome == "present":
        found = f"via {args.by}" if args.by else "introducing PR not searched (no clone dir)"
        set_outcome(project, item, PRESENT, f"{args.note or 'Already in ' + version.branch}; {found}", args.by)
    else:
        set_outcome(project, item, NOT_APPLICABLE, args.note)


def cmd_describe(args, gh: Github, config: Config) -> None:
    pr_repo, number = git_ops.parse_pr_url(args.pr)
    pr = gh.get_repo(pr_repo).get_pull(number)
    category, entry = pr_body.source_parts(pr.body or "")
    print(category)
    print("\n".join(pr_body.attribute(entry, pr.html_url, pr.user.login if pr.user else "")))


def main() -> None:
    # Show progress immediately even when output goes to a pipe or a log file.
    sys.stdout.reconfigure(line_buffering=True)
    load_dotenv()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token with repo and project scopes (default: GITHUB_TOKEN from the environment or .env)",
    )
    with_dirs = argparse.ArgumentParser(add_help=False, parents=[common])
    with_dirs.add_argument(
        "-C", dest="dirs", action="append", default=[], metavar="KEY=DIR",
        help="Clone dir for a version, e.g. -C stable-25.8=../releases/258 (repeatable; overrides the dirs file)",
    )

    parser = argparse.ArgumentParser(
        description="Backport tracker backed by a GitHub Project.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="Format an empty project and save its link to config")
    p.add_argument("url", help="Project URL, e.g. https://github.com/orgs/Altinity/projects/N")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add-version", parents=[common], help="Add a ClickHouse version (its branch must be in [branches] of the config)")
    p.add_argument("version", help="X.Y, e.g. 27.3")
    p.add_argument("--flavor", required=True, choices=[*FLAVORS, "both"])
    p.set_defaults(func=cmd_add_version)

    p = sub.add_parser("add", parents=[common], help="Track a backport to a version and all versions it implies")
    p.add_argument("pr", help="Source PR URL")
    p.add_argument("version", help="Version key, e.g. stable-25.8")
    p.add_argument(
        "--not-needed-from", metavar="X.Y",
        help="Version from which the backport isn't needed; those versions are marked Not applicable",
    )
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", parents=[common], help="Stop tracking a version and what only it implied")
    p.add_argument("pr", help="Source PR URL")
    p.add_argument("version", help="Version key, e.g. stable-25.8")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("scan", parents=[common], help="Process web UI requests, refresh statuses, list pending")
    p.add_argument("--version", help="Only this version key")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("start", parents=[with_dirs], help="Cherry-pick, push and open backport PRs")
    p.add_argument("pr", help="Source PR URL")
    p.add_argument("versions", nargs="*", help="Version keys (default: all 'Not started' for this PR)")
    p.add_argument("--ai", action="store_true", help="Let AI resolve conflicts (API if ANTHROPIC_API_KEY is set, else claude CLI)")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser(
        "run", parents=[with_dirs], help="scan, then backport every Not started item whose Decision is Approved",
    )
    p.add_argument("--no-ai", action="store_true", help="Don't use AI for conflicts (default: [run] ai in config)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("continue", parents=[with_dirs], help="Finish a backport after resolving conflicts")
    p.add_argument("pr", help="Source PR URL")
    p.add_argument("version", help="Version key, e.g. stable-25.8")
    p.add_argument("--ai", action="store_true", help="Let AI resolve the remaining conflicts first")
    p.set_defaults(func=cmd_continue)

    p = sub.add_parser(
        "mark", parents=[with_dirs], help="Record that a backport is already present or not applicable",
    )
    p.add_argument("pr", help="Source PR URL")
    p.add_argument("version", help="Version key, e.g. stable-25.8")
    p.add_argument("outcome", choices=["present", "not-applicable"])
    p.add_argument("--by", help="URL of the PR that already brought the change (present)")
    p.add_argument("--note", help="Reason, stored in the project's Note field (required for not-applicable)")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("describe", parents=[common], help="Print changelog description of a PR")
    p.add_argument("pr", help="PR URL")
    p.set_defaults(func=cmd_describe)

    args = parser.parse_args()
    gh = guarded_github(args.token)
    try:
        args.func(args, gh, load_config())
    except (RuntimeError, ValueError, GithubException) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
