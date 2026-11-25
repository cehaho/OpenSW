"""A part (``.SLDPRT``): its exact bodies and its sketches.

The bodies come from the Parasolid partition of the file, written as STEP by
the reader's direct writer and read back by FreeCAD's own importer. The
sketches go in one folder, each labelled with the feature that absorbs it:
SolidWorks features are not replayed, so the FeatureManager tree itself
would only be a list of empty folders, and is not rebuilt.
"""

from __future__ import annotations

import Part

import FreeCAD

from . import sketches
from .importer import group, insert_step, say, warn

#: How far SolidWorks truncates a name in a STEP header; the reader's writer
#: does the same (``MAX_EXPORT_NAME_LENGTH`` in ``tools/convert.py``).
MAX_NAME = 40


def write_step(path, target) -> str:
    """Write the part at *path* as STEP at *target*; say what went in."""
    from .sldprt.load import load_model
    from .sldprt.step import write_step as write

    model = load_model(path)
    report = write(model, target, name=target.stem[:MAX_NAME])
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


def split(doc, imported) -> list:
    """One object per body.

    A multibody part is one STEP product holding several solids, and FreeCAD
    imports that as a single compound: one line in the tree for the whole
    part. Each solid (or surface body) becomes its own ``Part::Feature``
    here, with the colours the import gave its faces, in a ``Bodies``
    folder. A part with one body is left as FreeCAD made it.
    """
    out = []
    for obj in imported:
        if obj.TypeId != "Part::Feature" or obj.Shape.isNull():
            out.append(obj)
            continue
        shape = obj.Shape
        children = shape.SubShapes if shape.ShapeType == "Compound" else []
        # In the FreeCAD window the compound also lists the edges and the
        # vertices of its solids as children of its own; only what has
        # faces is a body. A curve that belongs to no body is kept apart.
        pieces = [c for c in children if c.Faces]
        if len(pieces) < 2 or sum(len(c.Faces) for c in pieces) != len(shape.Faces):
            out.append(obj)
            continue
        owned = {e.hashCode() for c in pieces for e in c.Edges}
        loose = [c for c in children if not c.Faces
                 and any(e.hashCode() not in owned for e in c.Edges)]
        view = obj.ViewObject if FreeCAD.GuiUp else None
        colours = list(view.DiffuseColor) if view is not None else []
        per_face = len(colours) == len(shape.Faces)
        folder = group(doc, "Bodies")
        start = 0
        for rank, piece in enumerate(pieces, start=1):
            body = doc.addObject("Part::Feature", "Body")
            body.Shape = piece
            body.Label = f"{obj.Label} · body {rank}"
            count = len(piece.Faces)
            if view is not None and body.ViewObject is not None:
                try:
                    body.ViewObject.ShapeColor = view.ShapeColor
                    if per_face:
                        body.ViewObject.DiffuseColor = colours[start:start + count]
                except Exception:
                    pass
            start += count
            folder.addObject(body)
            out.append(body)
        if loose:
            extra = doc.addObject("Part::Feature", "Curves")
            extra.Shape = Part.makeCompound(loose)
            extra.Label = f"{obj.Label} · curves"
            folder.addObject(extra)
            out.append(extra)
        doc.removeObject(obj.Name)
    return out


def load(path, doc) -> None:
    try:
        bodies = insert_step(lambda target: write_step(path, target), path.stem, doc)
        bodies = split(doc, bodies)
    except Exception as failure:
        bodies = []
        warn(f"{path.name}: no geometry — {type(failure).__name__}: {failure}")
    try:
        reading = sketches.read(path)
    except Exception as failure:
        warn(f"{path.name}: sketches not read — {type(failure).__name__}: {failure}")
        if not bodies:
            raise
        return
    made = sketches.build(doc, reading)
    if made:
        folder = group(doc, "Sketches")
        for objects in made.values():
            for obj in objects:
                folder.addObject(obj)
    say(f"{len(made)} sketches ({sum(e['3d'] for e in reading.sketches)} in 3D)")
