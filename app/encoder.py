"""Bit-level message encoder: the inverse of parser.py."""

import math
from typing import Any, Dict, List, Optional

from .checksum import compute
from .parser import _OP_SYMBOLS, ParseError, _eval_condition


class EncodeError(Exception):
    """Raised when values cannot be encoded against a message definition."""


class BitWriter:
    """Writes bits MSB-first into a byte buffer (mirror of BitReader)."""

    def __init__(self):
        self._buf = bytearray()
        self.pos = 0  # current bit position

    def write(self, value: int, nbits: int) -> None:
        if nbits <= 0:
            raise EncodeError(f"bit_length must be positive, got {nbits}")
        if value < 0 or value >= (1 << nbits):
            raise EncodeError(f"value {value} does not fit in {nbits} bits")
        for i in range(nbits - 1, -1, -1):
            if self.pos % 8 == 0:
                self._buf.append(0)
            if (value >> i) & 1:
                self._buf[-1] |= 1 << (7 - (self.pos % 8))
            self.pos += 1

    def to_bytes(self) -> tuple[bytes, int]:
        """Returns (bytes, padding_bits). The final partial byte is zero-padded."""
        padding = (8 - self.pos % 8) % 8
        return bytes(self._buf), padding


def _check_range(field: Dict[str, Any], raw: int) -> None:
    n = field["bit_length"]
    if field["type"] == "int":
        lo, hi = -(1 << (n - 1)), (1 << (n - 1)) - 1
    else:
        lo, hi = 0, (1 << n) - 1
    if not lo <= raw <= hi:
        raise EncodeError(
            f"field '{field['name']}': raw value {raw} out of range [{lo}, {hi}] "
            f"for {n}-bit {field['type']}"
        )


def _invert_scale_offset(field: Dict[str, Any], value: float) -> int:
    """Solve raw = (value - offset) / scale. The result must be an integer that
    reproduces the value exactly (or exactly as the parse response would display
    it, which is rounded to 6 decimals); otherwise the value is not representable."""
    name = field["name"]
    scale = field.get("scale")
    offset = field.get("offset")
    s = scale if scale is not None else 1.0
    o = offset if offset is not None else 0.0
    if s == 0:
        raise EncodeError(f"field '{name}': scale is 0, cannot invert value {value}")
    raw_f = (value - o) / s
    tol = max(1e-9, abs(value) * 1e-12)
    candidates = sorted({math.floor(raw_f), math.ceil(raw_f)}, key=lambda c: abs(c - raw_f))
    for cand in candidates:
        produced = cand * s + o
        if abs(produced - value) <= tol or abs(round(produced, 6) - value) <= tol:
            return cand
    raise EncodeError(
        f"field '{name}': value {value} cannot be exactly represented with "
        f"scale={s} offset={o} (no integer raw value reproduces it)"
    )


def _convert_numeric(field: Dict[str, Any], value: Any) -> tuple[int, Any]:
    """Returns (raw_int, display_value) for a uint/int field."""
    name = field["name"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EncodeError(f"field '{name}': expected a number, got {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise EncodeError(f"field '{name}': value must be finite, got {value!r}")
    has_transform = field.get("scale") is not None or field.get("offset") is not None
    if has_transform:
        raw = _invert_scale_offset(field, float(value))
    else:
        if isinstance(value, float):
            if not value.is_integer():
                raise EncodeError(
                    f"field '{name}': value {value} is not an integer and the field "
                    f"has no scale/offset"
                )
            value = int(value)
        raw = value
    _check_range(field, raw)
    display: Any = raw
    if has_transform:
        s = field.get("scale")
        display = round(raw * (s if s is not None else 1.0) + (field.get("offset") or 0.0), 6)
    return raw, display


def _convert_hex(field: Dict[str, Any], value: Any) -> tuple[int, str]:
    name = field["name"]
    n = field["bit_length"]
    if isinstance(value, bool):
        raise EncodeError(f"field '{name}': expected a hex string, got {value!r}")
    if isinstance(value, int):
        raw = value
    elif isinstance(value, str):
        s = value.strip()
        if s[:2].lower() == "0x":
            s = s[2:]
        if not s:
            raise EncodeError(f"field '{name}': empty hex string")
        try:
            raw = int(s, 16)
        except ValueError:
            raise EncodeError(f"field '{name}': invalid hex string {value!r}")
    else:
        raise EncodeError(f"field '{name}': expected a hex string, got {value!r}")
    if raw < 0 or raw >= (1 << n):
        raise EncodeError(f"field '{name}': value {raw:#x} does not fit in {n} bits")
    return raw, f"0x{raw:0{n // 4}x}"


def _convert_ascii(field: Dict[str, Any], value: Any) -> tuple[int, str]:
    name = field["name"]
    if not isinstance(value, str):
        raise EncodeError(f"field '{name}': expected a string, got {value!r}")
    try:
        data = value.encode("ascii")
    except UnicodeEncodeError:
        raise EncodeError(f"field '{name}': value {value!r} is not ASCII-encodable")
    need = field["bit_length"] // 8
    if len(data) != need:
        raise EncodeError(
            f"field '{name}': expected exactly {need} ASCII character(s), got {len(data)}"
        )
    return int.from_bytes(data, "big"), value


def _convert_value(field: Dict[str, Any], value: Any) -> tuple[int, Any]:
    """Converts a caller-provided value (parse-response type) to (raw_int, display)."""
    ftype = field["type"]
    if ftype == "bool":
        if isinstance(value, bool):
            raw = int(value)
        elif isinstance(value, int) and value in (0, 1):
            raw = value
        else:
            raise EncodeError(f"field '{field['name']}': expected a boolean, got {value!r}")
        return raw, bool(raw)
    if ftype in ("uint", "int"):
        return _convert_numeric(field, value)
    if ftype == "hex":
        return _convert_hex(field, value)
    if ftype == "ascii":
        return _convert_ascii(field, value)
    raise EncodeError(f"unsupported field type: {ftype}")


def _write_field(writer: BitWriter, field: Dict[str, Any], raw: int) -> None:
    ftype = field["type"]
    n = field["bit_length"]
    if ftype == "bool":
        writer.write(raw, 1)
        return
    if ftype in ("uint", "int"):
        if raw < 0:  # two's complement
            raw += 1 << n
        if field.get("endianness", "big") == "little":
            # validated at registration: n % 8 == 0; least-significant byte first
            for i in range(n // 8):
                writer.write((raw >> (8 * i)) & 0xFF, 8)
        else:
            writer.write(raw, n)
        return
    # hex / ascii: raw is the big-endian integer of the byte sequence
    writer.write(raw, n)


def encode_message(
    fields: List[Dict[str, Any]],
    checksum_cfg: Optional[Dict[str, Any]],
    values: Dict[str, Any],
) -> Dict[str, Any]:
    checksum_field = checksum_cfg["field"] if checksum_cfg else None
    if checksum_field and checksum_field in values:
        raise EncodeError(
            f"checksum field '{checksum_field}' is computed automatically; "
            f"do not provide a value for it"
        )
    known = {f["name"] for f in fields}
    unknown = sorted(set(values) - known)
    if unknown:
        raise EncodeError(f"unknown field(s) in values: {', '.join(unknown)}")

    writer = BitWriter()
    raws: Dict[str, Any] = {}  # raw values, used by conditions (same as parser)
    out_fields: List[Dict[str, Any]] = []
    branch_path: List[Dict[str, Any]] = []
    warnings: List[str] = []
    checksum_info: Optional[Dict[str, Any]] = None

    for field in fields:
        name = field["name"]
        condition = field.get("condition")
        if condition:
            try:
                matched, actual = _eval_condition(condition, raws)
            except ParseError as exc:
                raise EncodeError(str(exc))
            symbol = _OP_SYMBOLS[condition["op"]]
            branch_path.append(
                {
                    "field": name,
                    "condition": f"{condition['field']} {symbol} {condition['value']}",
                    "actual_value": actual,
                    "matched": matched,
                    "action": "encoded" if matched else "skipped",
                }
            )
            if not matched:
                if name in values:
                    warnings.append(
                        f"field '{name}' was skipped by its branch condition; "
                        f"provided value ignored"
                    )
                continue

        bit_offset = writer.pos
        if name == checksum_field:
            if bit_offset % 8 != 0:
                raise EncodeError(
                    f"checksum field '{name}' does not start on a byte boundary "
                    f"(bit offset {bit_offset})"
                )
            prefix, _ = writer.to_bytes()  # byte-aligned: exactly the preceding bytes
            raw = compute(checksum_cfg["algorithm"], prefix)
            value: Any = raw
        else:
            if name not in values:
                raise EncodeError(f"missing required value for field '{name}'")
            raw, value = _convert_value(field, values[name])

        _write_field(writer, field, raw)
        entry: Dict[str, Any] = {
            "name": name,
            "bit_offset": bit_offset,
            "bit_length": field["bit_length"],
            "value": value,
        }
        if field["type"] in ("uint", "int"):
            entry["raw_value"] = raw
        out_fields.append(entry)
        if field["type"] in ("uint", "int", "bool"):
            raws[name] = raw

        if checksum_cfg and name == checksum_field:
            checksum_info = {
                "field": name,
                "algorithm": checksum_cfg["algorithm"],
                "range": f"bytes [0, {bit_offset // 8})",
                "calculated": raw,
                "calculated_hex": f"0x{raw:0{field['bit_length'] // 4}x}",
            }

    data, padding = writer.to_bytes()
    if padding:
        warnings.append(f"{padding} zero padding bit(s) appended to reach a byte boundary")

    return {
        "hex": data.hex(),
        "fields": out_fields,
        "branch_path": branch_path,
        "checksum": checksum_info,
        "encoded_bits": writer.pos,
        "padded_bits": padding,
        "message_bits": len(data) * 8,
        "warnings": warnings,
    }
