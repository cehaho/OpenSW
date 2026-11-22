"""Sketch geometry read in the tree stream: segments and plane.

Saves older than v12000 are not read: their layout differs.

**A sketch is made of raw arrays**, not MFC objects. Each record starts with
its id (``u32``) then a fixed header ::

    ff ff · u16 bits · u8 · ff ff ff ff · u16 · ff ff · f32 −1

and its payload starts 64 bytes after the first ``ff ff``. A table is
announced by a count (``u16``) 14 bytes before its first record. The first
table in which at least half of the records are marked ``1e 00`` holds the
**points**: x, y as ``f64``, in the sketch's frame, in metres.

The next tables hold the **segments**: index of the start and end in the
point table (+64, +66). An **arc** is known by the shape of its record
(``fe ff ff ff`` at +86); it carries its sense (``i32`` at +80, 1
counter-clockwise, −1 clockwise, as ``GetRotationDir``) and the index of its
centre (+84). A circle is an arc whose start is its end. Bit 0x08 of the
word at +37 marks **construction** geometry.

SolidWorks keeps **old versions** of the geometry in the file: segments
whose bit 0 of the word at +37 is set or bit 2 clear. They are dropped; two
live segments with the same geometry stay two segments. Ellipses and splines
are not read here (:mod:`sldprt.sketch_curves`): a record that is not an arc
is a line only if its word at +80 is null and it does not carry a spline's
signature (:func:`_spline`).

**The plane** is the ``ModelToSketchTransform`` matrix written as is:
rotation row by row, translation, scale, as ``f64``, preceded by the byte
``01``. A region sometimes holds several (flipped views); the right one is
the one another rotation follows at 108 bytes, or the first. Sketches with
no such block have an identity rotation, which SolidWorks does not write:
see :func:`_identity_plane` and :func:`frame_record`.
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field

from .container import Container
from .features import _payload, carchive_strings, document_version, read_keywords, tree_stream
from .nodes import _regions, named_objects

#: Below this version, sketches are not read.
SKETCHES_SINCE = 12000

_MINUS_ONE = b"\x00\x00\x80\xbf"
_POINT = b"\x1e\x00"
_ARC = b"\xfe\xff\xff\xff"
#: Id of service records (65534), never an entity.
_SERVICE = 0xFFFE
#: Distance between the right transform and the rotation that follows it.
_PLANE_FOLLOWER = 108


@dataclass(frozen=True)
class Segment:
    kind: str  # "line" or "arc"
    ident: int
    start: tuple[float, float]
    end: tuple[float, float]
    construction: bool
    centre: tuple[float, float] | None = None
    sense: int | None = None
    #: The number of the face the segment generates in an extrusion or a
    #: revolution: the ``u32`` written just before the id.
    face: int | None = field(default=None, compare=False)


@dataclass
class Sketch:
    name: str
    segments: list[Segment] = field(default_factory=list)
    #: ``ModelToSketchTransform.ArrayData`` as 13 numbers, or ``None``.
    plane: list[float] | None = None


def records(data: bytes, start: int, stop: int) -> list[int]:
    """The position of the first ``ff ff`` of each record in the range."""
    out = []
    at = data.find(_MINUS_ONE, start)
    while 0 <= at < stop:
        head = at - 13
        if (data[head : head + 2] == b"\xff\xff" and data[head + 5 : head + 9] == b"\xff" * 4
                and data[head + 11 : head + 13] == b"\xff\xff"):
            out.append(head)
        at = data.find(_MINUS_ONE, at + 1)
    return out


def _tables(data: bytes, found: list[int]) -> list[list[int]]:
    tables, i = [], 0
    while i < len(found):
        count = int.from_bytes(data[found[i] - 14 : found[i] - 12], "little")
        if 1 <= count <= len(found) - i:
            tables.append(found[i : i + count])
            i += count
        else:
            i += 1
    return tables


def extension(data: bytes, found: list[int], tables: list[list[int]], first: int,
              stop: int) -> list[int]:
    """The segment records that follow the sketch's tables without being
    announced by a count (see :func:`segments`): after the point table
    ``first``, before the next point table, marked real (``u16`` at +68 = 1)
    or construction (bit 0x08 of the word at +37), and carrying an entity
    id."""
    following = next((t[0] for t in tables[first + 1 :] if _is_points(data, t)), stop)
    announced = {q for t in tables for q in t}
    return [q for q in found if tables[first][-1] < q < following and q not in announced
            and (int.from_bytes(data[q + 68 : q + 70], "little") == 1 or data[q + 37] & 0x08)
            and 0 <= struct.unpack_from("<i", data, q - 4)[0] < _SERVICE]


def _is_points(data: bytes, table: list[int]) -> bool:
    return sum(1 for q in table if data[q + 64 : q + 66] == _POINT) >= len(table) / 2


def _spline(data: bytes, q: int) -> bool:
    """A spline record, whether its word at +80 is 2 or not: the ``u16`` 4
    at +84, then the class of its point list written in full (``ff ff``,
    schema, length at +90, name ending in ``SplineList_c`` at +92) or its
    tag (byte +91 has bit ``0x80``) followed by ``ff ff ff ff`` at +94.
    Some splines carry 0 at +80 and differ from a line only by this."""
    if data[q + 84 : q + 86] != b"\x04\x00":
        return False
    if data[q + 86 : q + 88] == b"\xff\xff":
        n = int.from_bytes(data[q + 90 : q + 92], "little")
        return data[q + 92 : q + 92 + n].endswith(b"SplineList_c")
    return bool(data[q + 91] & 0x80) and data[q + 94 : q + 98] == b"\xff" * 4


def segments(data: bytes, start: int, stop: int) -> list[Segment]:
    """The live segments of a sketch region.

    A segment table **extends beyond its count**: records of the same shape
    follow it, announced by nothing. They are read when they carry 1 in the
    ``u16`` at +68 (a real segment) or the construction bit, and an entity
    id, up to the next point table.
    """
    found = records(data, start, stop)
    tables = _tables(data, found)
    first = next((i for i, t in enumerate(tables) if _is_points(data, t)), None)
    if first is None:
        return []
    points = [struct.unpack_from("<dd", data, q + 66) if data[q + 64 : q + 66] == _POINT else None
              for q in tables[first]]
    out: list[Segment] = []
    doubtful: list[Segment] = []
    extended = extension(data, found, tables, first, stop)
    for table in [*tables[first + 1 :], extended]:
        if table is not extended and _is_points(data, table):
            break  # another point table: another sketch starts
        for q in table:
            ident = struct.unpack_from("<i", data, q - 4)[0]
            if ident < 0:
                continue
            s, e = struct.unpack_from("<HH", data, q + 64)
            if s >= len(points) or e >= len(points) or points[s] is None or points[e] is None:
                continue
            word = int.from_bytes(data[q + 37 : q + 39], "little")
            if word & 0x01 or not word & 0x04:
                continue  # an old version of a segment
            arc = data[q + 86 : q + 90] == _ARC
            if not arc and s == e:
                continue  # service record
            if not arc and (data[q + 80 : q + 82] != b"\x00\x00" or _spline(data, q)):
                continue  # 1 an ellipse, 2 a spline (sldprt.sketch_curves)
            dubious = not word & 0x08 and int.from_bytes(data[q + 68 : q + 70], "little") == 0
            centre = sense = None
            if arc:
                c = int.from_bytes(data[q + 84 : q + 86], "little")
                if c >= len(points) or points[c] is None:
                    continue
                centre, sense = points[c], struct.unpack_from("<i", data, q + 80)[0]
            face = struct.unpack_from("<I", data, q - 8)[0] if q >= 8 else None
            item = Segment("arc" if arc else "line", ident, points[s], points[e],
                           bool(word & 0x08), centre, sense, face)
            if dubious:
                doubtful.append(item)
                continue
            # Two live segments with the same geometry are two entities; dead
            # copies are dropped by the word at +37.
            out.append(item)
    # A real segment carries 1 in the u16 at +68; 0 most often marks a
    # segment the API does not return, sometimes a real line with no other
    # copy. A doubtful line is kept when it carries a face number, both its
    # ends touch kept segments and no copy doubles it; a doubtful arc, never.
    ends = {p for o in out if not o.construction for p in (o.start, o.end)}
    for item in doubtful:
        if (item.kind == "line" and item.face and item.start in ends and item.end in ends
                and not any(_same(o, item) for o in out)):
            out.append(item)
    return out


def _same(a: Segment, b: Segment) -> bool:
    if a.kind != b.kind or a.centre != b.centre:
        return False
    return (a.start, a.end) == (b.start, b.end) or (a.start, a.end) == (b.end, b.start)


def _rotations(data: bytes, start: int, stop: int) -> list[tuple[int, tuple[float, ...]]]:
    out = []
    for p in range(start + 1, stop - 104):
        if data[p - 1] != 1:
            continue
        v = struct.unpack_from("<13d", data, p)
        if not all(math.isfinite(x) and abs(x) < 10 for x in v) or abs(v[12] - 1.0) > 1e-12:
            continue
        r = (v[0:3], v[3:6], v[6:9])
        if all(abs(sum(x * x for x in row) - 1) < 1e-9 for row in r) and all(
                abs(sum(r[i][k] * r[j][k] for k in range(3))) < 1e-9
                for i, j in ((0, 1), (0, 2), (1, 2))):
            out.append((p, v))
    return out


def plane(data: bytes, start: int, stop: int) -> list[float] | None:
    """The model → sketch transform, as 13 numbers, or ``None``."""
    found = _rotations(data, start, stop)
    if not found:
        return _identity_plane(data, start, stop)
    starts = {p for p, _ in found}
    # Without a block followed by another, the last one of the region.
    pick = next((v for p, v in found if p + _PLANE_FOLLOWER in starts), found[-1][1])
    return list(pick)


#: The ±z normal as three ``f64``.
_NORMALS_Z = (struct.pack("<3d", 0.0, 0.0, 1.0), struct.pack("<3d", 0.0, 0.0, -1.0))


def _identity_plane(data: bytes, start: int, stop: int) -> list[float]:
    """The plane of a sketch whose rotation is not written: the identity.

    Only the z offset is missing. The face planes of the region carry the
    origin ``(0, 0, o)`` followed by the normal ``(0, 0, ±1)``; the **last**
    one gives the translation ``(0, 0, −o)``, and with none it is zero.
    """
    last = None
    for normal in _NORMALS_Z:
        at = data.find(normal, start)
        while 0 <= at < stop:
            x, y, z = struct.unpack_from("<3d", data, at - 24)
            if (x == 0.0 and y == 0.0 and math.isfinite(z) and abs(z) < 10
                    and (last is None or at > last[0])):
                last = (at, z)
            at = data.find(normal, at + 1)
    tz = -last[1] if last is not None and last[1] else 0.0
    return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, tz, 1.0]


#: What follows a sketch's frame at the end of its region: the 1.0 scale of
#: the last record, then ``00 00 00`` (one record) or ``00`` (two), a ``u32``
#: and ``ff ff ff ff``.
_FRAME_TAIL = re.compile(
    rb"\x00{6}\xf0\x3f(\x00\x00\x00|\x00)[\x00-\x40]\x00\x00\x00\xff\xff\xff\xff")
#: Between the two records of a frame.
_FRAME_BETWEEN = b"\x00\x01\x00"
_ONE = struct.pack("<d", 1.0)


def _record(data: bytes, stop: int, start: int) -> tuple[int, bool, list[float]] | None:
    """The frame record ending at ``stop``: where it starts, whether its
    rotation is written, and its thirteen numbers.

    A record is ``u8 rotated`` · [9 ``f64``: the rotation, row by row] · 3
    ``f64``: the translation · ``f64``: the scale, 1.0. The rotation is only
    written if ``rotated`` is 1; at 0 it is the identity."""
    if data[stop - 8 : stop] != _ONE:
        return None
    at = stop - 105
    if at >= start and data[at] == 1:
        v = struct.unpack_from("<13d", data, at + 1)
        r = (v[0:3], v[3:6], v[6:9])
        if (all(math.isfinite(x) and abs(x) < 10 for x in v)
                and all(abs(sum(x * x for x in row) - 1) < 1e-9 for row in r)
                and all(abs(sum(r[i][k] * r[j][k] for k in range(3))) < 1e-9
                        for i, j in ((0, 1), (0, 2), (1, 2)))):
            return at, True, list(v)
    at = stop - 33
    if at >= start and data[at] == 0:
        t = struct.unpack_from("<3d", data, at + 1)
        if all(math.isfinite(x) and abs(x) < 10 for x in t):
            return at, False, [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, *t, 1.0]
    return None


def frame_record(data: bytes, start: int, stop: int) -> tuple[list[float], bool] | None:
    """The frame of the sketch whose region is ``[start, stop)``: the model →
    sketch transform as thirteen numbers, and whether its rotation is
    written; or ``None``.

    A sketch region ends with one or two records (:func:`_record`) followed
    by :data:`_FRAME_TAIL`; when there are two, :data:`_FRAME_BETWEEN`
    separates them and **the first** is ``ModelToSketchTransform``."""
    found = None
    for m in _FRAME_TAIL.finditer(data, start, stop):
        last = _record(data, m.start() + 8, start)
        if last is None:
            continue
        if len(m.group(1)) == 3:
            found = (last[2], last[1])
        elif data[last[0] - 3 : last[0]] == _FRAME_BETWEEN:
            first = _record(data, last[0] - 3, start)
            if first is not None:
                found = (first[2], first[1])
    return found


#: An equation driving a sketch dimension: ``"D2@Sketch1" = …``.
_EQUATION = re.compile(r'^"D\d+@([^"@]+)"\s*=')


def equation_blocks(data: bytes) -> dict[str, tuple[int, int]]:
    """The geometry of sketches driven by an equation, in ``Config-N``.

    A sketch one of whose dimensions is the target of an equation has no
    record in the tree stream. Its point and segment tables are written in
    ``Contents/Config-N``, after the first equation targeting it, up to the
    equation targeting another sketch.
    """
    starts: list[tuple[int, str]] = []
    for offset, text in carchive_strings(data):
        m = _EQUATION.match(text)
        if m and m[1] not in {name for _, name in starts}:
            starts.append((offset, m[1]))
    return {name: (offset, starts[i + 1][0] if i + 1 < len(starts) else len(data))
            for i, (offset, name) in enumerate(starts)}


#: A dimension cited in an equation: ``"D2@Sketch2"``.
_CITED_DIMENSION = re.compile(r'"D\d+@([^"@]+)"')


def operand_blocks(data: bytes) -> dict[str, tuple[int, int]]:
    """The geometry of sketches an equation **cites on its right side**, in
    ``Config-N``.

    The equation's block holds one point table per sketch, in the order the
    equation names them: the target, then the operands. The k-th sketch
    cited on the right is read from the k-th point table after the target's
    up to the next one. The rule only applies when the count matches: as
    many point tables in the block as named sketches.
    """
    starts: list[tuple[int, str, str]] = []
    for offset, text in carchive_strings(data):
        m = _EQUATION.match(text)
        if m and m[1] not in {name for _, name, _ in starts}:
            starts.append((offset, m[1], text))
    targets = {name for _, name, _ in starts}
    points = [t[0] for t in _tables(data, records(data, 0, len(data))) if _is_points(data, t)]
    out: dict[str, tuple[int, int]] = {}
    for i, (offset, _target, text) in enumerate(starts):
        stop = starts[i + 1][0] if i + 1 < len(starts) else len(data)
        operands: list[str] = []
        for name in _CITED_DIMENSION.findall(text.split("=", 1)[1]):
            if name not in targets and name not in operands and name not in out:
                operands.append(name)
        inside = [p for p in points if offset <= p < stop]
        if not operands or len(inside) != 1 + len(operands):
            continue
        for rank, name in enumerate(operands, 1):
            # 14 bytes before the first record: the table count.
            out[name] = (inside[rank] - 14,
                         inside[rank + 1] - 14 if rank + 1 < len(inside) else stop)
    return out


def _frame(data: bytes, node, stop: int) -> list[float] | None:
    """A sketch's frame: :func:`plane`'s, except where the end-of-region
    record (:func:`frame_record`) gives another one (sketches without a
    rotation block on a plane parallel to the front plane, whose full
    translation only the record writes). Where both agree (to 1e-12),
    :func:`plane`'s value is kept as is."""
    old = plane(data, node.offset, stop)
    found = frame_record(data, node.end, stop)
    if found is None:
        return old
    if old is not None and max(abs(a - b)
                               for a, b in zip(found[0][:12], old[:12], strict=True)) < 1e-12:
        return old
    return found[0]


#: The class of a sketch picture node, declared before its name.
_PICTURE = b"moSketchBitmap_c"


def picture_stop(data: bytes, regions: list, rank: int) -> int | None:
    """The end of sketch ``regions[rank]`` when it holds a picture, or ``None``.

    A sketch picture is a named node of class ``moSketchBitmap_c`` written
    **in the middle** of its sketch: the sketch region, cut at the next
    node, stops 40 bytes after its name, at the class declaration, and the
    rest of the sketch (its tables, its frame) is in the picture's region.
    Only the class **declaration** is recognised: a second picture in the
    same part would only write a reference to the class and not be seen.
    """
    if rank + 1 >= len(regions):
        return None
    stop = regions[rank][1]
    if data[stop - 2 - len(_PICTURE) : stop - 2] != _PICTURE:
        return None
    return regions[rank + 1][1]


def read_sketches(container: Container, names: set[str] | None = None) -> dict[str, Sketch]:
    """The top-level sketches, by name; ``names`` restricts the reading.

    A sketch with no record in the tree stream is looked for in its
    equation's block (:func:`equation_blocks`), then in the block of the
    equation that cites it (:func:`operand_blocks`).
    """
    if (document_version(container) or 0) < SKETCHES_SINCE:
        return {}
    found = tree_stream(container)
    if not found:
        return {}
    data = found[1]
    sketch_ids = {k.id for k in read_keywords(container) if k.tag == "Sketch"}
    out = {}
    regions = list(_regions(data, named_objects(data)))
    for rank, (node, stop) in enumerate(regions):
        if node.ident not in sketch_ids or (names is not None and node.name not in names):
            continue
        large = picture_stop(data, regions, rank)
        if large is not None:
            # The frame of a sketch with a picture is its record's, with
            # ``plane`` as the fallback.
            frame = frame_record(data, node.end, large)
            out[node.name] = Sketch(node.name, segments(data, node.offset, large),
                                    frame[0] if frame is not None
                                    else plane(data, node.offset, large))
            continue
        out[node.name] = Sketch(node.name, segments(data, node.offset, stop),
                                _frame(data, node, stop))
    empty = [name for name, sketch in out.items() if not sketch.segments]
    config = _payload(container, found[0].removesuffix("__ZLB").removesuffix("-ResolvedFeatures"))
    if empty and config is not None and config[0] != found[0]:
        blocks = equation_blocks(config[1])
        for name in empty:
            if name in blocks:
                out[name].segments = segments(config[1], *blocks[name])
        cited = operand_blocks(config[1]) if any(not out[name].segments for name in empty) else {}
        for name in empty:
            if not out[name].segments and name in cited:
                out[name].segments = segments(config[1], *cited[name])
    return out
