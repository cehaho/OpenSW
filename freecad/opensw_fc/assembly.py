"""An assembly (``.SLDASM``).

An assembly file does not hold the exact geometry of its parts — that stays
in their ``.SLDPRT`` files. So:

* each part found **next to the assembly** (same folder, or one below) is
  read exactly, and the assembly arrives as SolidWorks would export it to
  STEP: one product per part, one placed instance per component;
* a component whose part is not found arrives as the **display mesh** the
  assembly keeps of it — what SolidWorks itself shows before the parts are
  resolved. It is a mesh, not a solid, and the report view says which.

Suppressed and hidden components are left out, as in SolidWorks' own export.
Mates are not converted.
"""

from __future__ import annotations

from pathlib import Path

import FreeCAD

from . import sketches
from .importer import group, insert_step, paint, say, warn
from .sldprt.assembly import read_assembly

MM = 1000.0


def neighbours(path: Path) -> dict[str, Path]:
    """The part files around the assembly, by lower-cased file name: its own
    folder first, then the folders directly below it."""
    found: dict[str, Path] = {}
    folders = [path.parent, *sorted(p for p in path.parent.iterdir() if p.is_dir())]
    for folder in folders:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if (entry.is_file() and name.lower().endswith(".sldprt")
                    and not name.startswith("~$")):
                found.setdefault(name.lower(), entry)
    return found


def placement(component) -> FreeCAD.Placement:
    """Where the assembly puts a component: ``p·R + t``, metres to mm."""
    m = component.matrice
    return FreeCAD.Placement(FreeCAD.Matrix(
        m[0], m[3], m[6], m[9] * MM,
        m[1], m[4], m[7], m[10] * MM,
        m[2], m[5], m[8], m[11] * MM,
        0, 0, 0, 1))


def load(path, doc) -> None:
    asm = read_assembly(path)
    visible = [c for c in asm.composants if not (c.cache or c.supprime)]
    say(f"{len(asm.composants)} components, {len(visible)} shown")
    for motif, n in asm.non_lus.items():
        warn(f"not shown: {motif} — {n}")

    files = neighbours(path)
    wanted = {c.fichier.lower(): c.fichier for c in visible}
    exact: set[str] = set()
    if any(key in files for key in wanted):
        exact = _exact(path, doc, asm, files, wanted)
    meshed = [c for c in visible if c.fichier.lower() not in exact]
    if meshed:
        _meshes(doc, meshed)
        names = sorted({c.fichier for c in meshed})
        warn(f"{len(meshed)} component(s) shown by their display mesh, not exact — "
             f"put these parts next to the assembly: {', '.join(names)}")
    _sketches(doc, visible, files, exact)


def _exact(path, doc, asm, files, wanted) -> set[str]:
    """Import the assembly as STEP from the parts found. Returns the
    lower-cased names of the parts that went in."""
    from .sldprt.load import load_model
    from .sldprt.step_assembly import write_step_assembly

    models = {}
    for key, name in wanted.items():
        if key not in files:
            continue
        try:
            models[name] = load_model(files[key])
        except Exception as failure:
            warn(f"{name}: unreadable — {type(failure).__name__}: {failure}")
    if not models:
        return set()
    written: set[str] = set()

    def write(target) -> str:
        report = write_step_assembly(asm, models, target, name=path.stem)
        for name, why in report.refused.items():
            warn(f"{name}: no body written — {why}")
        written.update(name.lower() for name in report.parts)
        return (f"exact geometry: {report.instances} instance(s) of "
                f"{len(report.parts)} part(s)")

    try:
        insert_step(write, path.stem, doc)
    except Exception as failure:
        warn(f"no exact assembly — {type(failure).__name__}: {failure}")
        return set()
    return written


def _meshes(doc, components) -> None:
    """Components as the display mesh the assembly keeps, placed."""
    import Mesh

    folder = group(doc, "Display meshes (not exact)")
    for c in components:
        triangles = [
            tuple(tuple(MM * v for v in p) for p in tri)
            for face in c.faces for tri in face
        ]
        if not triangles:
            warn(f"{c.nom}: no display mesh in the assembly")
            continue
        obj = doc.addObject("Mesh::Feature", "Component")
        obj.Mesh = Mesh.Mesh(triangles)
        obj.Placement = placement(c)
        obj.Label = c.nom
        folder.addObject(obj)


def _sketches(doc, components, files, exact) -> None:
    """The sketches of each part read exactly, placed once per instance."""
    shapes = {}
    for key in sorted({c.fichier.lower() for c in components} & exact):
        try:
            shapes[key] = sketches.compound(sketches.read(files[key]))
        except Exception as failure:
            warn(f"{files[key].name}: sketches not read — "
                 f"{type(failure).__name__}: {failure}")
    placed = [(c, shapes[c.fichier.lower()]) for c in components
              if shapes.get(c.fichier.lower()) is not None]
    if not placed:
        return
    folder = group(doc, "Sketches")
    for c, shape in placed:
        obj = doc.addObject("Part::Feature", "Sketches")
        obj.Shape = shape
        obj.Placement = placement(c)
        obj.Label = f"{c.nom} · sketches"
        paint(obj, sketches.COLOURS[sketches.GEOMETRY], 2.0)
        folder.addObject(obj)
    say(f"sketches of {len(shapes)} part(s) placed on {len(placed)} instance(s)")
