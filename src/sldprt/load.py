"""Decode a part or a Parasolid transmit into a B-rep model."""

from __future__ import annotations

from pathlib import Path

from .container import open_container
from .carve import carve_sections
from .features import _payload, carchive_strings, read_keywords
from .nodes import named_objects, node_types
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


def _terminal_feature_body(container, config_id, root):
    """Read the unique imported feature of the selected configuration.

    A feature-local body is not the finished part if another modeling
    operation follows the import.
    """
    features = _payload(container, f"Contents/Config-{config_id}-ResolvedFeatures")
    local = _payload(container, f"Config-{config_id}-FeatureBodies/LocalBodies")
    if not features or not local:
        return None
    data = features[1]
    nodes = named_objects(data)
    types = node_types(data, nodes, {k.id: k.type for k in read_keywords(container)})
    administrative = {"CommentsFolder", "SensorFolder", "DocsFolder", "DetailCabinet",
                      "SurfaceBodyFolder", "SolidBodyFolder", "EqnFolder", "MaterialFolder",
                      "RefPlane", "OriginProfileFeature"}
    geometry = [n for n in nodes if types.get(n.name) not in administrative]
    if len(geometry) != 1 or types.get(geometry[0].name) not in {"BaseBody", "RefSurface"}:
        return None
    record = local[1]
    if record[:4] != (1).to_bytes(4, "little"):
        return None
    names = carchive_strings(record)
    if not names or names[0][0] != 4 or names[0][1] not in {geometry[0].name, geometry[0].name + "_0"}:
        return None
    sections = carve_sections(record)
    if len(sections) != 1 or not sections[0].data.startswith(BINARY_MAGIC):
        return None
    transmit = read_transmit(sections[0].data)
    if transmit.guise != "transmit":
        return None
    base, current = _schemas(transmit, root)
    decoded, _ = parse_nodes(transmit.data, transmit.body_offset, base, current=current)
    model = build(decoded)
    return model if len(model.bodies) == 1 else None


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
    if not model.bodies and configuration is not None:
        stored = _terminal_feature_body(container, matches[0]["id"], root)
        if stored is not None:
            return stored
    model.from_partition = True
    return model
