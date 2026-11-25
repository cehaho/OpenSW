"""A drawing (``.SLDDRW``), sheet by sheet, as flat geometry.

What a SolidWorks drawing shows is stored already computed: the lines, arcs
and texts of each sheet, and the projected edges of each view. They arrive
as they are written, in millimetres on the XY plane — one folder per sheet,
sheets side by side. It is a drawing to look at and measure, **not** a
TechDraw page: nothing links it back to a model.

Not drawn, and said in the report view: shaded views and pictures.
"""

from __future__ import annotations

import math

import Part

import FreeCAD
from FreeCAD import Vector

from .importer import group, paint, say, warn
from .sldprt import drawing as reader

#: Gap between two sheets laid side by side (mm).
GAP = 20.0
#: Where the baseline of a text sits above the point the file writes, in
#: text heights — the same figure the reader's SVG and DXF writers use.
BASELINE = 0.289
PAPER = (0.6, 0.6, 0.6)


def _rgb(colour):
    """``#rrggbb`` as FreeCAD wants it; black when the file says nothing."""
    if not colour:
        return (0.0, 0.0, 0.0)
    return tuple(int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _polygon(points, dx, closed=False):
    pts = []
    for x, y in points:
        p = Vector(x + dx, y, 0)
        if not pts or p.distanceToPoint(pts[-1]) > 1e-7:
            pts.append(p)
    if closed and len(pts) > 2 and pts[0].distanceToPoint(pts[-1]) > 1e-7:
        pts.append(pts[0])
    return Part.makePolygon(pts) if len(pts) > 1 else None


def _shape(el, dx):
    """The edge or wire of one sheet element, or ``None``."""
    if isinstance(el, reader.Segment):
        a, b = Vector(el.x1 + dx, el.y1, 0), Vector(el.x2 + dx, el.y2, 0)
        return Part.makeLine(a, b) if a.distanceToPoint(b) > 1e-7 else None
    if isinstance(el, reader.Arc):
        if el.r <= 1e-7:
            return None
        centre, up = Vector(el.cx + dx, el.cy, 0), Vector(0, 0, 1)
        if el.cercle:
            return Part.makeCircle(el.r, centre, up)
        a0, a1 = math.degrees(el.a0), math.degrees(el.a1)
        while a1 <= a0:
            a1 += 360.0
        return Part.makeCircle(el.r, centre, up, a0, a1)
    if isinstance(el, reader.Polygone):
        wire = _polygon(el.points, dx, closed=True)
        if wire is None:
            return None
        try:
            return Part.Face(wire)  # an arrowhead is a filled shape
        except Exception:
            return wire
    return None


def _feature(doc, folder, label, shapes, colour, width=None):
    obj = doc.addObject("Part::Feature", "Lines")
    obj.Shape = Part.makeCompound(shapes)
    obj.Label = label
    paint(obj, colour, width)
    if FreeCAD.GuiUp and obj.ViewObject is not None:
        try:
            obj.ViewObject.ShapeColor = tuple(colour)
        except Exception:
            pass
    folder.addObject(obj)
    return obj


def _text(doc, folder, el, dx):
    """A text at its baseline, turned as the file turns it."""
    import Draft

    lift = BASELINE * el.hauteur
    base = Vector(el.x + dx - lift * math.sin(el.angle),
                  el.y + lift * math.cos(el.angle), 0)
    place = FreeCAD.Placement(
        base, FreeCAD.Rotation(Vector(0, 0, 1), math.degrees(el.angle)))
    obj = Draft.make_text(el.texte.split("\n"), placement=place)
    obj.Label = el.texte.replace("\n", " ")[:60] or el.nom
    if FreeCAD.GuiUp and obj.ViewObject is not None:
        view = obj.ViewObject
        for name, value in (("FontSize", el.hauteur), ("TextColor", _rgb(el.couleur)),
                            ("FontName", el.police)):
            if value:
                try:
                    setattr(view, name, value)
                except Exception:
                    pass
    folder.addObject(obj)


def load(path, doc) -> None:
    dessin = reader.read_drawing(path)
    dx = 0.0
    for feuille in dessin.feuilles:
        folder = group(doc, feuille.nom or "Sheet")
        w, h = feuille.largeur, feuille.hauteur
        border = _polygon([(0, 0), (w, 0), (w, h), (0, h)], dx, closed=True)
        if border is not None:
            _feature(doc, folder, "Paper", [border], PAPER, 1.0)

        by_colour: dict[str | None, list] = {}
        texts, skipped = [], 0
        for el in feuille.elements:
            if isinstance(el, reader.Texte):
                texts.append(el)
                continue
            shape = _shape(el, dx)
            if shape is None:
                skipped += not isinstance(el, (reader.Segment, reader.Arc, reader.Polygone))
                continue
            by_colour.setdefault(el.couleur, []).append(shape)
        for colour, shapes in by_colour.items():
            _feature(doc, folder, "Sheet" if colour is None else f"Sheet {colour}",
                     shapes, _rgb(colour))

        shaded = 0
        for vue in feuille.vues:
            shapes = [s for s in (_polygon(a.points, dx) for a in vue.aretes) if s is not None]
            if shapes:
                _feature(doc, folder, vue.nom, shapes, (0.0, 0.0, 0.0))
            shaded += bool(vue.ombree)

        if texts:
            notes = group(doc, "Texts", folder)
            for el in texts:
                try:
                    _text(doc, notes, el, dx)
                except Exception as failure:
                    warn(f"text {el.nom} not placed — {type(failure).__name__}: {failure}")

        say(f"sheet {feuille.nom}: {len(feuille.elements)} elements, "
            f"{len(feuille.vues)} view(s), {len(texts)} text(s)")
        if skipped:
            warn(f"sheet {feuille.nom}: {skipped} picture(s) not drawn")
        if shaded:
            warn(f"sheet {feuille.nom}: {shaded} shaded view(s) drawn by their edges only")
        dx += w + GAP
    for motif, n in dessin.non_lus.items():
        warn(f"not shown: {motif} — {n}")
