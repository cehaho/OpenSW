"""One entry point for the three file kinds, and the helpers they share."""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import FreeCAD


def say(text: str) -> None:
    """A line in FreeCAD's report view."""
    FreeCAD.Console.PrintMessage(f"OpenSW: {text}\n")


def warn(text: str) -> None:
    FreeCAD.Console.PrintWarning(f"OpenSW: {text}\n")


def load(filename: str, doc) -> None:
    """Read *filename* into *doc*, by its extension."""
    path = Path(filename)
    suffix = path.suffix.lower()
    start = time.monotonic()
    if suffix == ".sldprt":
        from . import part

        part.load(path, doc)
    elif suffix == ".sldasm":
        from . import assembly

        assembly.load(path, doc)
    elif suffix == ".slddrw":
        from . import drawing

        drawing.load(path, doc)
    else:
        raise ValueError(f"not a SolidWorks file: {path.name}")
    doc.recompute()
    say(f"{path.name} read in {time.monotonic() - start:.1f} s")
    if FreeCAD.GuiUp:
        import FreeCADGui

        try:
            FreeCADGui.SendMsgToActiveView("ViewFit")
        except Exception:
            pass


def insert_step(write, stem: str, doc) -> list:
    """Have *write(path)* write a STEP file, then let FreeCAD read it.

    The file only lives in a temporary folder for the time of the import.
    Returns the objects the import added to *doc*.
    """
    folder = Path(tempfile.mkdtemp(prefix="opensw-"))
    try:
        target = folder / (stem + ".step")
        told = write(target)
        if told:
            say(told)
        before = {o.Name for o in doc.Objects}
        if FreeCAD.GuiUp:
            import ImportGui as reader  # colours live on the view objects
        else:
            import Import as reader
        reader.insert(str(target), doc.Name)
        return [o for o in doc.Objects if o.Name not in before]
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def group(doc, label: str, parent=None):
    """A folder of the model tree, labelled as the file names it."""
    g = doc.addObject("App::DocumentObjectGroup", "Group")
    g.Label = label
    if parent is not None:
        parent.addObject(g)
    return g


def paint(obj, colour, width: float | None = None) -> None:
    """Line colour (``(r, g, b)`` in 0..1) — only when there is a view."""
    if not FreeCAD.GuiUp or obj.ViewObject is None:
        return
    view = obj.ViewObject
    try:
        if colour is not None:
            rgb = tuple(float(c) for c in colour)
            view.LineColor = rgb
            view.PointColor = rgb
        if width is not None:
            view.LineWidth = width
    except Exception:
        pass
