"""Built-in Parasolid schema facts and assembly envelopes."""

from __future__ import annotations

from functools import cache

from .schema_facts import BASE_KEY, BASE_NODES, ENVELOPE_BY_VERSION, ENVELOPE_FAMILIES
from .xt.schema import FieldSpec, NodeSpec, Schema

_ASSEMBLY, _INSTANCE, _TRANSFORM = 10, 11, 100
_ENVELOPE_NAMES = {_ASSEMBLY: "ASSEMBLY", _INSTANCE: "INSTANCE", _TRANSFORM: "TRANSFORM"}


def _fields(rows) -> tuple[FieldSpec, ...]:
    return tuple(
        FieldSpec(name=name, type=kind, transmitted=sent, ptr_class=at, n_elts=n)
        for name, kind, sent, at, n in rows
    )


@cache
def base_schema() -> Schema:
    nodes = {
        node_type: NodeSpec(
            node_type=node_type,
            name=name,
            description="",
            transmitted=sent,
            variable=variable,
            fields=_fields(rows),
        )
        for node_type, (name, sent, variable, rows) in BASE_NODES.items()
    }
    return Schema(key=BASE_KEY, modeller_version=None, nodes=nodes)


@cache
def envelope_for(version: str | int, infer: bool = False) -> Schema | None:
    try:
        number = int(version)
    except (TypeError, ValueError):
        return None
    family = ENVELOPE_BY_VERSION.get(number)
    if family is None:
        if not infer:
            return None
        family = _inferred_family(number)
        if family is None:
            return None
    return _envelope(f"SCH_{version}", ENVELOPE_FAMILIES[family])


def _inferred_family(version: int) -> int | None:
    known = sorted(ENVELOPE_BY_VERSION)
    below = [v for v in known if v < version]
    above = [v for v in known if v > version]
    if not below or not above:
        return None
    low, high = ENVELOPE_BY_VERSION[below[-1]], ENVELOPE_BY_VERSION[above[0]]
    return low if low == high else None


def _envelope(key: str, family) -> Schema:
    nodes = {
        node_type: NodeSpec(
            node_type=node_type,
            name=_ENVELOPE_NAMES[node_type],
            description="",
            transmitted=True,
            variable=False,
            fields=_fields(rows),
        )
        for node_type, rows in zip((_ASSEMBLY, _INSTANCE, _TRANSFORM), family, strict=True)
    }
    return Schema(key=key, modeller_version=None, nodes=nodes)

