"""Decode a part or a Parasolid transmit into a B-rep model."""

from __future__ import annotations

from pathlib import Path

from .container import open_container
from .parasolid import BINARY_MAGIC, TEXT_PREAMBLE, extract_transmits, main_partition, read_transmit
from .properties import read_properties
from .xt import text as xt_text
from .xt.binary import parse_nodes
from .xt.model import Model, build
from .xt.schema import Schema, SchemaError, resolve_schemas

DEFAULT_SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas"


class LoadError(Exception):
    pass


def _schemas(transmit, root: Path) -> tuple[Schema, Schema | None]:
    try:
        return resolve_schemas(root, transmit.base_schema, transmit.schema_key)
    except SchemaError as missing:
        raise LoadError(f"{missing} — see README.md") from missing


def load_model(path: str | Path, *, schema_root: Path | None = None,
               configuration: str | None = None) -> Model:
    """Decode a .SLDPRT, .x_t or .x_b into a model."""
    root = schema_root or DEFAULT_SCHEMA_ROOT
    data = Path(path).read_bytes()
    if data[:2] == BINARY_MAGIC or data.startswith(TEXT_PREAMBLE[:20]):
        if configuration is not None:
            raise LoadError("a Parasolid transmit does not declare SolidWorks configurations")
        transmit = read_transmit(data)
        base, current = _schemas(transmit, root)
        if transmit.form == "text":
            nodes = xt_text.parse_nodes(
                transmit.data[transmit.body_offset :].decode("latin-1"),
                base,
                current=current,
            )
        else:
            nodes, _ = parse_nodes(
                transmit.data, transmit.body_offset, base, current=current
            )
        return build(nodes)

    container = open_container(path)
    if configuration is None:
        transmit = main_partition(container)
    else:
        configs = read_properties(container)["configurations"]
        if not configs:
            raise LoadError(f"no configuration-name mapping was decoded for {configuration!r}")
        matches = [c for c in configs if c["nom"] == configuration]
        if len(matches) != 1:
            names = [c["nom"] for c in configs]
            raise LoadError(f"expected one match for configuration {configuration!r}, found {len(matches)}; decoded names: {names}")
        stream = f"Contents/Config-{matches[0]['id']}-Partition"
        partitions = [t for name, transmits in extract_transmits(container).items()
                      if name.removesuffix("__ZLB") == stream for t in transmits if t.guise == "partition"]
        if len(partitions) != 1:
            raise LoadError(f"configuration {configuration!r} needs one saved partition in {stream}")
        transmit = partitions[0]
    if transmit is None:
        raise LoadError(f"{path}: no Parasolid partition found")
    base, current = _schemas(transmit, root)
    nodes, _ = parse_nodes(
        transmit.data, transmit.body_offset, base, current=current
    )
    model = build(nodes)
    model.from_partition = True
    return model
