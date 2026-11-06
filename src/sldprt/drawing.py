"""Read a SolidWorks drawing (``.SLDDRW``) to **look at** it.

A drawing is the same container as a part (:func:`sldprt.open_container`
opens it as is), but nothing it shows is Parasolid. What a sheet shows comes
from three streams, all written **already computed** by SolidWorks: this
module recomputes nothing, it reads them and places them.

``Contents/DisplayLists``
    The sheets' display list: a flat sequence of items
    ``[code u32][type u32][body]``. Types read here:

    ====  ==========================================================
    4     segment: two ``(x, y, z)`` points in metres, then one byte
    2     arc or circle: start, end, centre, normal; a circle if
          start = end (type ``0x65``: the same, inside a symbol glyph)
    6     filled polygon: ``u32`` loops, then per loop ``u32 n`` and
          ``n`` points (dimension arrowheads)
    10    font: height in m (or −1 then ``u32`` points), style bits,
          font name; the code is ``0x62 + 2 × len(name)``
    12    pen: RGB, layer, line type name
    9     (code ``0x3C``) colour of the next text: a table cell's
          ``<FONT color=…>``
    18    text: base point, then the string
    19    symbol ``<MOD-DIAM>``: base point, library, name, then the
          number of glyph items that follow
    ====  ==========================================================

    It also carries, for each **model view**, its matrix: 3×3 rotation,
    translation, scale, and its position on the sheet.

``Contents/VBLists``
    The edges of each view, **in 3D, in the part's frame**, already
    tessellated: ``[body u32][kind u32][n u32][n × (x, y, z)]``, kind
    ``0x321`` edge, ``0x331`` silhouette (older versions write kind 0). A
    view projects them with its matrix: ``sheet = position + scale·(p·R) + t``.
    Edges precede the name of the view they belong to; a section view's
    come under another name (``…-SectionAssembly-1``).

``Contents/Definition``
    Only the order of names (which view sits on which sheet) and the
    colours set by hand on sketch entities.

What is **not** read, and :attr:`Drawing.non_lus` counts instead of hiding:
items of unknown type, centre marks, hatches, images, OLE sheet formats.
Dimensions and sketches placed *inside* a view are read: they are written in
sheet coordinates, before their view.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from struct import pack, unpack_from

from .container import Container, open_container

#: Prefix of an MFC UTF-16 CString (the fourth byte is the length).
UTF16 = b"\xff\xfe\xff"

#: Item types of ``DisplayLists`` read here, and the constant codes of those
#: whose code does not vary (text, font and symbol codes vary; their
#: structure identifies them).
LIGNE, ARC, POLYGONE, POLICE, PLUME, TEXTE, SYMBOLE = 4, 2, 6, 10, 12, 18, 19
#: The ``(0x3C, 9)`` item that colours the next text.
TEINTE, CODE_TEINTE = 9, 0x3C
CODES = {LIGNE: 0x50, ARC: 0x80, POLYGONE: 0x78, PLUME: 0x4E}
#: The type of an arc inside a symbol **glyph**: same code, same body.
ARC_GLYPHE = 0x65

#: The ``<MOD-…>`` symbols rendered as characters. Others stay drawn by
#: their glyph, which the file also carries.
SYMBOLES = {"DIAM": "Ø", "PM": "±", "DEG": "°"}

#: Edge kinds of ``VBLists``; older versions write 0 for an edge.
ARETE, SILHOUETTE, ARETE_ANCIENNE = 0x321, 0x331, 0

#: Offset, after a view's matrix, of the record that repeats its position.
COMPAGNON = 410
IDENTITE = pack("<9d", 1, 0, 0, 0, 1, 0, 0, 0, 1)

#: Beyond this, a coordinate in metres is not a coordinate.
BORNE_M = 100.0

#: A font point, in metres: the one the DXF export applies.
POINT_M = 0.0254 / 96

#: Style bits of a font item, ``u32`` at +28.
GRAS, ITALIQUE = 0x08, 0x01


@dataclass
class Segment:
    x1: float
    y1: float
    x2: float
    y2: float
    nom: str
    #: ``#rrggbb``, or ``None`` for print black.
    couleur: str | None = None


@dataclass
class Arc:
    """An arc from ``a0`` to ``a1`` (radians, counter-clockwise)."""

    cx: float
    cy: float
    r: float
    a0: float
    a1: float
    cercle: bool
    nom: str
    couleur: str | None = None


@dataclass
class Polygone:
    points: list[tuple[float, float]]
    nom: str
    couleur: str | None = None


@dataclass
class Texte:
    """A text: ``(x, y)`` the bottom-left of its box, ``hauteur`` its font's."""

    x: float
    y: float
    hauteur: float
    texte: str
    nom: str
    couleur: str | None = None
    police: str | None = None
    gras: bool = False
    italique: bool = False


@dataclass
class Polyligne:
    """A view edge projected on the sheet."""

    points: list[tuple[float, float]]
    corps: int
    silhouette: bool
    nom: str


@dataclass
class Vue:
    nom: str
    echelle: float
    position: tuple[float, float]
    #: ``None`` for a projected view that keeps no rotation of its own.
    rotation: tuple[tuple[float, float, float], ...] | None
    translation: tuple[float, float, float]
    aretes: list[Polyligne] = field(default_factory=list)

    def projeter(self, p) -> tuple[float, float]:
        """A point of the part (m) on the sheet (mm)."""
        r = self.rotation
        q = [p[0] * r[0][j] + p[1] * r[1][j] + p[2] * r[2][j] for j in range(2)]
        return (
            1000 * (self.position[0] + self.echelle * q[0] + self.translation[0]),
            1000 * (self.position[1] + self.echelle * q[1] + self.translation[1]),
        )


@dataclass
class Feuille:
    nom: str
    largeur: float
    hauteur: float
    active: bool
    elements: list = field(default_factory=list)
    vues: list[Vue] = field(default_factory=list)


@dataclass
class Drawing:
    feuilles: list[Feuille]
    #: What was seen but not read, by reason: nothing is silently dropped.
    non_lus: dict[str, int] = field(default_factory=dict)

    @property
    def active(self) -> Feuille:
        return next((f for f in self.feuilles if f.active), self.feuilles[0])


class DrawingError(ValueError):
    """The file is not a drawing this module can read."""


#: The :attr:`Drawing.non_lus` reason for centre marks. The value means "at
#: least one": the class is declared once, whatever the number of marks.
REPERE_CENTRE = "centre marks (Definition, not decoded)"


# --- low-level reading --------------------------------------------------------


def _doubles(d: bytes, at: int, n: int) -> tuple[float, ...] | None:
    if at + 8 * n > len(d):
        return None
    v = unpack_from(f"<{n}d", d, at)
    if all(math.isfinite(x) and abs(x) < BORNE_M for x in v):
        return v
    return None


def _cstring(d: bytes, at: int) -> tuple[str, int] | None:
    if d[at : at + 3] != UTF16 or at + 4 > len(d):
        return None
    n = d[at + 3]
    fin = at + 4 + 2 * n
    if fin > len(d):
        return None
    try:
        return d[at + 4 : fin].decode("utf-16-le"), fin
    except UnicodeDecodeError:
        return None


def _mm(v: float) -> float:
    return 1000.0 * v


# --- DisplayLists: the sheets' primitives ---------------------------------------


def _ligne(d, at, nom):
    v = _doubles(d, at, 6)
    if v is None:
        return None
    return Segment(_mm(v[0]), _mm(v[1]), _mm(v[3]), _mm(v[4]), nom)


def _arc(d, at, nom):
    v = _doubles(d, at, 12)
    if v is None:
        return None
    (sx, sy, _), (ex, ey, _), (cx, cy, _), (nx, ny, nz) = (
        v[0:3], v[3:6], v[6:9], v[9:12])
    if abs(nx * nx + ny * ny + nz * nz - 1) > 1e-6:
        return None
    r = math.hypot(sx - cx, sy - cy)
    if r <= 0:
        return None
    a0, a1 = math.atan2(sy - cy, sx - cx), math.atan2(ey - cy, ex - cx)
    if nz < 0:  # clockwise: made counter-clockwise
        a0, a1 = a1, a0
    cercle = math.hypot(sx - ex, sy - ey) < 1e-12
    if cercle:
        a1 = a0 + 2 * math.pi
    elif a1 <= a0:
        a1 += 2 * math.pi
    return Arc(_mm(cx), _mm(cy), _mm(r), a0, a1, cercle, nom)


def _polygone(d, at, nom):
    if at + 8 > len(d):
        return None
    boucles, n = unpack_from("<II", d, at)
    if boucles != 1 or not 2 <= n <= 1000:
        return None
    v = _doubles(d, at + 8, 3 * n)
    if v is None:
        return None
    return Polygone([(_mm(v[3 * i]), _mm(v[3 * i + 1])) for i in range(n)], nom)


def _police(d, at):
    """``(height, name, style)`` of a font: height in m, or −1 followed by a
    ``u32`` of points; style at +28; name at +32."""
    v = _doubles(d, at, 1)
    lu = _cstring(d, at + 32)
    if v is None or lu is None:
        return None
    h = v[0]
    if h == -1.0:
        h = unpack_from("<I", d, at + 8)[0] * POINT_M
    if not 0 < h < 1:
        return None
    return h, lu[0] or None, unpack_from("<I", d, at + 28)[0]


def _plume(d, at):
    """``(r, g, b, layer)`` of a pen: RGB then a zero byte, two zero bytes,
    ``f32 −1``, ``i32`` layer (−1: none), four zero bytes, line type name."""
    if at + 22 > len(d) or d[at + 3] != 0 or _cstring(d, at + 18) is None:
        return None
    calque = unpack_from("<i", d, at + 10)[0]
    return d[at], d[at + 1], d[at + 2], calque >= 0


def _teinte_texte(d, at):
    """``#rrggbb`` of a text colour item: ``i32 −2``, ``u32 0x0B``, RGB, ``00``."""
    if at + 16 > len(d) or d[at + 11] != 0:
        return None
    marque, genre = unpack_from("<iI", d, at)
    if (marque, genre) != (-2, 0x0B):
        return None
    return "#{:02x}{:02x}{:02x}".format(*d[at + 8 : at + 11])


def _symbole(d, at):
    """``(x, y, library, name, n, end)`` of a ``<MOD-DIAM>`` symbol: base
    point, the library at +44, the name, then ``u32 n``, the number of glyph
    items that follow."""
    v = _doubles(d, at, 3)
    lib = _cstring(d, at + 44)
    if v is None or lib is None or not lib[0]:
        return None
    nom = _cstring(d, lib[1])
    if nom is None or not nom[0] or nom[1] + 4 > len(d):
        return None
    n = unpack_from("<I", d, nom[1])[0]
    if n > 64:
        return None
    return _mm(v[0]), _mm(v[1]), lib[0], nom[0], n, nom[1] + 4


def _couleurs_explicites(defn: bytes) -> set[tuple[int, int, int]]:
    """The colours set by hand on sketch entities, read in ``Definition``."""
    out = set()
    for m in re.finditer(rb"\xff\xff\x1f\x00\x03(.)(.)(.)\x00", defn, re.S):
        out.add((m[1][0], m[2][0], m[3][0]))
    return out


def _texte(d, at, hauteur, nom):
    """Base point, then the string 12 or 16 bytes further, then ``u16``,
    ``u32 n`` (as many as characters) and ``n`` floats."""
    v = _doubles(d, at, 3)
    if v is None:
        return None
    for ecart in (36, 40):
        lu = _cstring(d, at + ecart)
        if lu is None:
            continue
        texte, fin = lu
        if fin + 6 <= len(d) and unpack_from("<I", d, fin + 2)[0] == len(texte):
            return Texte(_mm(v[0]), _mm(v[1]), _mm(hauteur or 0.0035),
                         texte.strip(), nom)
    return None


def _feuilles(d: bytes) -> list[tuple[int, str, float, float]]:
    """Sheet headers: a name preceded by (height, width) in m.

    Returns ``(offset, name, width_mm, height_mm)`` in stream order.
    """
    out = []
    for m in re.finditer(re.escape(UTF16), d):
        at = m.start()
        lu = _cstring(d, at)
        if lu is None or not lu[0] or at < 16:
            continue
        h, w = unpack_from("<2d", d, at - 16)
        if 0.01 < h < 10 and 0.01 < w < 10 and lu[0][0].isprintable():
            if lu[0].startswith("<"):  # "<Default>_Display State"
                continue
            out.append((at, lu[0], _mm(w), _mm(h)))
    return out


def _vues(d: bytes) -> list[tuple[int, Vue]]:
    """Model views: nine orthonormal doubles, t, scale, position."""
    out = []
    for i in range(len(d) - 122):
        r = unpack_from("<9d", d, i)
        if not all(math.isfinite(x) and abs(x) <= 1.000001 for x in r):
            continue
        rows = (r[0:3], r[3:6], r[6:9])
        if any(abs(sum(a * a for a in row) - 1) > 1e-5 for row in rows):
            continue
        if any(abs(sum(a * b for a, b in zip(rows[p], rows[q], strict=True))) > 1e-5
               for p, q in ((0, 1), (0, 2), (1, 2))):
            continue
        t = unpack_from("<3d", d, i + 72)
        s = unpack_from("<d", d, i + 96)[0]
        pos = unpack_from("<2d", d, i + 106)
        if not (0 < s < 1e4 and all(0 <= p < 10 for p in pos)
                and all(math.isfinite(x) and abs(x) < 10 for x in t)):
            continue
        out.append((i, Vue("", s, pos, rows, t)))
    # A projected view may keep no rotation, but its scale and position are
    # there, repeated by the record that follows: it is listed all the
    # same, so that the views after it keep their names.
    vus = {i for i, _ in out}
    j = d.find(IDENTITE)
    while j >= 0:
        i = j - COMPAGNON
        if i >= 0 and i not in vus and j + 96 <= len(d):
            t = unpack_from("<3d", d, i + 72)
            s = unpack_from("<d", d, i + 96)[0]
            pos = unpack_from("<2d", d, i + 106)
            repete = unpack_from("<3d", d, j + 72)[1:]
            if (0 < s < 1e4 and all(0 < p < 10 for p in pos)
                    and all(abs(a - b) < 1e-9 for a, b in zip(repete, pos, strict=True))
                    and all(math.isfinite(x) and abs(x) < 10 for x in t)):
                out.append((i, Vue("", s, pos, None, t)))
        j = d.find(IDENTITE, j + 1)
    return sorted(out, key=lambda x: x[0])


def _hex(r: int, g: int, b: int) -> str | None:
    return None if (r, g, b) == (0, 0, 0) else f"#{r:02x}{g:02x}{b:02x}"


def _primitives(d: bytes, debut: int, non_lus: dict,
                explicites: frozenset = frozenset()) -> list[tuple[int, object]]:
    out = []
    hauteur = police = couleur = None
    style = 0
    plume_texte = None  # the raw pen colour: a text takes it as is
    teinte = None  # a table cell's colour, for the next text
    glyphe = 0  # glyph items of a symbol rendered as a character, to skip
    i = debut
    n = len(d) - 8
    while i < n:
        code, genre = unpack_from("<II", d, i)
        corps = i + 8
        nom = f"E{i}"
        el = None
        if genre == LIGNE and code == CODES[LIGNE]:
            el = _ligne(d, corps, nom)
        elif genre in (ARC, ARC_GLYPHE) and code == CODES[ARC]:
            el = _arc(d, corps, nom)
        elif genre == POLYGONE and code == CODES[POLYGONE]:
            el = _polygone(d, corps, nom)
        elif genre == POLICE and 0x40 <= code <= 0x400:
            lu = _police(d, corps)
            if lu is not None:
                hauteur, police, style = lu
                i = corps + 8
                continue
        elif genre == PLUME and code == CODES[PLUME]:
            lu = _plume(d, corps)
            if lu is not None:
                r, g, b, calque = lu
                # A pen's RGB is the colour **on screen**: an under-defined
                # sketch shows blue though nobody chose it. Only a colour set
                # by hand (``Definition``) or a layer's colour counts.
                visible = calque or (r, g, b) in explicites
                couleur = _hex(r, g, b) if visible else None
                # Texts have no status colour: a note's ``<FONT color=…>``
                # arrives as a pen.
                plume_texte = _hex(r, g, b)
                i = corps + 18
                continue
        elif genre == TEINTE and code == CODE_TEINTE:
            lu = _teinte_texte(d, corps)
            if lu is not None:
                teinte = lu
                i = corps + 16
                continue
        elif genre == SYMBOLE and 0x40 <= code <= 0x400:
            lu = _symbole(d, corps)
            if lu is not None:
                x, y, lib, sym, k, fin = lu
                if lib == "MOD" and sym in SYMBOLES:
                    out.append((i, Texte(x, y, _mm(hauteur or 0.0035),
                                         SYMBOLES[sym], nom,
                                         teinte or plume_texte, police,
                                         bool(style & GRAS),
                                         bool(style & ITALIQUE))))
                    teinte = None
                    glyphe = k
                else:
                    motif = f"symbol <{lib}-{sym}> (drawn by its glyph)"
                    non_lus[motif] = non_lus.get(motif, 0) + 1
                i = fin
                continue
        elif genre == TEXTE and 0x40 <= code <= 0x400:
            el = _texte(d, corps, hauteur, nom)
            if el is not None:
                el.police = police
                el.gras, el.italique = bool(style & GRAS), bool(style & ITALIQUE)
        if el is not None:
            if isinstance(el, Texte):
                el.couleur, teinte = teinte or plume_texte, None
            else:
                el.couleur = couleur
            if glyphe and not isinstance(el, Texte):
                glyphe -= 1
            else:
                out.append((i, el))
            i = corps + 8
            continue
        i += 1
    return out


# --- VBLists: the views' edges -----------------------------------------------

NOM_VUE = re.compile(r"@(Drawing View\d+)$")


def _aretes(vb: bytes) -> list[tuple[str | None, list]]:
    """``[(view name or None, [(offset, body, kind, 3D points)…])…]``.

    Edges precede the name they belong to (``cube-1@Drawing View1``) in
    the stream; a name that is not a view's (a section's) gives ``None``.
    """
    noms = []
    for m in re.finditer(re.escape(UTF16), vb):
        lu = _cstring(vb, m.start())
        if lu and "@" in lu[0]:
            vue = NOM_VUE.search(lu[0])
            noms.append((m.start(), vue.group(1) if vue else None))
    # Kind 0 only in a file that has no edge of the current kinds: in one
    # that has, it also matches bytes that are not an edge.
    for genres, depuis in (((ARETE, SILHOUETTE), 0), ((ARETE_ANCIENNE,), 1)):
        paquets = [(nom, []) for _, nom in noms]
        trouve = False
        i, n = 0, len(vb) - 12
        while i < n:
            corps, genre, k = unpack_from("<III", vb, i)
            if genre in genres and depuis <= corps < 4096 and 1 <= k < 500_000:
                pts = _doubles(vb, i + 12, 3 * k)
                if pts is not None:
                    trouve = True
                    j = next((x for x, (at, _) in enumerate(noms) if at > i), None)
                    if j is not None:
                        paquets[j][1].append(
                            (i, corps, genre,
                             [pts[3 * a : 3 * a + 3] for a in range(k)]))
                    i += 12 + 24 * k
                    continue
            i += 1
        if trouve:
            break
    return paquets


def _ordre_definition(defn: bytes, feuilles: list[str]) -> list[tuple[str, str]]:
    """``[(sheet, view)…]``: the order of names at the head of ``Definition``.

    A view belongs to the last sheet named before it.
    """
    out, courante, vus = [], None, set()
    for m in re.finditer(re.escape(UTF16), defn):
        lu = _cstring(defn, m.start())
        if not lu:
            continue
        nom = lu[0]
        if nom.lower().endswith((".sldprt", ".sldasm")):
            break
        if nom in feuilles:
            courante = nom
        elif re.fullmatch(r"Drawing View\d+", nom) and nom not in vus:
            vus.add(nom)
            out.append((courante, nom))
    return out


# --- assembly -------------------------------------------------------------------


def read_drawing(source: str | Path | Container) -> Drawing:
    """A drawing, ready to display: sheets, primitives, views."""
    conteneur = source if isinstance(source, Container) else open_container(source)
    flux = {b.name: b.data for b in conteneur.blobs}
    dl = flux.get("Contents/DisplayLists")
    if dl is None:
        raise DrawingError("no Contents/DisplayLists stream: not a drawing")
    debut = dl.find(b"uiLayerMgr_c")
    debut = 0 if debut < 0 else debut
    non_lus: dict[str, int] = {}

    entetes = [e for e in _feuilles(dl) if e[0] > debut]
    if not entetes:
        raise DrawingError("no sheet found in DisplayLists")
    feuilles = [
        Feuille(nom.removesuffix("-Active"), w, h, nom.endswith("-Active"))
        for _, nom, w, h in entetes
    ]
    fin_feuilles = entetes[-1][0]

    definition = flux.get("Contents/Definition", b"")
    explicites = frozenset(_couleurs_explicites(definition))
    dans_vues = []
    for at, el in _primitives(dl, debut, non_lus, explicites):
        k = next((x for x, e in enumerate(entetes) if e[0] > at), None)
        if k is None:
            # After the last sheet: a dimension or a sketch placed inside a
            # view, already in sheet coordinates; which sheet is found below.
            dans_vues.append((at, el))
            continue
        feuilles[k].elements.append(el)

    lues = [(at, v) for at, v in _vues(dl) if at > fin_feuilles]
    vues = [v for _, v in lues]
    ordre = _ordre_definition(definition, [f.nom for f in feuilles])
    places = []
    for k, vue in enumerate(vues):
        if k < len(ordre):
            feuille_nom, vue.nom = ordre[k]
        else:
            feuille_nom, vue.nom = None, f"Drawing View{k + 1}"
            non_lus["view on no known sheet"] = non_lus.get(
                "view on no known sheet", 0) + 1
        places.append((vue, next((f for f in feuilles if f.nom == feuille_nom),
                                 feuilles[0])))
    # What is placed inside a view precedes the view's record, as its edges
    # precede its name: a primitive goes to the sheet of the next view.
    for at, el in dans_vues:
        k = next((x for x, (av, _) in enumerate(lues) if av > at), None)
        if k is None:
            non_lus["primitive of no known view"] = non_lus.get(
                "primitive of no known view", 0) + 1
            continue
        places[k][1].elements.append(el)
    # A view's name comes back once per body it shows; edges are merged. A
    # group under another name (a section view's) belongs to the view that
    # follows the previous group's.
    ordre_vues = [v.nom for v, _ in places]
    aretes: dict[str, list] = {}
    precedente = None
    for nom, paquet in _aretes(flux.get("Contents/VBLists", b"")):
        if nom is None and precedente in ordre_vues:
            k = ordre_vues.index(precedente) + 1
            nom = ordre_vues[k] if k < len(ordre_vues) else None
        if nom is None:
            if paquet:
                non_lus["edges of no known view"] = non_lus.get(
                    "edges of no known view", 0) + len(paquet)
            continue
        aretes.setdefault(nom, []).extend(paquet)
        precedente = nom
    for vue, cible in places:
        paquet = aretes.get(vue.nom, [])
        if vue.rotation is None:
            if paquet:
                non_lus["projected view (no rotation stored)"] = non_lus.get(
                    "projected view (no rotation stored)", 0) + 1
            paquet = []
        for at, corps, genre, pts in paquet:
            q = [vue.projeter(p) for p in pts]
            if not all(-1 <= x <= cible.largeur + 1 and -1 <= y <= cible.hauteur + 1
                       for x, y in q):
                non_lus["edge off the sheet"] = non_lus.get("edge off the sheet", 0) + 1
                continue
            vue.aretes.append(Polyligne(q, corps, genre == SILHOUETTE, f"A{at}"))
        cible.vues.append(vue)
    # Centre marks are not in the display list: they live only in
    # ``Definition`` (class ``moCenterMarkSym_c``), which is not decoded.
    if b"moCenterMarkSym_c" in definition:
        non_lus[REPERE_CENTRE] = 1
    return Drawing(feuilles, non_lus)


# --- SVG ------------------------------------------------------------------------


def _f(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".")


def to_svg(feuille: Feuille) -> str:
    """The sheet as SVG, in millimetres, y flipped as on paper.

    Every stroke carries ``data-nom``: ``E<n>`` for a sheet element (its
    offset in ``DisplayLists``), ``A<n>`` for a view edge (its offset in
    ``VBLists``), the name used to point at a stroke.
    """
    w, h = feuille.largeur, feuille.hauteur
    y = lambda v: _f(h - v)  # noqa: E731
    # The frame covers the sheet **and** what overflows it: SolidWorks lets a
    # table or a note sit off the paper, and it stays on screen.
    x0, y0, x1, y1 = _boite(feuille)
    x0, y0, x1, y1 = min(x0, 0.0), min(y0, 0.0), max(x1, w), max(y1, h)
    vb = f"{_f(x0)} {_f(h - y1)} {_f(x1 - x0)} {_f(y1 - y0)}"
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" '
        f'width="{_f(x1 - x0)}mm" height="{_f(y1 - y0)}mm" '
        f'data-feuille="{_esc(feuille.nom)}">',
        f'<rect class="papier" x="0" y="0" width="{_f(w)}" height="{_f(h)}"/>',
        '<g class="trait" fill="none" stroke-linecap="round" stroke-linejoin="round">',
    ]
    for el in feuille.elements:
        c = _teinte(el, "stroke")
        if isinstance(el, Segment):
            out.append(f'<line data-nom="{el.nom}"{c} x1="{_f(el.x1)}" y1="{y(el.y1)}" '
                       f'x2="{_f(el.x2)}" y2="{y(el.y2)}"/>')
        elif isinstance(el, Arc):
            if el.cercle:
                out.append(f'<circle data-nom="{el.nom}"{c} cx="{_f(el.cx)}" '
                           f'cy="{y(el.cy)}" r="{_f(el.r)}"/>')
            else:
                x0 = el.cx + el.r * math.cos(el.a0)
                y0 = el.cy + el.r * math.sin(el.a0)
                x1 = el.cx + el.r * math.cos(el.a1)
                y1 = el.cy + el.r * math.sin(el.a1)
                grand = 1 if (el.a1 - el.a0) > math.pi else 0
                # y flipped: counter-clockwise becomes clockwise.
                out.append(f'<path data-nom="{el.nom}"{c} d="M{_f(x0)} {y(y0)} '
                           f'A{_f(el.r)} {_f(el.r)} 0 {grand} 0 {_f(x1)} {y(y1)}"/>')
        elif isinstance(el, Polygone):
            pts = " ".join(f"{_f(px)},{y(py)}" for px, py in el.points)
            out.append(f'<polygon data-nom="{el.nom}" class="plein"'
                       f'{_teinte(el, "stroke", "fill")} points="{pts}"/>')
    for vue in feuille.vues:
        out.append(f'<g class="vue" data-vue="{_esc(vue.nom)}">')
        for a in vue.aretes:
            pts = " ".join(f"{_f(px)},{y(py)}" for px, py in a.points)
            out.append(f'<polyline data-nom="{a.nom}" data-corps="{a.corps}" '
                       f'points="{pts}"/>')
        out.append("</g>")
    out.append("</g>")
    # The stored point is the **bottom** of the text box, not its baseline
    # (the DXF's MTEXT top is 1.289 h above it): ``text-after-edge`` puts the
    # bottom of the box on the point.
    for el in feuille.elements:
        if isinstance(el, Texte):
            famille = (f' font-family="{_esc(el.police)}, sans-serif"'
                       if el.police else "")
            famille += ' font-weight="bold"' if el.gras else ""
            famille += ' font-style="italic"' if el.italique else ""
            out.append(f'<text data-nom="{el.nom}"{_teinte(el, "fill")}{famille} '
                       f'x="{_f(el.x)}" y="{y(el.y)}" font-size="{_f(el.hauteur)}" '
                       f'dominant-baseline="text-after-edge">{_esc(el.texte)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def _teinte(el, *attributs: str) -> str:
    """SVG colour attributes of an element; nothing for black."""
    if el.couleur is None:
        return ""
    return "".join(f' {a}="{el.couleur}"' for a in attributs)


def _boite(feuille: Feuille) -> tuple[float, float, float, float]:
    """The box (mm) of everything the sheet shows, paper excluded."""
    xs: list[float] = []
    ys: list[float] = []
    for el in feuille.elements:
        if isinstance(el, Segment):
            xs += [el.x1, el.x2]
            ys += [el.y1, el.y2]
        elif isinstance(el, Arc):
            xs += [el.cx - el.r, el.cx + el.r]
            ys += [el.cy - el.r, el.cy + el.r]
        elif isinstance(el, Polygone):
            xs += [p[0] for p in el.points]
            ys += [p[1] for p in el.points]
        elif isinstance(el, Texte):
            xs += [el.x, el.x + 0.7 * el.hauteur * len(el.texte)]
            ys += [el.y, el.y + 1.3 * el.hauteur]
    for vue in feuille.vues:
        for a in vue.aretes:
            xs += [p[0] for p in a.points]
            ys += [p[1] for p in a.points]
    if not xs:
        return 0.0, 0.0, feuille.largeur, feuille.hauteur
    marge = 5.0
    return min(xs) - marge, min(ys) - marge, max(xs) + marge, max(ys) + marge


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


#: AutoCAD colour index of the plain colours a DXF R12 can carry.
_ACI = {1: (255, 0, 0), 2: (255, 255, 0), 3: (0, 255, 0), 4: (0, 255, 255),
        5: (0, 0, 255), 6: (255, 0, 255), 7: (0, 0, 0), 8: (128, 128, 128),
        9: (192, 192, 192)}


def _aci(couleur: str | None) -> int:
    """The nearest colour index; 7 (black on paper) for print black."""
    if couleur is None:
        return 7
    rgb = tuple(int(couleur[i:i + 2], 16) for i in (1, 3, 5))
    return min(_ACI, key=lambda k: sum((a - b) ** 2 for a, b in zip(_ACI[k], rgb, strict=True)))


def _calque(nom: str) -> str:
    return re.sub(r"[^A-Z0-9_$-]", "_", nom.upper())[:31] or "_"


def _dxf_texte(s: str) -> str:
    """Text a cp1252 DXF can hold: other characters as ``\\U+XXXX``."""
    return "".join(c if c.encode("cp1252", "ignore") else f"\\U+{ord(c):04X}"
                   for c in s.replace("\n", " "))


def to_dxf(feuille: Feuille) -> str:
    """The sheet as DXF R12, in millimetres: encode it as cp1252.

    Sheet elements go on layer ``SHEET``, texts on ``TEXT``, each view's
    edges on a layer named after the view, the paper outline on ``PAPER``.
    Colours are the nearest of the nine plain ones; filled shapes
    (arrowheads) become closed outlines.
    """
    g = lambda v: f"{v:.10g}"  # noqa: E731
    out = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009",
           "9", "$DWGCODEPAGE", "3", "ANSI_1252", "9", "$INSUNITS", "70", "4",
           "9", "$EXTMIN", "10", "0", "20", "0", "30", "0",
           "9", "$EXTMAX", "10", g(feuille.largeur), "20", g(feuille.hauteur),
           "30", "0", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]

    def polyligne(points, calque, couleur, fermee):
        out.extend(["0", "POLYLINE", "8", calque, "62", str(couleur), "66", "1",
                    "10", "0", "20", "0", "30", "0", "70", "1" if fermee else "0"])
        for x, y in points:
            out.extend(["0", "VERTEX", "8", calque, "10", g(x), "20", g(y), "30", "0"])
        out.extend(["0", "SEQEND", "8", calque])

    w, h = feuille.largeur, feuille.hauteur
    polyligne([(0, 0), (w, 0), (w, h), (0, h)], "PAPER", 8, True)
    for el in feuille.elements:
        c = str(_aci(el.couleur))
        if isinstance(el, Segment):
            out.extend(["0", "LINE", "8", "SHEET", "62", c, "10", g(el.x1),
                        "20", g(el.y1), "30", "0", "11", g(el.x2), "21", g(el.y2),
                        "31", "0"])
        elif isinstance(el, Arc):
            out.extend(["0", "CIRCLE" if el.cercle else "ARC", "8", "SHEET",
                        "62", c, "10", g(el.cx), "20", g(el.cy), "30", "0",
                        "40", g(el.r)])
            if not el.cercle:
                out.extend(["50", g(math.degrees(el.a0)), "51", g(math.degrees(el.a1))])
        elif isinstance(el, Polygone):
            polyligne(el.points, "SHEET", c, True)
        elif isinstance(el, Texte):
            # The stored point is the bottom of the text box; the baseline
            # sits 0.289 h above it (see to_svg).
            out.extend(["0", "TEXT", "8", "TEXT", "62", c, "10", g(el.x),
                        "20", g(el.y + 0.289 * el.hauteur), "30", "0",
                        "40", g(el.hauteur), "1", _dxf_texte(el.texte)])
    for vue in feuille.vues:
        calque = _calque(vue.nom)
        for a in vue.aretes:
            polyligne(a.points, calque, 7, False)
    out.extend(["0", "ENDSEC", "0", "EOF"])
    return "\n".join(out) + "\n"
