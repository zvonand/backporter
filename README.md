# backporter

Tracks and performs ClickHouse backports to our versions. All tracking data lives in a GitHub Project; the tool keeps no local state besides the list of local clone dirs.

Project: https://github.com/orgs/Altinity/projects/38

## Requesting a backport

Name only the **oldest** version that needs the change. The newer ones are added automatically:

- **stable X** also brings every newer stable and every antalya version ≥ X;
- **antalya X** also brings every newer antalya, but **not** stable. To get a stable backport too, name a stable version as well.

Version keys look like `stable-25.8` or `antalya-26.3`. Each version has its own view (tab) in the project, named by its key.

### From the project's web page (nothing to install)

1. Open the [project](https://github.com/orgs/Altinity/projects/38) and switch to the **Requests** view.
2. Click **+ Add item** at the bottom of the table.
3. Type the PR link followed by the version keys, then press Enter:

   ```
   https://github.com/ClickHouse/ClickHouse/pull/109551 stable-25.8
   ```

   You can list several keys, e.g. `... antalya-25.8 stable-26.3`. Or type only the link and pick a version in the row's **Version** column.
4. Done. The row stays in **Requests** until the next `backporter scan`. The scan replaces it with one tracked item per version, explicit plus implied, and they appear in the version views with Status `Not started`.

For `ClickHouse/ClickHouse` PRs, always paste the link as text. **Never** add the upstream PR itself as a project item (e.g. via **Add item from repository**), because that can leave events on the upstream PR. `scan` skips such items and doesn't touch them, so remove them by hand. An `Altinity/ClickHouse` PR may be added as the item itself.

A request is skipped, and stays in **Requests**, if it has no PR link, no version, or a version key the project doesn't have. `scan` prints the reason. Fix the row and it's picked up next time.

The PR link can also go in the **Source PR** field or in the item body, and version keys can also go in the body. All of them are used.

### From the command line

```bash
backporter add https://github.com/ClickHouse/ClickHouse/pull/109551 stable-25.8
```

This creates the items immediately. To undo it, run `backporter remove <PR> <version>` (see [`remove`](#remove)).

### What happens next

- `backporter start <PR>` does the backport into each `Not started` version: it cherry-picks, pushes, and opens a labeled PR in `Altinity/ClickHouse`. With `--ai`, Claude resolves conflicts.
- Each item's **Status**, **Backport PR** and **Note** show where it stands. Possible states are `Not started`, `Conflicts`, `PR open`, `Merged`, `Closed`, `Already present` and `Not applicable`, and `Note` explains the last two.
- `backporter scan` refreshes the statuses from GitHub and lists what's still pending.

## Installation

Requires Python 3.10+ and Git. For `--ai`: Claude Code (`claude` on PATH), or an `ANTHROPIC_API_KEY`.

Editable install from the repo:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

Or build a wheel and install it anywhere. `backporter/config.toml` is packaged into the wheel, so run `init` (which writes the project link into it) from the repo before building:

```bash
python3 -m build            # or: uv build
pip install dist/backporter-*.whl
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
- `[labels]`: labels added to every backport PR the backporter opens.
  - `all` applies to every PR.
  - A flavor (`stable`, `antalya`) adds labels for that flavor.
  - A version key (e.g. `"antalya-25.8"`) adds labels for that version.

  ```toml
  [labels]
  all = ["backport"]
  antalya = ["antalya"]
  "antalya-25.8" = ["antalya-25.8"]
  ```

  With this config, a backport into `antalya-25.8` gets `backport`, `antalya` and `antalya-25.8`, and one into `stable-25.8` gets only `backport`. Labels must already exist in `Altinity/ClickHouse`; the backporter never creates them. `start` and `continue` check this before any git work and stop on a missing label.

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

Source PRs may come from `ClickHouse/ClickHouse`, but the backporter only ever reads from it. It never writes anything there: no PRs, comments, reviews, labels, edits or issues. It also never comments anywhere, including in Altinity repos.

The target repo is hardcoded (`TARGET_REPO` in `backporter/config.py`) and is not configurable.

**GitHub API allowlist** (`backporter/github_guard.py`): every request PyGithub makes, including redirects, is checked before it is sent. Allowed:
- reads (`GET`/`HEAD`);
- `POST /repos/Altinity/ClickHouse/pulls` (opening a backport PR);
- `POST /repos/Altinity/ClickHouse/issues/<n>/labels` (adding the configured labels to it; labels are never removed or created);
- a GraphQL mutation only if it is a single operation whose top-level fields are all project mutations: `createProjectV2Field`, `updateProjectV2Field`, `createProjectV2View`, `updateProjectV2View`, `addProjectV2DraftIssue`, `updateProjectV2ItemFieldValue`, `deleteProjectV2Item`.

Anything else is refused with an error. If the installed PyGithub doesn't have the request hook the guard needs, the tool refuses to start.

**Git:**

- **Before any git work and before every push:** every push URL of `origin` in the clone dir must point to `github.com/Altinity/ClickHouse`. The URLs are read with `git remote get-url --push --all origin`, so `insteadOf`/`pushInsteadOf` rewrites are applied. Otherwise the command refuses. `start` checks all the clone dirs it will use before it starts.
- **Pushes:** only to `origin`, with an explicit `refs/heads/<branch>:refs/heads/<branch>` refspec. The `upstream` remote (the source PR's repo) is only fetched from.
- **PRs:** opened only via the API in `Altinity/ClickHouse`, after checking that the resolved repo name is exactly that. A rename or redirect is refused.

## The GitHub Project

### Versions

Each version has a key `<flavor>-X.Y`, e.g. `stable-25.8` or `antalya-26.3`. The list of versions is stored in the project, as the options of its `Version` field. Each version's branch in `Altinity/ClickHouse` comes from `[branches]` in the config.

Propagation rules:

- A backport to **stable X** also requires every stable version ≥ X **and** every antalya version ≥ X.
- A backport to **antalya X** also requires every antalya version ≥ X.

Example: with stable 25.3, 25.8, 26.3, 26.8 and antalya 25.8, 26.3, 26.6, 26.8, `stable-25.3` implies all the others, and `antalya-26.3` implies `antalya-26.6` and `antalya-26.8`.

### Items

Each item is a draft issue for one (source PR, version) pair. It is titled `[<version key>] <owner/repo>#<N> <source PR title>`, its body is the source PR URL, and it has these fields:

| Field | Meaning |
|-------|---------|
| Source PR | URL of the PR being backported |
| Version | Version key, e.g. `stable-25.8` |
| Flavor | `stable` / `antalya` |
| Requested | `explicit` (named in `add`) or `implied` (required by the propagation rules) |
| Status | `Not started`, `Conflicts`, `PR open`, `Merged`, `Closed`, `Already present`, `Not applicable` |
| Backport PR | URL of the backport PR in `Altinity/ClickHouse`; for `Already present`, the PR that already brought the change (Altinity or ClickHouse) |
| Note | One line explaining the outcome: why it is not applicable, which PR already brought it, what the AI adapted or why it gave up |

Draft items can't have comments, so the explanation goes in `Note`. Notes and found PRs are written only to the project, never anywhere else.

### Views

- One table view per version, named by its key and filtered with `version:"<key>"`. For example, open the `stable-25.8` view to see everything going into stable 25.8.
- `Requests`: requests added in the web page that `scan` hasn't processed yet (filter `no:requested`). See [Requesting a backport](#requesting-a-backport).
- `All`: a board of everything.

The project is the source of truth. Items can also be edited in the GitHub UI, e.g. to set a Status by hand.

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
   backporter add-version 25.3 --flavor stable
   backporter add-version 25.8 --flavor both
   backporter add-version 26.3 --flavor both
   backporter add-version 26.6 --flavor antalya
   backporter add-version 26.8 --flavor both
   ```

## Usage

`--token` and `-C` go after the subcommand, e.g. `backporter start -C ... <PR-URL>`. `<PR-URL>` is a merged or unmerged PR in any repo, typically `ClickHouse/ClickHouse` or `Altinity/ClickHouse`.

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
backporter add <PR-URL> <version>
```

Creates items for `<version>` (Requested `explicit`) and every version it implies (`implied`), all with Status `Not started`.
- Items that already exist are left as they are, except that an `implied` item for `<version>` becomes `explicit`.
- Re-running is safe and fills in any missing implied items.

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

1. Processes requests added in the web page (see [Requesting a backport](#requesting-a-backport)): each requested version is handled like `add`, then the request row is deleted.
2. Refreshes every item that is not `Merged`, `Closed`, `Already present` or `Not applicable`. It finds the backport PR from the item's `Backport PR` field, or else from a PR in `Altinity/ClickHouse` with head `backports/<branch>/<N>`, so backports opened by hand are picked up. It then sets `Backport PR` and Status (`PR open` / `Merged` / `Closed`).
3. Lists items still `Not started` or in `Conflicts`, grouped by version.
4. Reports PRs that are missing versions implied by their explicit requests.

`--version` limits steps 2 and 3 to one version.

### `start`

```bash
backporter start <PR-URL> [<version> ...] [--ai]
```

Backports a merged PR. Without versions it takes all of the PR's items with Status `Not started`; with versions it takes those (they must already be added, in any status). Every version needs a clone dir (see below). The versions are processed oldest first, and for each one, in its clone dir, it:

1. Fetches and checks out the target branch and pulls it.
2. Creates `backports/<branch>/<N>`.
3. Cherry-picks the PR's merge commit with `-m 1`. If the PR is not from `Altinity/ClickHouse`, the commit is fetched from an `upstream` remote, which is added or updated automatically.
4. Pushes the branch to `origin`.
5. Opens a PR into the target branch titled `<Stable|Antalya> X.Y Backport of #N - <title>`. Its body links the source PR and carries the Changelog category and entry from the source PR, with the source link and author appended to the entry. If an open PR already exists for the branch, it is reused. The PR gets the labels configured for its version (see Configuration).
6. Sets `Backport PR` and Status `PR open`.

**Already present.** If the cherry-pick turns out empty, the change is already in the target branch. The tool then:
1. Drops the local backport branch.
2. Sets Status `Already present`.
3. Looks for the PR that brought the change, and records it in `Backport PR` and `Note`:
   - If the source PR's merge commit is already an ancestor of the branch, it's the source PR itself.
   - Otherwise it checks the newest commits on the branch whose message mentions `#<N>`, the source PR's title, or its merge commit. It records the first one that belongs to a merged PR in Altinity/ClickHouse, or else in the source PR's repo.

It then continues with the next version.

**Conflicts without `--ai`.** It sets Status `Conflicts`, prints the conflicted files, leaves the branch with conflicts in the clone dir, and stops (exit code 1). Versions after it are not started.

**Conflicts with `--ai`.** The AI resolves them in the clone dir (see [AI conflict resolution](#ai-conflict-resolution)) and reports one outcome:

| AI outcome | What the backporter does |
|---|---|
| `RESOLVED` | Checks that it holds: HEAD hasn't moved, the cherry-pick is still in progress, nothing is unmerged and no conflict markers are left. Then it continues with steps 4–6 and writes `AI resolved conflicts: <note>` to `Note`. If the resolution leaves no changes, the item is marked `Already present` as above. |
| `ALREADY_PRESENT` | Drops the branch and marks `Already present`, using the PR the AI names or, failing that, the lookup above. |
| `NOT_APPLICABLE` | Drops the branch and marks `Not applicable` with the AI's reason in `Note`. |
| `UNRESOLVED` | Sets `Conflicts` with the AI's reason in `Note`, leaves the conflicts for a human, and stops. |

If the backport branch already exists locally, or the clone is in a conflicted/dirty state, it asks before recreating the branch from a clean target. Answering no aborts.

### `continue`

```bash
backporter continue <PR-URL> <version> [--ai]
```

Use after resolving the conflicts left by `start`. The clone dir must be on `backports/<branch>/<N>` with the cherry-pick in progress. It runs `git add .` and `git cherry-pick --continue`, then does steps 4–6 of `start`.
- If the resolution leaves no changes, the item is marked `Already present` instead.
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

Prints the Changelog category and entry from the PR body. It does not use the project.

## AI conflict resolution

`start --ai` and `continue --ai` hand conflicts to Claude:

- **Anthropic API**, when `ANTHROPIC_API_KEY` is set. The backporter runs the agent loop itself (`backporter/ai_api.py`, model `claude-opus-5-5`), and its tools run locally:
  - `bash` runs one command without a shell. Allowed: `git status/diff/log/show/grep/blame/ls-files/merge-base/rev-parse/cat-file/add/rm`, `git checkout --ours|--theirs`, and `ls/cat/head/tail/grep/rg/wc` on paths inside the repo.
  - `read_file`, `edit_file` and `write_file` work only inside the repo's working tree.
  - If the model declines, the API retries on its recommended fallback model (`fallbacks: "default"`).
- **`claude` CLI**, otherwise. This uses your Claude plan's limits. It runs `claude -p` in the clone dir with:
  - `--safe-mode --strict-mcp-config --setting-sources ""`: no MCP servers, plugins, hooks or user/project permission rules;
  - `--permission-mode dontAsk`: only the listed tools are allowed. These are the same git and read commands as above, plus Read/Edit/Write/Glob/Grep limited to `./**`. `git push/commit/fetch/remote/config`, `gh`, `curl`, `wget`, `ssh`, web tools and credential directories are denied explicitly.

In both modes the AI process gets no `GITHUB_TOKEN`/`GH_TOKEN`/`ANTHROPIC_API_KEY`, an empty `GH_CONFIG_DIR` (so `gh` is logged out), and `GIT_ALLOW_PROTOCOL=file` (so git can't reach any remote). The AI only edits and stages files. Committing, pushing, opening PRs and updating the project are done by the backporter under the safety rules above.

## Clone dirs

`start` and `continue` need a local ClickHouse clone per version, with `origin` pointing to `Altinity/ClickHouse` (enforced, see Safety). One clone can serve several versions. Configure them in `~/.config/backporter/dirs.toml` (local, not committed):

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
