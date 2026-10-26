# backporter

Tracks and performs ClickHouse backports to our versions, and bumps (merging an upstream release into a version). All tracking data lives in a GitHub Project; the tool keeps no local state besides the list of local clone dirs.

Project: https://github.com/orgs/Altinity/projects/39

## Requesting a backport

Name only the **oldest** version that needs the change. The newer ones are added automatically:

- **stable X** also brings every newer stable and every antalya version ≥ X;
- **antalya X** also brings every newer antalya, but **not** stable. To get a stable backport too, name a stable version as well.

Version keys look like `stable-25.8` or `antalya-26.3`. Each version has its own view (tab) in the project, named by its key.

**Unmerged PRs.** A PR can be requested before it's merged, and its Antalya versions can be backported before then too. Stable versions wait until it's merged: until then `start`, `run` and `check` skip them. An unmerged PR, open or closed, is backported as its diff, the same one GitHub shows under "Files changed", even if it conflicts with its base branch.

### From the project's web page (nothing to install)

1. Open the [project](https://github.com/orgs/Altinity/projects/39). It opens on the **Requests** view.
2. Paste the original PR's link into the "Start typing to create an item" row at the bottom and press Enter, or use **Add item from repository**. The PR is added as a row.
3. In that row's **Version** cell, pick the oldest version that needs the change. Leave **Source PR** empty: a PR row with another link there is an [existing port](#tracking-an-existing-port).
4. Done. The next `backporter scan` creates one tracked item per version, explicit plus implied, in the version views, with Status `Not started` and **Requested by** set to whoever added the request row. Then it deletes the request row.

**Two starting versions.** Only needed when the stable and antalya starting points differ, e.g. antalya from 25.8 but stable only from 26.3. Pick one in **Version** and type the other key(s) in the row's **Note** cell, e.g. `stable-26.3`.

**Not needed from newer versions.** If the change is known to be unnecessary from some version on (e.g. the code changed in 26.3), pick that version number in the request's **Not needed from** cell (empty = no limit). All implied versions from that number up, stable and antalya alike, get rows with Status `Not applicable` and "Not needed from 26.3 (per request by @…)" in Note. Rows that already exist are left as they are. The limit is stored on the explicit row, so versions added later with `add-version` are also created as `Not applicable` for that PR. A request whose own version is at or above its limit is skipped. On the command line: `backporter add <PR> <version> --not-needed-from 26.3`.

**Requesting again.** A request never creates a duplicate row: if the PR is already tracked for a version, that row is kept as it is (an implied row becomes explicit). If the row was **Discarded**, a new request for that exact version clears its Decision (back to undecided), sets Requested by to the new requester, and writes "Previously discarded; requested again by @… on <date>" in Note. Implied versions that were discarded individually stay discarded.

**What GitHub records.** Adding an upstream `ClickHouse/ClickHouse` PR to the project makes GitHub log "added to project" on that PR, and deleting the processed row may log "removed from project". These are project events. The backporter never writes to the upstream PR itself.

**Alternative: a draft row, which leaves nothing on the upstream PR.** Type the version key(s), e.g. `stable-25.8`, and press **Cmd+Enter** (Ctrl+Enter on Linux/Windows). Then open the draft and paste the PR link into its body. Plain Enter on typed text creates a real issue instead ([GitHub changelog](https://github.blog/changelog/2025-02-18-github-issues-projects-february-18th-update/)).

A request is skipped, and stays in **Requests**, if it has no PR link, no version, or a version key the project doesn't have. `scan` prints the reason. Fix the row and it's picked up next time.

### Requesting a bump

A bump merges an upstream release into a version: its tag (e.g. `v26.3.53.4-lts`) is fetched from upstream and merged into the version's branch. Request it like a backport, with the release's link instead of a PR's:

1. In **Requests**, paste the release link, e.g. `https://github.com/ClickHouse/ClickHouse/releases/tag/v26.3.53.4-lts`, and press **Cmd+Enter** (Ctrl+Enter on Linux/Windows) to create a draft. Plain Enter would create a real issue.
2. In **Version**, pick the version to bump. It must be the release's X.Y.
3. The next `scan` tracks the bump for **every flavor of that X.Y and nothing else**: a bump of `stable-26.3` also brings `antalya-26.3`, but not 26.6, 26.8 or 25.8.

Everything else works as for backports: Decision, Status, `run`, `start`, `continue`, `mark` and `remove`, which all take the release link in place of a PR link. A bump request is skipped if its version isn't the release's X.Y, or if **Not needed from** is set (it doesn't apply to bumps). On the command line: `backporter add https://github.com/ClickHouse/ClickHouse/releases/tag/v26.3.53.4-lts stable-26.3`.

A bump PR follows the team's convention: branch `bump/<branch>/<X.Y.Z>`, title `<Stable|Antalya> X.Y: Bump to X.Y.Z`, a merge commit `Merge tag '<tag>' into bump/<branch>/<X.Y.Z>`, and the same labels and builds project as a backport PR. If the tag is already in the branch, the item becomes `Already present`.

### Tracking an existing port

A backport or bump PR made outside the backporter (by hand, or a port with changes of its own) can be tracked like the ones it opens:

1. In **Requests**, paste the port PR's link (a PR in `Altinity/ClickHouse`) into the bottom row and press Enter.
2. In the row's **Source PR** cell, paste the link of the PR it ports, or the release link for a bump. A request row that is a PR with a different PR (or release) in **Source PR** is a port; **Version** isn't needed.
3. The next `backporter scan` makes the port the tracked row of that source for the version whose branch the port targets (its base branch, looked up in `[branches]`). The row moves to that version's view with Requested `explicit`, Status from the port (`PR open` / `Merged` / `Closed`), and the port is added to its builds project like any backport PR.

Only that version is tracked: no implied versions are added, so `scan` reports them as missing for that PR. One version has one port:
- If the source is already tracked for that version, the port takes over its row and its values (Decision, Requested, Note, …).
- A row marked `Already present` or `Not applicable` is overwritten; its old Status and Note go into Note as `Was <status>: <note>`.
- A row that already has another backport PR is left alone and the port is skipped.

A port is also skipped, and stays in **Requests**, if it isn't a PR in `Altinity/ClickHouse`, its base branch is no version's branch, or its Source PR has no PR or release link. `scan` prints the reason. On the command line: `backporter add-port <port-URL> <source-URL>` (see [`add-port`](#add-port)).

### From the command line

```bash
backporter add https://github.com/ClickHouse/ClickHouse/pull/109551 stable-25.8
```

This creates the items immediately. To undo it, run `backporter remove <PR> <version>` (see [`remove`](#remove)).

### Deciding what gets done (also from the web page)

Two columns, with separate owners:

- **Decision** is yours. Set it in any view; empty means undecided, and nothing is backported until someone decides.
  - **`Approved`**: do it. The next `backporter run` backports it: cherry-pick (for a bump: merge the release's tag), push, and a labeled PR in `Altinity/ClickHouse`. AI resolves conflicts by default.
  - **`Discarded`**: don't do it. The item stays as a record, and the backporter never touches it again. [`backporter check`](#check) also sets it, on undecided items it finds to make no sense.
- **Status** belongs to the backporter. Don't edit it. Together with **Note**, it shows what happened. Once a backport PR exists, the row *is* that PR, so its title links to it:

| Status | Meaning |
|---|---|
| `Not started` | nothing done yet; with Decision `Approved` it's queued for the next `run` (`Note` says why, if a run couldn't do it) |
| `Conflicts` | needs a human: unresolved conflicts, or a PR body that couldn't be built; `Note` has the reason |
| `PR open` / `Merged` / `Closed` | the row is the backport PR |
| `Already present` | already in that branch; `Note` names the PR that brought it, if found |
| `Not applicable` | makes no sense for that version; `Note` says why |

`backporter scan` refreshes statuses from GitHub and lists what's still pending. To backport one PR right away without approving, run `backporter start <PR>`. To narrow down what needs a decision, run `backporter check`: it tries the undecided items locally, discards those that make no sense, and notes how the rest went (see [`check`](#check)).

## Installation

```bash
# Requires Python 3.10+ and Git. For --ai: Claude Code (`claude` on PATH) or ANTHROPIC_API_KEY in .env
git clone git@github.com:zvonand/backporter.git
cd backporter
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
# or as a wheel (config.toml is packaged into it): python3 -m build && pip install dist/backporter-*.whl

# GitHub token with `repo` and `project` scopes (see "GitHub token" below)
echo 'GITHUB_TOKEN=ghp_...' > .env
chmod 600 .env

# Local ClickHouse clone(s) with origin = Altinity/ClickHouse, one line per version you backport into
mkdir -p ~/.config/backporter
echo '"stable-25.8" = "~/src/ClickHouse"' >> ~/.config/backporter/dirs.toml

backporter scan
```

## GitHub token

The tool reads the token from `GITHUB_TOKEN`, or from `--token` on any command. The easiest place for it is a `.env` file in the repo root, which is gitignored:

```bash
# .env
GITHUB_TOKEN=ghp_...
# ANTHROPIC_API_KEY=...   # optional: makes --ai use the Anthropic API instead of the claude CLI
```

`.env` is read from the current directory, or else from the backporter repo root (editable installs). For a wheel install, run from a directory that has the `.env`. Variables already set in the environment take precedence, and empty values are ignored. Keep the file private (`chmod 600 .env`).

The token needs:

- a classic personal access token with the `repo` and `project` scopes (`read:project` is not enough). If the org enforces SSO, authorize the token for it.
- or the `gh` CLI token with the project scope added:

  ```bash
  gh auth refresh -s project
  export GITHUB_TOKEN=$(gh auth token)
  ```

The token is used for the GitHub API only. `git fetch`/`git push` in the clone dirs use your normal git credentials.

## Configuration

[`backporter/config.toml`](backporter/config.toml) is committed and shared by the team. It holds:

- `project_owner`, `project_number`: the project, written by `backporter init <project-URL>`.
- `[run]`: `ai = true` turns on AI conflict resolution by default in `backporter run`.
- `[labels]`: labels added to every backport and bump PR the backporter opens.
  - `all` applies to every PR.
  - A flavor (`stable`, `antalya`) adds labels for that flavor.
  - A version key (e.g. `"antalya-25.8"`) adds labels for that version.

  ```toml
  [labels]
  all = ["backport"]
  stable = ["stable"]
  antalya = ["antalya"]
  "stable-25.8" = ["25.8", "stable-25.8"]
  "antalya-25.8" = ["antalya-25.8"]
  ```

  With this config, a backport into `antalya-25.8` gets `backport`, `antalya` and `antalya-25.8`, and one into `stable-25.8` gets `backport`, `stable`, `25.8` and `stable-25.8`. The committed config follows the team's backport PR convention for every version. Labels must already exist in `Altinity/ClickHouse`; the backporter never creates them. `start` and `continue` check this before any git work and stop on a missing label.

- `[builds]`: the team's builds projects that backport and bump PRs are also added to, once, per flavor: project number, the text field that gets the version number, the Status for a newly added open PR, and the Priority for a newly added PR when the source PR's row there has none. Currently stable goes to project 26 (Stable Builds, "Port Versions", Priority "P0") and antalya to project 17 (Project Antalya, "Release Versions (Major)", Priority "p0"), both with Status "Needs Review". See step 7 of [`start`](#start) for how the source PR's row there is replaced.
- `[branches]`: the branch in `Altinity/ClickHouse` for each version key. Branch names don't follow one pattern, so each is listed explicitly:

  ```toml
  [branches]
  "stable-25.3" = "releases/25.3.14"
  "stable-26.3" = "stable-26.3"
  "antalya-26.3" = "antalya-26.3"
  ```

  To move a version to a new branch (e.g. `releases/25.3.15`), change its entry and commit. Every version in the project must have an entry: commands stop and name any version without one.

Everything else, including the list of versions, lives in the project.

## Safety: the only destination is Altinity/ClickHouse

Source PRs and releases may come from `ClickHouse/ClickHouse`, but the backporter only ever reads from it. It never writes anything there: no PRs, comments, reviews, labels, edits or issues. Its only comment anywhere is the conflict summary on a backport or bump PR it opened in `Altinity/ClickHouse` (see `start`).

The target repo is hardcoded (`TARGET_REPO` in `backporter/config.py`) and is not configurable.

**GitHub API allowlist** (`backporter/github_guard.py`): every request PyGithub makes, including redirects, is checked before it is sent. Allowed:
- reads (`GET`/`HEAD`);
- `POST /repos/Altinity/ClickHouse/pulls` (opening a backport or bump PR);
- `POST /repos/Altinity/ClickHouse/issues/<n>/labels` (adding the configured labels to it; labels are never removed or created);
- `POST /repos/Altinity/ClickHouse/issues/<n>/comments`, only for a backport or bump PR the backporter just opened or reused (registered with the guard by number), for the AI conflict summary;
- `POST /orgs/Altinity/projectsV2/<n>/views` (creating a project view; only this REST call can set a view's grouping);
- a GraphQL mutation only if it is a single operation whose top-level fields are all project mutations: `createProjectV2Field`, `updateProjectV2Field`, `createProjectV2View`, `updateProjectV2View`, `deleteProjectV2View`, `addProjectV2DraftIssue`, `updateProjectV2ItemFieldValue`, `clearProjectV2ItemFieldValue`, `deleteProjectV2Item`, and `addProjectV2ItemById` only for a PR the code has just verified to be in `Altinity/ClickHouse` (registered with the guard by its node id). Fields are never deleted.

Anything else is refused with an error. If the installed PyGithub doesn't have the request hook the guard needs, the tool refuses to start.

**Git:**

- **Before any git work and before every push:** every push URL of `origin` in the clone dir must point to `github.com/Altinity/ClickHouse`. The URLs are read with `git remote get-url --push --all origin`, so `insteadOf`/`pushInsteadOf` rewrites are applied. Otherwise the command refuses. `start` checks all the clone dirs it will use before it starts.
- **Pushes:** only to `origin`, with an explicit `refs/heads/<branch>:refs/heads/<branch>` refspec, so fetched upstream tags are never pushed. The `upstream` remote (the source PR's or release's repo) is only fetched from.
- **PRs:** opened only via the API in `Altinity/ClickHouse`, after checking that the resolved repo name is exactly that. A rename or redirect is refused.

## The GitHub Project

### Versions

Each version has a key `<flavor>-X.Y`, e.g. `stable-25.8` or `antalya-26.3`. The list of versions is stored in the project, as the options of its `Version` field. Each version's branch in `Altinity/ClickHouse` comes from `[branches]` in the config.

Propagation rules:

- A backport to **stable X** also requires every stable version ≥ X **and** every antalya version ≥ X.
- A backport to **antalya X** also requires every antalya version ≥ X.
- A bump of **any flavor of X.Y** requires every flavor of X.Y, and no other version.

Example: with stable 25.3, 25.8, 26.3, 26.8 and antalya 25.8, 26.3, 26.6, 26.8, `stable-25.3` implies all the others, and `antalya-26.3` implies `antalya-26.6` and `antalya-26.8`.

### Items

Each item is one (source PR, version) pair, or (release, version) for a bump. It starts as a draft titled `<owner/repo>#<N> <source PR title>` (`Bump to <tag>` for a bump), with the source URL as its body. When a backport PR exists (opened by the backporter, or opened by hand and found by `scan`), the draft is **replaced by that PR itself**, with all field values copied. The Title column, which GitHub always shows first, then is the backport PR: clickable, with its state. Only `Altinity/ClickHouse` PRs are ever added this way. Fields:

| Field | Meaning |
|-------|---------|
| Source PR | URL of the PR being backported, or of the release for a bump |
| Version | Version key, e.g. `stable-25.8` |
| Flavor | `stable` / `antalya` |
| Requested | `explicit` (named in `add`, a request or a port) or `implied` (required by the propagation rules) |
| Requested by | GitHub login of whoever added the request or port row (for `backporter add` and `add-port`: the token's owner). Implied rows, including those added later by `add-version`, get the same person. |
| Decision | Set by people: empty (undecided), `Approved`, `Discarded` (see [Deciding what gets done](#deciding-what-gets-done-also-from-the-web-page)) |
| Status | Set by the backporter: `Not started`, `Conflicts`, `PR open`, `Merged`, `Closed`, `Already present`, `Not applicable` |
| Backport PR | URL of the backport PR in `Altinity/ClickHouse`; for `Already present`, the PR that already brought the change (Altinity or ClickHouse) |
| Note | One line explaining the outcome: why it is not applicable, which PR already brought it, what the AI adapted or why it gave up |

Draft items can't have comments, so the explanation goes in `Note`. Notes and found PRs are written only to the project, never anywhere else.

### Views

- One table view per version, named by its key, filtered with `version:"<key>" has:requested` (tracked rows only, so unprocessed requests stay in `Requests`) and grouped by Status. For example, open the `stable-25.8` view to see everything going into stable 25.8. Grouping can only be set when a view is created (via the REST API), so views created by older versions of the backporter have to be recreated to get it.
- `Requests`: the first tab, which the project opens with. It shows requests added in the web page that `scan` hasn't processed yet (filter `no:requested`), so a processed request disappears from it, replaced by tracked items in the version views. See [Requesting a backport](#requesting-a-backport), [Requesting a bump](#requesting-a-bump) and [Tracking an existing port](#tracking-an-existing-port). GitHub can't reorder tabs through the API, so `init` turns GitHub's default first view ("View 1") into `Requests`. If someone has already customized the first view, `init` leaves it alone and asks you to drag the `Requests` tab to the front.
- `All`: a board of all tracked rows (filter `has:requested`).

`init` and `add-version` set up the columns of every view. Views created through the API would otherwise show only GitHub's defaults.
- **Shown:** Decision, Status, Version, Requested, Requested by and Note. Hidden in the tables: Backport PR, because a row with a backport PR is that PR, and Not needed from, which is set in requests; the rows it excludes are `Not applicable` with the reason in Note. `Requests` gets only Version, Source PR (for ports) and Note (its rows are the PRs themselves), and the board's cards get Version and Decision.
- **Hidden:** the built-in Assignees, Linked pull requests and Sub-issues progress, which are always empty for draft items.
- **Left alone:** other columns you turn on yourself.

GitHub's API can't set column order. Columns follow the order in which the fields were created, so `init` creates the fields people use first: Decision, Backport PR, Note, then Requested, Requested by, Flavor and Source PR. Version and Not needed from come last, because GitHub won't create a select field without options and the first `add-version` creates them. To change the order in a view, drag the column headers in the web page and save the view.

The project is the source of truth. People set Decision (and the request and port cells); everything else is maintained by the backporter.

## Setup

1. Create an empty project in GitHub (e.g. under the Altinity org).
2. Format it and save its link to the config:

   ```bash
   backporter init https://github.com/orgs/Altinity/projects/<N>
   ```

   - **Empty project:** it creates the fields and the `Requests` and `All` views, and replaces the built-in Status options (Todo / In Progress / Done) with the ones above.
   - **Project already formatted by the backporter** (it has a `Source PR` field): only adds what is missing, and keeps existing items and views.
   - **Any other non-empty project** (has items or custom fields): refused and left untouched.

   Commit the updated `backporter/config.toml`.
3. Make sure each version's branch is in `[branches]` of `backporter/config.toml`, then add the versions:

   ```bash
   backporter add-version 24.8 --flavor stable
   backporter add-version 25.3 --flavor stable
   backporter add-version 25.8 --flavor both
   backporter add-version 26.3 --flavor both
   backporter add-version 26.6 --flavor antalya
   backporter add-version 26.8 --flavor both
   ```

## Usage

`--token` and `-C` go after the subcommand, e.g. `backporter start -C ... <PR-URL>`. `<PR-URL>` is a merged or unmerged PR in any repo, typically `ClickHouse/ClickHouse` or `Altinity/ClickHouse`. `add`, `remove`, `start`, `continue` and `mark` also take a release URL instead, for a [bump](#requesting-a-bump).

### `add-version`

```bash
backporter add-version X.Y --flavor stable|antalya|both
```

Adds `stable-X.Y` and/or `antalya-X.Y` to the project: a `Version` option plus the version's view.
- Each key needs a branch in `[branches]` of the config, and that branch must exist in `Altinity/ClickHouse`. Add the entry first.
- Existing versions are refused.

It then backfills: every tracked PR whose explicit requests imply a new version gets an `implied` item for it. For example, after `add-version 27.3 --flavor both`, a PR with an explicit `stable-25.8` gets `stable-27.3` and `antalya-27.3`.

### `add`

```bash
backporter add <PR-URL> <version> [--not-needed-from X.Y]
```

The command-line equivalent of a web request, with Requested by = the token's owner. It creates items for `<version>` (Requested `explicit`) and every version it implies (`implied`), all with Status `Not started`.
- `--not-needed-from` works like the request's **Not needed from** cell: implied versions from X.Y up get `Not applicable` rows.
- Items that already exist are left as they are, except that an `implied` item for `<version>` becomes `explicit`, and a `Discarded` one is reopened (see [Requesting again](#requesting-a-backport)).
- Re-running is safe and fills in any missing implied items.
- With a release URL instead of `<PR-URL>`, it tracks a [bump](#requesting-a-bump): `<version>` must be the release's X.Y, and `--not-needed-from` isn't allowed.

### `add-port`

```bash
backporter add-port <port-URL> <source-URL>
```

The command-line equivalent of [Tracking an existing port](#tracking-an-existing-port), with Requested by = the token's owner. `<port-URL>` is a backport or bump PR in `Altinity/ClickHouse`; its base branch gives the version. `<source-URL>` is the PR it ports, or the release for a bump. If the port is waiting in **Requests**, that row is used; otherwise the port is added to the project.

### `remove`

```bash
backporter remove <PR-URL> <version>
```

Deletes the item for `<version>` and the items it implies, except those still implied by another explicit request for the same PR.
- If `<version>` itself is implied by another explicit request (e.g. removing `stable-26.3` while `stable-25.3` is explicit), it refuses and names that request.
- Backport PRs and branches on GitHub are not touched.

Example: with `stable-25.3` and `antalya-26.3` both explicit, `remove <PR> stable-25.3` leaves `antalya-26.3`, `antalya-26.6` and `antalya-26.8`.

### `scan`

```bash
backporter scan [--version KEY]
```

1. Processes requests added in the web page (see [Requesting a backport](#requesting-a-backport)): each requested version is handled like `add`, then the request row is deleted. A port in **Requests** is handled like `add-port` (see [Tracking an existing port](#tracking-an-existing-port)).
2. Refreshes every item that is not `Merged`, `Closed`, `Already present` or `Not applicable`, and not `Discarded`. It finds the backport PR from the item's `Backport PR` field, or else from a PR in `Altinity/ClickHouse` with head `backports/<branch>/<N>` (`bump/<branch>/<X.Y.Z>` for a bump), so backports and bumps opened by hand are picked up. It then sets `Backport PR` and Status (`PR open` / `Merged` / `Closed`), turns a draft row into that PR's row, and adds the PR to its builds project if it isn't there yet, replacing the source PR's row for that version as in step 7 of [`start`](#start). A merged PR is added without a Status; a closed one isn't added.
3. Lists items still `Not started` or in `Conflicts`, grouped by version, with their Decision. `Discarded` items are left out.
4. Reports PRs that are missing versions implied by their explicit requests.

`--version` limits steps 2 and 3 to one version.

### `start`

```bash
backporter start <PR-URL> [<version> ...] [--ai]
```

Backports a PR (or, given a release URL, does a bump) right away. An unmerged PR is backported only to Antalya versions; stable versions are skipped until it's merged (see [Unmerged PRs](#requesting-a-backport)). Running it counts as the decision, so no approval is needed. Without versions it takes all of the PR's `Not started` items that aren't `Discarded`; with versions it takes those (they must already be added, in any status). Every version needs a clone dir (see below). The versions are processed oldest first, and for each one, in its clone dir, it:

1. Fetches and checks out the target branch and pulls it.
2. Creates `backports/<branch>/<N>`.
3. Cherry-picks the PR's merge commit with `-m 1`. If the PR is not from `Altinity/ClickHouse`, the commit is fetched from an `upstream` remote, which is added or updated automatically.
   For an unmerged PR, it first fetches the PR's head (`refs/pull/<N>/head`) and its base branch into `refs/backporter/pull/<N>/head` and `.../base`. It then makes a local commit with the head's tree on top of their merge base, authored by the head commit's author, so the commit's diff is the PR's diff. That commit is what gets cherry-picked.
   For a bump, the branch is `bump/<branch>/<X.Y.Z>` and this step fetches only the release's tag from `upstream` and runs `git merge --no-ff <tag>`.
4. Pushes the branch to `origin`.
5. Opens a PR into the target branch titled `<Stable|Antalya> X.Y Backport of #N - <exact source PR title>`, with the labels configured for its version (see Configuration). If an open PR already exists for the branch, it is reused. The body is built before anything is pushed (see [PR body](#pr-body)).
6. Replaces the draft row by the backport PR (same field values), and sets `Backport PR` and Status `PR open`.
7. Adds the backport PR to its flavor's builds project (see `[builds]`), unless it's already there: the version number goes in that project's version field, and Status is set to the configured one (e.g. "Needs Review"). If the source PR has a row there whose version field lists this version (e.g. `25.8` in `25.8, 25.3`), the backport replaces it for that version: that row's text, single select, number and date values (other than Status and the version field) are copied to the new row, and the version is taken out of it; a row left with no version is deleted. Priority is set to the configured one when the source row has none (or there is no source row). Bumps have no source row. Nothing there is changed afterwards; QA and release status stay with people.

**Already present.** For a bump: the tag is already in the target branch, so the branch is dropped and the item is marked `Already present` with that in `Note`. For a backport: if the cherry-pick turns out empty, the change is already in the target branch. The tool then:
1. Drops the local backport branch.
2. Sets Status `Already present`.
3. Looks for the PR that brought the change, and records it in `Backport PR` and `Note`:
   - If the source PR's merge commit is already an ancestor of the branch, it's the source PR itself.
   - Otherwise it checks the newest commits on the branch whose message mentions `#<N>`, the source PR's title, or its merge commit. It records the first one that belongs to a merged PR in Altinity/ClickHouse, or else in the source PR's repo.

It then continues with the next version.

**Conflicts without `--ai`.** It sets Status `Conflicts`, prints the conflicted files, leaves the branch with conflicts in the clone dir, and stops (exit code 1). Versions after it are not started.

**Conflicts with `--ai`.** The backporter first commits the cherry-pick (for a bump, the merge) **as-is, with the conflict markers**. That commit keeps the original message and adds "Conflicts committed as-is; the next commit resolves them" and the list of files. The AI then resolves the conflicts in the clone dir (see [AI conflict resolution](#ai-conflict-resolution)) and reports one outcome. For a backport, a `RESOLVED` outcome is first checked by a fresh AI reviewer, which can change the outcome (see [AI review](#ai-review)). A successful resolution becomes a **second, separate commit**, so reviewers see exactly what the AI changed.

| AI outcome | What the backporter does |
|---|---|
| `RESOLVED` | Checks that HEAD hasn't moved and no conflict markers are left, then commits the resolution ("Resolve conflicts in backport of #N" with the AI's note). It continues with steps 4–6, writes `AI resolved conflicts: <note>; AI review: <reviewer's note>` to `Note` (a bump has no review part), and posts **one comment on the backport PR** with the conflicted files, the two commits and the AI's short summary of what conflicted and how it was resolved. If a backport's branch ends up identical to the target branch, the item is marked `Already present` as above; a bump's merge is kept. |
| `ALREADY_PRESENT` | Drops the branch and marks `Already present`, using the PR the AI names or, failing that, the lookup above. Not offered for a bump. |
| `NOT_APPLICABLE` | Drops the branch and marks `Not applicable` with the AI's reason in `Note`. Not offered for a bump. |
| `UNRESOLVED` | Undoes the conflict commit and redoes the cherry-pick (or merge), so the conflicts are in progress again for a human (`continue`). Sets `Conflicts` with the AI's reason in `Note`, and stops. |

The reviewer's `ALREADY_PRESENT` or `NOT_APPLICABLE` is handled like the resolver's, with `AI: review: <reason>` in `Note`. When the reviewer still finds the backport wrong after the last resolution, or gives no outcome, the result is `UNRESOLVED`.

If the backport branch already exists locally, it depends on the item:
- `Not started` with no backport PR: a branch kept by [`check`](#check) is reused as it is (no new cherry-pick or merge): it is pushed and gets its PR (steps 4–7), with the AI's conflict comment if `check` had the AI resolve conflicts. Any other leftover branch is recreated from a clean target without asking.
- Otherwise it asks before recreating the branch from a clean target. Answering no aborts.

If the clone is in a conflicted/dirty state, it asks before recreating the branch from a clean target. Answering no aborts.

### `run`

```bash
backporter run [--no-ai]
```

The command for working through the project, by hand or on a schedule:
1. Runs `scan`.
2. Backports every `Not started` item whose Decision is `Approved`, grouped by source PR, oldest version first, the same way as `start`.

AI conflict resolution is on by default. Set `[run] ai = false` in `config.toml` to turn it off, or pass `--no-ai` for one run. If neither the `claude` CLI nor `ANTHROPIC_API_KEY` is available, `run` says so and continues without AI. `start --ai`/`continue --ai` stop with an error instead.

Unlike `start`, a problem doesn't stop the whole run:
- An item whose version has no clone dir, or a stable item whose source PR isn't merged yet, is skipped and stays queued.
- An error (e.g. a misconfigured clone) is written to the item's `Note`, and the item stays queued for the next run.
- Unresolved conflicts set `Conflicts`, and that attempt is dropped (cherry-pick aborted, branch deleted) so the shared clone stays clean for the next PR. `Note` lists the conflicted files and how to do it by hand: `backporter start <PR> <version>`, resolve, then `backporter continue <PR> <version>`. The run goes on with the remaining versions and PRs.

The run exits with code 1 if anything failed or needs a human.

### `check`

```bash
backporter check
```

Tries undecided items locally, so that fewer of them need a decision. Nothing is pushed and no PR is opened; only the project row is updated. It takes every item with Status `Not started`, an empty Decision and a `Note` that doesn't start with `Checked:` (items it has already tried). It skips those whose version has no clone dir, stable ones whose source PR isn't merged yet, and those that already have a backport PR (`scan` updates those). For each remaining item, oldest version first, it does steps 1–3 of [`start`](#start) (a leftover local branch is recreated without asking) and, on conflicts, lets the AI resolve them as `start --ai` does. AI is used whenever it's available; if it isn't, `check` says so and goes on without it.

| Result | Branch | Row |
|---|---|---|
| Already in the branch: the cherry-pick is empty (for a bump: the tag is in the branch), the AI or its reviewer says `ALREADY_PRESENT`, or a backport's AI resolution changes nothing | deleted | Status `Already present`, Decision `Discarded`, and `Backport PR`/`Note` as in [`start`](#start) |
| The AI or its reviewer says `NOT_APPLICABLE` | deleted | Status `Not applicable`, Decision `Discarded`, `Note` `AI: <reason>` (`AI: review: <reason>` from the reviewer) |
| Applies cleanly | kept | `Note` `Checked: the cherry-pick applies cleanly; local branch <branch> kept` |
| AI resolved the conflicts (and, for a backport, its reviewer accepted the result) | kept, with the conflict and resolution commits | `Note` `Checked: AI resolved conflicts: <note>; AI review: <reviewer's note>; local branch <branch> kept` (a bump has no review part) |
| AI couldn't resolve them (or its reviewer kept finding the result wrong), or no AI | deleted (with the AI's edits) | `Note` `Checked: AI could not resolve conflicts in <files>: <reason>` (or `...; AI unavailable to resolve them`); nothing else changes |

A kept branch is marked in the clone's git config (`branch.<branch>.backporterChecked`, holding the AI's conflict comment for the PR, if any). Once the item is `Approved`, `run` (or `start`) pushes that branch as it is and opens its PR, without cherry-picking again (see [`start`](#start)). The branch isn't updated if the target branch moves in the meantime. Deleting the branch (`git branch -D`) removes the mark, and the item is then backported from scratch.

An item that `check` left undecided stays `Not started` with its `Checked:` note, so later checks skip it. To check it again, clear its `Note`.

Errors are printed (the item is left as it is), and `check` exits with code 1 if there were any.

### `continue`

```bash
backporter continue <PR-URL> <version> [--ai]
```

Use after resolving the conflicts left by `start`. The clone dir must be on `backports/<branch>/<N>` with the cherry-pick in progress (for a bump: on `bump/<branch>/<X.Y.Z>` with the merge in progress). It runs `git add .` and `git cherry-pick --continue` (`git merge --continue`), then does steps 4–6 of `start`.
- If a backport's resolution leaves no changes, the item is marked `Already present` instead. A bump's merge is always committed.
- With `--ai`, the AI first resolves whatever conflicts remain, with the same outcomes as `start --ai`.

To continue with the remaining versions, run `start <PR-URL>` again.

### `mark`

```bash
backporter mark <PR-URL> <version> present [--by PR-URL] [--note TEXT]
backporter mark <PR-URL> <version> not-applicable --note TEXT
```

Records a decision made while resolving by hand. It drops the local backport branch if it exists.
- **`present`:** looks up the PR that brought the change the same way `start` does, unless you give `--by`. The lookup needs a clone dir for the version.
- **`not-applicable`:** requires a reason, which goes to `Note`.

### `describe`

```bash
backporter describe <PR-URL>
```

Prints the PR's selected Changelog category and its entry with attribution, exactly as they would go into a backport PR body. It doesn't use the project.

## PR body

A backport PR's body is the **`.github/PULL_REQUEST_TEMPLATE.md` of Altinity/ClickHouse's current default branch**, whatever the target branch. The default branch is looked up on GitHub and the template read through the GitHub API on every backport; the target branch's or the clone's template is never used. The body has exactly two regions filled from the source PR:

- **Changelog category:** the template's list of choices is replaced by the one category the source PR selected, copied verbatim (list marker and parenthetical included, no reclassification).
- **Changelog entry:** the template's placeholder is replaced by the source PR's entry, copied verbatim, with ` (<source PR URL> by @<author login>)` appended to its last line.

Everything else stays byte-for-byte as in the template, in particular the **CI/CD options and their checkbox states**, so every backport gets the default branch's current CI/CD defaults. Nothing else is copied from the source PR, including its CI/CD selections.

A **bump** PR's body is the same template with only the category list replaced by the template's own `Not for changelog` choice. The entry placeholder stays as it is.

**Unclear source PR.** The code handles the normal case. The AI ([AI conflict resolution](#ai-conflict-resolution) explains which backend) gets a prompt with the template, the source PR body and these same rules when:
- the source PR selects no category or several;
- its entry is empty or a placeholder;
- the entry is followed by more text with no heading in between;
- the entry already has an attribution;
- the entry ends in a code block, table, quote or HTML.

The AI either returns a body or says why it can't. Its body is accepted only if it's the template with nothing changed except one category line and an entry ending in the attribution.

If neither the code nor the AI produces a valid body (or no AI is available, or the template can't be read from the default branch), **nothing is pushed**. The item gets Status `Conflicts` with `PR body needs a human: <reason>` in `Note`, and the backport branch stays in the clone.

## AI conflict resolution

`run` (unless `--no-ai` or `[run] ai = false`), `check`, `start --ai` and `continue --ai` hand conflicts to Claude, for backports and bumps alike (a bump's prompt asks to keep both upstream's and Altinity's changes, and offers only `RESOLVED` or `UNRESOLVED`):

- **Anthropic API**, when `ANTHROPIC_API_KEY` is set. The backporter runs the agent loop itself (`backporter/ai_api.py`, model `claude-opus-5-5`), and its tools run locally:
  - `bash` runs one command without a shell. Allowed: `git status/diff/log/show/grep/blame/ls-files/merge-base/rev-parse/cat-file/add/rm`, `git checkout --ours|--theirs`, and `ls/cat/head/tail/grep/rg/wc` on paths inside the repo.
  - `read_file`, `edit_file` and `write_file` work only inside the repo's working tree.
  - If the model declines, the API retries on its recommended fallback model (`fallbacks: "default"`).
- **`claude` CLI**, otherwise. This uses your Claude plan's limits. It runs `claude -p` in the clone dir with:
  - `--safe-mode --strict-mcp-config --setting-sources ""`: no MCP servers, plugins, hooks or user/project permission rules;
  - `--permission-mode dontAsk`: only the listed tools are allowed. These are the same git and read commands as above, plus Read/Edit/Write/Glob/Grep limited to `./**`. `git push/commit/fetch/remote/config`, `gh`, `curl`, `wget`, `ssh`, web tools and credential directories are denied explicitly.

In both modes the AI process gets no `GITHUB_TOKEN`/`GH_TOKEN`/`ANTHROPIC_API_KEY`, an empty `GH_CONFIG_DIR` (so `gh` is logged out), and `GIT_ALLOW_PROTOCOL=file` (so git can't reach any remote). The AI only edits and stages files. Committing (the conflict commit and the resolution commit), pushing, opening PRs, the summary comment and updating the project are done by the backporter under the safety rules above. Besides OUTCOME and NOTE, the AI ends its answer with a SUMMARY: 2-5 bullet points for reviewers, posted in the comment.

### AI review

When the AI reports `RESOLVED` for a backport (not for a bump), a fresh agent reviews the result before anything is committed. It gets the source PR's title, description and change (`git diff <merge commit>^1 <merge commit>`), the conflict commit, and the backport as it would be committed (`git diff <target branch>`). It checks that:
- the bug the source PR fixes, or the code it changes, exists in the target branch and isn't already fixed or changed there, fully or in part;
- the backport brings the whole change the target branch still needs, without duplicates or unrelated changes;
- the adapted code fits the target branch: the functions, types, settings and APIs it uses exist there, and its tests fit.

It reports one outcome:

| Review outcome | What happens |
|---|---|
| `VALID` | The resolution is committed as usual; the reviewer's note goes to `Note` (`; AI review: <note>`), not to the PR comment. |
| `ALREADY_PRESENT` | The resolution is discarded and the item is handled as for the resolver's `ALREADY_PRESENT`, using the PR the reviewer names. |
| `NOT_APPLICABLE` | The resolution is discarded and the item is handled as for the resolver's `NOT_APPLICABLE`. |
| `INVALID` | The reviewer's findings are added to the resolver's prompt and the resolver runs again on its previous resolution, which is still in the working tree. A fresh reviewer then checks the new result. After 3 resolutions in all (`REVIEW_ATTEMPTS` in `backporter/main.py`), a result still found wrong becomes `UNRESOLVED`. |

A review without an outcome makes the result `UNRESOLVED`.

The reviewer runs like the resolver, but read-only: with the API, its tools are `bash` without `git add/rm/checkout` and `read_file`; with the CLI, only Read/Glob/Grep and the read-only git and read commands are allowed.

## Clone dirs

`run`, `check`, `start` and `continue` (and `mark present`, for its lookup) need a local ClickHouse clone per version, with `origin` pointing to `Altinity/ClickHouse` (enforced, see Safety). One clone can serve several versions. Configure them in `~/.config/backporter/dirs.toml` (local, not committed):

```toml
"stable-25.8" = "~/src/releases/258"
"antalya-25.8" = "~/src/releases/258"
"antalya-26.8" = "~/src/antalya"
```

Or pass `-C KEY=DIR` (repeatable), which overrides the file:

```bash
backporter start -C stable-25.8=../releases/258 https://github.com/ClickHouse/ClickHouse/pull/109551 stable-25.8
```

Keys must be version keys that exist in the project. Use a clean working tree.
