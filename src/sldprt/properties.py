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
"""

from __future__ import annotations

import re
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
#: (measured on a 10 mm cube); the others stay unnamed.
MASSE = {"centre_m": slice(0, 3), "volume_m3": 3, "aire_m2": 4, "masse_kg": 5}

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
    out["autres"] = numbers[6:]
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
    return {
        "resume": {key: info.get(name, "") for key, name in RESUME.items()},
        **_dates(container),
        "proprietes": properties(_xml(container, "docProps/custom.xml")),
        "configurations": configs,
        "apercu_png": png if png.startswith(PNG_SIGNATURE) else None,
    }
