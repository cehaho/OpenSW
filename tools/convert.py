#!/usr/bin/env python3
"""Convert a .SLDPRT to STEP and to Parasolid, without SolidWorks."""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sldprt.container import open_container
from sldprt.parasolid import is_partition_stream, main_partition, to_part_transmit
from sldprt.xt.binary import parse_nodes
from sldprt.xt.schema import SchemaError, resolve_schemas

SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schemas"
MAX_EXPORT_NAME_LENGTH = 40


def _decode(path: Path):
    transmit = main_partition(open_container(path, is_partition_stream))
    if transmit is None:
        raise SystemExit(f"{path.name}: no Parasolid partition found")

    try:
        base, current = resolve_schemas(
            SCHEMA_ROOT, transmit.base_schema, transmit.schema_key
        )
    except SchemaError as missing:
        raise SystemExit(f"{missing} — see README.md") from missing

    header: dict = {}
    nodes, layouts = parse_nodes(
        transmit.data, transmit.body_offset, base, current=current, header=header
    )
    return transmit, nodes, layouts, base, current, header


def parasolid(path: Path, destination: Path) -> str:
    transmit, nodes, layouts, base, current, header = _decode(path)

    text = to_part_transmit(
        transmit,
        nodes,
        base,
        layouts,
        current,
        key=destination.stem[:MAX_EXPORT_NAME_LENGTH],
        max_node_types=header.get("max_node_types"),
    )
    partial = destination.with_name(destination.name + ".part")
    partial.write_text(text, encoding="latin-1")
    os.replace(partial, destination)
    bodies = sum(1 for n in nodes if n.name == "BODY")
    return f"{len(text) / 1e6:.1f} MB, {bodies} bodies"


def step(path: Path, destination: Path) -> str:
    from sldprt.load import load_model
    from sldprt.step import write_step

    model = load_model(path)
    report = write_step(model, destination, name=destination.stem[:MAX_EXPORT_NAME_LENGTH])
    told = f"{report.bodies} bodies, {report.faces} faces"
    if report.skipped_bodies:
        told += f", {report.skipped_bodies} bodies left out"
        which = report.dropped_bodies()
        if which:
            told += f" ({which})"
        why = report.refusals()
        if why:
            told += f" — {why}"
    return told


def libre(destination: Path, *, force: bool) -> bool:
    if force or not destination.exists():
        return True
    print(
        f"{destination}: already exists, nothing written. "
        f"Use another folder with -o, or pass --force.",
        file=sys.stderr,
    )
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", type=Path, nargs="+")
    parser.add_argument("-o", "--out", type=Path, help="output folder")
    parser.add_argument("--step", action="store_true", help="STEP only")
    parser.add_argument("--parasolid", action="store_true", help="Parasolid only")
    parser.add_argument("--force", action="store_true", help="overwrite existing output")
    args = parser.parse_args()

    both = not (args.step or args.parasolid)
    failures = 0
    for path in args.paths:
        if not path.exists():
            print(f"{path}: not found", file=sys.stderr)
            failures += 1
            continue
        already_parasolid = path.suffix.lower() in (".x_t", ".x_b")
        folder = args.out or path.parent
        folder.mkdir(parents=True, exist_ok=True)
        for wanted, suffix, write in (
            ((both or args.parasolid) and not already_parasolid, ".x_t", parasolid),
            (both or args.step, ".STEP", step),
        ):
            if not wanted:
                continue
            destination = folder / (path.stem + suffix)
            if not libre(destination, force=args.force):
                failures += 1
                continue
            started = time.monotonic()
            try:
                told = write(path, destination)
            except Exception as error:
                print(f"{path.name} → {suffix}: {type(error).__name__}: {error}", file=sys.stderr)
                failures += 1
                continue
            print(f"{destination}  {told}  ({time.monotonic() - started:.1f} s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
