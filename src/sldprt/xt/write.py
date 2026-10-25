"""Write a Parasolid text transmit (.x_t)."""

from __future__ import annotations

import math
from decimal import Decimal
from functools import lru_cache

from .binary import Node
from .edit import WriteError, prepare, renumber
from .schema import DOUBLES_PER_TYPE, FieldSpec, Schema
from .text import NULL_DOUBLE, TERMINATOR, UNCHANGED

WRAP = 80


class Tokens:
    def __init__(self) -> None:
        self._parts: list[str] = []
        self._after_number = False

    def _emit(self, text: str, *, number: bool) -> None:
        if not text:
            return
        if self._after_number:
            self._parts.append(" ")
        self._parts.append(text)
        self._after_number = number

    def raw(self, text: str) -> None:
        if not text:
            return
        self._parts.append(text)
        self._after_number = False

    def number(self, text: str) -> None:
        self._emit(text, number=True)

    def integer(self, value: int) -> None:
        self._emit(str(int(value)), number=True)

    def double(self, value: float) -> None:
        if value == NULL_DOUBLE:
            self.null()
            return
        self._emit(spell(float(value)), number=True)

    def logical(self, value) -> None:
        self._emit("T" if value else "F", number=False)

    def op(self, letter: str) -> None:
        self._emit(letter, number=False)

    def null(self) -> None:
        self._emit("?", number=False)

    def char(self, value) -> None:
        self._emit(
            chr(value) if isinstance(value, int) else str(value)[:1], number=False
        )

    def short_string(self, text: str) -> None:
        self._emit(str(len(text)), number=True)
        self.raw(" " + text)

    def text(self) -> str:
        return "".join(self._parts).lstrip()


@lru_cache(maxsize=1 << 16)
def spell(value: float) -> str:
    if value == 0.0:
        return "0"

    text = repr(float(value))
    if text in ("inf", "-inf", "nan"):
        return text

    number = Decimal(text).normalize()
    sign, digits, exponent = number.as_tuple()
    mantissa = "".join(str(d) for d in digits)
    lead = "-" if sign else ""

    exponential = lead + mantissa + (f"e{exponent}" if exponent else "")

    if exponent >= 0:
        positional = lead + mantissa + "0" * exponent
    elif -exponent < len(mantissa):
        cut = len(mantissa) + exponent
        positional = lead + mantissa[:cut] + "." + mantissa[cut:]
    else:
        positional = lead + "." + "0" * (-exponent - len(mantissa)) + mantissa

    if len(exponential) != len(positional):
        return min(exponential, positional, key=len)
    return exponential if exponent > 0 else positional


def _shape(spec: FieldSpec) -> tuple:
    return (spec.name, spec.type or "p", spec.transmitted, spec.ptr_class, spec.n_elts)


def _same(left, right) -> bool:
    return [_shape(f) for f in left] == [_shape(f) for f in right]


def _write_field_spec(tokens: Tokens, spec: FieldSpec) -> None:
    tokens.short_string(spec.name)
    tokens.integer(spec.ptr_class)
    tokens.integer(spec.n_elts)
    if not spec.ptr_class:
        tokens.short_string(spec.type)
    if spec.n_elts == 1:
        tokens.logical(spec.transmitted)


def _write_layout(
    tokens: Tokens,
    node_type: int,
    fields: list[FieldSpec],
    base: Schema,
    name: str = "",
) -> None:
    if node_type not in base:
        tokens.integer(len(fields))
        tokens.short_string(name)
        tokens.short_string(name.replace("_", " ").capitalize())
        for spec in fields:
            _write_field_spec(tokens, spec)
        return

    if _same(base[node_type].effective_fields, fields):
        tokens.integer(UNCHANGED)
        return

    base_fields = list(base[node_type].effective_fields)
    tokens.integer(len(fields))
    at = want = 0
    while want < len(fields):
        if (
            at < len(base_fields)
            and want < len(fields)
            and _shape(base_fields[at]) == _shape(fields[want])
        ):
            tokens.op("C")
            at += 1
            want += 1
        elif at < len(base_fields) and _shape(base_fields[at]) not in [
            _shape(f) for f in fields[want:]
        ]:
            tokens.op("D")
            at += 1
        else:
            tokens.op("A" if at >= len(base_fields) else "I")
            _write_field_spec(tokens, fields[want])
            want += 1
    tokens.op("Z")


def _write_value(tokens: Tokens, spec: FieldSpec, value, count: int) -> None:
    if spec.type in DOUBLES_PER_TYPE:
        width = DOUBLES_PER_TYPE[spec.type]
        values = value if isinstance(value, list) else [value]
        if len(values) != count * width:
            raise WriteError(
                f"{spec.name}: {len(values)} doubles for {count} elements of "
                f"width {width}"
            )
        for element in range(count):
            chunk = values[element * width : (element + 1) * width]
            if all(v == NULL_DOUBLE for v in chunk):
                tokens.null()
                continue
            for component in chunk:
                tokens.double(component)
        return

    values = value if isinstance(value, list) else [value]
    if len(values) != count:
        raise WriteError(f"{spec.name}: {len(values)} values for {count} elements")
    if spec.type == "c" and count > 1:
        tokens.char(values[0])
        tokens.raw("".join(
            chr(v) if isinstance(v, int) else str(v)[:1] for v in values[1:]
        ))
        return
    for element in values:
        if spec.type == "c":
            if element in (0, "\x00"):
                tokens.null()
            else:
                tokens.char(element)
        elif spec.type == "l":
            tokens.logical(element)
        else:
            tokens.integer(element)


def write_nodes(
    nodes: list[Node],
    base: Schema,
    layouts: dict[int, list[FieldSpec]],
    *,
    max_node_types: int,
) -> str:
    tokens = Tokens()
    tokens.integer(max_node_types)
    tokens.integer(0)

    seen: set[int] = set()
    for node in nodes:
        fields = layouts.get(node.node_type)
        if fields is None:
            raise WriteError(
                f"node type {node.node_type} ({node.name}) has no decoded layout"
            )

        tokens.integer(node.node_type)
        if node.node_type not in seen:
            _write_layout(tokens, node.node_type, fields, base, node.name)
            seen.add(node.node_type)

        if fields and fields[-1].is_variable:
            last = fields[-1]
            value = node.values.get(last.name, [])
            values = value if isinstance(value, list) else [value]
            width = DOUBLES_PER_TYPE.get(last.type, 1)
            tokens.integer(len(values) // width)
        tokens.integer(node.index)

        for field_spec in fields:
            count = field_spec.n_elts or 1
            value = node.values.get(field_spec.name)
            if field_spec.is_variable:
                values = value if isinstance(value, list) else [value]
                width = DOUBLES_PER_TYPE.get(field_spec.type, 1)
                count = len(values) // width
            _write_value(tokens, field_spec, value, count)

    tokens.integer(TERMINATOR)
    tokens.integer(0)
    return tokens.text() + " "


def wrap(text: str, width: int = WRAP) -> str:
    lines = []
    at = 0
    while at < len(text):
        end = min(at + width, len(text))
        if end < len(text) and text[end - 1] == " ":
            end -= 1
        lines.append(text[at:end])
        at = end
    return "\n".join(lines)


PREAMBLE = (
    "**ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "**************************"
)
PREAMBLE_2 = (
    "**PARASOLID !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~0123456789"
    "**************************"
)
END_OF_HEADER = "**END_OF_HEADER"


def _padded(line: str, width: int = WRAP) -> str:
    return line if len(line) >= width else line + "*" * (width - len(line))


def write_transmit(
    nodes: list[Node],
    base: Schema,
    layouts: dict[int, list[FieldSpec]],
    *,
    max_node_types: int,
    modeller_version: str,
    schema_key: str,
    key: str = "part",
    date: str = "Mon Jan  1 00:00:00 2001",
    current: Schema | None = None,
) -> str:
    if not (modeller_version.isdigit() and len(modeller_version) >= 5):
        digits = modeller_version if modeller_version.isdigit() else "0"
        modeller_version = digits.zfill(7)

    kept = prepare(nodes, layouts, current)
    body = write_nodes(
        renumber(kept, layouts),
        base,
        layouts,
        max_node_types=max_node_types,
    )
    banner = f": TRANSMIT FILE created by modeller version {modeller_version}"
    schema = f"{schema_key}_{base.key.rsplit('_', 1)[-1]}"
    stream = f"T{len(banner)} {banner}{len(schema)} {schema}{body}"

    header = "\n".join(
        [
            _padded(PREAMBLE),
            _padded(PREAMBLE_2),
            "**PART1;",
            "MC=AMD64;",
            "MC_MODEL=unknown;",
            "MC_ID=unknown;",
            "OS=unknown;",
            "OS_RELEASE=unknown;",
            f"FRU=Parasolid Version {modeller_version[:2]}."
            f"{int(modeller_version[2:4])}, build {int(modeller_version[4:])};",
            "APPL=openSW;",
            "SITE=;",
            "USER=unknown;",
            "FORMAT=text;",
            "GUISE=transmit;",
            f"KEY={key};",
            f"FILE={key}.x_t;",
            f"DATE={date};",
            "**PART2;",
            f"SCH={schema_key};",
            "USFLD_SIZE=0;",
            "**PART3;",
            _padded(END_OF_HEADER),
            "",
        ]
    )
    return header + wrap(stream) + "\n"


def verify(
    text: str,
    nodes: list[Node],
    base: Schema,
    current: Schema,
    *,
    layouts: dict[int, list[FieldSpec]],
) -> None:
    from ..parasolid import read_text_transmit
    from . import text as text_reader

    transmit = read_text_transmit(text.encode("latin-1"))
    if transmit.guise != "transmit":
        raise WriteError(f"wrote guise {transmit.guise!r}, not 'transmit'")
    body = transmit.data[transmit.body_offset :].decode("latin-1")
    again = text_reader.parse_nodes(body, base, current=current)

    expected = prepare(nodes, layouts, current)
    if len(again) != len(expected):
        raise WriteError(
            f"wrote {len(expected)} nodes and read back {len(again)}"
        )
    for position, (wanted, got) in enumerate(zip(expected, again, strict=False), start=1):
        if wanted.node_type != got.node_type:
            raise WriteError(
                f"node {wanted.index} ({wanted.name}) came back as type "
                f"{got.node_type}"
            )
        if got.index != position:
            raise WriteError(
                f"node {position} came back numbered {got.index}; the file is "
                f"written with dense indices so that its root carries 1"
            )

    for wanted, got in zip(renumber(expected, layouts), again, strict=False):
        if wanted.values == got.values:
            continue
        for field, value in wanted.values.items():
            other = got.values.get(field)
            if not _same_value(value, other):
                raise WriteError(
                    f"node {got.index} ({wanted.name}) field {field!r}: "
                    f"wrote {value!r}, read back {other!r}"
                )
        surplus = set(got.values) - set(wanted.values)
        if surplus:
            raise WriteError(
                f"node {got.index} ({wanted.name}) read back with fields "
                f"never written: {sorted(surplus)}"
            )


def _same_value(wanted, got) -> bool:
    if isinstance(wanted, float) and isinstance(got, float):
        return wanted == got or (math.isnan(wanted) and math.isnan(got))
    if isinstance(wanted, (list, tuple)) and isinstance(got, (list, tuple)):
        return len(wanted) == len(got) and all(
            _same_value(a, b) for a, b in zip(wanted, got, strict=False)
        )
    if isinstance(wanted, int) and isinstance(got, str) and len(got) == 1:
        return ord(got) == wanted
    if isinstance(wanted, str) and len(wanted) == 1 and isinstance(got, int):
        return ord(wanted) == got
    if {wanted, got} == {0, 63}:
        return True
    return wanted == got

