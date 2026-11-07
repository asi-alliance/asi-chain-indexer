"""Exact native token amounts and decimal-string API values."""

from decimal import Decimal

MAX_AMOUNT = (1 << 256) - 1


def _canonical_digits(value: str) -> bool:
    return value.isascii() and value.isdigit() and (len(value) == 1 or value[0] != "0")


def canonical_signed_decimal(value: str) -> str:
    """Validate a signed decimal string, including out-of-range failed attempts."""
    if not isinstance(value, str):
        raise ValueError("amount must be a canonical signed integer")
    digits = value[1:] if value.startswith("-") else value
    if not _canonical_digits(digits) or value == "-0":
        raise ValueError("amount must be a canonical signed integer")
    return value


def signed_integer_text(value: int) -> str:
    """Format a signed integer without Python's decimal digit limit."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("amount must be an exact integer")
    return format(Decimal(value), "f")


def parse_signed_integer(value: str) -> int:
    """Restore a canonical signed integer without Python's decimal digit limit."""
    return int(Decimal(canonical_signed_decimal(value)))


def amount(value: int | str | Decimal) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("amount must be an exact integer")
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise ValueError("amount must be an integer")
        value = int(value)
    text = str(value)
    if not _canonical_digits(text):
        raise ValueError("amount must be a canonical unsigned integer")
    result = int(text)
    if result > MAX_AMOUNT:
        raise ValueError("amount exceeds uint256")
    return result


def token_decimal(raw: int, decimals: int, *, aggregate: bool = False) -> Decimal:
    if aggregate:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError("aggregate must be a nonnegative integer")
    else:
        raw = amount(raw)
    if (
        isinstance(decimals, bool)
        or not isinstance(decimals, int)
        or not 0 <= decimals <= 77
    ):
        raise ValueError("invalid decimals")
    text = str(raw).zfill(decimals + 1)
    return Decimal(text if decimals == 0 else text[:-decimals] + "." + text[-decimals:])


def integer_json(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return {key: integer_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [integer_json(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
