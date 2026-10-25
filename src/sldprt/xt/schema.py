"""Read Parasolid schema files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

DOUBLES_PER_TYPE = {"f": 1, "i": 2, "v": 3, "b": 6, "h": 3}

_NODE_LINE = re.compile(r"^(\d+)\s+(\S+);\s*(.*?);\s*(\d+)\s+(\d+)\s+(\d+)\s*$")
_FIELD_LINE = re.compile(r"^(.+?);\s*(\w+);\s*(\d+)\s+(\d+)\s+(\d+)\s*$")

_END_OF_HEADER = "**END_OF_HEADER"


class SchemaError(Exception):
    pass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    transmitted: bool
    ptr_class: int
    n_elts: int

    @property
    def is_variable(self) -> bool:
        return self.n_elts == 1


@dataclass(frozen=True)
class NodeSpec:
    node_type: int
    name: str
    description: str
    transmitted: bool
    variable: bool
    fields: tuple[FieldSpec, ...]

    @property
    def effective_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if f.transmitted or f.is_variable)


@dataclass
class Schema:
    key: str
    modeller_version: str | None
    nodes: dict[int, NodeSpec]

    def __getitem__(self, node_type: int) -> NodeSpec:
        try:
            return self.nodes[node_type]
        except KeyError:
            raise SchemaError(
                f"node type {node_type} is not in schema {self.key}"
            ) from None

    def __contains__(self, node_type: int) -> bool:
        return node_type in self.nodes


def parse_schema(text: str) -> Schema:
    marker = text.find(_END_OF_HEADER)
    if marker == -1:
        raise SchemaError("no **END_OF_HEADER marker")
    body = text[text.find("\n", marker) + 1 :]

    lines = [ln.rstrip() for ln in body.splitlines()]
    lines = [ln for ln in lines if ln.strip()]

    key = ""
    modeller = None
    for line in lines[:6]:
        match = re.search(r"SCHEMA FILE created by modeller version (\d+)/(\d+)", line)
        if match:
            modeller, key = match.group(1), f"SCH_{match.group(2)}"
            break

    nodes: dict[int, NodeSpec] = {}
    index = 0
    while index < len(lines):
        match = _NODE_LINE.match(lines[index])
        if match is None:
            index += 1
            continue
        node_type = int(match.group(1))
        n_fields = int(match.group(5))
        index += 1

        fields: list[FieldSpec] = []
        for _ in range(n_fields):
            if index >= len(lines):
                raise SchemaError(
                    f"node {node_type} declares {n_fields} fields, "
                    f"file ends after {len(fields)}"
                )
            field_match = _FIELD_LINE.match(lines[index])
            if field_match is None:
                raise SchemaError(
                    f"node {node_type}: expected a field, got {lines[index]!r}"
                )
            fields.append(
                FieldSpec(
                    name=field_match.group(1).strip(),
                    type=field_match.group(2),
                    transmitted=field_match.group(3) == "1",
                    ptr_class=int(field_match.group(4)),
                    n_elts=int(field_match.group(5)),
                )
            )
            index += 1

        nodes[node_type] = NodeSpec(
            node_type=node_type,
            name=match.group(2),
            description=match.group(3),
            transmitted=match.group(4) == "1",
            variable=match.group(6) == "1",
            fields=tuple(fields),
        )

    if not nodes:
        raise SchemaError("no node definitions found")
    return Schema(key=key, modeller_version=modeller, nodes=nodes)


@cache
def load_schema(path: str | Path) -> Schema:
    return parse_schema(Path(path).read_text(encoding="latin-1"))


@cache
def find_schema(root: Path, key: str) -> Path | None:
    number = key.upper().removeprefix("SCH_")
    wanted = f"sch_{number}.s_t".lower()
    for candidate in root.rglob("*"):
        if candidate.name.lower() == wanted:
            return candidate
    return None


def resolve_schemas(
    root: Path, base_schema: str | None, schema_key: str
) -> tuple[Schema, Schema | None]:
    from ..builtin import base_schema as builtin_base, envelope_for

    base_path = find_schema(root, f"SCH_{base_schema}")
    if base_path is not None:
        base = load_schema(base_path)
    else:
        base = builtin_base()
        if base_schema and str(base_schema) not in base.key:
            raise SchemaError(
                f"base schema sch_{base_schema}.s_t not found under {root}, "
                f"and the built-in facts state {base.key} instead"
            )

    version = schema_key.split("_")[-1]
    version_path = find_schema(root, version)
    if version_path is not None:
        return base, load_schema(version_path)
    return base, envelope_for(version, infer=True)

