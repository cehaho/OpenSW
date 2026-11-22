"""The streams that carry a part's feature tree, as far as sketches need them.

``Contents/Config-N-ResolvedFeatures``
    The tree itself, a serialised MFC ``CArchive`` graph in **display
    order**. Saves older than this stream keep it in ``Contents/Config-N``.
``swXmlContents/KeyWords``
    The search index SolidWorks writes, plain XML: an id, a name and a type
    per node. **Flat**: no display order, no nesting.

Only what the sketch reader uses is here; the features themselves (their
parameters, end conditions, equations) are not read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree

from .carve import carve_sections
from .container import Container

#: Introduces an MFC ``CString``: length, then UTF-16LE.
_UTF16_INTRO = b"\xff\xfe\xff"

#: The suffix SolidWorks gives the node that closes a folder of the tree.
END_TAG = "___EndTag___"

#: The tags ``KeyWords`` uses for a tree node. ``Configuration`` is left out:
#: it describes the configuration, not a node.
KEYWORD_TAGS = ("Feature", "Sketch", "Extrusion")


def carchive_strings(data: bytes, *, minimum: int = 1) -> list[tuple[int, str]]:
    """Every ``CString`` of the stream, as ``(offset, text)``.

    MFC writes a wide string as ``ff fe ff``, then a length in UTF-16 units
    (one byte, or ``ff`` and a ``u16``, or ``ff ff ff`` and a ``u32``), then
    the units. The offset is the introducer's, not the text's.
    """
    out: list[tuple[int, str]] = []
    off = 0
    while True:
        start = data.find(_UTF16_INTRO, off)
        if start < 0:
            break
        cursor = start + 3
        if cursor >= len(data):
            break
        length = data[cursor]
        cursor += 1
        if length == 0xFF:
            length = int.from_bytes(data[cursor : cursor + 2], "little")
            cursor += 2
            if length == 0xFFFF:
                length = int.from_bytes(data[cursor : cursor + 4], "little")
                cursor += 4
        raw = data[cursor : cursor + 2 * length]
        if len(raw) != 2 * length:
            off = start + 3
            continue
        try:
            text = raw.decode("utf-16-le")
        except UnicodeDecodeError:
            off = start + 3
            continue
        if len(text) >= minimum and all(c.isprintable() or c in "\t\n" for c in text):
            out.append((start, text))
        off = cursor + 2 * length
    return out


def _payload(container: Container, suffix: str) -> tuple[str, bytes] | None:
    """The stream named ``suffix``, unpacked if it is a ``__ZLB`` companion.

    OLE2 saves wrap some streams in a ``__ZLB`` companion made of framed
    sections; block saves give the content as is.
    """
    for blob in container.blobs:
        if blob.name == suffix or blob.name == suffix + "__ZLB":
            data = blob.data
            if blob.name.endswith("__ZLB"):
                regions = carve_sections(data)
                if regions:
                    data = b"".join(region.data for region in regions)
            return blob.name, data
    return None


def _payload_any(container: Container, suffix: str) -> tuple[str, bytes] | None:
    """Like :func:`_payload`, on any name ending: for streams named after
    the active configuration (``Config-0-…``, ``Config-2-…``)."""
    for blob in container.blobs:
        stem = blob.name[: -len("__ZLB")] if blob.name.endswith("__ZLB") else blob.name
        if stem.endswith(suffix):
            data = blob.data
            if blob.name.endswith("__ZLB"):
                regions = carve_sections(data)
                if regions:
                    data = b"".join(region.data for region in regions)
            return blob.name, data
    return None


def tree_stream(container: Container) -> tuple[str, bytes] | None:
    """The stream that carries the tree in display order:
    ``Config-N-ResolvedFeatures``, or ``Contents/Config-N`` in old saves
    that do not have it."""
    found = _payload_any(container, "-ResolvedFeatures")
    if found is not None:
        return found
    for blob in container.blobs:
        stem = blob.name[: -len("__ZLB")] if blob.name.endswith("__ZLB") else blob.name
        if re.fullmatch(r"Contents/Config-\d+", stem):
            return _payload(container, blob.name)
    return None


@dataclass(frozen=True)
class Keyword:
    """An entry of ``swXmlContents/KeyWords``."""

    id: int
    name: str
    type: str
    #: ``Feature``, ``Sketch`` or ``Extrusion``: the tag used.
    tag: str


def parse_keywords(xml: bytes) -> list[Keyword]:
    """Read the ``KeyWords`` index, sorted by numeric id.

    The stream sometimes starts with a length byte before the XML prolog. A
    node without a ``Type`` attribute takes its tag as type.
    """
    start = xml.find(b"<?xml")
    if start < 0:
        return []
    try:
        root = ElementTree.fromstring(xml[start:])
    except ElementTree.ParseError as exc:
        raise ValueError(f"unreadable KeyWords index: {exc}") from exc

    out: list[Keyword] = []
    for node in root:
        tag = node.tag.rpartition("}")[2]
        if tag not in KEYWORD_TAGS:
            continue
        ident = node.get("id")
        if ident is None or not ident.lstrip("-").isdigit():
            continue
        out.append(Keyword(id=int(ident), name=node.get("Name", ""),
                           type=node.get("Type") or tag, tag=tag))
    return sorted(out, key=lambda k: k.id)


def read_keywords(container: Container) -> list[Keyword]:
    """The part's ``KeyWords`` index, empty if the stream is missing."""
    found = _payload(container, "swXmlContents/KeyWords")
    return parse_keywords(found[1]) if found else []


def document_version(container) -> int | None:
    """The ``_MO_VERSION_NNNNN`` stamp of a container, or ``None``."""
    best = None
    for blob in container.blobs:
        name = blob.name
        if "_MO_VERSION_" not in name:
            continue
        digits = name.split("_MO_VERSION_", 1)[1].split("/", 1)[0]
        if digits.isdigit():
            best = max(best or 0, int(digits))
    return best
