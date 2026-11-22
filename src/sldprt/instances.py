"""Shared-sketch instances (« X<k> ») read in the tree stream.

A sketch reused by a second feature comes back there as « X<k> », a
``moProfileFeature_c`` node like the others (even for a 3D sketch). In the
node's body, counted from the end of its header
(:attr:`sldprt.nodes.Named.end`):

- **+28**, ``u32``: ``0x65`` for an instance; for an ordinary sketch, its
  creation number;
- **+33**, ``u32``: 1 for an instance, 0 otherwise;
- further on, after the timestamp (``ff ff ff ff`` · eight bytes · ``02 00``
  · ``u32`` · ``01 00`` · twelve null bytes): a ``u32``, **the instance
  rank** (k − 1; 0 for an ordinary sketch), then **the place of the
  geometry object**. An ordinary sketch writes its object inline there (a
  class tag, ``0x8000 | rank``); **an instance writes an archive
  back-reference** there (``u16`` below ``0x7fff``, or ``ff 7f`` then a
  ``u32``): **the same reference a child list cites the original sketch
  by**.

Saves older than v12000 do not have that layout: nothing is read.

The reference is mapped to a name through the child lists
(:func:`sldprt.nodes.children`). An original that no child list cites is
not named (``origin`` is ``None``).
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from .nodes import _children_count, children, named_objects

#: In a sketch node's body: an instance's mark.
MARK_A, MARK_B = 28, 33
MARK = (0x65, 1)

#: The timestamp, then the instance rank (group 1) and the place of the
#: geometry object (it starts at the end of the match).
_PLACE = re.compile(rb"\xff\xff\xff\xff.{8}\x02\x00.{4}\x01\x00\x00{12}(.{4})", re.S)

#: How far from the end of the header to look for the place.
WINDOW = 400

#: Beyond 32 766 objects, an archive reference is written ``ff 7f`` · ``u32``.
LONG = 0x7FFF


@dataclass(frozen=True)
class Instance:
    """A shared-sketch instance, as its node writes it."""

    name: str
    #: The written rank: k − 1 for « X<k> ».
    rank: int
    #: The archive reference written in place of the geometry object.
    reference: int
    #: The name of the sketch this reference points to, when a child list
    #: cites it; ``None`` otherwise.
    origin: str | None = None


def _reference(data: bytes, at: int) -> tuple[int, int] | None:
    """(reference, length) of the back-reference written at ``at``, or
    ``None``: null, or a class tag (an inline object)."""
    if at + 6 > len(data):
        return None
    short = struct.unpack_from("<H", data, at)[0]
    if short == LONG:
        return struct.unpack_from("<I", data, at + 2)[0], 6
    return (short, 2) if 0 < short < LONG else None


def _citations(data: bytes, nodes, kids: dict[str, list[str | None]]) -> dict[int, set[str]]:
    """Archive reference → names of the children it cites in the lists."""
    out: dict[int, set[str]] = {}
    for node in nodes:
        names = kids.get(node.name)
        at = _children_count(data, node.end)
        if at is None or not names:
            continue
        cursor = at + 2
        for name in names:
            ref = _reference(data, cursor)
            if ref is None:
                break
            if name:
                out.setdefault(ref[0], set()).add(name)
            if ref[1] != 2:
                # The lists are assigned assuming two-byte references: after
                # a long one, the alignment is no longer certain.
                break
            cursor += 2
    return out


def instances(data: bytes, sketch_ids: set[int],
              kids: dict[str, list[str | None]] | None = None) -> dict[str, Instance]:
    """The sketch instances of the stream, by node name."""
    nodes = [n for n in named_objects(data) if not n.is_dimension]
    if kids is None:
        kids = children(data, sketch_ids)
    citations = _citations(data, nodes, kids)
    out: dict[str, Instance] = {}
    for i, node in enumerate(nodes):
        if node.ident not in sketch_ids or node.name in out or node.end + MARK_B + 4 > len(data):
            continue
        mark = (struct.unpack_from("<I", data, node.end + MARK_A)[0],
                struct.unpack_from("<I", data, node.end + MARK_B)[0])
        if mark != MARK:
            continue
        stop = min(node.end + WINDOW, nodes[i + 1].offset if i + 1 < len(nodes) else len(data))
        m = _PLACE.search(data, node.end, stop)
        ref = _reference(data, m.end()) if m else None
        if ref is None:
            continue
        names = citations.get(ref[0], set()) - {node.name}
        out[node.name] = Instance(node.name, struct.unpack("<I", m.group(1))[0], ref[0],
                                  next(iter(names)) if len(names) == 1 else None)
    return out
