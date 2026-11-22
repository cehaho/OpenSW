"""A part's sketches placed in the model, for the page's 3D view.

    from sldprt.sketch_view import read, strokes, write_step

    reading = read("part.SLDPRT")
    strokes(reading)                    # [{nom, 3d, fonction, trait, construction}]
    write_step(reading, "part.sketches.step", "part — sketches")

Everything runs in the page's process, in pure Python, under Pyodide too (no
OpenCASCADE, no subprocess). The tree nodes, their type (MFC class, ``KeyWords`` otherwise), the
sketches (:mod:`sldprt.sketches`, :mod:`sldprt.sketch_curves` for a planar
sketch, :mod:`sldprt.sketches3d` for a 3D sketch), the folder ends and the
hierarchy: an absorbed sketch is the child of its feature, a shared-sketch
instance (« 3DSketch11<3> ») takes its original's geometry
(:mod:`sldprt.instances`). Each sketch is placed by its ``model_to_sketch``
frame. For the view, ellipses and splines become polylines within
:data:`SAG`; the STEP writes them exactly.

Only the sketches are read: dimensions, planes, feature definitions are
not. Stroke units: millimetres.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from pathlib import Path

from .container import open_container
from .features import END_TAG, document_version, read_keywords, tree_stream
from .instances import instances as read_instances
from .nodes import (
    _regions,
    closes_folder,
    folder_end,
    named_objects,
    node_types,
    read_children,
)
from .sketch_curves import segments as curve_segments
from .sketches import SKETCHES_SINCE, read_sketches
from .sketches3d import IDENTITY, sketch3d

MM = 1000.0
#: Largest gap (mm) between a polyline and the ellipse or spline it follows.
SAG = 0.001
#: The node types that carry a planar sketch, and a 3D sketch.
PLANAR, SPATIAL = "ProfileFeature", "3DProfileFeature"
SKETCH_TYPE = PLANAR
LIBRARY_TYPE = "LibraryFeature"
#: Allowed gap (mm) between an approximated B-spline and the points it follows.
TOLERANCE_MM = 1e-4


# --------------------------------------------------------------------------
# The tree, as far as sketches need it.

#: The API type from the type ``KeyWords`` publishes, which is **localised**.
#: Here it only tells folders, library features and sketches apart, but it is
#: kept whole: a missing type would change a node's type, and with it the
#: reader that fills its segments.
API_TYPES = {
    "Ambiante": "AmbientLight", "Annotations": "DetailCabinet", "Axes": "RefAxisFtrFolder",
    "Balayage": "Sweep", "Capteurs": "SensorFolder", "Combiner": "CombineBodies",
    "Commentaires": "CommentsFolder", "Congé": "Fillet", "Coque": "Shell",
    "Corps-Déplacer/Copier": "MoveCopyBody", "Corps-Supprimer/Garder ": "DeleteBody",
    "Courbe": "RefCurve", "Coïncidente": "MateCoordinate", "Directionnelle": "DirectionLight",
    "Dossier": "FtrFolder", "Déplacer la face": "MoveFace", "Embouti": "Punch",
    "Enlèv. mat.-Surface": "SurfCut", "Enlèvement de matière-épaissir": "ThickenCut",
    "Ensembles de sélections": "SelectionSetFolder", "Epaissir": "Thicken",
    "Equations": "EqnFolder", "Esquisses": "ProfileFtrFolder", "Favoris": "FavoriteFolder",
    "Feature": "LibraryFeature", "Fractionner": "Split", "Historique": "HistoryFolder",
    "Lissage": "Blend", "Lumières et caméras": "EnvFolder", "Marquages": "InkMarkupFolder",
    "Notes": "NotesAreaFtrFolder", "Plan": "RefPlane", "Plans": "RefPlaneFtrFolder",
    "Point": "RefPointFtrFolder", "Remplacer la face": "ReplaceFace", "SupprimerFace": "DelFace",
    "Surface-Décalage": "OffsetRefSurface", "Surface-Extrusion": "ExtruRefSurface",
    "Surface-Prolonger": "ExtendRefSurface", "Surface-Restreindre": "TrimRefSurface",
    "Surface-Rétablir": "UnTrimRefSurf", "Symétrie": "MirrorSolid",
    "Système de coordonnées": "CoordSys", "Extrusion": "ICE", "Origine": "OriginProfileFeature",
    "Origin": "OriginProfileFeature", "Ambient": "AmbientLight", "Body-Move/Copy": "MoveCopyBody",
    "Boundary-Surface": "NetBlend",
    "Chamfer": "Chamfer", "Combine": "CombineBodies", "Comments": "CommentsFolder",
    "Concentric": "MateConcentric", "Design Binder": "DocsFolder",
    "Directional": "DirectionLight", "Distance": "MateDistanceDim",
    "Favorites": "FavoriteFolder", "Fillet": "Fillet", "Folder": "FtrFolder",
    "History": "HistoryFolder", "Lights and Cameras": "EnvFolder", "Markups": "InkMarkupFolder",
    "Mirror": "MirrorSolid", "Move Face": "MoveFace", "Plane": "RefPlane",
    "Planes": "RefPlaneFtrFolder", "Revolve": "Revolution",
    "SOLIDWORKS Materials": "MaterialFolder", "Selection Sets": "SelectionSetFolder",
    "Sensors": "SensorFolder", "Sketches": "ProfileFtrFolder", "Split": "Split",
    "Surface-Extend": "ExtendRefSurface", "Surface-Extrude": "ExtruRefSurface",
    "Surface-Offset": "OffsetRefSurface", "Surface-Revolve": "RevolvRefSurf",
    "SurfaceCut": "SurfCut",
}
#: API types that share an MFC class with another one.
VARIANTS = {"Thicken": {"ThickenCut"}}


def _type(klass: str | None, keyword: str | None) -> str | None:
    """A node's type: its MFC class, or ``KeyWords`` when it is more precise."""
    if klass is not None and keyword in VARIANTS.get(klass, ()):
        return keyword
    return klass or keyword


def _segment(segment) -> dict:
    """A planar sketch segment (:class:`sldprt.sketches.Segment`) as a dict
    (see :func:`sldprt.sketch_curves.segments`)."""
    def point(p):
        return None if p is None else {"x": p[0], "y": p[1], "z": 0.0}

    arc = segment.kind == "arc"
    return {
        "type": 1 if arc else 0,
        "id": [None, segment.ident],
        "construction": segment.construction,
        "start": point(segment.start),
        "end": point(segment.end),
        "centre": point(segment.centre),
        "radius": math.dist(segment.start, segment.centre) if arc and segment.centre else None,
        "rotation": segment.sense,
        "circle": bool(arc and segment.start == segment.end),
        "face": segment.face,
    }


def _segment3d(s) -> dict:
    """A 3D sketch segment (:class:`sldprt.sketches3d.Segment3D`) as a dict."""
    spline = s.kind == "spline"
    arc = s.kind == "arc"
    out = {
        "type": 3 if spline else 1 if arc else 0,
        "id": [None, s.ident],
        "construction": s.construction,
        # A spline's ends are not kept.
        "start": None if spline else _point3d(s.start),
        "end": None if spline else _point3d(s.end),
        "centre": _point3d(s.centre),
        "radius": math.dist(s.start, s.centre) if arc else None,
        "rotation": s.sense if arc else None,
        "circle": bool(arc and s.start == s.end) if arc else None,
        "face": s.face,
    }
    if spline and s.spline is not None:
        out["bspline"] = {"degree": s.spline.degree, "knots": list(s.spline.knots),
                          "poles": [list(p) for p in s.spline.poles], "periodic": False}
    return out


def _point3d(p) -> dict | None:
    return None if p is None else {"x": p[0], "y": p[1], "z": p[2]}


def _complete(data: bytes, node: dict, named, end: int, recent: bool) -> None:
    """Fill a sketch node's segments: ellipses and splines for a planar
    sketch, the whole sketch for a 3D one. A reader that fails leaves the
    node as it was."""
    try:
        if node["type"] == PLANAR:
            sketch = node.get("sketch")
            if not sketch:
                return
            found = curve_segments(data, named.offset, end)
            if not found:
                return
            node["sketch"] = {**sketch, "segments": found}
        elif node["type"] == SPATIAL and recent:
            sk = sketch3d(data, named.offset, end)
            node["sketch"] = {"model_to_sketch": list(IDENTITY),
                              "segments": [_segment3d(s) for s in sk.segments]}
    except Exception:
        return


def _close_folders(data: bytes, order: list[str], regions: dict[str, tuple[int, int]]) -> None:
    """Move each folder-end marker after the folder's last member."""
    for name in [n for n in order if n.endswith(END_TAG)]:
        i = order.index(name)
        ref = folder_end(data, *regions[name])
        if ref is None:
            continue
        for j in range(i, len(order)):
            if closes_folder(data, regions[order[j]][1], ref):
                order.insert(j, order.pop(i))
                break


def tree(container) -> list[dict]:
    """The part's top-level nodes, each ``{name, type, children}`` and
    ``sketch`` for a sketch: the tree hierarchy, reduced to what sketches
    need."""
    found = tree_stream(container)
    if not found:
        raise ValueError("no tree stream")
    data = found[1]
    keywords = read_keywords(container)
    sketch_ids = {k.id for k in keywords if k.tag == "Sketch"}
    kw_types = {k.id: API_TYPES.get(k.type) for k in keywords}
    classes = node_types(data, named_objects(data), {k.id: k.type for k in keywords})
    sketches = read_sketches(container)
    resolved: dict[str, list[bool]] = {}
    children = read_children(container, resolved)
    shared = read_instances(data, sketch_ids, children)
    recent = (document_version(container) or 0) >= SKETCHES_SINCE

    nodes: dict[str, dict] = {}
    order: list[str] = []
    regions: dict[str, tuple[int, int]] = {}
    for named, end in _regions(data, named_objects(data)):
        regions[named.name] = (named.offset, end)
        node = {"name": named.name,
                "type": _type(classes.get(named.name), kw_types.get(named.ident)),
                "children": []}
        if named.ident in sketch_ids:
            node["type"] = node["type"] or SKETCH_TYPE
            sketch = sketches.get(named.name)
            if sketch is not None:
                node["sketch"] = {"model_to_sketch": sketch.plane,
                                  "segments": [_segment(s) for s in sketch.segments]}
        _complete(data, node, named, end, recent)
        nodes[named.name] = node
        order.append(named.name)
    _close_folders(data, order, regions)

    # The hierarchy: an absorbed sketch becomes its feature's child and
    # leaves the top level, as in the SolidWorks API.
    absorbed: set[str] = set()
    for rank, name in enumerate(order):
        if nodes[name]["type"] == LIBRARY_TYPE and children.get(name):
            # A library feature points to its children by back-reference: the
            # top-level nodes written just before it, or those the archive
            # index anchors resolve.
            count = len(children[name])
            listed = children[name]
            if (all(resolved.get(name) or [False])
                    and all(n in nodes and n not in absorbed for n in listed)):
                before = list(listed)
            else:
                before = [n for n in order[:rank] if n not in absorbed][-count:]
            nodes[name]["children"] = [nodes[n] for n in before]
            absorbed.update(before)
            continue
        for child in children.get(name) or []:
            if child is None:
                continue
            base = child.split("<", 1)[0]
            instance = shared.get(child)
            if instance is not None and instance.origin:
                base = instance.origin
            if child not in nodes and "<" in child and base in nodes and "sketch" in nodes[base]:
                # A shared-sketch instance (`Sketch7<3>`): its original's geometry.
                nodes[child] = {"name": child, "type": SKETCH_TYPE, "children": [],
                                "sketch": nodes[base]["sketch"]}
            elif (instance is not None and instance.origin and child in nodes
                  and "sketch" in nodes[child] and base in nodes and "sketch" in nodes[base]):
                # An instance with its own region writes no geometry there,
                # only the reference to its original: take the original's.
                nodes[child]["sketch"] = nodes[base]["sketch"]
            if child in nodes and child != name:
                nodes[name]["children"].append(nodes[child])
                absorbed.add(child)
    return [nodes[n] for n in order if n not in absorbed]


def sketches(nodes: list[dict]) -> list[dict]:
    """Each sketch of the tree, once: ``name``, ``3d``, ``frame``,
    ``feature`` and ``segments``."""
    out, seen, spatial = [], set(), set()

    def types(items):
        for n in items:
            if n.get("type") == SPATIAL:
                spatial.add(n["name"])
            types(n.get("children") or [])

    def walk(items, parent=None):
        for n in items:
            sk = n.get("sketch")
            if sk and n["name"] not in seen and sk.get("segments"):
                seen.add(n["name"])
                # An instance (« 3DSketch11<3> ») has its original's geometry.
                out.append({"name": n["name"], "3d": n["name"].split("<", 1)[0] in spatial,
                            "frame": sk.get("model_to_sketch"),
                            # The feature that absorbs it, or None: a free sketch.
                            "feature": parent,
                            "segments": [s for s in sk["segments"]
                                         if s.get("type") in (0, 1, 2, 3)]})
            folder = str(n.get("type") or "").endswith("Folder")
            walk(n.get("children") or [], parent if folder or sk else n["name"])

    types(nodes)
    walk(nodes)
    return [e for e in out if e["segments"]]


def _xy(p) -> tuple[float, float, float] | None:
    return None if p is None else (p["x"], p["y"], p.get("z", 0.0))


# --------------------------------------------------------------------------
# Geometry: curves → points, sketch frame → model.


def _de_boor(degree: int, knots: list[float], poles: list[list[float]], t: float
             ) -> tuple[float, ...]:
    """A point of the NURBS (poles ``[x, y, z]`` or ``[x, y, z, w]``)."""
    n = len(poles)
    k = max(degree, min(n - 1, next((i - 1 for i in range(degree + 1, n + 1) if t < knots[i]),
                                    n - 1)))
    d = []
    for j in range(k - degree, k + 1):
        p = poles[j]
        w = p[3] if len(p) > 3 else 1.0
        d.append([p[0] * w, p[1] * w, p[2] * w, w])
    for r in range(1, degree + 1):
        for j in range(degree, r - 1, -1):
            i = j + k - degree
            den = knots[i + degree - r + 1] - knots[i]
            a = 0.0 if den == 0 else (t - knots[i]) / den
            d[j] = [(1 - a) * d[j - 1][c] + a * d[j][c] for c in range(4)]
    x, y, z, w = d[degree]
    return (x / w, y / w, z / w)


def _closed(start, end) -> bool:
    """A trimmed spline whose two ends are the same point is closed: it is
    taken whole. Trimmed between its ends, a periodic spline fell to zero
    length and was missing from the view."""
    return math.dist(start, end) < 1e-9


def _spline_points(b: dict, start=None, end=None) -> list[tuple[float, ...]]:
    """The spline sampled finely enough to stay within :data:`SAG`; trimmed
    (``start``, ``end``) between the parameters closest to its ends."""
    deg, kn, po = b["degree"], b["knots"], b["poles"]
    t0, t1 = kn[deg], kn[len(kn) - deg - 1]
    # 200 points per span keeps the sag within SAG.
    n = min(20000, 200 * max(1, len(po) - deg))
    ts = [t0 + (t1 - t0) * i / n for i in range(n + 1)]
    pts = [_de_boor(deg, kn, po, t) for t in ts]
    if start is not None and end is not None and not _closed(start, end):
        i0 = min(range(len(pts)), key=lambda i: math.dist(pts[i], start))
        i1 = min(range(len(pts)), key=lambda i: math.dist(pts[i], end))
        a, b_ = sorted((i0, i1))
        pts = pts[a : b_ + 1]
        pts[0], pts[-1] = (start, end) if i0 <= i1 else (end, start)
    return pts


def _ellipse_points(s: dict) -> list[tuple[float, ...]]:
    """The ellipse (or its arc), from ``start`` to ``end`` counter-clockwise
    in the sketch."""
    c, g, p = _xy(s["centre"]), _xy(s["major"]), _xy(s["minor"])
    a = math.dist(c, g)
    b = math.dist(c, p)
    u = [(g[i] - c[i]) / a for i in range(3)]
    v = [-u[1], u[0], 0.0]  # counter-clockwise, in the sketch plane

    def angle(q):
        d = [q[i] - c[i] for i in range(3)]
        return math.atan2(sum(d[i] * v[i] for i in range(3)) / b,
                          sum(d[i] * u[i] for i in range(3)) / a)

    start, end = _xy(s.get("start")), _xy(s.get("end"))
    if start is None or end is None or math.dist(start, end) < 1e-12:
        t0, t1 = 0.0, 2 * math.pi
    else:
        t0, t1 = angle(start), angle(end)
        if t1 <= t0:
            t1 += 2 * math.pi
    n = max(32, int((t1 - t0) * math.sqrt(max(a, b) * MM / (8 * SAG))) + 1)
    return [tuple(c[i] + a * u[i] * math.cos(t) + b * v[i] * math.sin(t) for i in range(3))
            for t in (t0 + (t1 - t0) * k / n for k in range(n + 1))]


class Frame:
    """``model_to_sketch`` (``s = m · R + T``): from the sketch frame to the model."""

    def __init__(self, m2s):
        r = [m2s[0:3], m2s[3:6], m2s[6:9]]
        t = m2s[9:12]
        self.origin = [-sum(r[i][k] * t[k] for k in range(3)) for i in range(3)]
        self.axes = [[r[0][j], r[1][j], r[2][j]] for j in range(3)]

    def point(self, p) -> tuple[float, float, float]:
        return tuple(self.origin[i] + sum(self.axes[j][i] * p[j] for j in range(3))
                     for i in range(3))

    @property
    def normal(self):
        return self.axes[2]


IDENTITY_FRAME = Frame([1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1])


def _ocs(n) -> list[float]:
    """The X, Y axes of a normal's object coordinate system (the DXF
    "arbitrary axis algorithm")."""
    if abs(n[0]) < 1 / 64 and abs(n[1]) < 1 / 64:
        ax = [n[2], 0.0, -n[0]]  # Wy × N
    else:
        ax = [-n[1], n[0], 0.0]  # Wz × N
    lx = math.sqrt(sum(c * c for c in ax))
    ax = [c / lx for c in ax]
    ay = [n[1] * ax[2] - n[2] * ax[1], n[2] * ax[0] - n[0] * ax[2], n[0] * ax[1] - n[1] * ax[0]]
    return ax, ay


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _angle(c, p, ax, ay) -> float:
    d = [p[i] - c[i] for i in range(3)]
    return math.degrees(math.atan2(_dot(d, ay), _dot(d, ax))) % 360.0


class Strokes:
    """A sketch's segments as 3D strokes: each curve as a run of segments
    (consecutive ends), in millimetres, by layer."""

    def __init__(self):
        self.by_layer: dict[str, list[float]] = {}

    def line(self, layer, a, b):
        self.by_layer.setdefault(layer, []).extend(c * MM for c in (*a[:3], *b[:3]))

    def arc(self, layer, centre, radius, a0, a1, normal=(0.0, 0.0, 1.0)):
        ax, ay = _ocs(normal)
        if a1 is None:
            a0, a1 = 0.0, 360.0
        sweep = (a1 - a0) % 360.0 or 360.0
        n = max(8, int(sweep / 3))
        self.polyline(layer, [
            [centre[i] + radius * (math.cos(a) * ax[i] + math.sin(a) * ay[i]) for i in range(3)]
            for a in (math.radians(a0 + sweep * k / n) for k in range(n + 1))])

    def polyline(self, layer, pts):
        for q, r in itertools.pairwise(pts):
            self.line(layer, q, r)


def draw_segment(drawing, layer: str, s: dict, frame: Frame, normal=None) -> bool:
    """One segment, from ``frame`` to ``drawing`` (:class:`Strokes`,
    :class:`Step`); ``False`` if it could not be drawn."""
    t = s["type"]
    if t == 0:
        a, b = _xy(s["start"]), _xy(s["end"])
        if a is None or b is None:
            return False
        drawing.line(layer, frame.point(a), frame.point(b))
    elif t == 1:
        c, a, b = _xy(s["centre"]), _xy(s["start"]), _xy(s["end"])
        if c is None or a is None or b is None:
            return False
        n = normal or frame.normal
        if normal is None and (s.get("rotation") or 1) < 0:
            a, b = b, a  # clockwise: arcs are drawn counter-clockwise only
        cm, am, bm = frame.point(c), frame.point(a), frame.point(b)
        radius = math.dist(cm, am)
        if s.get("circle") or math.dist(am, bm) < 1e-12:
            drawing.arc(layer, cm, radius, None, None, n)
        else:
            ax, ay = _ocs(n)
            drawing.arc(layer, cm, radius, _angle(cm, am, ax, ay), _angle(cm, bm, ax, ay), n)
    elif t == 2:
        if hasattr(drawing, "ellipse"):
            _exact_ellipse(drawing, layer, s, frame)
        else:
            drawing.polyline(layer, [frame.point(p) for p in _ellipse_points(s)])
    elif t == 3:
        b = s.get("bspline")
        if not b:
            return False
        if hasattr(drawing, "spline"):
            _exact_spline(drawing, layer, b, s, frame)
            return True
        pts = (_spline_points(b, _xy(s.get("start")), _xy(s.get("end"))) if b.get("trimmed")
               else _spline_points(b))
        drawing.polyline(layer, [frame.point(p) for p in pts])
    else:
        return False
    return True


def _exact_ellipse(drawing, layer: str, s: dict, frame: Frame) -> None:
    """The ellipse of ``s`` to ``drawing.ellipse``, same angles as :func:`_ellipse_points`."""
    c, g, p = _xy(s["centre"]), _xy(s["major"]), _xy(s["minor"])
    a, b = math.dist(c, g), math.dist(c, p)
    u = [(g[i] - c[i]) / a for i in range(3)]
    v = [-u[1], u[0], 0.0]

    def angle(q):
        d = [q[i] - c[i] for i in range(3)]
        return math.atan2(sum(d[i] * v[i] for i in range(3)) / b, sum(d[i] * u[i] for i in range(3)) / a)

    start, end = _xy(s.get("start")), _xy(s.get("end"))
    if start is None or end is None or math.dist(start, end) < 1e-12:
        t0, t1 = 0.0, 2 * math.pi
    else:
        t0, t1 = angle(start), angle(end)
        if t1 <= t0:
            t1 += 2 * math.pi
    cm = frame.point(c)
    axis = [frame.point([c[i] + u[i] for i in range(3)])[i] - cm[i] for i in range(3)]
    y = [frame.point([c[i] + v[i] for i in range(3)])[i] - cm[i] for i in range(3)]
    drawing.ellipse(layer, cm, a, b, axis, _cross(axis, y), t0, t1)


def _exact_spline(drawing, layer: str, b: dict, s: dict, frame: Frame) -> None:
    """The stored spline (poles placed by ``frame``, weights kept) to
    ``drawing.spline``; trimmed, between the parameters of its ends."""
    deg, kn, po = b["degree"], b["knots"], b["poles"]
    t0, t1 = kn[deg], kn[len(kn) - deg - 1]
    if b.get("trimmed") and s.get("start") and s.get("end") and not _closed(_xy(s["start"]), _xy(s["end"])):
        u0, u1 = _parameter(b, _xy(s["start"]), t0, t1), _parameter(b, _xy(s["end"]), t0, t1)
        t0, t1 = min(u0, u1), max(u0, u1)
    poles = [list(frame.point(q[:3])) + list(q[3:4]) for q in po]
    drawing.spline(layer, deg, kn, poles, t0, t1)


def _normal_3d(s: dict, axis=None):
    """The plane of a 3D sketch arc, oriented so that the arc runs from
    ``start`` to ``end`` counter-clockwise.

    ``axis`` is the third point the arc's record points to
    (:attr:`sldprt.sketches3d.Segment3D.third`): the centre plus the radius
    times the arc's axis. It sets the plane of a circle or a half circle;
    its sign is taken for counter-clockwise (``rotation`` = 1). Without it,
    the plane of the centre and the ends, shortest arc; ``None`` when these
    three points are aligned."""
    c, a, b = _xy(s["centre"]), _xy(s["start"]), _xy(s["end"])
    if axis is not None:
        n = [axis[i] - c[i] for i in range(3)]
        ln = math.sqrt(_dot(n, n))
        if ln > 0:
            sign = -1.0 if (s.get("rotation") or 1) < 0 else 1.0
            return [sign * x / ln for x in n]
    u = [a[i] - c[i] for i in range(3)]
    v = [b[i] - c[i] for i in range(3)]
    n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
    ln = math.sqrt(_dot(n, n))
    scale = math.sqrt(_dot(u, u) * _dot(v, v))
    return None if scale == 0 or ln < 1e-9 * scale else [x / ln for x in n]


def _third_points(data: bytes, names: set[str]) -> dict[str, dict[int, tuple]]:
    """Per 3D sketch, the axis point of each arc (see :func:`_normal_3d`)."""
    if not names:
        return {}
    return {n.name: {s.ident: s.third for s in sketch3d(data, n.offset, stop).segments
                     if s.kind == "arc" and s.third}
            for n, stop in _regions(data, named_objects(data)) if n.name in names}


# --------------------------------------------------------------------------
# The reading, and what comes out of it.


@dataclass
class Reading:
    """A part's sketches (:func:`sketches`) and the axis point of its 3D
    sketch arcs (:func:`_third_points`): what :func:`strokes` and
    :func:`write_step` share."""

    name: str
    sketches: list[dict] = field(default_factory=list)
    thirds: dict[str, dict[int, tuple]] = field(default_factory=dict)


def read(part) -> Reading:
    """Read the sketches of ``part`` (a ``.SLDPRT`` path)."""
    part = Path(part)
    container = open_container(part)
    found_sketches = sketches(tree(container))
    thirds: dict[str, dict[int, tuple]] = {}
    names = {e["name"].split("<", 1)[0] for e in found_sketches if e["3d"]}
    if names:
        found = tree_stream(container)
        if found:
            thirds = _third_points(found[1], names)
    return Reading(part.stem, found_sketches, thirds)


def _walk(reading: Reading, drawing, layer) -> None:
    """Each segment of each sketch to ``drawing``; ``layer(e, s)`` names the
    layer of segment ``s`` of sketch ``e``."""
    for e in reading.sketches:
        frame = IDENTITY_FRAME if e["3d"] or not e["frame"] else Frame(e["frame"])
        thirds = reading.thirds.get(e["name"].split("<", 1)[0], {}) if e["3d"] else {}
        for s in e["segments"]:
            normal = None
            if e["3d"] and s["type"] == 1:
                normal = _normal_3d(s, thirds.get(s["id"][1]))
                if normal is None:
                    continue
            draw_segment(drawing, layer(e, s), s, frame, normal)


def strokes(reading) -> list[dict]:
    """Each sketch placed in the model, as 3D strokes (mm): ``trait`` and
    ``construction``, flat x, y, z lists of segment ends; ``nom``, ``3d``
    and ``fonction`` (the absorbing feature, or ``None``). ``reading`` is a
    :class:`Reading` or a part path."""
    if not isinstance(reading, Reading):
        reading = read(reading)
    out = []
    for e in reading.sketches:
        st = Strokes()
        _walk(Reading(reading.name, [e], reading.thirds), st,
              lambda _e, s: "construction" if s["construction"] else "trait")
        out.append({"nom": e["name"], "3d": e["3d"], "fonction": e["feature"],
                    "trait": st.by_layer.get("trait", []),
                    "construction": st.by_layer.get("construction", [])})
    return out


# --------------------------------------------------------------------------
# The STEP of the sketches alone, written in pure Python.


def _real(x: float) -> str:
    """A STEP real: always a decimal point, exponent as ``E``."""
    x = float(x)
    if x == 0:
        return "0."
    s = repr(x)
    if "e" in s:
        m, e = s.split("e")
        return (m if "." in m else m + ".") + "E" + str(int(e))
    return s if "." in s else s + "."


def _string(s: str) -> str:
    """A STEP string: quotes and backslashes doubled, non-ASCII as
    ``\\X2\\hhhh\\X0\\``."""
    out = []
    for c in s.replace("\\", "\\\\").replace("'", "''"):
        out.append(c if 32 <= ord(c) < 127 else f"\\X2\\{ord(c):04X}\\X0\\")
    return "'" + "".join(out) + "'"


def _unit(v) -> list[float]:
    n = math.sqrt(sum(c * c for c in v))
    return [c / n for c in v]


def _cross(a, b) -> list[float]:
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _parameter(b: dict, q, t0: float, t1: float) -> float:
    """The parameter of spline ``b`` closest to point ``q``: the closest of
    400 samples, then a golden-section search between its neighbours."""
    deg, kn, po = b["degree"], b["knots"], b["poles"]
    n = 400
    ts = [t0 + (t1 - t0) * i / n for i in range(n + 1)]
    i = min(range(n + 1), key=lambda k: math.dist(_de_boor(deg, kn, po, ts[k]), q))
    a, c = ts[max(0, i - 1)], ts[min(n, i + 1)]
    r = (math.sqrt(5) - 1) / 2
    for _ in range(80):
        x1, x2 = c - r * (c - a), a + r * (c - a)
        if math.dist(_de_boor(deg, kn, po, x1), q) < math.dist(_de_boor(deg, kn, po, x2), q):
            c = x2
        else:
            a = x1
    return (a + c) / 2


class Step:
    """The :class:`Strokes` interface, plus :meth:`ellipse` and
    :meth:`spline`, which :func:`draw_segment` uses when they exist;
    ``layer`` names the curve set (the sketch, or its construction).

    An AP214 wireframe STEP in millimetres holding **only the sketches**: no
    solid, no surface, one product. Each curve is a ``TRIMMED_CURVE`` of a
    ``LINE``, ``CIRCLE``, ``ELLIPSE`` or ``B_SPLINE_CURVE_WITH_KNOTS`` — the
    spline stored in the file, its poles, knots and weights, not an
    approximation. A closed ellipse or spline is cut in two: in one piece,
    SolidWorks 2026 kept only one curve of the whole file. Imported in
    SolidWorks 2026, such a file opens as one part whose curves become 3D
    sketches; SolidWorks groups them its own way and drops the names."""

    def __init__(self):
        self.entities: list[str] = []
        self.curves: dict[str, list[int]] = {}

    def _e(self, text: str) -> int:
        self.entities.append(text)
        return len(self.entities)

    def _point(self, q) -> int:
        return self._e("CARTESIAN_POINT('',(" + ",".join(_real(c * MM) for c in q[:3]) + "))")

    def _direction(self, v) -> int:
        return self._e("DIRECTION('',(" + ",".join(_real(c) for c in _unit(v)) + "))")

    def _placement(self, centre, normal, axis) -> int:
        return self._e(f"AXIS2_PLACEMENT_3D('',#{self._point(centre)},#{self._direction(normal)},"
                       f"#{self._direction(axis)})")

    def _trimmed(self, layer, curve: int, a, ta: float, b, tb: float) -> None:
        pa, pb = self._point(a), self._point(b)
        i = self._e(f"TRIMMED_CURVE('',#{curve},(#{pa},PARAMETER_VALUE({_real(ta)})),"
                    f"(#{pb},PARAMETER_VALUE({_real(tb)})),.T.,.PARAMETER.)")
        self.curves.setdefault(layer, []).append(i)

    def line(self, layer, a, b):
        length = math.dist(a[:3], b[:3])
        if length * MM <= 1e-9:
            return
        d = [(b[i] - a[i]) / length for i in range(3)]
        v = self._e(f"VECTOR('',#{self._direction(d)},1.)")
        curve = self._e(f"LINE('',#{self._point(a)},#{v})")
        self._trimmed(layer, curve, a, 0.0, b, length * MM)

    def arc(self, layer, centre, radius, a0, a1, normal=(0.0, 0.0, 1.0)):
        ax, ay = _ocs(normal)
        circle = self._e(f"CIRCLE('',#{self._placement(centre, normal, ax)},{_real(radius * MM)})")

        def point(t):
            return [centre[i] + radius * (math.cos(t) * ax[i] + math.sin(t) * ay[i]) for i in range(3)]

        if a1 is None:
            t0, t1 = 0.0, 2 * math.pi
        else:
            t0, t1 = math.radians(a0), math.radians(a1)
            if t1 <= t0:
                t1 += 2 * math.pi
        self._trimmed(layer, circle, point(t0), t0, point(t1), t1)

    def ellipse(self, layer, centre, a, b, axis, normal, t0, t1):
        """The ellipse of semi-axes ``a`` (along ``axis``) and ``b``, from ``t0`` to ``t1`` (radians)."""
        axis = _unit(axis)
        y = _unit(_cross(normal, axis))
        curve = self._e(f"ELLIPSE('',#{self._placement(centre, normal, axis)},{_real(a * MM)},{_real(b * MM)})")

        def point(t):
            return [centre[i] + a * math.cos(t) * axis[i] + b * math.sin(t) * y[i] for i in range(3)]

        if t1 - t0 > 2 * math.pi - 1e-9:
            m = (t0 + t1) / 2
            self._trimmed(layer, curve, point(t0), t0, point(m), m)
            self._trimmed(layer, curve, point(m), m, point(t1), t1)
            return
        self._trimmed(layer, curve, point(t0), t0, point(t1), t1)

    def spline(self, layer, degree, knots, poles, t0, t1):
        """The NURBS (poles already placed in the model, weight 4th), from ``t0`` to ``t1``."""
        distinct, mult = [], []
        for k in knots:
            if distinct and abs(k - distinct[-1]) < 1e-15:
                mult[-1] += 1
            else:
                distinct.append(k)
                mult.append(1)
        ids = ",".join(f"#{self._point(q)}" for q in poles)
        base = f"{degree},({ids}),.UNSPECIFIED.,.F.,.F."
        kn = f"({','.join(map(str, mult))}),({','.join(_real(k) for k in distinct)}),.UNSPECIFIED."
        weights = [q[3] if len(q) > 3 else 1.0 for q in poles]
        if any(abs(w - 1.0) > 1e-15 for w in weights):
            curve = self._e(f"( BOUNDED_CURVE() B_SPLINE_CURVE({base}) B_SPLINE_CURVE_WITH_KNOTS({kn}) "
                            "CURVE() GEOMETRIC_REPRESENTATION_ITEM() RATIONAL_B_SPLINE_CURVE(("
                            f"{','.join(_real(w) for w in weights)})) REPRESENTATION_ITEM('') )")
        else:
            curve = self._e(f"B_SPLINE_CURVE_WITH_KNOTS('',{base},{kn})")

        def point(t):
            return _de_boor(degree, knots, poles, t)

        if math.dist(point(t0), point(t1)) * MM < 1e-6:
            m = (t0 + t1) / 2
            self._trimmed(layer, curve, point(t0), t0, point(m), m)
            self._trimmed(layer, curve, point(m), m, point(t1), t1)
            return
        self._trimmed(layer, curve, point(t0), t0, point(t1), t1)

    def text(self, part_name: str, timestamp: str) -> str:
        """The whole file."""
        body = list(self.entities)

        def e(s):
            body.append(s)
            return len(body)

        sets = [e(f"GEOMETRIC_CURVE_SET({_string(name)},({','.join(f'#{i}' for i in ids)}))")
                for name, ids in self.curves.items()]
        mm = e("( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) )")
        rad = e("( NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.) )")
        sr = e("( NAMED_UNIT(*) SI_UNIT($,.STERADIAN.) SOLID_ANGLE_UNIT() )")
        unc = e(f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),#{mm},'distance_accuracy_value',"
                "'confusion accuracy')")
        ctx = e(f"( GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{unc})) "
                f"GLOBAL_UNIT_ASSIGNED_CONTEXT((#{mm},#{rad},#{sr})) REPRESENTATION_CONTEXT('Context #1',"
                "'3D Context with UNIT and UNCERTAINTY') )")
        o = e("CARTESIAN_POINT('',(0.,0.,0.))")
        z = e("DIRECTION('',(0.,0.,1.))")
        x = e("DIRECTION('',(1.,0.,0.))")
        origin = e(f"AXIS2_PLACEMENT_3D('',#{o},#{z},#{x})")
        rep = e(f"GEOMETRICALLY_BOUNDED_WIREFRAME_SHAPE_REPRESENTATION('',(#{origin},"
                f"{','.join(f'#{i}' for i in sets)}),#{ctx})")
        app = e("APPLICATION_CONTEXT('core data for automotive mechanical design processes')")
        e(f"APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,#{app})")
        pctx = e(f"PRODUCT_CONTEXT('',#{app},'mechanical')")
        name = _string(part_name)
        product = e(f"PRODUCT({name},{name},'',(#{pctx}))")
        e(f"PRODUCT_RELATED_PRODUCT_CATEGORY('part',$,(#{product}))")
        form = e(f"PRODUCT_DEFINITION_FORMATION('','',#{product})")
        dctx = e(f"PRODUCT_DEFINITION_CONTEXT('part definition',#{app},'design')")
        definition = e(f"PRODUCT_DEFINITION('design','',#{form},#{dctx})")
        shape = e(f"PRODUCT_DEFINITION_SHAPE('','',#{definition})")
        e(f"SHAPE_DEFINITION_REPRESENTATION(#{shape},#{rep})")
        head = ["ISO-10303-21;", "HEADER;", "FILE_DESCRIPTION(('openSW sketches'),'2;1');",
                f"FILE_NAME({name},'{timestamp}',(''),(''),'openSW','openSW','');",
                "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));", "ENDSEC;", "DATA;"]
        return "\n".join(head + [f"#{i} = {s};" for i, s in enumerate(body, start=1)]
                         + ["ENDSEC;", "END-ISO-10303-21;", ""])


def write_step(reading, out, part_name: str | None = None, timestamp: str | None = None) -> dict:
    """Write the sketches' STEP (``out``); return the count
    ``{"sketches", "edges"}``. Nothing is written when there is no curve.
    ``reading`` is a :class:`Reading` or a part path. Pure Python: runs under
    Pyodide too."""
    import datetime

    if not isinstance(reading, Reading):
        reading = read(reading)
    step = Step()
    _walk(reading, step, lambda e, s: e["name"] + (" construction" if s["construction"] else ""))
    n = sum(len(c) for c in step.curves.values())
    if n:
        timestamp = timestamp or datetime.datetime.now().replace(microsecond=0).isoformat()
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(step.text(part_name or f"{reading.name} — sketches", timestamp),
                       encoding="ascii", newline="\n")
    return {"sketches": len(reading.sketches), "edges": n}
