"""Read-only InvenTree tools: plain functions over an InvenTreeClient.

No MCP import lives here. Every function sends GET requests only and returns a
JSON-serialisable dict. Failures raise InvenTreeError with the client's stable
codes (a 404 is `not_found`, a 401 or 403 is `unauthorized`).

Limits are clamped to MIN_LIMIT..MAX_LIMIT. A clamp, or a list cut short by the
limit, is reported in `notes` and by `truncated`. Nothing is truncated silently.

VERIFY-LIVE marks a query parameter name the plan does not spell out. It was not
confirmed against the pinned instance's /api-doc/.
"""

from __future__ import annotations

from typing import Any, Final

from meridian_inventree.client import InvenTreeClient, InvenTreeError

__all__ = [
    "MAX_LIMIT",
    "MIN_LIMIT",
    "get_part",
    "get_stock_item",
    "list_categories",
    "list_locations",
    "roles",
    "search_parts",
    "search_stock",
]

MIN_LIMIT: Final[int] = 1
MAX_LIMIT: Final[int] = 200
SEARCH_LIMIT_DEFAULT: Final[int] = 50
TREE_LIMIT_DEFAULT: Final[int] = 200

PART_PATH: Final[str] = "/api/part/"
CATEGORY_PATH: Final[str] = "/api/part/category/"
STOCK_PATH: Final[str] = "/api/stock/"
LOCATION_PATH: Final[str] = "/api/stock/location/"

# A primary key as the caller gives it: a positive int, or a string of ASCII digits.
Pk = int | str


def search_parts(
    client: InvenTreeClient,
    query: str = "",
    category: Pk | None = None,
    limit: int = SEARCH_LIMIT_DEFAULT,
) -> dict[str, Any]:
    """Search parts by text, optionally inside one category."""
    applied, notes = _limit(limit)
    items, truncated = client.paginate_with_flag(
        PART_PATH,
        params={"search": _search_text(query), "category": _optional_pk(category, "category")},
        limit=applied,
    )
    return _page_result(items, truncated, applied, notes)


def get_part(client: InvenTreeClient, pk: Pk) -> dict[str, Any]:
    """Return one part by primary key."""
    return _object(client.get(f"{PART_PATH}{_pk(pk, 'pk')}/"), "part")


def list_categories(
    client: InvenTreeClient,
    parent: Pk | None = None,
    limit: int = TREE_LIMIT_DEFAULT,
) -> dict[str, Any]:
    """List part categories, or only the children of `parent` when it is given."""
    applied, notes = _limit(limit)
    # VERIFY-LIVE: confirm the `parent` filter name on GET /api/part/category/ against /api-doc/.
    items, truncated = client.paginate_with_flag(
        CATEGORY_PATH,
        params={"parent": _optional_pk(parent, "parent")},
        limit=applied,
    )
    return _page_result(items, truncated, applied, notes)


def search_stock(
    client: InvenTreeClient,
    query: str = "",
    location: Pk | None = None,
    cascade: bool = True,
    limit: int = SEARCH_LIMIT_DEFAULT,
) -> dict[str, Any]:
    """Search stock items by text, optionally inside one location.

    With `cascade` true, stock in child locations of `location` is included.
    """
    applied, notes = _limit(limit)
    items, truncated = client.paginate_with_flag(
        STOCK_PATH,
        params={
            "search": _search_text(query),
            "location": _optional_pk(location, "location"),
            "cascade": bool(cascade),
        },
        limit=applied,
    )
    return _page_result(items, truncated, applied, notes)


def get_stock_item(client: InvenTreeClient, pk: Pk) -> dict[str, Any]:
    """Return one stock item by primary key."""
    return _object(client.get(f"{STOCK_PATH}{_pk(pk, 'pk')}/"), "stock item")


def list_locations(
    client: InvenTreeClient,
    parent: Pk | None = None,
    limit: int = TREE_LIMIT_DEFAULT,
) -> dict[str, Any]:
    """List stock locations, or only the children of `parent` when it is given."""
    applied, notes = _limit(limit)
    # VERIFY-LIVE: confirm the `parent` filter name on GET /api/stock/location/ against /api-doc/.
    items, truncated = client.paginate_with_flag(
        LOCATION_PATH,
        params={"parent": _optional_pk(parent, "parent")},
        limit=applied,
    )
    return _page_result(items, truncated, applied, notes)


def roles(client: InvenTreeClient) -> dict[str, Any]:
    """Return the roles of the token's user (GET /api/user/roles/)."""
    return {"roles": client.roles()}


def _limit(limit: int) -> tuple[int, list[str]]:
    """Clamp a requested limit into range. Returns (applied_limit, notes)."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    applied = min(max(limit, MIN_LIMIT), MAX_LIMIT)
    if applied == limit:
        return applied, []
    return applied, [
        f"limit {limit} was clamped to {applied} (allowed range {MIN_LIMIT} to {MAX_LIMIT})."
    ]


def _page_result(items: list[Any], truncated: bool, limit: int, notes: list[str]) -> dict[str, Any]:
    if truncated:
        notes.append(
            f"Showing the first {len(items)} results. More match the request; "
            "narrow the query or filters to see the rest."
        )
    return {
        "items": items,
        "returned": len(items),
        "truncated": truncated,
        "limit": limit,
        "notes": notes,
    }


def _search_text(query: str) -> str | None:
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    # An empty query is left out of the request entirely.
    return query or None


def _pk(value: Pk, name: str) -> str:
    """Validate a primary key before it goes into a URL path or query.

    Rejects anything that is not a positive whole number, so a crafted value
    cannot change the path. The error text does not echo the value.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive whole number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        number = int(value)
    else:
        raise ValueError(f"{name} must be a positive whole number")
    if number < 1:
        raise ValueError(f"{name} must be a positive whole number")
    return str(number)


def _optional_pk(value: Pk | None, name: str) -> str | None:
    return None if value is None else _pk(value, name)


def _object(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise InvenTreeError("bad_response", f"InvenTree returned something other than an object for a {what}")
    return data
