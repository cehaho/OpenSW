"""What a SolidWorks document says about itself: its properties.

Nothing is computed here: each value is **read** where SolidWorks wrote it,
and a missing value stays missing, never inferred. Every stream involved is
plain XML (established by difference: copies of one part, each with one
property more, checked against what the SolidWorks API reads back):

``docProps/ISolidWorksInformation.xml``
    The summary (title, subject, author, keywords, comments), under the
    English names ``SW-Title``… Localised names that also sit there
    (``SW-Titre``…) come from the template and do not follow the summary.
    It also holds ``SW-MassProp-Config-<n>``, the mass properties, but
    **only** when a property links to them (``"SW-Mass@…"``); otherwise
    SolidWorks does not write them, and neither does this record.
``docProps/core.xml``
    Created and saved dates, in UTC, and who saved.
``docProps/custom.xml``
    Custom properties: name, type, displayed value. A linked property keeps
    its expression in an unnamed entry whose ``pid`` is 2**24 + its own.
``docProps/Config-<n>-Properties.xml``
    The properties of configuration ``n``, same layout.
``swXmlContents/Features``
    The configurations, name and id (``swConfiguration``). ``<n>`` in the
    stream names above is that id, not the configuration's rank. Assemblies
    and drawings have no such stream, so no configuration here.
``swXmlContents/MATERIALTREE``
    Each configuration's material, as UTF-16 XML: its name, its library, and
    its physical properties as the part keeps them.
``PreviewPNG``
    The preview image, a plain PNG.
``Preview``
    The legacy preview, a Windows 256-colour bitmap (DIB) behind a 4-byte
    length, larger than ``PreviewPNG``; shown as a PNG, pixel for pixel.
``SheetPreviews/SheetNames`` and ``Images/Sheet_<n>``
    In a drawing: each sheet's name, in order, and the PNG SolidWorks keeps
    of it, at the same rank. A sheet never displayed has no image.
"""

from __future__ import annotations

import re
import struct
import zlib
from itertools import pairwise
from xml.etree import ElementTree

from .container import Container

#: The summary: our key, the name SolidWorks writes.
RESUME = {
    "titre": "SW-Title",
    "sujet": "SW-Subject",
    "auteur": "SW-Author",
    "mots_cles": "SW-Keywords",
    "commentaires": "SW-Comments",
}
#: The unnamed entry that holds a linked property's expression.
_EXPRESSION = 1 << 24
#: ``swCustomInfoType_e.swCustomInfoDate``: the displayed value is in
#: ``FPVals``; the ``vt:filetime`` one is a UTC date.
_DATE = "64"
#: The quantities of ``SW-MassProp-Config-<n>`` whose rank is known
#: (measured on a 10 mm cube; the inertia on the cube and an offset block);
#: the last two stay unnamed. The inertia is about the centre of mass, in
#: model axes: Lxx Lyy Lzz Lxy Lxz Lyz, in kg·m², the products with the
#: sign of the SolidWorks API (``GetMomentOfInertia(0)``).
MASSE = {"centre_m": slice(0, 3), "volume_m3": 3, "aire_m2": 4, "masse_kg": 5,
         "inertie_kg_m2": slice(6, 12)}

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_NS_SW = "http://www.solidworks.com/sw2003/schema"
_NS_CORE = {"dc": "http://purl.org/dc/elements/1.1/",
            "dcterms": "http://purl.org/dc/terms/",
            "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"}


def _xml(container: Container, name: str):
    blob = container.get(name)
    if blob is None:
        return None
    try:
        return ElementTree.fromstring(bytes(blob.data))
    except ElementTree.ParseError:
        return None


def _text(prop) -> str:
    for child in prop:
        return "".join(child.itertext())
    return ""


def properties(root) -> list[dict]:
    """The named properties of a ``custom.xml`` or ``Config-N-Properties.xml``,
    in file order: ``{nom, valeur, type, expression}``."""
    if root is None:
        return []
    out, expressions = [], {}
    for section in root.iter("propertySection"):
        if section.get("name") != "UserDefinedProperties":
            continue
        for prop in section.iter("property"):
            pid, name = int(prop.get("pid", 0)), prop.get("name") or ""
            if not name:
                if pid > _EXPRESSION:
                    expressions[pid - _EXPRESSION] = _text(prop)
                continue
            value = _text(prop)
            if prop.get("TypeID") == _DATE:
                fp = prop.find("FPVals")
                if fp is not None:
                    value = _text(fp)
            out.append({"nom": name, "valeur": value, "type": prop.get("TypeID"),
                        "pid": pid})
    for p in out:
        p["expression"] = expressions.get(p.pop("pid"))
    return out


def _information(container: Container) -> dict[str, str]:
    root = _xml(container, "docProps/ISolidWorksInformation.xml")
    if root is None:
        return {}
    return {p.get("name"): _text(p) for p in root.iter("property") if p.get("name")}


def _dates(container: Container) -> dict[str, str]:
    root = _xml(container, "docProps/core.xml")
    if root is None:
        return {}
    out = {}
    for key, path in (("cree", "dcterms:created"), ("enregistre", "dcterms:modified"),
                      ("enregistre_par", "dc:lastModifiedBy")):
        e = root.find(path, _NS_CORE)
        if e is not None and e.text:
            out[key] = e.text
    return out


def _materials(container: Container) -> dict[str, dict]:
    """Each configuration's material, by configuration name."""
    blob = container.get("swXmlContents/MATERIALTREE")
    if blob is None:
        return {}
    data = bytes(blob.data)
    text = data.decode("utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode()
    # The declaration names an encoding the string no longer has.
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return {}
    by_name = {}
    for m in root.iter("material"):
        physical = {}
        for p in m.findall("physicalproperties/*"):
            physical[p.tag] = {"nom": p.get("displayname"), "valeur": p.get("value")}
        by_name[m.get("name")] = physical
    out = {}
    for c in root.iter("configuration"):
        library, _, rest = (c.get("material") or "").partition("|")
        name = rest.rsplit("|", 1)[0] if "|" in rest else rest
        out[c.get("name")] = {"nom": name, "base": library,
                              "physiques": by_name.get(f"{library}|{name}", {})}
    return out


def _mass(values: str) -> dict:
    numbers = [float(x) for x in values.split(",")]
    out = {key: numbers[r] for key, r in MASSE.items()}
    out["autres"] = numbers[12:]
    return out


#: A UTF-16 MFC ``CString`` (``FF FE FF``, a one-byte length).
_CSTRING = re.compile(rb"\xff\xfe\xff(.)", re.S)


def linked_texts(container: Container) -> list[dict]:
    """The linked texts of a drawing (a title block, a note): each
    ``$PRP…`` expression and the value it shows, as SolidWorks wrote
    them side by side in ``Contents/Definition``."""
    blob = container.get("Contents/Definition")
    if blob is None:
        return []
    data = blob.data
    strings = []
    for m in _CSTRING.finditer(data):
        end = m.end() + 2 * m.group(1)[0]
        strings.append((m.start(), end, data[m.end():end].decode("utf-16-le", "replace")))
    out, seen = [], set()
    for (_, end, text), (start, _, value) in pairwise(strings):
        if text.startswith("$PRP") and start == end and value and (text, value) not in seen:
            seen.add((text, value))
            out.append({"expression": text, "valeur": value})
    return out


#: The body shared by the parameters of ``Contents/Definition``, between the
#: name and the value.
_PARAMETER_BODY = bytes.fromhex("0000000000000040ffffffff00000000fffeff000000")
_LENGTH_CLASS = b"moLengthParameter_c"
#: An angular dimension: the same body, the value in radians.
_ANGLE_CLASS = b"moAngleParameter_c"
_TOLERANCE_CLASS = bytes.fromhex("ffff01001000") + b"moDimTolerance_c"


def _parameter(data: bytes, name_start: int) -> tuple[str, float, int] | None:
    """``(name, value, end)`` of a parameter body whose name starts at
    ``name_start``, or ``None`` if what follows is not a dimension's."""
    if data[name_start:name_start + 3] != bytes.fromhex("fffeff"):
        return None
    name_end = name_start + 4 + 2 * data[name_start + 3]
    body = name_end + len(_PARAMETER_BODY)
    if data[name_end:body] != _PARAMETER_BODY or body + 8 > len(data):
        return None
    name = data[name_start + 4:name_end].decode("utf-16-le", "replace")
    return name, struct.unpack_from("<d", data, body)[0], body + 8


def dimensions(container: Container) -> dict:
    """The length dimensions of a drawing as numbers: name, value in metres
    and, when there is one, the tolerance: its type (``swTolType_e``), its
    upper and lower deviations, in metres.

    MFC writes the class name at the first dimension only; the next ones
    carry a two-byte class reference, followed by the same two bytes as the
    first after its class name. A tolerance is read only when written with
    its class name; those with a reference only are counted in
    ``non_lues``."""
    blob = container.get("Contents/Definition")
    return dimensions_in(blob.data if blob is not None else b"")


def dimensions_in(data: bytes) -> dict:
    """The length and angle parameters of a stream, as :func:`dimensions`:
    a drawing's, or an assembly's mate distances. A radius or diameter
    dimension is a length holding the value shown (the diameter for a Ø
    dimension); its symbol is in the displayed text."""
    out = {"cotes": [], "non_lues": 0}
    starts = []
    for cls, kind in ((_LENGTH_CLASS, "longueur"), (_ANGLE_CLASS, "angle")):
        i = data.find(cls)
        if i < 0:
            continue
        mark = data[i + len(cls):i + len(cls) + 2]
        starts.append((i + len(cls) + 2, kind))
        j = starts[-1][0]
        while (j := data.find(mark + bytes.fromhex("fffeff"), j + 1)) >= 0:
            if j >= 2 and data[j - 1] & 0x80:  # a class reference, "xx 8y"
                starts.append((j + 2, kind))
    for start, kind in sorted(starts):
        read = _parameter(data, start)
        if read is None:
            continue
        name, value, end = read
        if kind == "angle":
            # An angle's tolerance is not measured: not read.
            out["cotes"].append({"nom": name, "genre": "angle", "valeur_rad": value,
                                 "tolerance": None})
            continue
        tolerance = None
        if data[end + 1:end + 1 + len(_TOLERANCE_CLASS)] == _TOLERANCE_CLASS:
            t = end + 1 + len(_TOLERANCE_CLASS)
            kind = int.from_bytes(data[t:t + 4], "little")
            upper, lower = struct.unpack_from("<2d", data, t + 8)
            tolerance = {"type": kind, "haut_m": upper, "bas_m": lower}
        elif data[end + 2] & 0x80 and data[end + 3:end + 11].count(0) >= 7:
            # A reference followed by a type and zeros: likely a tolerance
            # already named earlier, which is not followed.
            out["non_lues"] += 1
        out["cotes"].append({"nom": name, "genre": "longueur", "valeur_m": value,
                             "tolerance": tolerance})
    return out


def _dib_png(data: bytes) -> bytes | None:
    """The legacy ``Preview`` stream as a PNG, pixel for pixel, or ``None``.

    A u32 length, a 40-byte BITMAPINFOHEADER, the palette, the pixels. 8-bit
    DIBs only, RLE8 or uncompressed: the palette becomes the PNG's own
    (colour type 3), so no colour is converted. Rows are bottom-up when the
    height is positive; pixels an RLE8 delta or early end of line skips keep
    index 0, as Windows draws them.
    """
    if len(data) < 44 or struct.unpack_from("<I", data, 4)[0] != 40:
        return None
    w, height = struct.unpack_from("<ii", data, 8)
    bits = struct.unpack_from("<H", data, 18)[0]
    compression = struct.unpack_from("<I", data, 20)[0]
    h = abs(height)
    if bits != 8 or compression not in (0, 1) or not (0 < w <= 20_000 and 0 < h <= 20_000):
        return None
    colours = struct.unpack_from("<I", data, 36)[0] or 256
    start = 44 + 4 * colours
    if colours > 256 or start > len(data):
        return None
    palette = b"".join(data[44 + 4 * k : 47 + 4 * k][::-1] for k in range(colours))
    pixels = data[start:]
    rows = [bytearray(w) for _ in range(h)]
    if compression == 0:
        step = (w + 3) & ~3
        if len(pixels) < step * h:
            return None
        for y in range(h):
            rows[y][:] = pixels[y * step : y * step + w]
    else:
        x = y = i = 0
        n = len(pixels)
        while i + 1 < n:
            count, value = pixels[i], pixels[i + 1]
            i += 2
            if count:
                end = min(x + count, w)
                if y < h and end > x:
                    rows[y][x:end] = bytes([value]) * (end - x)
                x += count
            elif value == 0:
                x, y = 0, y + 1
            elif value == 1:
                break
            elif value == 2:
                if i + 1 >= n:
                    return None
                x, y = x + pixels[i], y + pixels[i + 1]
                i += 2
            else:
                run = pixels[i : i + value]
                if len(run) < value:
                    return None
                end = min(x + value, w)
                if y < h and end > x:
                    rows[y][x:end] = run[: end - x]
                x += value
                i += value + (value & 1)
        else:
            return None
    if height > 0:
        rows.reverse()

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    raw = b"".join(b"\0" + bytes(row) for row in rows)
    return (PNG_SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0))
            + chunk(b"PLTE", palette)
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


#: The mark of an MFC UTF-16 string, followed by its length on one byte.
_UTF16 = b"\xff\xfe\xff"


def sheets(container: Container) -> list[dict]:
    """``[{"nom", "png"}]``: a drawing's sheets and the image SolidWorks
    keeps of each, ``None`` when it has none.

    ``SheetPreviews/SheetNames``: a u16, the number of sheets, then their
    names as MFC strings (``ff fe ff``, length, UTF-16). The image of the
    sheet of rank ``n`` is ``Images/Sheet_<n>``.
    """
    stream = container.get("SheetPreviews/SheetNames")
    if stream is None or len(stream.data) < 2:
        return []
    d = stream.data
    out = []
    at = 2
    for n in range(struct.unpack_from("<H", d, 0)[0]):
        if d[at : at + 3] != _UTF16 or at + 4 > len(d):
            break
        end = at + 4 + 2 * d[at + 3]
        name = d[at + 4 : end].decode("utf-16-le", "replace")
        image = container.get(f"Images/Sheet_{n}")
        png = image.data if image is not None and image.data[:8] == PNG_SIGNATURE else None
        out.append({"nom": name, "png": png})
        at = end
    return out


def read_properties(container: Container) -> dict:
    """Everything the document says about itself, read, never computed."""
    info = _information(container)
    configs_found = []
    root = _xml(container, "swXmlContents/Features")
    if root is not None:
        configs_found = [(int(e.get("swID")), e.get("swName"))
                         for e in root.iter(f"{{{_NS_SW}}}swConfiguration")
                         if (e.get("swID") or "").isdigit()]
    streams = {b.name for b in container.blobs}
    configs = []
    for n, name in configs_found:
        mass = info.get(f"SW-MassProp-Config-{n}")
        configs.append({
            "nom": name,
            "proprietes": properties(_xml(container, f"docProps/Config-{n}-Properties.xml")),
            # Was this configuration's geometry saved in the file?
            "geometrie": f"Contents/Config-{n}-Partition" in streams,
            "masse": _mass(mass) if mass else None,
        })
    materials = _materials(container)
    for c in configs:
        c["materiau"] = materials.get(c["nom"])
    preview = container.get("PreviewPNG")
    png = bytes(preview.data) if preview is not None else b""
    legacy = container.get("Preview")
    return {
        "resume": {key: info.get(name, "") for key, name in RESUME.items()},
        **_dates(container),
        "proprietes": properties(_xml(container, "docProps/custom.xml")),
        "configurations": configs,
        "cotes": dimensions(container)["cotes"],
        "liens": linked_texts(container),
        "apercu_png": png if png.startswith(PNG_SIGNATURE) else None,
        "apercu_grand_png": _dib_png(bytes(legacy.data)) if legacy is not None else None,
        "feuilles": sheets(container),
    }
