"""Parameter checks for InvenTree write operations.

Every check raises WriteValidationError with a stable `code` before any request is sent.
Messages name the field and the rule. They never echo a token or a full request body.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Final, TypeVar

from meridian_inventree.audit import scrub
from meridian_inventree.client import InvenTreeError

MAX_NAME_LENGTH: Final[int] = 200
MAX_CLIENT_KEY_LENGTH: Final[int] = 200  # matches plans.MAX_CLIENT_KEY_LENGTH
# A quantity's adjusted exponent (the power of ten of its leading digit) must sit in this range,
# and it may carry at most MAX_QUANTITY_DIGITS significant digits.
MIN_QUANTITY_EXPONENT: Final[int] = -9
MAX_QUANTITY_EXPONENT: Final[int] = 9
MAX_QUANTITY_DIGITS: Final[int] = 15

_T = TypeVar("_T")


class WriteValidationError(InvenTreeError):
    """A parameter or live-state check failed. No write was sent."""


def invalid(code: str, message: str) -> WriteValidationError:
    return WriteValidationError(code, message)


def as_mapping(params: object) -> Mapping[str, Any]:
    if not isinstance(params, Mapping):
        raise invalid("invalid_params", "params must be an object")
    return params


def reject_unknown(params: Mapping[str, Any], allowed: frozenset[str]) -> None:
    """Refuse unknown keys so a typo such as `quanity` cannot be silently ignored."""
    unknown = sorted(str(key) for key in params if key not in allowed)
    if unknown:
        raise invalid("unknown_field", f"unknown parameter(s): {', '.join(unknown)}")


def required(
    params: Mapping[str, Any],
    field: str,
    convert: Callable[[Any, str], _T],
    *,
    missing_code: str = "missing_field",
) -> _T:
    if params.get(field) is None:
        raise invalid(missing_code, f"{field} is required")
    return convert(params[field], field)


def optional(params: Mapping[str, Any], field: str, convert: Callable[[Any, str], _T]) -> _T | None:
    value = params.get(field)
    return None if value is None else convert(value, field)


def as_name(value: object, field: str = "name") -> str:
    """A name is a string of 1 to 200 characters after trimming."""
    if not isinstance(value, str):
        raise invalid("invalid_name", f"{field} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise invalid("invalid_name", f"{field} must not be empty")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise invalid("invalid_name", f"{field} must be at most {MAX_NAME_LENGTH} characters")
    return cleaned


def as_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise invalid("invalid_field", f"{field} must be a string")
    return value


def as_flag(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise invalid("invalid_field", f"{field} must be true or false")
    return value


def as_pk(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise invalid("invalid_pk", f"{field} must be a positive whole number id")
    return value


def _significant_digits(number: Decimal) -> int:
    """Digits in the coefficient with trailing zeros dropped. Reads the tuple, so no string is built."""
    digits = number.as_tuple().digits
    count = len(digits)
    while count > 1 and digits[count - 1] == 0:
        count -= 1
    return count


def as_number(value: object, field: str) -> Decimal:
    """A finite quantity as Decimal, inside the wire limits. Booleans, NaN and infinities are refused.

    The limits are checked on the Decimal itself, before any string is built. So 1E+999999999 is
    refused at once, not after a billion-digit string is made for it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise invalid("invalid_quantity", f"{field} must be a number")
    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, int):
            number = Decimal(value)  # no str() here: str() of a very long int raises ValueError
        else:
            number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise invalid("invalid_quantity", f"{field} must be a number") from None
    if not number.is_finite():
        raise invalid("invalid_quantity", f"{field} must be a finite number")
    if not MIN_QUANTITY_EXPONENT <= number.adjusted() <= MAX_QUANTITY_EXPONENT:
        raise invalid("invalid_quantity", f"{field} is outside the allowed range (exponent -9 to 9)")
    if _significant_digits(number) > MAX_QUANTITY_DIGITS:
        raise invalid("invalid_quantity", f"{field} may have at most {MAX_QUANTITY_DIGITS} significant digits")
    return number


def as_non_negative(value: object, field: str) -> Decimal:
    number = as_number(value, field)
    if number < 0:
        raise invalid("invalid_quantity", f"{field} must not be negative")
    return number


def as_positive(value: object, field: str) -> Decimal:
    number = as_number(value, field)
    if number <= 0:
        raise invalid("invalid_quantity", f"{field} must be greater than zero")
    return number


def as_nonzero(value: object, field: str) -> Decimal:
    number = as_number(value, field)
    if number == 0:
        raise invalid("invalid_quantity", f"{field} must not be zero")
    return number


def as_wire(number: Decimal) -> str:
    """A quantity as the plain decimal string sent to InvenTree. VERIFY-LIVE: string or number.

    The wire limits are checked again here, so no string is built for a number outside them.
    """
    as_number(number, "quantity")
    return format(number, "f")


def as_client_key(value: object) -> str:
    """A client key is a non-blank string of at most 200 characters. It is compared as given.

    The audit log scrubs credential-looking text before it writes. A key that scrubbing would
    change could never be found in the log again, so the idempotency check would miss it.
    Such keys are refused.
    """
    if not isinstance(value, str):
        raise invalid("invalid_client_key", "client_key must be a string")
    if not value.strip() or len(value) > MAX_CLIENT_KEY_LENGTH:
        raise invalid("invalid_client_key", f"client_key must be 1 to {MAX_CLIENT_KEY_LENGTH} characters")
    if scrub(value) != value:
        raise invalid("invalid_client_key", "client_key must not contain credential-like text")
    return value


def drop_none(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}
