"""Exact native token amounts and decimal-string API values."""

from decimal import MAX_EMAX, MAX_PREC, MIN_EMIN, Context, Decimal, Inexact, Rounded

from src.off_loop import run_off_loop

MAX_AMOUNT = (1 << 256) - 1

_EXACT = Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN, traps=[Inexact, Rounded])
_DIRECT_BITS = 4096
_INLINE_BITS = 1 << 16


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


def _decimal_magnitude(value: int, bits: int, powers: dict) -> Decimal:
    # Decimal(int) is quadratic, so only small halves use it; NTT products join them.
    if bits <= _DIRECT_BITS:
        return Decimal(value)
    low_bits = bits >> 1
    high = value >> low_bits
    low = value - (high << low_bits)
    if low_bits not in powers:
        powers[low_bits] = _EXACT.power(2, low_bits)
    return _EXACT.add(
        _EXACT.multiply(
            _decimal_magnitude(high, bits - low_bits, powers), powers[low_bits]
        ),
        _decimal_magnitude(low, low_bits, powers),
    )


def signed_integer_text(value: int) -> str:
    """Format a signed integer exactly, in subquadratic time, without Python's digit limit."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("amount must be an exact integer")
    magnitude = abs(value)
    digits = format(_decimal_magnitude(magnitude, magnitude.bit_length(), {}), "f")
    return "-" + digits if value < 0 else digits


async def signed_integer_text_off_loop(value: int) -> str:
    """Format large integers in a worker process so the event loop keeps serving."""
    if isinstance(value, bool) or not isinstance(value, int) or value.bit_length() <= _INLINE_BITS:
        return signed_integer_text(value)
    return await run_off_loop(signed_integer_text, value)


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
