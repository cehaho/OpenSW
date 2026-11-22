"""Planar sketch segments read again: ellipses, splines, and what is live.

:func:`sldprt.sketches.segments` reads lines and arcs only. This module
reads the same tables (:func:`sldprt.sketches.records`) with three more
rules.

**The kind is the word at +80** when the record is not an arc (``fe ff ff
ff`` at +86, where +80 then holds the sense): 0 a line, 1 an **ellipse**, 2
a **spline**.

**A live segment has bit 0x04** of the word at +37 set (on top of bit 0x01
clear). An old construction segment is written ``0x0008``. Two live copies
are two segments, as the SolidWorks API returns them.

**An ellipse** holds point indexes at +84: the centre, the end of the major
axis (``major``), that of the minor axis (``minor``), then the two opposite
ends. Its sense is counter-clockwise, from the point at +64 to the one at
+66.

**A spline** holds at +86 a class (``ptsSplineList_c``, introduced or
cited), a ``u16`` ``n`` then ``n`` 12-byte references to its fit points;
from the end ``R`` of these references: the number of knots (``u32`` at
R + 86), the knots (``f64``), the number of poles (``u32``) and the poles
``(x, y, z)`` as ``f64``. The degree is ``knots − poles − 1``. A
**rational** spline then writes ``0 · −1 · 1 · 1 · poles · 2`` as ``u32``,
sixteen null bytes and the weights; without that pattern, the weights are 1.
A **trimmed** spline keeps its poles from before the cut: when its first and
last poles are not its ends, it is flagged ``trimmed`` and keeps its two
ends. **A spline can carry 0 at +80**: it then differs from a line only by
the rest of its record (:func:`sldprt.sketches._spline`).

**A spline whose u16 at +68 is zero gives way to an identical copy**
(same knots, poles, ends and construction) kept in the same sketch; with no
copy, it stays. Measured on "Sketch3" of "Fermoir echelle 2": two records of
the same spline, ids 1 and 2, differing only by +68 (0, 1), the face number
(0, 1) and the id; the API returns one. Across the 23 clasps, the corpus
without ``loim`` and the ring, only "Sketch3" changes. What would refute it:
an API spline with a zero +68 that has an identical copy in its sketch.

Not read: periodicity (no periodic spline seen), where a trimmed spline is
cut.
"""

from __future__ import annotations

import math
import struct

from .sketches import _ARC, _POINT, _is_points, _tables, extension, records
from .sketches import _spline as _written_spline

#: The kind of a segment record, word at +80.
LINE, ELLIPSE, SPLINE = 0, 1, 2
#: The pattern announcing a rational spline's weights (after the number of poles).
_RATIONAL = struct.pack("<iii", -1, 1, 1)


def _points(data: bytes, table: list[int]) -> list:
    return [struct.unpack_from("<dd", data, q + 66) if data[q + 64 : q + 66] == _POINT else None
            for q in table]


def _spline(data: bytes, q: int, stop: int) -> dict | None:
    at = q + 86
    if data[at : at + 2] == b"\xff\xff":  # class introduced: ff ff · schema · length · name
        at += 6 + int.from_bytes(data[at + 4 : at + 6], "little")
    else:
        at += 2  # class cited
    n = int.from_bytes(data[at : at + 2], "little")
    r = at + 2 + 12 * n
    if r + 90 > stop:
        return None
    nk = struct.unpack_from("<I", data, r + 86)[0]
    k0 = r + 90
    if not 2 <= nk <= 1000 or k0 + 8 * nk + 4 > stop:
        return None
    knots = list(struct.unpack_from(f"<{nk}d", data, k0))
    p0 = k0 + 8 * nk
    np_ = struct.unpack_from("<I", data, p0)[0]
    if not 1 <= np_ < nk or p0 + 4 + 24 * np_ > stop or knots != sorted(knots):
        return None
    poles = [list(struct.unpack_from("<3d", data, p0 + 4 + 24 * i)) for i in range(np_)]
    end = p0 + 4 + 24 * np_
    header = struct.pack("<I", 0) + _RATIONAL + struct.pack("<II", np_, 2)
    if data[end : end + len(header)] == header:
        w0 = end + len(header) + 16
        weights = struct.unpack_from(f"<{np_}d", data, w0)
        if all(0 < w < 1e3 for w in weights):
            poles = [[*p, w] for p, w in zip(poles, weights, strict=True)]
    return {"degree": nk - np_ - 1, "knots": knots, "poles": poles, "periodic": False}


#: The flags word at +37: old version, live segment, construction.
OLD, LIVE, CONSTRUCTION = 0x01, 0x04, 0x08


def segments(data: bytes, start: int, stop: int) -> list[dict]:
    """The live segments of the region, as dicts: ``type`` (0 line, 1 arc,
    2 ellipse, 3 spline), ``id`` = ``[None, id]``, ``construction``,
    ``start``, ``end``, ``centre`` (``{x, y, z}`` in the sketch frame,
    metres), ``radius``, ``rotation``, ``circle``, ``face``; ``major`` and
    ``minor`` for an ellipse, ``bspline`` for a spline."""
    found = records(data, start, stop)
    tables = _tables(data, found)
    first = next((i for i, t in enumerate(tables) if _is_points(data, t)), None)
    if first is None:
        return []
    points = _points(data, tables[first])
    # The segments written after the tables without being counted in them
    # (sldprt.sketches.extension).
    extended = extension(data, found, tables, first, stop)

    def point(i):
        p = points[i] if 0 <= i < len(points) else None
        return None if p is None else {"x": p[0], "y": p[1], "z": 0.0}

    out: list[dict] = []
    doubtful: list[dict] = []
    doubtful_splines: set[int] = set()
    for table in [*tables[first + 1 :], extended]:
        if table is not extended and _is_points(data, table):
            break
        for q in table:
            ident = struct.unpack_from("<i", data, q - 4)[0]
            word = int.from_bytes(data[q + 37 : q + 39], "little")
            s, e = struct.unpack_from("<HH", data, q + 64)
            if ident < 0 or word & OLD or not word & LIVE or point(s) is None or point(e) is None:
                continue
            arc = data[q + 86 : q + 90] == _ARC
            kind = None if arc else struct.unpack_from("<H", data, q + 80)[0]
            if kind == LINE and _written_spline(data, q):
                kind = SPLINE  # a spline carrying 0 at +80
            # As in sldprt.sketches.segments: a null u16 at +68 makes the
            # segment doubtful.
            dubious = ((arc or kind == LINE) and not word & CONSTRUCTION
                       and int.from_bytes(data[q + 68 : q + 70], "little") == 0)
            seg = {"type": 0, "id": [None, ident], "construction": bool(word & CONSTRUCTION),
                   "start": point(s), "end": point(e), "centre": None, "radius": None,
                   "rotation": None, "circle": None,
                   "face": struct.unpack_from("<I", data, q - 8)[0] if q >= 8 else None}
            if arc:
                c = int.from_bytes(data[q + 84 : q + 86], "little")
                if point(c) is None:
                    continue
                ps, pc = points[s], points[c]
                seg.update(type=1, centre=point(c),
                           radius=((ps[0] - pc[0]) ** 2 + (ps[1] - pc[1]) ** 2) ** 0.5,
                           rotation=struct.unpack_from("<i", data, q + 80)[0], circle=s == e)
            elif kind == ELLIPSE:
                c, major, minor = struct.unpack_from("<3H", data, q + 84)
                if None in (point(c), point(major), point(minor)):
                    continue
                seg.update(type=2, centre=point(c), major=point(major), minor=point(minor),
                           rotation=1)
            elif kind == SPLINE:
                b = _spline(data, q, stop)
                trimmed = b is not None and not _at_ends(b["poles"], points[s], points[e])
                if trimmed:
                    # The poles go beyond its ends: keep the curve and both
                    # ends, the curve is cut between them when drawn.
                    b["trimmed"] = True
                seg.update(type=3, start=point(s) if trimmed else None,
                           end=point(e) if trimmed else None, bspline=b)
            elif kind != LINE or s == e:
                continue  # unknown kind, or a service record
            if dubious:
                doubtful.append(seg)
                continue
            if seg["type"] == 3 and int.from_bytes(data[q + 68 : q + 70], "little") == 0:
                doubtful_splines.add(id(seg))
            out.append(seg)
    # The same doubtful lines sldprt.sketches.segments keeps: a face number,
    # both ends on kept segments, no copy.
    ends = {(p["x"], p["y"]) for o in out if not o["construction"] and o["type"] in (0, 1)
            for p in (o["start"], o["end"]) if p}
    for seg in doubtful:
        if (seg["type"] == 0 and seg.get("face")
                and (seg["start"]["x"], seg["start"]["y"]) in ends
                and (seg["end"]["x"], seg["end"]["y"]) in ends
                and not any(_same(o, seg) for o in out)):
            out.append(seg)

    # A spline with a zero +68 gives way to an identical copy; the others keep
    # their place.
    def copy(seg):
        return any(id(o) not in doubtful_splines and o["type"] == 3
                   and o["construction"] == seg["construction"] and o["bspline"] == seg["bspline"]
                   and o["start"] == seg["start"] and o["end"] == seg["end"]
                   for o in out)

    return [s for s in out if id(s) not in doubtful_splines or not copy(s)]


def _at_ends(poles, start, end, tol=1e-9) -> bool:
    """The first and last poles are the segment's ends (either way)."""
    a, b = poles[0][:2], poles[-1][:2]
    return ((math.dist(a, start) <= tol and math.dist(b, end) <= tol)
            or (math.dist(a, end) <= tol and math.dist(b, start) <= tol))


def _same(a: dict, b: dict) -> bool:
    """Two lines or two arcs with the same geometry."""
    if (a["type"] != b["type"] or a["centre"] != b["centre"]
            or a["construction"] != b["construction"]):
        return False
    return (a["start"], a["end"]) in ((b["start"], b["end"]), (b["end"], b["start"]))
