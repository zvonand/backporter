"""
Propagation rules between versions.

stable X  -> every stable >= X and every antalya >= X
antalya X -> every antalya >= X

A bump (merging an upstream release X.Y.Z) to any flavor of X.Y -> every flavor of X.Y, nothing else.
"""

from backporter.config import Version


def sort_versions(versions: list[Version]) -> list[Version]:
    """Oldest first; stable before antalya for the same version."""
    return sorted(versions, key=lambda v: (v.version, v.flavor != "stable"))


def closure(target: Version, versions: list[Version], bump: bool = False) -> list[Version]:
    """All versions a backport (or, with bump, a bump) to target implies, target included."""
    if bump:
        return sort_versions([v for v in versions if v.version == target.version])
    return sort_versions([
        v for v in versions
        if v.version >= target.version and (v.flavor == target.flavor or target.flavor == "stable")
    ])


def plan_remove(
    target: Version, other_explicit: list[Version], versions: list[Version], bump: bool = False,
) -> list[Version]:
    """
    Versions to drop when removing target, keeping whatever the remaining explicit
    requests still imply. Raises ValueError if target itself is still implied.
    """
    keep: dict[str, Version] = {}
    for e in other_explicit:
        for v in closure(e, versions, bump):
            keep.setdefault(v.key, e)
    if target.key in keep:
        raise ValueError(
            f"{target.key} is required by explicit request {keep[target.key].key}; remove that instead"
        )
    return [v for v in closure(target, versions, bump) if v.key not in keep]
