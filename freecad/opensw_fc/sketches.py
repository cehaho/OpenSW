"""The sketches of a part, as FreeCAD objects.

The reader (:mod:`.sldprt.sketch_view`) hands each sketch segment to a
"drawing" object through four calls — ``line``, ``arc``, ``ellipse``,
``spline`` — the same interface its own STEP and DXF writers implement. The
two classes here implement it for FreeCAD:

:class:`Edges`
    exact Part edges, millimetres, in whatever frame the reader places them;
:class:`Flat`
    Sketcher geometry in the sketch's own plane.

A planar sketch becomes a ``Sketcher::SketchObject`` placed on its plane,
**without constraints**: the file is read for the resolved positions of its
points, not for the constraints that produced them. A 3D sketch — FreeCAD
has no such object — becomes a ``Part::Feature`` of edges.
"""

from __future__ import annotations

import itertools
import math

import Part

import FreeCAD
from FreeCAD import Vector

from .importer import paint, warn
from .sldprt import sketch_view as sv

MM = 1000.0
#: Below this length (mm) an edge is a point, and OpenCASCADE refuses it.
TINY = 1e-7

CONSTRUCTION = "construction"
GEOMETRY = "geometry"
#: SolidWorks' own sketch grey-blue, and a paler one for construction lines.
COLOURS = {GEOMETRY: (0.25, 0.35, 0.85), CONSTRUCTION: (0.55, 0.6, 0.75)}


def _v(p) -> Vector:
    return Vector(p[0] * MM, p[1] * MM, (p[2] if len(p) > 2 else 0.0) * MM)


def _knots(knots) -> tuple[list[float], list[int]]:
    """A flat knot vector as distinct knots and their multiplicities."""
    distinct, mults = [], []
    for k in knots:
        if distinct and abs(k - distinct[-1]) < 1e-15:
            mults[-1] += 1
        else:
            distinct.append(k)
            mults.append(1)
    return distinct, mults


def _bspline(degree, knots, poles, t0, t1) -> Part.BSplineCurve:
    """The NURBS as written in the file, cut to ``[t0, t1]``."""
    distinct, mults = _knots(knots)
    curve = Part.BSplineCurve()
    weights = [q[3] if len(q) > 3 else 1.0 for q in poles]
    curve.buildFromPolesMultsKnots(
        [_v(q) for q in poles], mults, distinct, False, degree, weights)
    lo, hi = curve.FirstParameter, curve.LastParameter
    t0, t1 = max(t0, lo), min(t1, hi)
    if t1 - t0 > 1e-12 and (t0 - lo > 1e-12 or hi - t1 > 1e-12):
        curve.segment(t0, t1)
    return curve


class Edges:
    """Sketch segments as Part edges, by layer."""

    def __init__(self):
        self.layers: dict[str, list] = {}

    def _add(self, layer, edge) -> None:
        self.layers.setdefault(layer, []).append(edge)

    def line(self, layer, a, b):
        a, b = _v(a), _v(b)
        if a.distanceToPoint(b) > TINY:
            self._add(layer, Part.makeLine(a, b))

    def arc(self, layer, centre, radius, a0, a1, normal=(0.0, 0.0, 1.0)):
        if radius * MM < TINY:
            return
        if a1 is None:
            self._add(layer, Part.Circle(_v(centre), Vector(*normal), radius * MM).toShape())
            return
        ax, ay = sv._ocs(normal)
        sweep = (a1 - a0) % 360.0 or 360.0

        def point(angle):
            t = math.radians(angle)
            return _v([centre[i] + radius * (math.cos(t) * ax[i] + math.sin(t) * ay[i])
                       for i in range(3)])

        self._add(layer, Part.Arc(point(a0), point(a0 + sweep / 2), point(a0 + sweep)).toShape())

    def ellipse(self, layer, centre, a, b, axis, normal, t0, t1):
        axis = sv._unit(axis)
        y = sv._unit(sv._cross(normal, axis))

        def point(t):
            return [centre[i] + a * math.cos(t) * axis[i] + b * math.sin(t) * y[i]
                    for i in range(3)]

        if a < b:  # OpenCASCADE wants the major axis first: follow the points
            n = 96
            self.polyline(layer, [point(t0 + (t1 - t0) * k / n) for k in range(n + 1)])
            return
        curve = Part.Ellipse(_v(point(0.0)), _v(point(math.pi / 2)), _v(centre))
        if t1 - t0 > 2 * math.pi - 1e-9:
            self._add(layer, curve.toShape())
        else:
            self._add(layer, curve.toShape(t0, t1))

    def spline(self, layer, degree, knots, poles, t0, t1):
        self._add(layer, _bspline(degree, knots, poles, t0, t1).toShape())

    def polyline(self, layer, pts):
        pts = [_v(p) for p in pts]
        kept = pts[:1]
        for p in pts[1:]:
            if p.distanceToPoint(kept[-1]) > TINY:
                kept.append(p)
        if len(kept) > 1:
            self._add(layer, Part.makePolygon(kept))


class Flat:
    """Sketch segments as Sketcher geometry, in the plane of the sketch."""

    def __init__(self):
        self.layers: dict[str, list] = {}

    def _add(self, layer, geometry) -> None:
        self.layers.setdefault(layer, []).append(geometry)

    def line(self, layer, a, b):
        a, b = _v(a), _v(b)
        if a.distanceToPoint(b) > TINY:
            self._add(layer, Part.LineSegment(a, b))

    def arc(self, layer, centre, radius, a0, a1, normal=(0.0, 0.0, 1.0)):
        if radius * MM < TINY:
            return
        circle = Part.Circle(_v(centre), Vector(0, 0, 1), radius * MM)
        if a1 is None:
            self._add(layer, circle)
            return
        sweep = (a1 - a0) % 360.0 or 360.0
        start = math.radians(a0)
        self._add(layer, Part.ArcOfCircle(circle, start, start + math.radians(sweep)))

    def ellipse(self, layer, centre, a, b, axis, normal, t0, t1):
        axis = sv._unit(axis)
        y = sv._unit(sv._cross(normal, axis))

        def point(t):
            return [centre[i] + a * math.cos(t) * axis[i] + b * math.sin(t) * y[i]
                    for i in range(3)]

        if a < b:
            n = 96
            self.polyline(layer, [point(t0 + (t1 - t0) * k / n) for k in range(n + 1)])
            return
        curve = Part.Ellipse(_v(point(0.0)), _v(point(math.pi / 2)), _v(centre))
        if t1 - t0 > 2 * math.pi - 1e-9:
            self._add(layer, curve)
        else:
            self._add(layer, Part.ArcOfEllipse(curve, t0, t1))

    def spline(self, layer, degree, knots, poles, t0, t1):
        self._add(layer, _bspline(degree, knots, poles, t0, t1))

    def polyline(self, layer, pts):
        pts = [_v(p) for p in pts]
        for a, b in itertools.pairwise(pts):
            if a.distanceToPoint(b) > TINY:
                self._add(layer, Part.LineSegment(a, b))


# --------------------------------------------------------------------------


def read(path):
    """The sketches of the part at *path* — the reader's own ``Reading``."""
    return sv.read(path)


def label(sketch) -> str:
    """A sketch's name, with the feature that absorbs it when there is one."""
    feature = sketch.get("feature")
    return f"{sketch['name']} ({feature})" if feature else sketch["name"]


def _layer(_e, s) -> str:
    return CONSTRUCTION if s["construction"] else GEOMETRY


def edges(reading, sketch) -> Edges:
    """One sketch as edges in the model frame (millimetres)."""
    out = Edges()
    sv._walk(sv.Reading(reading.name, [sketch], reading.thirds), out, _layer)
    return out


def _placement(m2s):
    """The plane of a sketch as a placement, or ``None`` when its frame is
    not one FreeCAD can hold (mirrored, or not orthonormal)."""
    frame = sv.Frame(m2s)
    x, y, z = (Vector(*a) for a in frame.axes)
    if (abs(x.Length - 1) > 1e-6 or abs(y.Length - 1) > 1e-6
            or abs(x.dot(y)) > 1e-6 or x.cross(y).distanceToPoint(z) > 1e-6):
        return None
    o = frame.origin
    m = FreeCAD.Matrix(
        x.x, y.x, z.x, o[0] * MM,
        x.y, y.y, z.y, o[1] * MM,
        x.z, y.z, z.z, o[2] * MM,
        0, 0, 0, 1)
    return FreeCAD.Placement(m)


def _sketcher(doc, sketch):
    """A planar sketch as a Sketcher object, or ``None`` if it cannot be."""
    if sketch["3d"] or not sketch["frame"]:
        return None
    placement = _placement(sketch["frame"])
    if placement is None:
        return None
    flat = Flat()
    for s in sketch["segments"]:
        sv.draw_segment(flat, _layer(sketch, s), s, sv.IDENTITY_FRAME)
    if not flat.layers:
        return None
    obj = doc.addObject("Sketcher::SketchObject", "Sketch")
    try:
        obj.Placement = placement
        for layer, geometry in flat.layers.items():
            obj.addGeometry(geometry, layer == CONSTRUCTION)
    except Exception as failure:
        doc.removeObject(obj.Name)
        warn(f"{sketch['name']}: not a Sketcher sketch ({failure}), kept as edges")
        return None
    return obj


def _features(doc, reading, sketch) -> list:
    """A sketch as plain edges: one object for its geometry, one for its
    construction lines."""
    out = []
    for layer, shapes in edges(reading, sketch).layers.items():
        obj = doc.addObject("Part::Feature", "Sketch")
        obj.Shape = Part.makeCompound(shapes)
        obj.Label = label(sketch) + (" [construction]" if layer == CONSTRUCTION else "")
        paint(obj, COLOURS[layer], 2.0 if layer == GEOMETRY else 1.0)
        out.append(obj)
    return out


def build(doc, reading) -> dict[str, list]:
    """Every sketch of *reading* as document objects, by sketch name."""
    made: dict[str, list] = {}
    for sketch in reading.sketches:
        try:
            obj = _sketcher(doc, sketch)
            if obj is not None:
                obj.Label = label(sketch)
                made[sketch["name"]] = [obj]
            else:
                made[sketch["name"]] = _features(doc, reading, sketch)
        except Exception as failure:
            warn(f"{sketch['name']}: sketch not built — {type(failure).__name__}: {failure}")
    return made


def compound(reading):
    """All the sketches of a part as one compound of edges in the part's
    frame, or ``None`` — what an assembly places once per instance."""
    shapes = []
    for sketch in reading.sketches:
        try:
            for layer, found in edges(reading, sketch).layers.items():
                if layer == GEOMETRY:
                    shapes.extend(found)
        except Exception as failure:
            warn(f"{sketch['name']}: sketch not built — {type(failure).__name__}: {failure}")
    return Part.makeCompound(shapes) if shapes else None
