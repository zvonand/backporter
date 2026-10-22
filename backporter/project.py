"""
GitHub Projects v2 storage for backport items (GraphQL via PyGithub's requester).

Each tracked item is a draft issue for one (source PR, version) pair.
Items without a Requested value are requests added by hand in the web UI.
Versions are the options of the Version field; their branches come from the config.
"""

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

NOT_STARTED = "Not started"
CONFLICTS = "Conflicts"
PR_OPEN = "PR open"
MERGED = "Merged"
CLOSED = "Closed"
PRESENT = "Already present"
NOT_APPLICABLE = "Not applicable"

EXPLICIT = "explicit"
IMPLIED = "implied"

STATUS_COLORS = {
    NOT_STARTED: "GRAY", CONFLICTS: "RED", PR_OPEN: "YELLOW", MERGED: "GREEN", CLOSED: "PURPLE",
    PRESENT: "GREEN", NOT_APPLICABLE: "GRAY",
}
FLAVOR_COLORS = {"stable": "BLUE", "antalya": "ORANGE"}
REQUESTED_COLORS = {EXPLICIT: "GREEN", IMPLIED: "GRAY"}

_OWN_FIELDS = {F_SOURCE_PR, F_VERSION, F_FLAVOR, F_REQUESTED, F_STATUS, F_BACKPORT_PR, F_NOTE}
_CUSTOM_FIELD_TYPES = {"TEXT", "SINGLE_SELECT", "MULTI_SELECT", "NUMBER", "DATE", "ITERATION"}

_FIELDS_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on ProjectV2 {
      fields(first: 50) {
        nodes {
          ... on ProjectV2FieldCommon { id name dataType }
          ... on ProjectV2SingleSelectField { options { id name color description } }
        }
      }
      views(first: 50) { nodes { id name filter } }
      items(first: 1) { totalCount }
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


def _version_filter(key: str) -> str:
    return f'version:"{key}"'


class Project:
    def __init__(self, requester: Requester, project_id: str):
        self.requester = requester
        self.id = project_id
        self.fields: dict[str, dict] = {}
        self.views: dict[str, dict] = {}
        self.item_count = 0
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
        return cls(requester, project["id"])

    def _load_fields(self) -> None:
        _, data = self.requester.graphql_query(_FIELDS_QUERY, {"id": self.id})
        node = data["data"]["node"]
        self.fields = {f["name"]: f for f in node["fields"]["nodes"] if f}
        self.views = {v["name"]: v for v in node["views"]["nodes"]}
        self.item_count = node["items"]["totalCount"]

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
            if self.item_count or foreign:
                raise RuntimeError(
                    f"Project is not empty ({self.item_count} items, custom fields: {', '.join(foreign) or 'none'}) "
                    "and is not a backporter project; refusing to change it"
                )
        # Source PR goes first: its presence marks the project as formatted.
        for name in (F_SOURCE_PR, F_BACKPORT_PR, F_NOTE):
            if name not in self.fields:
                self._create_field(name, "TEXT")
        self._ensure_single_select(F_FLAVOR, FLAVOR_COLORS, exact=False)
        self._ensure_single_select(F_REQUESTED, REQUESTED_COLORS, exact=False)
        # The built-in Status field comes with Todo/In Progress/Done; replace them with ours.
        self._ensure_single_select(F_STATUS, STATUS_COLORS, exact=True)
        self._load_fields()
        if "All" not in self.views:
            self._create_view("All", "BOARD_LAYOUT", None)
        if "Requests" not in self.views:
            self._create_view("Requests", "TABLE_LAYOUT", "no:requested")
        for key in self.version_keys():
            if key not in self.views:
                self._create_view(key, "TABLE_LAYOUT", _version_filter(key))

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
            self._create_view(version.key, "TABLE_LAYOUT", _version_filter(version.key))
        self._load_fields()

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
                ))
            if not page["pageInfo"]["hasNextPage"]:
                return result
            after = page["pageInfo"]["endCursor"]

    def add_item(self, title: str, body: str, values: dict[str, str]) -> None:
        _, out = self.requester.graphql_named_mutation(
            "addProjectV2DraftIssue", {"projectId": self.id, "title": title, "body": body}, "projectItem { id }"
        )
        item_id = out["projectItem"]["id"]
        for name, value in values.items():
            self.set_value(item_id, name, value)

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

    def delete_item(self, item_id: str) -> None:
        self.requester.graphql_named_mutation(
            "deleteProjectV2Item", {"projectId": self.id, "itemId": item_id}, "deletedItemId"
        )
