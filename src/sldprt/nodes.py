"""Tree nodes read in the objects' bodies: id, type, children.

:mod:`sldprt.features` finds the tree stream. This module goes into the
serialised objects through the one part every node shares, without decoding
class by class.

**Every named object starts with a ``moNodeName_c``.** Folder, feature,
sketch or dimension: the tag of that class (``K``, specific to each file),
then

    CString name · u32 flags · u32 flags · i32 id · u32 0

The id is the ``KeyWords`` one; a dimension has ``−1``. ``K`` is the most
frequent tag in front of a string of the stream.

**A node** goes on with a ``CString``, six bytes, then a ``u16``: the
number of its children (recent saves); or two bytes then that number (older
ones). Each child is either written inline (the object follows) or a
**back-reference** to an object already written (``u16`` < ``0x8000``). The
stream is flat, in creation order: an absorbed sketch is written **before**
the feature that absorbs it, and the feature cites it by reference.

What is not done: the exact rank of an object in the MFC table would need
the length of every object, so every class decoded. Instead a child
reference goes to the last free top-level sketch written before the parent,
smaller references to older sketches, unless archive-index anchors resolve
it (:func:`resolve_references`). A child written inline hides the rest of
the list, whose position depends on its length: it stays ``None``.
"""

from __future__ import annotations

import re
import struct
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise

from .container import Container
from .features import read_keywords, tree_stream

_UTF16 = b"\xff\xfe\xff"

#: MFC class back-reference tags.
_CLASS_LOW, _CLASS_HIGH = 0x8001, 0x8FFE

#: What the API calls a sketch node: the ``KeyWords`` tag.
SKETCH_TAG = "Sketch"


@dataclass(frozen=True)
class Named:
    """A named object of the tree stream."""

    #: Offset of the name string (its ``ff fe ff`` introducer).
    offset: int
    name: str
    #: The node's ``KeyWords`` id, ``−1`` for a dimension.
    ident: int
    #: Offset just after the id.
    end: int
    #: Written in another node's child list rather than at the top level.
    nested: bool
    #: The second flags word of the header.
    flags: int = 0

    @property
    def is_dimension(self) -> bool:
        return self.ident == -1


def node_name_tag(data: bytes) -> bytes | None:
    """``K``: the most frequent class tag in front of a string of the stream."""
    seen: Counter[bytes] = Counter()
    at = data.find(_UTF16)
    while at >= 0:
        tag = data[at - 2 : at]
        if len(tag) == 2 and 0x80 <= tag[1] <= 0x8F:
            seen[tag] += 1
        at = data.find(_UTF16, at + 1)
    return seen.most_common(1)[0][0] if seen else None


def named_objects(data: bytes) -> list[Named]:
    """Every named object of the stream, in order."""
    tag = node_name_tag(data)
    if tag is None:
        return []
    needle = tag + _UTF16
    out = []
    at = data.find(needle)
    while at >= 0:
        start = at + 2
        length = data[start + 3]
        stop = start + 4 + 2 * length
        if 0 < length < 0xFF and stop + 12 <= len(data):
            try:
                name = data[start + 4 : stop].decode("utf-16-le")
            except UnicodeDecodeError:
                name = ""
            if name and name.isprintable():
                flags, ident = struct.unpack_from("<Ii", data, stop + 4)
                klass = int.from_bytes(data[at - 2 : at], "little")
                count = int.from_bytes(data[at - 4 : at - 2], "little")
                nested = _CLASS_LOW <= klass <= _CLASS_HIGH and 1 <= count <= 3
                out.append(Named(start, name, ident, stop + 12, nested, flags))
        at = data.find(needle, at + 1)
    return out


def _children_count(data: bytes, end: int) -> int | None:
    """Offset of the children count, from the header, or ``None``."""
    # The word before it is 0, or 0x8000 (an extrusion would otherwise have
    # no children).
    if data[end : end + 4] not in (b"\x00\x00\x00\x00", b"\x00\x80\x00\x00"):
        return None
    if data[end + 4 : end + 7] == _UTF16:
        return end + 8 + 2 * data[end + 7] + 6
    return end + 6


#: Beyond index 0x7ffe the archive writes a reference in long form: ``ff 7f``
#: then the index as a ``u32``.
_LONG = 0x7FFF


def _references(data: bytes, at: int, count: int) -> tuple[list[int], int]:
    """The back-references at the head of a child list, and the offset after
    them. Reading stops at the first child written inline (a class tag) or
    null. Long form: ``ff 7f`` then a ``u32``."""
    refs: list[int] = []
    cursor = at
    while len(refs) < count and cursor + 2 <= len(data):
        ref = int.from_bytes(data[cursor : cursor + 2], "little")
        if ref == _LONG:
            long = int.from_bytes(data[cursor + 2 : cursor + 6], "little")
            if not _LONG <= long < 0x80000000:
                break
            refs.append(long)
            cursor += 6
            continue
        if not 0 < ref < _LONG:
            break
        refs.append(ref)
        cursor += 2
    return refs, cursor


def anchors(data: bytes, nodes: list[Named], keyword_types: dict[int, str]) -> list[tuple[int, int]]:
    """The nodes whose archive index is certain, as increasing ``(offset, index)``.

    ``CArchive`` numbers classes and objects in a single table, in writing
    order: an unseen class takes an index at its ``ff ff`` declaration, then
    the object that follows it. The first node of a class is preceded by
    that declaration, the next ones by the tag ``0x8000 | c``: the class has
    index ``c``, its first node ``c + 1``.

    A class two tags point to, a tag two classes compete for, or indexes
    that do not grow with the offset: nothing is returned."""
    real = [n for n in nodes if not n.is_dimension]
    intro: dict[str, Named] = {}
    uses: dict[int, list[Named]] = {}
    for n in real:
        found = _class_before(data, n)
        if isinstance(found, str):
            klass = _class_type(found)
            if klass:
                intro.setdefault(klass, n)
        elif found is not None:
            uses.setdefault(found, []).append(n)
    # A tag is given a class only if a single node class introduced before
    # its first use has the ``KeyWords`` type of its users: the oldest one,
    # which a vote would pick, can be wrong.
    assigned: dict[int, str] = {}
    changed = True
    while changed:
        changed = False
        taken = set(assigned.values())
        for tag, users in uses.items():
            if tag in assigned:
                continue
            first = min(n.offset for n in users)
            kinds = {keyword_types.get(n.ident) for n in users}
            if None in kinds or len(kinds) != 1:
                continue
            candidates = [k for k, n in intro.items() if n.offset < first and k not in taken
                          and keyword_types.get(n.ident) in kinds]
            if len(candidates) == 1:
                assigned[tag] = candidates[0]
                changed = True
    out = sorted((intro[k].offset, tag - 0x8000 + 1) for tag, k in assigned.items())
    if any(b[1] <= a[1] for a, b in pairwise(out)):
        return []
    return out


def resolve_references(data: bytes, nodes: list[Named],
                       keyword_types: dict[int, str]) -> dict[int, Named]:
    """The child-list back-references the anchors are enough to resolve:
    ``cited index → node``.

    Without counting objects: the index grows with the offset, a reference
    points to a node written before the one citing it, and two different
    references point to two different nodes. Between two consecutive
    anchors, the references whose index falls between theirs share, in
    order, the nodes written between them. A reference is resolved when the
    leftmost and rightmost assignments give it the same node; a reference
    equal to an anchor's index is that anchor. Below the first anchor,
    nothing is resolved: the archive starts in another stream."""
    real = [n for n in nodes if not n.is_dimension]
    sure = anchors(data, nodes, keyword_types)
    if not sure:
        return {}
    by_offset = {n.offset: n for n in real}
    first: dict[int, int] = {}  # cited index → offset of the first node citing it
    for n in real:
        at = _children_count(data, n.end)
        if at is None:
            continue
        refs, _ = _references(data, at + 2, int.from_bytes(data[at : at + 2], "little"))
        for ref in refs:
            first[ref] = min(first.get(ref, n.offset), n.offset)
    offsets, index = [a[0] for a in sure], [a[1] for a in sure]
    out: dict[int, Named] = {}
    groups: dict[int, list[int]] = {}
    for ref in sorted(first):
        k = bisect_left(index, ref)
        if k < len(index) and index[k] == ref:
            if offsets[k] < first[ref]:
                out[ref] = by_offset[offsets[k]]
        elif k:
            groups.setdefault(k, []).append(ref)
    for k, refs in groups.items():
        stop = offsets[k] if k < len(offsets) else len(data)
        candidates = [n for n in real if offsets[k - 1] < n.offset < stop]
        places = [n.offset for n in candidates]
        ceilings = [bisect_left(places, first[ref]) - 1 for ref in refs]
        left: list[int] = []
        for ceiling in ceilings:
            rank = left[-1] + 1 if left else 0
            if rank > ceiling:
                break
            left.append(rank)
        if len(left) < len(refs):
            continue  # more references than nodes: the assumption fails here
        right = [0] * len(refs)
        following = len(candidates)
        for i in reversed(range(len(refs))):
            following = min(following - 1, ceilings[i])
            right[i] = following
        for ref, g, d in zip(refs, left, right, strict=True):
            if g == d:
                out[ref] = candidates[g]
    return out


def children(data: bytes, sketch_ids: set[int], keyword_types: dict[int, str] | None = None,
             resolved: dict[str, list[bool]] | None = None) -> dict[str, list[str | None]]:
    """Each node's children, in order; ``None`` where reading stops.

    With ``keyword_types`` (the ``KeyWords`` type by id), the back-references
    the anchors resolve (:func:`resolve_references`) take the resolved node,
    sketch or not; the others keep the free-sketch rule. ``resolved``
    receives, per node, which of its references were resolved that way."""
    every = named_objects(data)
    nodes = [n for n in every if not n.is_dimension]
    sure = resolve_references(data, every, keyword_types) if keyword_types is not None else {}
    reserved = {n.name for n in sure.values()}
    # A library feature cites its member features, not sketches: what the
    # anchors do not resolve stays ``None``, and no free sketch is given to it.
    types = node_types(data, every, keyword_types) if keyword_types is not None else {}
    libraries = {name for name, klass in types.items() if klass == "LibraryFeature"}
    out: dict[str, list[str | None]] = {}
    written: list[str] = []
    taken: set[str] = set()
    backward: list[tuple[int, str, int]] = []  # (reference, node, child rank) of cited sketches
    for index, node in enumerate(nodes):
        if node.name in out:
            continue
        if node.ident in sketch_ids and not node.nested:
            written.append(node.name)
        at = _children_count(data, node.end)
        if at is None:
            continue
        count = int.from_bytes(data[at : at + 2], "little")
        refs, cursor = _references(data, at + 2, count)
        found: list[str | None] = []
        if refs:
            read = [sure.get(ref) for ref in refs]
            rest = [i for i, r in enumerate(read) if r is None]
            free = [w for w in written if w not in taken and w != node.name and w not in reserved]
            pick = (free[-len(rest) :] if rest and len(free) >= len(rest)
                    and node.name not in libraries else [])
            mapped: list[str | None] = [r.name if r else None for r in read]
            for rank, i in enumerate(sorted(rest, key=refs.__getitem__)):
                mapped[i] = pick[rank] if pick else None
            found += mapped
            taken.update(x for x in mapped if x)
            backward += [(ref, node.name, rank)
                         for rank, (ref, x) in enumerate(zip(refs, mapped, strict=True))
                         if x and read[rank] is None]
            if resolved is not None:
                resolved[node.name] = [r is not None for r in read]
        # Inline children: each is the next named node. Move to the next one
        # only if this one has no children itself; otherwise its descendants
        # come in between, and reading stops.
        following = [m for m in nodes[index + 1 :] if m.offset > cursor]
        while len(found) < count and following:
            child = following.pop(0)
            found.append(child.name)
            at_child = _children_count(data, child.end)
            if at_child is None or int.from_bytes(data[at_child : at_child + 2], "little"):
                break
        found += [None] * (count - len(found))
        out[node.name] = found
    _reorder(out, backward, {n.name: n.offset for n in nodes})
    return out


def _reorder(out: dict[str, list[str | None]], backward: list[tuple[int, str, int]],
             offsets: dict[str, int]) -> None:
    """Give back-references the writing order of their sketches.

    A back-reference is an archive index, and the archive numbers its
    objects in writing order: of two references, the smaller points to the
    sketch written first. Taking "the last free sketches" for each node
    ignores this when two sketches precede two features. The chosen sketches
    stay the same; only who takes which changes, and only if each one stays
    written before the node that cites it.
    """
    if not backward:
        return
    by_reference = sorted(backward)
    chosen = sorted((out[name][rank] for _, name, rank in backward), key=offsets.__getitem__)
    if any(offsets[sketch] >= offsets[name]
           for (_, name, _), sketch in zip(by_reference, chosen, strict=True)):
        return
    for (_, name, rank), sketch in zip(by_reference, chosen, strict=True):
        out[name][rank] = sketch


def read_children(container: Container,
                  resolved: dict[str, list[bool]] | None = None) -> dict[str, list[str | None]]:
    """The tree hierarchy, read from the tree stream and ``KeyWords``.

    ``resolved`` receives, per node, which of its back-references the
    anchors resolve (:func:`resolve_references`) rather than the free-sketch
    rule."""
    found = tree_stream(container)
    if not found:
        return {}
    keywords = read_keywords(container)
    sketches = {k.id for k in keywords if k.tag == SKETCH_TAG}
    return children(found[1], sketches, {k.id: k.type for k in keywords}, resolved)


def _regions(data: bytes, items: list[Named]) -> list[tuple[Named, int]]:
    """Each top-level node and the end of its region: the next one."""
    tops = [n for n in items if not n.is_dimension and not n.nested]
    return [(n, tops[i + 1].offset if i + 1 < len(tops) else len(data))
            for i, n in enumerate(tops)]


# --------------------------------------------------------------------------
# A node's class: its type, in the API's words.

#: A node's MFC class is named ``mo<API type>_c``: ``moICE_c``,
#: ``moRefPlane_c``, ``mo3DProfileFeature_c``.
_NODE_CLASS = re.compile(r"mo(\w+)_c")

#: The node classes that do not follow that pattern, and their API type.
_OUTSIDE_PATTERN = {"Chamfer_c": "Chamfer", "VarFillet_c": "VarFillet"}


def _class_type(name: str) -> str | None:
    m = _NODE_CLASS.fullmatch(name)
    return m[1] if m else _OUTSIDE_PATTERN.get(name)


def _class_before(data: bytes, node: Named) -> str | int | None:
    """What precedes the node's ``K`` tag: the name of a class introduced
    there (``ff ff`` · schema · length · name), or the reference
    ``0x8000 | index`` to a class already introduced, or ``None``.

    Beyond index 0x7ffe, the archive writes the reference in long form:
    ``ff 7f`` then ``u32 0x80000000 | index``. It is returned as
    ``0x8000 + index``, in the same order as the short ones."""
    at = node.offset - 2
    for length in range(3, 64):
        head = at - length - 6
        if (head >= 0 and data[head : head + 2] == b"\xff\xff"
                and int.from_bytes(data[head + 4 : head + 6], "little") == length):
            return data[head + 6 : at].decode("ascii", "replace")
    if at >= 6 and data[at - 6 : at - 4] == b"\xff\x7f":
        long = int.from_bytes(data[at - 4 : at], "little")
        if long & 0x80000000 and 0x7FFF <= long & 0x7FFFFFFF < 0x1000000:
            return 0x8000 + (long & 0x7FFFFFFF)
    tag = int.from_bytes(data[at - 2 : at], "little")
    return tag if 0x8000 < tag < 0xFFFF else None


def node_types(data: bytes, nodes: list[Named], keyword_types: dict[int, str]) -> dict[str, str]:
    """Each node's API type, from its MFC class.

    The class introduced just before a node's name is ``mo<type>_c``. Later
    nodes of the same class only write its reference, ``0x8000 | index``;
    the index is the class's rank in the archive table, so **increasing
    with the offset of its introduction**, and a class is introduced before
    its first use. References are resolved in index order: each takes,
    among the classes introduced after the previous reference's and before
    its own first use, the one whose introducing node most often shares its
    ``KeyWords`` type (``keyword_types``, by id) with the nodes citing it.

    What is not resolved is left out: the caller falls back on ``KeyWords``.
    """
    intros: list[tuple[int, str, str | None]] = []
    refs: dict[int, list[Named]] = {}
    out: dict[str, str] = {}
    for node in nodes:
        if node.is_dimension:
            continue
        found = _class_before(data, node)
        if isinstance(found, str):
            klass = _class_type(found)
            if klass:
                out[node.name] = klass
                intros.append((node.offset, klass, keyword_types.get(node.ident)))
        elif found is not None:
            refs.setdefault(found, []).append(node)
    floor = -1
    for tag in sorted(refs):
        users = refs[tag]
        first = min(n.offset for n in users)
        votes = Counter(keyword_types.get(n.ident) for n in users)
        candidates = [(votes[kw], -offset, offset, klass) for offset, klass, kw in intros
                      if floor < offset < first and votes[kw]]
        if not candidates:
            continue
        _, _, floor, klass = max(candidates)
        for n in users:
            out.setdefault(n.name, klass)
    return out


#: Precedes, in the region of a folder-end marker (``___EndTag___``), four
#: null bytes then the archive reference that designates it (u16).
_END_ANCHOR = b"\x05\x00\x01\x00\x00\x00\x01\x00\x00\x00"
#: Where the last member writes that reference: in its last bytes, just
#: before the next object's tag.
END_WINDOW = 12


def folder_end(data: bytes, start: int, stop: int) -> int | None:
    """The reference a folder's last member writes, read in the region
    ``data[start:stop]`` of its end marker, or ``None``.

    The stream writes the end marker just after the folder's start,
    whatever it holds; **the extent is written by the last member**, which
    closes its region with a ``u16`` equal to the value read here plus two.
    An empty folder carries the reference in its own marker's region.
    """
    a = data.rfind(_END_ANCHOR, start, stop)
    if a < 0 or a + 16 > stop or any(data[a + 10 : a + 14]):
        return None
    return int.from_bytes(data[a + 14 : a + 16], "little") + 2


def closes_folder(data: bytes, stop: int, reference: int) -> bool:
    """True if the region ending at ``stop`` closes with ``reference``."""
    return data.find(reference.to_bytes(2, "little"), max(0, stop - END_WINDOW), stop) >= 0
