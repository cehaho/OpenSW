"""3D sketches (``3DProfileFeature``) read in the tree stream.

A 3D sketch is written like a planar one (:mod:`sldprt.sketches`): raw
arrays of records, a point table then segment tables, with the same header
and the same payload at +64. Three things change:

**A 3D sketch point** is marked ``0e 00`` at +64 (``1e 00`` in 2D) and holds
**three** ``f64`` at +66: x, y, z in the **model** frame, in metres. A 3D
sketch's frame is the identity.

**A line** is a segment record as in 2D: start and end index in the point
table (+64, +66), construction at bit 0x08 of the word at +37, old version
at bit 0x01. Two records joining the same points are two copies: the higher
id is kept.

**A spline** has the ``u16`` at +80 equal to 2 (0 for a line). Its record
holds, at +200, the number of knots (``u32``) then the knots (``f64``), the
number of poles then the poles (three ``f64``), a null ``u32``, then, a
little further, the degree (``u32``) followed by the parameter bounds
(first and last knot, two ``f64``); the degree is also knots − poles − 1.
Each spline is written **twice**, between the same points: a copy in its own
parametrisation and the model's copy, with a higher id. The copy rule keeps
the right one, provided only two splines running the same way count as
copies. A spline whose ``u32`` after the poles is not null (weights) or
whose knots are not clamped (periodic) is returned without its curve.

**An arc** carries ``fe ff ff ff`` at +90 (+86 in 2D), the index of its
centre at +84 and its sense at +80, as in 2D, and the index of a third
point at +86.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

from .sketches import _ARC, _tables, records

#: A 3D sketch point's mark, at +64 (``1e 00`` in 2D).
POINT_3D = b"\x0e\x00"
#: The ``u16`` at +80 of a spline (0 for a line).
SPLINE = 2
#: Where a 3D sketch arc carries ``fe ff ff ff`` (+86 in 2D: the 3D arc
#: points to one more point, at +86).
ARC_MARK = 90
#: Where the number of knots starts in a spline record.
SPLINE_KNOTS = 200

#: A 3D sketch's frame: the identity, as 13 numbers like a planar sketch's
#: plane (rotation, translation, scale).
IDENTITY = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]

Point = tuple[float, float, float]


@dataclass(frozen=True)
class Spline:
    degree: int
    knots: tuple[float, ...]
    poles: tuple[Point, ...]


@dataclass(frozen=True)
class Segment3D:
    #: "line", "arc" or "spline".
    kind: str
    ident: int
    start: Point
    end: Point
    construction: bool
    centre: Point | None = None
    sense: int | None = None
    #: A spline's curve, or ``None`` when it could not be read.
    spline: Spline | None = field(default=None, compare=False)
    #: The ``u32`` written just before the id (see ``Segment.face``).
    face: int | None = field(default=None, compare=False)
    #: For an arc, the third point it points to (+86): the centre plus the
    #: radius times the arc's axis, which sets its plane.
    third: Point | None = field(default=None, compare=False)


@dataclass
class Sketch3D:
    segments: list[Segment3D]


def _is_points(data: bytes, table: list[int]) -> bool:
    """A point table: its first record is a point, and at least half of the
    others (the rest of the table can be service records)."""
    if not table or data[table[0] + 64 : table[0] + 66] != POINT_3D:
        return False
    return sum(1 for q in table if data[q + 64 : q + 66] == POINT_3D) >= len(table) / 2


def _spline(data: bytes, q: int, stop: int) -> Spline | None:
    """The curve of a spline record, or ``None``."""
    at = q + SPLINE_KNOTS
    if at + 4 > stop:
        return None
    nk = struct.unpack_from("<I", data, at)[0]
    if not 2 <= nk <= 10_000 or at + 4 + 8 * nk + 4 > stop:
        return None
    knots = struct.unpack_from(f"<{nk}d", data, at + 4)
    at += 4 + 8 * nk
    npoles = struct.unpack_from("<I", data, at)[0]
    if not 2 <= npoles < nk or at + 4 + 24 * npoles + 12 > stop:
        return None
    flat = struct.unpack_from(f"<{3 * npoles}d", data, at + 4)
    at += 4 + 24 * npoles
    degree = nk - npoles - 1
    if struct.unpack_from("<I", data, at)[0] != 0:
        return None  # weights, probably: never seen
    # The degree is the ``u32`` written just before the parameter bounds
    # (first and last knot): 8 or 20 bytes after the poles, by version.
    bound = struct.pack("<Idd", degree, knots[0], knots[-1])
    if data.find(bound, at, min(at + 64, stop)) < 0:
        return None
    if not all(math.isfinite(x) for x in (*knots, *flat)) or list(knots) != sorted(knots):
        return None
    if len(set(knots[: degree + 1])) != 1 or len(set(knots[-degree - 1 :])) != 1:
        return None  # unclamped knots: periodic, never seen
    poles = tuple(tuple(flat[3 * i : 3 * i + 3]) for i in range(npoles))
    return Spline(degree, tuple(knots), poles)  # type: ignore[arg-type]


def sketch3d(data: bytes, start: int, stop: int) -> Sketch3D:
    """The live segments of a 3D sketch region."""
    found = records(data, start, stop)
    tables = _tables(data, found)
    first = next((i for i, t in enumerate(tables) if _is_points(data, t)), None)
    if first is None:
        return Sketch3D([])
    points = [struct.unpack_from("<3d", data, q + 66) if data[q + 64 : q + 66] == POINT_3D
              else None for q in tables[first]]
    # Each record's bound: the next one, or the end of the region.
    bounds = {q: (found[i + 1] if i + 1 < len(found) else stop) for i, q in enumerate(found)}
    out: list[Segment3D] = []
    for table in tables[first + 1 :]:
        if _is_points(data, table):
            break  # another point table: another sketch starts
        for q in table:
            ident = struct.unpack_from("<i", data, q - 4)[0]
            if ident < 0:
                continue
            s, e = struct.unpack_from("<HH", data, q + 64)
            if s >= len(points) or e >= len(points) or points[s] is None or points[e] is None:
                continue
            word = int.from_bytes(data[q + 37 : q + 39], "little")
            if word & 0x01:
                continue  # an old version of a segment
            arc = data[q + ARC_MARK : q + ARC_MARK + 4] == _ARC
            spline = not arc and struct.unpack_from("<H", data, q + 80)[0] == SPLINE
            if not arc and not spline and s == e:
                continue  # service record
            centre = sense = curve = third = None
            if arc:
                c, t = struct.unpack_from("<HH", data, q + 84)
                if c >= len(points) or points[c] is None:
                    continue
                centre, sense = points[c], struct.unpack_from("<i", data, q + 80)[0]
                third = points[t] if t < len(points) else None
            if spline:
                curve = _spline(data, q, bounds[q])
            face = struct.unpack_from("<I", data, q - 8)[0] if q >= 8 else None
            kind = "arc" if arc else "spline" if spline else "line"
            item = Segment3D(kind, ident, points[s], points[e], bool(word & 0x08), centre, sense,
                             curve, face, third)
            twin = next((k for k, o in enumerate(out) if _same(o, item)), None)
            if twin is None:
                out.append(item)
            elif item.ident > out[twin].ident:
                out[twin] = item  # the most recent copy
    return Sketch3D(out)


def _same(a: Segment3D, b: Segment3D) -> bool:
    """Two copies of one segment. Two splines are copies only when running
    **the same way**: the two halves of a loop join the same points in
    opposite directions."""
    if a.kind != b.kind or a.centre != b.centre:
        return False
    if (a.start, a.end) == (b.start, b.end):
        return True
    return a.kind != "spline" and (a.start, a.end) == (b.end, b.start)
