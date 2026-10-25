"""Read the text node stream of a transmit (.x_t)."""

from __future__ import annotations

from .binary import Node
from .schema import DOUBLES_PER_TYPE, FieldSpec, Schema

TERMINATOR = 1

UNCHANGED = 255

_DIGITS = "0123456789"

NULL_DOUBLE = -3.14158e13

_NULL = "?"


class TextError(Exception):
    pass


class Cursor:
    def __init__(self, text: str, offset: int = 0) -> None:
        self.text = text
        self.pos = offset

    @property
    def remaining(self) -> int:
        return len(self.text) - self.pos

    def _skip_spaces(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] == " ":
            self.pos += 1

    def at_end(self) -> bool:
        self._skip_spaces()
        return self.pos >= len(self.text)

    def _number_token(self) -> str:
        self._skip_spaces()
        start = self.pos
        text = self.text
        end = len(text)

        if self.pos < end and text[self.pos] in "+-":
            self.pos += 1
        while self.pos < end and text[self.pos] in _DIGITS:
            self.pos += 1
        if self.pos < end and text[self.pos] == ".":
            self.pos += 1
            while self.pos < end and text[self.pos] in _DIGITS:
                self.pos += 1

        if self.pos < end and text[self.pos] in "eE":
            look = self.pos + 1
            if look < end and text[look] in "+-":
                look += 1
            if look < end and text[look] in _DIGITS:
                self.pos = look
                while self.pos < end and text[self.pos] in _DIGITS:
                    self.pos += 1

        if self.pos == start:
            raise TextError(
                f"expected a number at {start}, found {text[start:start + 12]!r}"
            )
        return text[start:self.pos]

    def _integer_token(self) -> str:
        self._skip_spaces()
        start = self.pos
        if self.pos < len(self.text) and self.text[self.pos] in "+-":
            self.pos += 1
        while self.pos < len(self.text) and self.text[self.pos] in _DIGITS:
            self.pos += 1
        if self.pos == start:
            raise TextError(
                f"expected an integer at {start}, "
                f"found {self.text[start:start + 12]!r}"
            )
        return self.text[start:self.pos]

    def integer(self) -> int:
        token = self._integer_token()
        try:
            return int(token)
        except ValueError as exc:
            raise TextError(f"bad integer {token!r} at {self.pos}") from exc

    def _at_null(self) -> bool:
        self._skip_spaces()
        return self.pos < len(self.text) and self.text[self.pos] == _NULL

    def index(self) -> int:
        return self.integer()

    def take_null(self) -> bool:
        if self._at_null():
            self.pos += 1
            return True
        return False

    def double(self) -> float:
        token = self._number_token()
        try:
            return float(token)
        except ValueError as exc:
            raise TextError(f"bad double {token!r} at {self.pos}") from exc

    def char(self) -> str:
        self._skip_spaces()
        return self.char_raw()

    def char_raw(self) -> str:
        if self.pos >= len(self.text):
            raise TextError("expected a character, found the end of the stream")
        self.pos += 1
        return self.text[self.pos - 1]

    def logical(self) -> int:
        value = self.char()
        if value not in "TF":
            raise TextError(f"expected T or F at {self.pos - 1}, found {value!r}")
        return 1 if value == "T" else 0

    def short_string(self) -> str:
        length = self.integer()
        if self.pos < len(self.text) and self.text[self.pos] == " ":
            self.pos += 1
        end = self.pos + length
        if end > len(self.text):
            raise TextError(f"string of {length} runs past the end at {self.pos}")
        value = self.text[self.pos:end]
        self.pos = end
        return value


def _read_field_spec(cursor: Cursor) -> FieldSpec:
    name = cursor.short_string()
    ptr_class = cursor.integer()
    n_elts = cursor.integer()
    type_letter = "" if ptr_class else cursor.short_string()
    transmitted = True if n_elts != 1 else bool(cursor.logical())
    return FieldSpec(
        name=name,
        type=type_letter or "p",
        transmitted=transmitted,
        ptr_class=ptr_class,
        n_elts=n_elts,
    )


def read_node_layout(
    cursor: Cursor, node_type: int, base: Schema
) -> tuple[list[FieldSpec], str]:
    if node_type not in base:
        count = cursor.integer()
        name = cursor.short_string()
        cursor.short_string()
        return [_read_field_spec(cursor) for _ in range(count)], name

    spec = base[node_type]
    base_fields = list(spec.effective_fields)

    flag = cursor.integer()
    if flag == UNCHANGED:
        return base_fields, spec.name

    fields: list[FieldSpec] = []
    at = 0
    while True:
        op = cursor.char()
        if op == "Z":
            break
        if op == "C":
            if at >= len(base_fields):
                raise TextError(f"copy past the end of base node {node_type}")
            fields.append(base_fields[at])
            at += 1
        elif op == "D":
            if at >= len(base_fields):
                raise TextError(f"delete past the end of base node {node_type}")
            at += 1
        elif op in ("I", "A"):
            fields.append(_read_field_spec(cursor))
        else:
            raise TextError(
                f"node {node_type}: unknown schema edit {op!r} at {cursor.pos - 1}"
            )

    if len(fields) != flag:
        raise TextError(
            f"node {node_type}: edit script produced {len(fields)} fields, "
            f"header declared {flag}"
        )
    return fields, spec.name


def _read_value(cursor: Cursor, spec: FieldSpec, count: int):
    if spec.type in DOUBLES_PER_TYPE:
        width = DOUBLES_PER_TYPE[spec.type]
        values: list[float] = []
        for _ in range(count):
            if cursor.take_null():
                values.extend([NULL_DOUBLE] * width)
            else:
                values.extend(cursor.double() for _ in range(width))
        return values[0] if count == 1 and width == 1 else values

    readers = {
        "u": cursor.integer,
        "c": cursor.char,
        "l": cursor.logical,
        "n": cursor.integer,
        "w": cursor.integer,
        "d": cursor.integer,
        "p": cursor.index,
    }
    if spec.type not in readers:
        raise TextError(f"unknown field type {spec.type!r} on {spec.name}")
    read = readers[spec.type]
    if count == 1:
        return 0 if cursor.take_null() else read()
    if spec.type == "c":
        return [cursor.char() if i == 0 else cursor.char_raw() for i in range(count)]
    return [0 if cursor.take_null() else read() for _ in range(count)]


def parse_nodes(
    body: str,
    base: Schema,
    *,
    current: Schema | None = None,
    layouts: dict[int, list[FieldSpec]] | None = None,
) -> list[Node]:
    cursor = Cursor(body)
    max_node_types = cursor.integer()
    user_field_size = cursor.integer()
    if user_field_size:
        raise TextError(f"user fields are not supported (size {user_field_size})")

    layouts = {} if layouts is None else layouts
    names: dict[int, str] = {}
    nodes: list[Node] = []

    terminated = False
    while not cursor.at_end():
        start = cursor.pos
        node_type = cursor.integer()
        if node_type == TERMINATOR:
            cursor.index()
            terminated = True
            break
        if node_type > max_node_types:
            raise TextError(
                f"node type {node_type} at {start} exceeds the declared "
                f"maximum {max_node_types}"
            )

        if node_type not in layouts:
            fields, name = read_node_layout(cursor, node_type, base)
            layouts[node_type] = fields
            names[node_type] = name
            if current is not None and node_type in current:
                declared = len(current[node_type].effective_fields)
                if declared != len(fields):
                    raise TextError(
                        f"node {node_type} ({name}): decoded {len(fields)} fields "
                        f"but schema {current.key} declares {declared}"
                    )

        fields = layouts[node_type]
        variable_count = cursor.integer() if fields and fields[-1].is_variable else None
        index = cursor.index()

        values: dict[str, object] = {}
        for field_spec in fields:
            count = field_spec.n_elts or 1
            if field_spec.is_variable:
                count = variable_count
            values[field_spec.name] = _read_value(cursor, field_spec, count)

        nodes.append(
            Node(
                node_type=node_type,
                name=names[node_type],
                index=index,
                values=values,
                start=start,
                end=cursor.pos,
            )
        )

    if not terminated:
        raise TextError(
            f"stream ends after {len(nodes)} nodes without a TERMINATOR at "
            f"{cursor.pos}: it is truncated"
        )
    if not cursor.at_end():
        raise TextError(
            f"stream did not consume its text: {cursor.remaining} characters left "
            f"at {cursor.pos} after {len(nodes)} nodes"
        )
    return nodes

