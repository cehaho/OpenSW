"""Read a SolidWorks assembly (``.SLDASM``) to **look at** it.

An assembly is the same container as a part (:func:`sldprt.open_container`
opens it as is). It does not carry its parts' exact geometry, which stays in
their ``.SLDPRT``, but two streams are enough to show it, both written
**already computed** by SolidWorks; nothing here is recomputed:

``swXmlContents/COMPINSTANCETREE``
    Plain XML: each component, its file, its states (suppressed, hidden)
    and its ``swTransform`` matrix, a 4×4 by rows
    ``(R₀, 0) (R₁, 0) (R₂, 0) (t, 1)``. A part point ``p`` lands in the
    assembly at ``p·R + t``.

``FaceTessellations/*``
    The display mesh of each **distinct part** (two instances of a part
    share it), face by face, as triangle strips. The ``Directory`` stream
    says which component reads which mesh. Older files have no such
    streams and keep the same strips in ``Contents/DisplayLists``.
"""

from __future__ import annotations

import base64
import math
import re
import sys
import xml.etree.ElementTree as ET
from array import array
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from struct import pack, unpack_from

from .container import Container, open_container

NS = "{http://www.solidworks.com/sw2003/schema}"
UTF16 = b"\xff\xfe\xff"

#: Header of a strip array: ``[4][8][2][k]`` then ``k`` lengths.
BANDES = (4, 8, 2)
#: Header of a vertex or normal array: ``[12][0x64][2][n]`` then ``n``
#: ``f32`` triplets (12 is a triplet's size in bytes).
POINTS = (12, 0x64, 2)


@dataclass
class Composant:
    """A placed part instance."""

    #: ``cube-1``: the part name and the instance number.
    nom: str
    fichier: str
    #: Sixteen numbers as ``MathTransform.ArrayData``: R (rows), t (m),
    #: scale, three zeros.
    matrice: tuple[float, ...]
    cache: bool = False
    supprime: bool = False
    #: Display mesh faces, each a list of triangles ``((x, y, z) ×3)`` in
    #: metres, **in the part's frame**.
    faces: list[list[tuple]] = field(default_factory=list)

    def placer(self, p) -> tuple[float, float, float]:
        """A part point (m) in the assembly (m): ``p·R + t``."""
        m = self.matrice
        return tuple(
            p[0] * m[j] + p[1] * m[3 + j] + p[2] * m[6 + j] + m[9 + j]
            for j in range(3))


@dataclass
class Mate:
    """An assembly mate, as the file names it."""

    nom: str
    #: ``swMateType_e``: 0 coincident, 5 distance (the two measured).
    type: int
    #: The mate class, ``moMateCoincident``, ``moMateDistanceDim_c``…
    classe: str
    #: The tree text: ``Coincident1 (cube<1>,cube<2>)``.
    libelle: str
    configuration: str


@dataclass
class Assembly:
    composants: list[Composant]
    contraintes: list[Mate] = field(default_factory=list)
    #: What was seen but not read, by reason.
    non_lus: dict[str, int] = field(default_factory=dict)


class AssemblyError(ValueError):
    """The file is not an assembly this module can read."""


# --- component tree ----------------------------------------------------------


def _matrice(texte: str) -> tuple[float, ...]:
    """``swTransform`` (a 4×4 by rows) in the API layout
    (``MathTransform.ArrayData``): R by rows, t, scale, three zeros."""
    v = tuple(float(x) for x in texte.split())
    if len(v) != 16:
        raise AssemblyError(f"swTransform with {len(v)} numbers")
    return (*v[0:3], *v[4:7], *v[8:11], *v[12:15], v[15], 0.0, 0.0, 0.0)


def _composer(enfant, parent) -> tuple[float, ...]:
    """A subassembly component's matrix, in the assembly."""
    r = [[sum(enfant[3 * i + k] * parent[3 * k + j] for k in range(3))
          for j in range(3)] for i in range(3)]
    t = [sum(enfant[9 + k] * parent[3 * k + j] for k in range(3)) + parent[9 + j]
         for j in range(3)]
    return (*r[0], *r[1], *r[2], *t, 1.0, 0.0, 0.0, 0.0)


IDENTITE = (1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0, 0, 0, 0)


def _arbre(xml: bytes) -> list[Composant]:
    racine = ET.fromstring(xml)
    fichiers = {f.get("id"): f for f in racine.iter(f"{NS}swFile")}
    modeles = {m.get("id"): m for m in racine.iter(f"{NS}swModel")}
    tete = next((f for f in fichiers.values()
                 if f.get("swDocType") == "ASSEMBLY"), None)
    if tete is None:
        raise AssemblyError("no ASSEMBLY file in the tree")
    # The assembly's model points to its file and holds references; a
    # subassembly has its own.
    def modele_de(fichier_id):
        return next((m for m in modeles.values()
                     if m.get("swFileRef") == fichier_id
                     and m.find(f"{NS}swReference") is not None), None)

    out: list[Composant] = []

    def descendre(modele, parent, prefixe, cache, supprime):
        for ref in modele.findall(f"{NS}swReference"):
            nom = f"{ref.get('swName')}-{ref.get('swReferenceNumber')}"
            matrice = _composer(_matrice(ref.get("swTransform")), parent)
            enfant = modeles.get(ref.get("swModelRef"))
            fichier = fichiers.get(enfant.get("swFileRef")) if enfant is not None else None
            c = cache or ref.get("swHidden") == "YES"
            s = supprime or ref.get("swSuppressed") == "YES"
            if fichier is not None and fichier.get("swDocType") == "ASSEMBLY":
                sous = modele_de(fichier.get("id"))
                if sous is not None:
                    descendre(sous, matrice, f"{prefixe}{nom}/", c, s)
                    continue
            chemin = fichier.get("swPath", "") if fichier is not None else ""
            out.append(Composant(f"{prefixe}{nom}", PureWindowsPath(chemin).name,
                                 matrice, c, s))

    modele = modele_de(tete.get("id"))
    if modele is None:
        raise AssemblyError("the assembly model has no reference")
    descendre(modele, IDENTITE, "", False, False)
    return out


# --- display mesh ------------------------------------------------------------


def _cstring(d: bytes, at: int):
    if d[at : at + 3] != UTF16 or at + 4 > len(d):
        return None
    fin = at + 4 + 2 * d[at + 3]
    if fin > len(d):
        return None
    try:
        return d[at + 4 : fin].decode("utf-16-le"), fin
    except UnicodeDecodeError:
        return None


def _repertoire(d: bytes) -> list[tuple[str, bytes]]:
    """``[(component, bytes of its entry)…]`` of the ``Directory`` stream.

    An entry starts with ``name@Assem…``; what follows it, up to the next
    one, holds the 8-byte key of the mesh it reads.
    """
    noms = []
    for m in re.finditer(re.escape(UTF16), d):
        lu = _cstring(d, m.start())
        if lu and "@" in lu[0]:
            noms.append((m.start(), lu[0].split("@")[0], lu[1]))
    out = []
    for k, (_, nom, fin) in enumerate(noms):
        suite = noms[k + 1][0] if k + 1 < len(noms) else len(d)
        out.append((nom, d[fin:suite]))
    return out


def _faces(d: bytes, debut: int, fin: int) -> list[list[tuple]]:
    """The faces of a block: strips, vertices, normals, checked together."""
    faces = []
    i = debut
    while i < fin - 16:
        if unpack_from("<3I", d, i) != BANDES:
            i += 1
            continue
        k = unpack_from("<I", d, i + 12)[0]
        if not 1 <= k <= 100_000 or i + 16 + 4 * k + 16 > len(d):
            i += 1
            continue
        longueurs = unpack_from(f"<{k}I", d, i + 16)
        n = sum(longueurs)
        j = i + 16 + 4 * k
        if min(longueurs) < 3 or unpack_from("<4I", d, j) != (*POINTS, n):
            i += 1
            continue
        if j + 16 + 12 * n > len(d):
            break
        v = unpack_from(f"<{3 * n}f", d, j + 16)
        pts = [v[3 * a : 3 * a + 3] for a in range(n)]
        tris = []
        o = 0
        for L in longueurs:
            for a in range(L - 2):
                p, q, r = o + a, o + a + 1, o + a + 2
                tris.append((pts[p], pts[q], pts[r]) if a % 2 == 0
                            else (pts[q], pts[p], pts[r]))
            o += L
        faces.append(tris)
        i = j + 16 + 12 * n
    return faces


def _maillages(flux: dict[str, bytes]) -> tuple[dict[str, list], dict[str, int]]:
    """``({component: faces}, unread)`` from ``FaceTessellations``."""
    non_lus: dict[str, int] = {}
    repertoire = flux.get("FaceTessellations/Directory")
    donnees = {k: v for k, v in flux.items()
               if k.startswith("FaceTessellations/") and k != "FaceTessellations/Directory"}
    if repertoire is None or not donnees:
        if "Contents/DisplayLists" in flux:
            return _maillages_anciens(flux["Contents/DisplayLists"])
        non_lus["no display mesh"] = 1
        return {}, non_lus
    # Entry keys, found again in the data: an eight-byte window with at most
    # two zero bytes, present in a mesh stream.
    entrees = []
    debuts: dict[str, list[tuple[int, bytes]]] = {k: [] for k in donnees}
    for nom, octets in _repertoire(repertoire):
        cles = []
        for a in range(len(octets) - 7):
            cle = octets[a : a + 8]
            if cle.count(0) > 2 or cle in cles:
                continue
            for k, d in donnees.items():
                at = d.find(cle)
                if at >= 0:
                    cles.append(cle)
                    if (at, cle) not in debuts[k]:
                        debuts[k].append((at, cle))
        entrees.append((nom, cles))
    blocs: dict[bytes, list] = {}
    for k, d in donnees.items():
        tries = sorted(debuts[k])
        for x, (at, cle) in enumerate(tries):
            fin = tries[x + 1][0] if x + 1 < len(tries) else len(d)
            blocs[cle] = _faces(d, at + 8, fin)
    out = {}
    for nom, cles in entrees:
        faces = next((blocs[c] for c in cles if blocs.get(c)), None)
        if faces is None:
            non_lus["component with no mesh read"] = non_lus.get(
                "component with no mesh read", 0) + 1
            continue
        out[nom] = faces
    return out, non_lus


def _maillages_anciens(d: bytes) -> tuple[dict[str, list], dict[str, int]]:
    """``({component: faces}, unread)`` from ``Contents/DisplayLists``.

    Files without ``FaceTessellations`` (older SolidWorks versions) keep the
    display mesh there, in the same strip arrays. Each component starts with
    a ``name-N@assembly`` string followed by its part file's path; the mesh
    of a part follows its first instance only, in the part's frame, and the
    other instances share it.
    """
    chaines = []
    for m in re.finditer(re.escape(UTF16), d):
        lu = _cstring(d, m.start())
        if lu:
            chaines.append((m.start(), lu[0]))
    # A component header is followed, a few strings later, by a part path;
    # annotation references (``name@assembly`` too) are not.
    tetes = []
    for x, (at, texte) in enumerate(chaines):
        if "@" not in texte:
            continue
        chemin = next((s for _, s in chaines[x + 1 : x + 5]
                       if s.lower().endswith(".sldprt")), None)
        if chemin is not None:
            tetes.append((at, texte.split("@")[0], PureWindowsPath(chemin).name.lower()))
    non_lus: dict[str, int] = {}
    if not tetes:
        non_lus["no display mesh"] = 1
        return {}, non_lus
    par_fichier: dict[str, list] = {}
    noms = []
    for k, (at, nom, fichier) in enumerate(tetes):
        fin = tetes[k + 1][0] if k + 1 < len(tetes) else len(d)
        faces = _faces(d, at, fin)
        if faces and fichier not in par_fichier:
            par_fichier[fichier] = faces
        noms.append((nom, fichier))
    out = {}
    for nom, fichier in noms:
        if fichier in par_fichier:
            out[nom] = par_fichier[fichier]
        else:
            non_lus["component with no mesh read"] = non_lus.get(
                "component with no mesh read", 0) + 1
    return out, non_lus


# --- mates -------------------------------------------------------------------

#: An MFC UTF-16 ``CString``: ``FF FE FF``, its length in characters on one
#: byte, then the text.
_CSTRING = re.compile(rb"\xff\xfe\xff(.)", re.S)


def _strings(data: bytes) -> list[tuple[int, int, str]]:
    """``(start, end, text)`` of each UTF-16 ``CString`` in the stream."""
    out = []
    for m in _CSTRING.finditer(data):
        n = m.group(1)[0]
        end = m.end() + 2 * n
        if end <= len(data):
            out.append((m.start(), end, data[m.end():end].decode("utf-16-le", "replace")))
    return out


def _mates(data: bytes | None) -> list[Mate]:
    """The ``moBCMate_c`` objects of ``SwDocContentMgr/SwDocContentMgrInfo``.

    Each writes, in a row: the configuration, four bytes, the name, two
    four-byte integers (the second is the type, ``swMateType_e``), the class
    (``moMate…``) and the tree text. They are found by their class, and only
    those whose sequence fits are kept.
    """
    if not data or b"moBCMate_c" not in data:
        return []
    base = data.index(b"moBCMate_c")
    strings = _strings(data[base:])
    out = []
    for k in range(2, len(strings) - 1):
        start, _, cls = strings[k]
        if not cls.startswith("moMate"):
            continue
        _, end_name, name = strings[k - 1]
        _, end_conf, conf = strings[k - 2]
        if end_name + 8 != start or end_conf + 4 != strings[k - 1][0]:
            continue
        a = base + end_name
        kind = int.from_bytes(data[a + 4:a + 8], "little")
        label = strings[k + 1][2] if strings[k + 1][0] == strings[k][1] else ""
        out.append(Mate(name, kind, cls, label, conf))
    return out


# --- assembly ----------------------------------------------------------------


def read_assembly(source: str | Path | Container) -> Assembly:
    """An assembly ready to show: placed components and their meshes."""
    conteneur = source if isinstance(source, Container) else open_container(source)
    flux = {b.name: b.data for b in conteneur.blobs}
    xml = flux.get("swXmlContents/COMPINSTANCETREE")
    if xml is None:
        raise AssemblyError("no swXmlContents/COMPINSTANCETREE stream: not an "
                            "assembly")
    composants = _arbre(xml)
    maillages, non_lus = _maillages(flux)
    for c in composants:
        # The directory names a component by its last level.
        c.faces = maillages.get(c.nom.rsplit("/", 1)[-1], [])
        if not c.faces and not c.supprime:
            non_lus["component with no mesh"] = non_lus.get(
                "component with no mesh", 0) + 1
    mates = _mates(flux.get("SwDocContentMgr/SwDocContentMgrInfo"))
    return Assembly(composants, mates, non_lus)


def boite(asm: Assembly) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    """Bounding box (m) of the placed mesh of visible components."""
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for c in asm.composants:
        if c.cache or c.supprime:
            continue
        for face in c.faces:
            for tri in face:
                for p in tri:
                    q = c.placer(p)
                    for j in range(3):
                        lo[j], hi[j] = min(lo[j], q[j]), max(hi[j], q[j])
    return None if lo[0] == math.inf else (tuple(lo), tuple(hi))


def to_view(asm: Assembly) -> dict:
    """The assembly in the page's 3D view format (``vue3d``).

    Millimetres, centred; a face is named ``<component>:<index>``.
    """
    b = boite(asm)
    if b is None:
        raise AssemblyError("no visible component has a mesh")
    centre = [(b[0][j] + b[1][j]) / 2 for j in range(3)]
    diagonale = 1000 * math.dist(b[0], b[1])
    sommets = array("f")
    faces = []
    visibles = [c for c in asm.composants if not (c.cache or c.supprime) and c.faces]
    for rang, c in enumerate(visibles, start=1):
        for k, face in enumerate(c.faces, start=1):
            for tri in face:
                for p in tri:
                    q = c.placer(p)
                    sommets.extend(1000 * (q[j] - centre[j]) for j in range(3))
            faces.append({"nom": f"{c.nom}:{k}", "corps": rang, "triangles": len(face)})
    if sys.byteorder != "little":
        sommets.byteswap()
    return {
        "faces": faces,
        "corps": len(visibles),
        "rayon": diagonale / 2,
        "sommets": base64.b64encode(sommets.tobytes()).decode("ascii"),
    }


def to_stl(asm: Assembly) -> bytes:
    """The visible components' display mesh as binary STL, placed, in mm.

    SolidWorks' display mesh, not the exact geometry: the parts are not in
    the assembly file.
    """
    triangles = []
    for c in asm.composants:
        if c.cache or c.supprime:
            continue
        for face in c.faces:
            for tri in face:
                triangles.append([tuple(1000 * v for v in c.placer(p)) for p in tri])
    if not triangles:
        raise AssemblyError("no visible component has a mesh")
    out = bytearray(b"openSW: SolidWorks display mesh, mm".ljust(80, b" "))
    out += pack("<I", len(triangles))
    for a, b, c in triangles:
        u = [b[j] - a[j] for j in range(3)]
        v = [c[j] - a[j] for j in range(3)]
        n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
             u[0] * v[1] - u[1] * v[0])
        lg = math.sqrt(sum(x * x for x in n)) or 1.0
        out += pack("<12fH", *(x / lg for x in n), *a, *b, *c, 0)
    return bytes(out)
