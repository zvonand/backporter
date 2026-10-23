"""
GitHub Projects v2 storage for backport items (GraphQL via PyGithub's requester).

Each tracked item is a draft issue for one (source PR, version) pair.
Items without a Requested value are requests added by hand in the web UI.
Versions are the options of the Version field; their branches come from the config.
"""

import sys
import time
from dataclasses import dataclass

from github.Requester import Requester

from backporter.config import Version

F_SOURCE_PR = "Source PR"
F_VERSION = "Version"
F_FLAVOR = "Flavor"
F_REQUESTED = "Requested"
F_STATUS = "Status"
F_BACKPORT_PR = "Backport PR"
F_NOTE = "Note"
F_DECISION = "Decision"
F_REQUESTED_BY = "Requested by"
# Version number (e.g. "26.3") from which a backport is not needed, set in a request; empty = no limit.
F_NOT_NEEDED_FROM = "Not needed from"

NOT_STARTED = "Not started"
CONFLICTS = "Conflicts"
PR_OPEN = "PR open"
MERGED = "Merged"
CLOSED = "Closed"
PRESENT = "Already present"
NOT_APPLICABLE = "Not applicable"

# Decision is set by people in the web page; empty means undecided.
APPROVED = "Approved"
DISCARDED = "Discarded"

EXPLICIT = "explicit"
IMPLIED = "implied"

STATUS_COLORS = {
    NOT_STARTED: "GRAY", CONFLICTS: "RED", PR_OPEN: "YELLOW", MERGED: "GREEN", CLOSED: "PURPLE",
    PRESENT: "GREEN", NOT_APPLICABLE: "GRAY",
}
# Statuses the backporter never changes again.
FINAL_STATUSES = (MERGED, CLOSED, PRESENT, NOT_APPLICABLE)
DECISION_COLORS = {APPROVED: "BLUE", DISCARDED: "GRAY"}

# Columns (cards, for boards) the backporter makes visible; views created via the API show only defaults.
# Column order can't be set via the API: it follows field creation order.
# Backport PR isn't shown: once it exists, the row is that PR (its Title links to it). Not needed from is set in
# requests only; rows it excludes are Not applicable with the reason in Note.
_TABLE_FIELDS = [F_DECISION, F_STATUS, F_VERSION, F_REQUESTED, F_REQUESTED_BY, F_NOTE]
_REQUESTS_FIELDS = [F_VERSION, F_NOT_NEEDED_FROM, F_NOTE]
_BOARD_FIELDS = [F_VERSION, F_DECISION]
REQUESTS_VIEW = "Requests"
# Built-in columns that are always empty for draft items (in Requests also Status, and Source PR: the row is the PR).
_HIDDEN_FIELDS = {"Assignees", "Linked pull requests", "Sub-issues progress"}
FLAVOR_COLORS = {"stable": "BLUE", "antalya": "ORANGE"}
REQUESTED_COLORS = {EXPLICIT: "GREEN", IMPLIED: "GRAY"}

_OWN_FIELDS = {
    F_SOURCE_PR, F_VERSION, F_FLAVOR, F_REQUESTED, F_STATUS, F_BACKPORT_PR, F_NOTE, F_DECISION, F_REQUESTED_BY,
    F_NOT_NEEDED_FROM,
}
_CUSTOM_FIELD_TYPES = {"TEXT", "SINGLE_SELECT", "MULTI_SELECT", "NUMBER", "DATE", "ITERATION"}

# GitHub sometimes answers project queries with an empty result (seen with totalCount 0 for a project
# with items), so empty answers are re-read a few times before being trusted.
_READ_ATTEMPTS = 5
_RETRY_SECONDS = 3

_FIELDS_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on ProjectV2 {
      fields(first: 50) {
        nodes {
          ... on ProjectV2FieldCommon { id databaseId name dataType }
          ... on ProjectV2SingleSelectField { options { id name color description } }
        }
      }
      views(first: 50) {
        nodes { id number name filter layout fields(first: 50) { nodes { ... on ProjectV2FieldCommon { id name } } } }
      }
    }
  }
}
"""

_ITEMS_QUERY = """
query($id: ID!, $after: String) {
  node(id: $id) {
    ... on ProjectV2 {
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          creator { login }
          content {
            ... on DraftIssue { title body }
            ... on PullRequest { title url }
          }
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
            }
          }
        }
      }
    }
  }
}
"""


@dataclass
class Item:
    id: str
    title: str
    body: str
    content_url: str
    source_pr: str
    version: str
    flavor: str
    requested: str
    status: str
    backport_pr: str
    note: str
    decision: str
    requested_by: str
    not_needed_from: str
    # Who added the item to the project
    creator: str


# Tracked rows only: unprocessed requests (no Requested value yet) belong in the Requests view.
TRACKED_FILTER = "has:requested"


def _version_filter(key: str) -> str:
    return f'version:"{key}" {TRACKED_FILTER}'


class Project:
    def __init__(self, requester: Requester, project_id: str, owner: str, number: int):
        self.requester = requester
        self.id = project_id
        self.owner = owner
        self.number = number
        self.fields: dict[str, dict] = {}
        self.views: dict[str, dict] = {}
        self._load_fields()

    @classmethod
    def open(cls, requester: Requester, owner: str, number: int) -> "Project":
        _, data = requester.graphql_query(
            """query($login: String!, $number: Int!) {
                 repositoryOwner(login: $login) { ... on ProjectV2Owner { projectV2(number: $number) { id } } }
               }""",
            {"login": owner, "number": number},
        )
        project = (data["data"]["repositoryOwner"] or {}).get("projectV2")
        if not project:
            raise RuntimeError(f"Project {owner}#{number} not found")
        return cls(requester, project["id"], owner, number)

    def _load_fields(self) -> None:
        for attempt in range(_READ_ATTEMPTS):
            _, data = self.requester.graphql_query(_FIELDS_QUERY, {"id": self.id})
            node = data["data"]["node"]
            # Every project has a Title field and at least one view.
            if any(f and f.get("name") == "Title" for f in node["fields"]["nodes"]) and node["views"]["nodes"]:
                break
            print("GitHub returned an incomplete project schema; retrying...", file=sys.stderr)
            time.sleep(_RETRY_SECONDS)
        else:
            raise RuntimeError("GitHub keeps returning an incomplete project schema; try again later")
        self.fields = {f["name"]: f for f in node["fields"]["nodes"] if f}
        self.views = {v["name"]: v for v in node["views"]["nodes"]}

    # --- schema ---

    def ensure_schema(self) -> None:
        """
        Format the project for the backporter: an empty project gets all fields and views,
        an already formatted one gets whatever is missing. Any other project is refused.
        """
        if F_SOURCE_PR not in self.fields:
            foreign = [
                name for name, f in self.fields.items()
                if f["dataType"] in _CUSTOM_FIELD_TYPES and name not in _OWN_FIELDS
            ]
            item_count = len(self.items())
            if item_count or foreign:
                raise RuntimeError(
                    f"Project is not empty ({item_count} items, custom fields: {', '.join(foreign) or 'none'}) "
                    "and is not a backporter project; refusing to change it"
                )
        # Columns follow field creation order, so the fields people use come first. Version needs at least
        # one option, so the first add-version creates it. Source PR goes last: it marks the project as formatted.
        self._ensure_single_select(F_DECISION, DECISION_COLORS, exact=False)
        for name in (F_BACKPORT_PR, F_NOTE):
            if name not in self.fields:
                self._create_field(name, "TEXT")
        self._ensure_single_select(F_REQUESTED, REQUESTED_COLORS, exact=False)
        if F_REQUESTED_BY not in self.fields:
            self._create_field(F_REQUESTED_BY, "TEXT")
        self._ensure_single_select(F_FLAVOR, FLAVOR_COLORS, exact=False)
        if F_SOURCE_PR not in self.fields:
            self._create_field(F_SOURCE_PR, "TEXT")
        # The built-in Status field comes with Todo/In Progress/Done; replace them with ours.
        self._ensure_single_select(F_STATUS, STATUS_COLORS, exact=True)
        self._load_fields()
        self._ensure_requests_first()
        if "All" not in self.views:
            self._create_view("All", "BOARD_LAYOUT", TRACKED_FILTER)
        for key in self.version_keys():
            if key not in self.views:
                self._create_version_view(key)
        self._fix_old_filters()
        self._ensure_version_numbers()
        self._load_fields()
        self._show_fields()

    def is_formatted(self) -> bool:
        return F_SOURCE_PR in self.fields

    def version_keys(self) -> list[str]:
        field = self.fields.get(F_VERSION)
        return [o["name"] for o in field["options"]] if field else []

    def add_version(self, version: Version) -> None:
        """Add a Version option and its view."""
        option = {"name": version.key, "color": FLAVOR_COLORS[version.flavor], "description": ""}
        field = self.fields.get(F_VERSION)
        if field is None:
            self._create_field(F_VERSION, "SINGLE_SELECT", [option])
        else:
            self._update_options(field, [*field["options"], option])
        if version.key not in self.views:
            self._create_version_view(version.key)
        self._load_fields()
        self._ensure_version_numbers()
        self._load_fields()
        self._show_fields()

    def _ensure_version_numbers(self) -> None:
        """'Not needed from' offers every version number in the project; it needs at least one option."""
        numbers = sorted({key.split("-", 1)[1] for key in self.version_keys()}, key=lambda n: tuple(map(int, n.split("."))))
        if numbers:
            self._ensure_single_select(F_NOT_NEEDED_FROM, {n: "GRAY" for n in numbers}, exact=False)

    def _create_field(self, name: str, data_type: str, options: list[dict] | None = None) -> None:
        payload = {"projectId": self.id, "dataType": data_type, "name": name}
        if options:
            payload["singleSelectOptions"] = options
        self.requester.graphql_named_mutation("createProjectV2Field", payload, "clientMutationId")
        print(f"Created field '{name}'")

    def _update_options(self, field: dict, options: list[dict]) -> None:
        """Replace a single-select field's options; options carrying an existing id keep their item values."""
        self.requester.graphql_named_mutation(
            "updateProjectV2Field", {"fieldId": field["id"], "singleSelectOptions": options}, "clientMutationId"
        )
        print(f"Updated options of field '{field['name']}'")

    def _ensure_single_select(self, name: str, colors: dict[str, str], exact: bool) -> None:
        """Make the field have the options named in colors; with exact, drop any other options."""
        wanted = [{"name": n, "color": c, "description": ""} for n, c in colors.items()]
        field = self.fields.get(name)
        if field is None:
            self._create_field(name, "SINGLE_SELECT", wanted)
            return
        existing = {o["name"]: o for o in field["options"]}
        if exact and list(existing) == list(colors):
            return
        if not exact and all(n in existing for n in colors):
            return
        if exact:
            options = [existing.get(o["name"], o) for o in wanted]
        else:
            options = [*field["options"], *(o for o in wanted if o["name"] not in existing)]
        self._update_options(field, options)

    def _ensure_requests_first(self) -> None:
        """
        Make Requests the first view, which is the one a project opens with. Views can't be reordered via the
        API, so GitHub's untouched default first view ("View 1") is turned into Requests.
        """
        first = min(self.views.values(), key=lambda v: v["number"])
        if first["name"] == REQUESTS_VIEW:
            return
        if first["name"] == "View 1" and not first["filter"]:
            if REQUESTS_VIEW in self.views:
                self.requester.graphql_named_mutation(
                    "deleteProjectV2View", {"viewId": self.views[REQUESTS_VIEW]["id"]}, "clientMutationId"
                )
            columns = [self.fields[n]["id"] for n in ("Title", *_REQUESTS_FIELDS) if n in self.fields]
            self.requester.graphql_named_mutation(
                "updateProjectV2View",
                {
                    "viewId": first["id"], "name": REQUESTS_VIEW, "layout": "TABLE_LAYOUT", "filter": "no:requested",
                    "configuration": {"visibleFieldIds": columns},
                },
                "clientMutationId",
            )
            print(f"Turned the first view into '{REQUESTS_VIEW}'")
        else:
            if REQUESTS_VIEW not in self.views:
                self._create_view(REQUESTS_VIEW, "TABLE_LAYOUT", "no:requested")
            print(f"The first view is '{first['name']}'; drag the '{REQUESTS_VIEW}' tab to the front to open with it")
        self._load_fields()

    def _show_fields(self) -> None:
        """Show the backporter's columns in every view and hide built-in ones that are empty for draft items."""
        for view in self.views.values():
            if view["name"] == REQUESTS_VIEW:
                wanted = _REQUESTS_FIELDS
            elif view["layout"] == "BOARD_LAYOUT":
                wanted = _BOARD_FIELDS
            else:
                wanted = _TABLE_FIELDS
            if view["name"] == REQUESTS_VIEW:
                hidden = _HIDDEN_FIELDS | {F_STATUS, F_SOURCE_PR}
            elif view["layout"] == "TABLE_LAYOUT":
                hidden = _HIDDEN_FIELDS | {F_BACKPORT_PR, F_NOT_NEEDED_FROM}
            else:
                hidden = _HIDDEN_FIELDS
            shown = [f for f in view["fields"]["nodes"] if f]
            visible = [f["id"] for f in shown if f["name"] not in hidden]
            missing = [self.fields[n]["id"] for n in wanted if n in self.fields and self.fields[n]["id"] not in visible]
            if missing or len(visible) != len(shown):
                self.requester.graphql_named_mutation(
                    "updateProjectV2View",
                    {"viewId": view["id"], "configuration": {"visibleFieldIds": visible + missing}},
                    "clientMutationId",
                )
                print(f"Updated columns of view '{view['name']}'")

    def _create_version_view(self, key: str) -> None:
        """
        A table view of one version, grouped by Status. Grouping can only be set when a view is created,
        and only via the REST API (GraphQL has no group-by input).
        """
        columns = ["Title", *_TABLE_FIELDS]
        self.requester.requestJsonAndCheck(
            "POST",
            f"/orgs/{self.owner}/projectsV2/{self.number}/views",
            headers={"X-GitHub-Api-Version": "2026-03-10"},
            input={
                "name": key,
                "layout": "table",
                "filter": _version_filter(key),
                "visible_fields": [self.fields[n]["databaseId"] for n in columns if n in self.fields],
                "group_by": [self.fields[F_STATUS]["databaseId"]],
            },
        )
        print(f"Created view '{key}' (grouped by Status)")

    def _fix_old_filters(self) -> None:
        """Views created before TRACKED_FILTER also showed unprocessed requests; add it to their default filter."""
        self._load_fields()
        old = {key: f'version:"{key}"' for key in self.version_keys()} | {"All": ""}
        for name, filter_ in old.items():
            view = self.views.get(name)
            if view and (view["filter"] or "") == filter_:
                new = _version_filter(name) if name != "All" else TRACKED_FILTER
                self.requester.graphql_named_mutation(
                    "updateProjectV2View", {"viewId": view["id"], "filter": new}, "clientMutationId"
                )
                print(f"Updated the filter of view '{name}' to '{new}'")

    def _create_view(self, name: str, layout: str, filter_: str | None) -> None:
        _, out = self.requester.graphql_named_mutation(
            "createProjectV2View", {"projectId": self.id, "name": name, "layout": layout}, "projectV2View { id }"
        )
        if filter_:
            self.requester.graphql_named_mutation(
                "updateProjectV2View", {"viewId": out["projectV2View"]["id"], "filter": filter_}, "clientMutationId"
            )
        print(f"Created view '{name}'")

    # --- items ---

    def items(self) -> list[Item]:
        for attempt in range(_READ_ATTEMPTS):
            result = self._read_items()
            if result:
                return result
            if attempt < _READ_ATTEMPTS - 1:
                print("GitHub returned no project items; re-checking...", file=sys.stderr)
                time.sleep(_RETRY_SECONDS)
        return []

    def _read_items(self) -> list[Item]:
        result: list[Item] = []
        after = None
        while True:
            _, data = self.requester.graphql_query(_ITEMS_QUERY, {"id": self.id, "after": after})
            page = data["data"]["node"]["items"]
            for node in page["nodes"]:
                values = {}
                for fv in node["fieldValues"]["nodes"]:
                    if fv and fv.get("field"):
                        values[fv["field"]["name"]] = fv.get("text", fv.get("name"))
                content = node.get("content") or {}
                result.append(Item(
                    id=node["id"],
                    title=content.get("title", ""),
                    body=content.get("body", ""),
                    content_url=content.get("url", ""),
                    source_pr=values.get(F_SOURCE_PR, ""),
                    version=values.get(F_VERSION, ""),
                    flavor=values.get(F_FLAVOR, ""),
                    requested=values.get(F_REQUESTED, ""),
                    status=values.get(F_STATUS, ""),
                    backport_pr=values.get(F_BACKPORT_PR, ""),
                    note=values.get(F_NOTE, ""),
                    decision=values.get(F_DECISION, ""),
                    requested_by=values.get(F_REQUESTED_BY, ""),
                    not_needed_from=values.get(F_NOT_NEEDED_FROM, ""),
                    creator=(node.get("creator") or {}).get("login", ""),
                ))
            if not page["pageInfo"]["hasNextPage"]:
                return result
            after = page["pageInfo"]["endCursor"]

    def add_item(self, title: str, body: str, values: dict[str, str]) -> str:
        """Add a draft item with field values; returns its id."""
        _, out = self.requester.graphql_named_mutation(
            "addProjectV2DraftIssue", {"projectId": self.id, "title": title, "body": body}, "projectItem { id }"
        )
        item_id = out["projectItem"]["id"]
        for name, value in values.items():
            self.set_value(item_id, name, value)
        return item_id

    def clear_value(self, item_id: str, field_name: str) -> None:
        self.requester.graphql_named_mutation(
            "clearProjectV2ItemFieldValue",
            {"projectId": self.id, "itemId": item_id, "fieldId": self.fields[field_name]["id"]},
            "clientMutationId",
        )

    def set_value(self, item_id: str, field_name: str, value: str) -> None:
        field = self.fields.get(field_name)
        if field is None:
            raise RuntimeError(f"Project has no field '{field_name}'; run `backporter init <project-URL>`")
        if field["dataType"] == "SINGLE_SELECT":
            option = next((o for o in field["options"] if o["name"] == value), None)
            if option is None:
                raise RuntimeError(f"Field '{field_name}' has no option '{value}'")
            v = {"singleSelectOptionId": option["id"]}
        else:
            v = {"text": value}
        self.requester.graphql_named_mutation(
            "updateProjectV2ItemFieldValue",
            {"projectId": self.id, "itemId": item_id, "fieldId": field["id"], "value": v},
            "clientMutationId",
        )

    def add_existing_item(self, content_id: str) -> str:
        """Add an existing issue/PR (by node id) to the project; returns the item id."""
        _, out = self.requester.graphql_named_mutation(
            "addProjectV2ItemById", {"projectId": self.id, "contentId": content_id}, "item { id }"
        )
        return out["item"]["id"]

    def delete_item(self, item_id: str) -> None:
        self.requester.graphql_named_mutation(
            "deleteProjectV2Item", {"projectId": self.id, "itemId": item_id}, "deletedItemId"
        )
