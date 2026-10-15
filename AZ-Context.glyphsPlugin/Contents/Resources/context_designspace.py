"""Pure-Python designspace slicing: group masters into 1-D lines per axis.

Ported from AZ-Argus's AXIS_TO_PROCESS. No AppKit / GlyphsApp imports — the
Glyphs-side caller passes plain data so this module is unit-testable.
"""

from __future__ import annotations

SLANT_AXIS_TAGS = ("slnt", "ital")


def group_masters_by_axis(
    axes_tags: list[str],
    master_names: list[str],
    master_values: dict[str, dict[str, float]],
    master_italic: dict[str, bool],
) -> dict[str, list[list[str]]]:
    """Map each axis tag to a list of master groups.

    A group lists master names (in ``master_names`` order) that share every
    OTHER axis coordinate. When the axis is not a slant axis, roman and italic
    masters split into separate groups so each group is a clean 1-D line.
    """
    result: dict[str, list[list[str]]] = {}
    for axis in axes_tags:
        other_axes = [tag for tag in axes_tags if tag != axis]
        discriminate_italic = axis not in SLANT_AXIS_TAGS
        coord_groups: dict[tuple, list[str]] = {}
        for name in master_names:
            values = master_values[name]
            coord_key: tuple = tuple(values[tag] for tag in other_axes)
            if discriminate_italic:
                coord_key = coord_key + ("italic" if master_italic.get(name) else "roman",)
            coord_groups.setdefault(coord_key, []).append(name)
        result[axis] = list(coord_groups.values())
    return result


def group_for_master(groups: list[list[str]], master_name: str) -> list[str]:
    """Return the group containing ``master_name`` (``[master_name]`` if none)."""
    for group in groups:
        if master_name in group:
            return group
    return [master_name]


def sort_group_by_axis(
    group: list[str],
    axis: str,
    master_values: dict[str, dict[str, float]],
) -> list[str]:
    """Return ``group`` sorted ascending by each master's value on ``axis``."""
    return sorted(group, key=lambda name: master_values[name][axis])


def auto_axis(
    grouped: dict[str, list[list[str]]],
    axes_tags: list[str],
    current_master: str,
) -> str | None:
    """Pick the axis whose group containing ``current_master`` has the most
    masters. Ties break by ``axes_tags`` order. ``None`` when no axis yields
    more than one master (nothing to compare)."""
    best_axis: str | None = None
    best_count = 1
    for axis in axes_tags:
        group = group_for_master(grouped.get(axis, []), current_master)
        if len(group) > best_count:
            best_count = len(group)
            best_axis = axis
    return best_axis
