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
    Only the order of names, in the body of the stream (which view sits on
    which sheet), and the colours set by hand on sketch entities.

What is **not** read, and :attr:`Drawing.non_lus` counts instead of hiding:
items of unknown type, centre marks, hatches. OLE images are shown as
SolidWorks keeps them. Dimensions and sketches placed *inside* a view are
read: they are written in sheet coordinates, before their view.
"""

from __future__ import annotations

import base64
import math
import re
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path, PureWindowsPath
from struct import pack, unpack_from

from .container import Container, open_container

#: The eight bytes that open a PNG file.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Prefix of an MFC UTF-16 CString (the fourth byte is the length).
UTF16 = b"\xff\xfe\xff"

#: Item types of ``DisplayLists`` read here, and the constant codes of those
#: whose code does not vary (text, font and symbol codes vary; their
#: structure identifies them).
LIGNE, ARC, POLYGONE, POLICE, PLUME, TEXTE, SYMBOLE = 4, 2, 6, 10, 12, 18, 19
#: The ``(0x3C, 9)`` item that colours the next text.
TEINTE, CODE_TEINTE = 9, 0x3C
CODES = {LIGNE: 0x50, ARC: 0x80, POLYGONE: 0x78, POLICE: 0x7E, PLUME: 0x4E}
#: The type of an arc inside a symbol **glyph**: same code, same body.
ARC_GLYPHE = 0x65

#: The patterns (mm: dash, gap, dash…) of the line types a pen names, as the
#: ``LTYPE`` table of SolidWorks DXF files defines them. A name missing from
#: the table is drawn continuous.
TRAITS = {
    "HIDDEN": (1.27, 0.635),
    "CENTER": (6.35, 1.27, 1.27, 1.27),
    # Missing from the tables: CHAIN strokes are, in the DXF, on the CENTER
    # layer, with type CENTER.
    "CHAIN": (6.35, 1.27, 1.27, 1.27),
    "PHANTOM": (6.35, 1.27, 1.27, 1.27, 1.27, 1.27),
}

#: The ``<MOD-…>`` symbols rendered as characters. Others stay drawn by
#: their glyph, which the file also carries.
SYMBOLES = {"DIAM": "Ø", "PM": "±", "DEG": "°"}

#: Edge kinds of ``VBLists``; older versions write 0 for an edge.
ARETE, SILHOUETTE, ARETE_ANCIENNE = 0x321, 0x331, 0
#: A third kind of visible edge, with the same record body. What sets it
#: apart from a 0x321 edge is not established.
ARETE_301 = 0x301
#: A fourth, also visible outside a shaded view (the outline of a faucet
#: lever, cabinet_bath); a shaded view writes it for edges it hides.
ARETE_311 = 0x311
#: A fifth: the outline and holes of the top view of foodprocessor.
ARETE_323 = 0x323

#: Offset, after a view's matrix, of the record that repeats its position.
COMPAGNON = 410
#: How far back to look for the view of a distant companion record.
RECUL = 8192
IDENTITE = pack("<9d", 1, 0, 0, 0, 1, 0, 0, 0, 1)
ROTATION_IDENTITE = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

#: After a view's matrix, the byte of its display mode (``swDisplayMode_e``),
#: and the one that says whether a shaded view keeps its edges.
OCTET_MODE, OCTET_ARETES = 211, 227
#: ``swDisplayMode_e.swSHADED``.
OMBRE = 3
#: The "hidden lines removed" mode (``swDisplayMode_e``).
CACHEES = 2

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
    #: The line type the pen names, ``None`` for a continuous stroke.
    trait: str | None = None


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
    trait: str | None = None


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
    #: The text direction, in radians from the sheet's x axis: π/2 for the
    #: dimension of a vertical edge.
    angle: float = 0.0


@dataclass
class Image:
    """An image placed on the sheet (an OLE object), rendered as SolidWorks
    kept it: ``(x0, y0)`` bottom-left corner, ``(x1, y1)`` top-right corner,
    in mm."""

    x0: float
    y0: float
    x1: float
    y1: float
    png: bytes
    nom: str
    couleur: str | None = None


@dataclass
class Polyligne:
    """A view edge projected on the sheet."""

    points: list[tuple[float, float]]
    corps: int
    silhouette: bool
    nom: str


@dataclass
class FaceOmbree:
    """A face of the display mesh: triangles in metres, in the part's frame;
    one normal per vertex when the file writes them."""

    triangles: list[tuple[tuple[float, float, float], ...]]
    normales: list[tuple[tuple[float, float, float], ...]] | None
    couleur: tuple[int, int, int]


@dataclass
class Vue:
    nom: str
    echelle: float
    position: tuple[float, float]
    #: ``None`` for a projected view that keeps no rotation of its own.
    rotation: tuple[tuple[float, float, float], ...] | None
    translation: tuple[float, float, float]
    aretes: list[Polyligne] = field(default_factory=list)
    #: The stored display mode (``swDisplayMode_e``): 2 hidden lines
    #: removed, 3 shaded; 0 if it was not read.
    mode: int = 0
    #: Does a shaded view also show its edges?
    avec_aretes: bool = True
    #: For a shaded view: the faces of its part's display mesh, in the part's
    #: frame, with their colour.
    ombrage: list[FaceOmbree] = field(default_factory=list)
    #: For a detail view: its circle on the sheet, ``(cx, cy, r)`` in mm,
    #: beyond which it shows nothing.
    cercle: tuple[float, float, float] | None = None

    @property
    def ombree(self) -> bool:
        return self.mode == OMBRE

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
    if v is None or lu is None or at + 32 > len(d):
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
        if fin + 6 <= len(d) and unpack_from("<I", d, fin + 2)[0] in (len(texte), 0):
            # At 40 bytes, a double sits between the point and the string:
            # the text angle, in radians.
            angle = unpack_from("<d", d, at + 28)[0] if ecart == 40 else 0.0
            if not (math.isfinite(angle) and abs(angle) <= 2 * math.pi + 1e-9):
                angle = 0.0
            return Texte(_mm(v[0]), _mm(v[1]), _mm(hauteur or 0.0035),
                         texte.strip(), nom, angle=angle)
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
        # A sheet's shorter side exceeds 5 cm: "MOD" headers 10 mm wide, which
        # are not sheets, hid the views before them (bolt-assembly in the
        # SolidWorks samples).
        if 0.05 <= min(h, w) and max(h, w) < 10 and lu[0][0].isprintable():
            if lu[0].startswith("<"):  # "<Default>_Display State"
                continue
            out.append((at, lu[0], _mm(w), _mm(h)))
    return out


def _modes(d: bytes, i: int, vue: Vue) -> Vue:
    """A view's display mode, read in its header."""
    if i + OCTET_ARETES < len(d):
        vue.mode = d[i + OCTET_MODE]
        vue.avec_aretes = vue.mode != OMBRE or d[i + OCTET_ARETES] == 1
    return vue


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
        out.append((i, _modes(d, i, Vue("", s, pos, rows, t))))
    # A view whose rotation is the identity does not write it: the space
    # serves something else. Its scale, translation and position are there,
    # the position repeated by the record that follows.
    # The companion usually follows the view by 410 bytes; for a detail
    # view, from further (3 098 on mobile gantry): we then look back for the
    # view whose position is, to the byte, the one it repeats.
    vus = {i for i, _ in out}
    j = d.find(IDENTITE)
    while j >= 0:
        if j + 96 <= len(d):
            repete = unpack_from("<2d", d, j + 80)
            for i in (j - COMPAGNON, *range(j - COMPAGNON - 1, max(j - RECUL, -1), -1)):
                if i < 0 or i in vus or i + 122 > len(d):
                    continue
                pos = unpack_from("<2d", d, i + 106)
                if not all(abs(a - b) < 1e-9 for a, b in zip(repete, pos, strict=True)):
                    continue
                t = unpack_from("<3d", d, i + 72)
                s = unpack_from("<d", d, i + 96)[0]
                # Far from the companion, its scale must match too, and the
                # position must be one no view already read carries.
                if i != j - COMPAGNON and (
                        j + 112 > len(d) or abs(unpack_from("<d", d, j + 104)[0] - s) > 1e-12
                        or any(abs(v.position[0] - pos[0]) < 1e-9
                               and abs(v.position[1] - pos[1]) < 1e-9 for _, v in out)):
                    continue
                # A detail view may have its position off the sheet (mobile
                # gantry: y = −182.7 mm, for the API too); near the companion,
                # a negative position is that of a view not placed (2012-sm:
                # x < 0, y = 0).
                bornes = (0, 10) if i == j - COMPAGNON else (-10, 10)
                if (0 < s < 1e4 and all(bornes[0] < p < bornes[1] for p in pos)
                        and all(math.isfinite(x) and abs(x) < 10 for x in t)):
                    out.append((i, _modes(d, i, Vue("", s, pos, ROTATION_IDENTITE, t))))
                    vus.add(i)
                    break
        j = d.find(IDENTITE, j + 1)
    return sorted(out, key=lambda x: x[0])


def _hex(r: int, g: int, b: int) -> str | None:
    return None if (r, g, b) == (0, 0, 0) else f"#{r:02x}{g:02x}{b:02x}"


def _primitives(d: bytes, debut: int, non_lus: dict,
                explicites: frozenset = frozenset()) -> list[tuple[int, object]]:
    out = []
    hauteur = police = couleur = trait = None
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
        elif genre == PLUME and 0x40 <= code <= 0x400:
            # The code follows the length of the line type name:
            # 0x3A + 2 × len; 0x4E is CONTINUOUS.
            lu = _plume(d, corps)
            nom_trait = _cstring(d, corps + 18) if lu is not None else None
            if nom_trait is not None and code == 0x3A + 2 * len(nom_trait[0]):
                r, g, b, calque = lu
                trait = None if nom_trait[0] == "CONTINUOUS" else nom_trait[0]
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
                if not isinstance(el, Polygone):
                    el.trait = trait
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


#: The body of an edge record that names none.
SANS_CORPS = 0xFFFFFFFF


def _aretes(vb: bytes) -> list[tuple[str | None, str, list]]:
    """``[(view name or None, raw name, [(offset, body, kind, 3D points)…])…]``.

    Edges precede the name they belong to (``cube-1@Drawing View1``) in
    the stream; a name that is not a view's (a section's, a component's)
    gives ``None``.
    """
    noms = []
    for m in re.finditer(re.escape(UTF16), vb):
        lu = _cstring(vb, m.start())
        if lu and "@" in lu[0] and lu[0].isprintable():
            vue = NOM_VUE.search(lu[0])
            noms.append((m.start(), vue.group(1) if vue else None, lu[0]))
    # Kind 0 only in a file that has no edge of the current kinds: in one
    # that has, it also matches bytes that are not an edge.
    for genres, depuis in (((ARETE, SILHOUETTE, ARETE_301, ARETE_311, ARETE_323), 0),
                           ((ARETE_ANCIENNE,), 1)):
        paquets = [(nom, brut, []) for _, nom, brut in noms]
        trouve = False
        i, n = 0, len(vb) - 12
        while i < n:
            corps, genre, k = unpack_from("<III", vb, i)
            # A body of −1 (0xFFFFFFFF) carries the two longest edges of the
            # cropped view of `chair`.
            if (genre in genres and (depuis <= corps < 4096 or corps == SANS_CORPS)
                    and 1 <= k < 500_000):
                pts = _doubles(vb, i + 12, 3 * k)
                if pts is not None:
                    trouve = True
                    j = next((x for x, (at, _, _) in enumerate(noms) if at > i), None)
                    if j is not None:
                        paquets[j][2].append(
                            (i, corps, genre,
                             [pts[3 * a : 3 * a + 3] for a in range(k)]))
                    i += 12 + 24 * k
                    continue
            i += 1
        if trouve:
            break
    # A record of body 0 closes its view's edges: those after it, before the
    # name, belong to the next view.
    coupes = []
    for nom, brut, paquet in paquets:
        k = next((x for x, r in enumerate(paquet) if r[1] == 0 and r[2]), None)
        if k is None:
            coupes.append((nom, brut, paquet))
        else:
            coupes += [(nom, brut, paquet[:k]), (None, "", paquet[k + 1:])]
    return coupes


def _rattacher(groupes: list, ordre_vues: list[str], non_lus: dict,
               bruts: dict[str, str] | None = None) -> dict[str, list]:
    """Each view's edges, by its name, from the groups of ``_aretes``.

    A group named after its view belongs to it. A group under another name
    (a section, an assembly component) goes to the view that follows the
    previous group's — to the first view if there is none.
    """
    aretes: dict[str, list] = {}
    precedente, coupe = None, None
    for nom, brut, paquet in groupes:
        # An assembly view names its groups after its components
        # (``leg-2@mobile gantry``), once per body: only the first carries
        # edges, the others are empty and do not count.
        if nom is None and not paquet:
            continue
        # An assembly section writes one group per cut component, all under
        # the same prefix (``faucet-1@cabinet_bath-SectionAssembly-1/``).
        m = SECTION_ASSEMBLAGE.match(brut)
        if nom is None and m and m.group() == coupe:
            nom = precedente
        coupe = m.group() if m else None
        if nom is None and (precedente is None or precedente in ordre_vues):
            k = 0 if precedente is None else ordre_vues.index(precedente) + 1
            nom = ordre_vues[k] if k < len(ordre_vues) else None
        if nom is None:
            non_lus["edges of no known view"] = non_lus.get(
                "edges of no known view", 0) + len(paquet)
            continue
        aretes.setdefault(nom, []).extend(paquet)
        if bruts is not None:
            bruts.setdefault(nom, brut)
        precedente = nom
    return aretes


#: The name of a detail circle in ``Definition`` (UTF-16), and the distance
#: from that name to the point (m) of its letter on the sheet.
CERCLE_DETAIL = re.compile(re.escape("Detail Circle".encode("utf-16-le"))
                           + rb"(?:[0-9]\x00)+")
CERCLE_DETAIL_LETTRE = 177
#: How far beyond its radius a circle's letter may be (mm).
CERCLE_DETAIL_MARGE = 15.0


def _cercles_detail(definition: bytes) -> list[tuple[float, float]]:
    """The point (mm) of each detail circle's letter, in ``Definition``
    order."""
    out = []
    for m in CERCLE_DETAIL.finditer(definition):
        at = m.start() + CERCLE_DETAIL_LETTRE
        if at + 16 <= len(definition):
            x, y = unpack_from("<2d", definition, at)
            if all(math.isfinite(v) and 1e-6 < v < 10 for v in (x, y)):
                out.append((_mm(x), _mm(y)))
    return out


def _meme_rotation(a: Vue, b: Vue) -> bool:
    return all(abs(x - y) < 1e-9 for ra, rb in zip(a.rotation, b.rotation, strict=True)
               for x, y in zip(ra, rb, strict=True))


def _details(places: list, definition: bytes, non_lus: dict) -> dict[str, str]:
    """Pairs each detail circle with its parent view and its detail view;
    returns ``{detail view: parent view}``.

    The k-th circle of ``Definition`` goes to the k-th detail view; it is
    the full circle of the sheet nearest its letter. Its parent is the view
    of its sheet nearest to it; the detail view, the first view still free
    that has the parent's rotation and another scale. Its stored transform
    already places what it shows (SolidWorks PDF); its circle is the
    parent's, carried from one projection to the other:
    ``(c − o_parent)·k + o_detail``, ``o`` the part's origin.
    """
    cercles = [(el, f) for f in {id(f): f for _, f in places}.values()
               for el in f.elements if isinstance(el, Arc) and el.cercle]
    tournees = [(v, f) for v, f in places if v.rotation is not None]
    meres: dict[str, str] = {}
    for lettre in _cercles_detail(definition):
        el = min((el for el, _ in cercles), default=None,
                 key=lambda el: math.dist((el.cx, el.cy), lettre))
        if el is not None and math.dist((el.cx, el.cy), lettre) > el.r + CERCLE_DETAIL_MARGE:
            el = None
        if el is None:
            non_lus["detail circle not found"] = non_lus.get(
                "detail circle not found", 0) + 1
            continue
        feuille = next(f for e, f in cercles if e is el)
        sur = [v for v, f in tournees if f is feuille and v.nom not in meres]
        if not sur:
            continue
        mere = min(sur, key=lambda v: math.dist(_mm_pt(v.position), (el.cx, el.cy)))
        detail = next((v for v, _ in tournees if v is not mere and v.nom not in meres
                       and v.nom not in meres.values() and _meme_rotation(v, mere)
                       and abs(v.echelle - mere.echelle) > 1e-12), None)
        if detail is None:
            non_lus["detail circle without a view"] = non_lus.get(
                "detail circle without a view", 0) + 1
            continue
        k = detail.echelle / mere.echelle
        om, od = mere.projeter((0, 0, 0)), detail.projeter((0, 0, 0))
        detail.cercle = ((el.cx - om[0]) * k + od[0], (el.cy - om[1]) * k + od[1],
                         k * el.r)
        meres[detail.nom] = mere.nom
    return meres


def _mm_pt(p) -> tuple[float, float]:
    return (_mm(p[0]), _mm(p[1]))


def _decouper(q: list, cercle: tuple[float, float, float]) -> list[list]:
    """The pieces of a polyline that fall inside a circle."""
    cx, cy, r = cercle
    dedans = lambda p: math.dist(p, (cx, cy)) <= r  # noqa: E731
    morceaux, courant = [], []
    for a, b in pairwise(q):
        dx, dy = b[0] - a[0], b[1] - a[1]
        fx, fy = a[0] - cx, a[1] - cy
        A, B, C = dx * dx + dy * dy, 2 * (fx * dx + fy * dy), fx * fx + fy * fy - r * r
        ts = [0.0, 1.0]
        if A > 0 and B * B - 4 * A * C > 0:
            rac = math.sqrt(B * B - 4 * A * C)
            ts += [t for t in ((-B - rac) / (2 * A), (-B + rac) / (2 * A)) if 0 < t < 1]
        ts.sort()
        for t0, t1 in pairwise(ts):
            p0 = (a[0] + t0 * dx, a[1] + t0 * dy)
            p1 = (a[0] + t1 * dx, a[1] + t1 * dy)
            if dedans(((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)):
                if not courant:
                    courant = [p0]
                courant.append(p1)
            elif courant:
                morceaux.append(courant)
                courant = []
    if courant:
        morceaux.append(courant)
    return [m for m in morceaux if len(m) > 1]


#: The name of an assembly view's group: ``leg-2@mobile gantry``,
#: ``hinge_assy-1@vanity_assembly/pin-1@hinge_assy``.
GROUPE_ASSEMBLAGE = re.compile(r"^.+?-\d+@([^/@]+)(?:/|$)")


def _assemblage_de(brut: str) -> str | None:
    """The assembly a view shows, from the name of its first group."""
    m = GROUPE_ASSEMBLAGE.match(brut)
    if not m or NOM_VUE.search(brut) or "-SectionAssembly-" in m.group(1):
        return None
    return m.group(1).lower()


def _placer(asm, maillages: dict[str, list[FaceOmbree]],
            par_configuration: dict | None = None) -> list[FaceOmbree]:
    """The mesh of each visible component of an assembly, placed by the
    matrix the assembly writes; its colours are those the drawing keeps for
    its part, grey if it keeps none."""
    out = []
    for c in asm.composants:
        if c.cache or c.supprime:
            continue
        r = c.matrice
        tourner = lambda n, r=r: tuple(  # noqa: E731
            n[0] * r[j] + n[1] * r[3 + j] + n[2] * r[6 + j] for j in range(3))
        piece = PureWindowsPath(c.fichier).stem.lower()
        base = ((par_configuration or {}).get((piece, c.configuration))
                or maillages.get(piece))
        if base:
            for f in base:
                out.append(FaceOmbree(
                    [tuple(c.placer(p) for p in t) for t in f.triangles],
                    None if f.normales is None
                    else [tuple(tourner(n) for n in t) for t in f.normales],
                    f.couleur))
        else:
            out.extend(FaceOmbree([tuple(c.placer(p) for p in t) for t in tris],
                                  None, (200, 200, 200)) for tris in c.faces)
    return out


# --- the display mesh (shaded views) --------------------------------------------

#: The headers of a mesh face: its triangle strips, then its vertices and
#: normals — the format of an assembly's ``FaceTessellations``, which
#: ``VBLists`` takes up.
BANDES = (4, 8, 2)
POINTS = (12, 100, 2)
#: What precedes a face's "appearance" field: an MFC back-reference (2
#: bytes) to the appearance it takes, 0 if none, or a new object.
CHAMP_APPARENCE = bytes.fromhex("01000000ffffffff")
#: The class of a break line: the drawing has a broken view.
BRISURE = b"moBreakLine_c"
#: The class a drawing with a cropped view declares (`chair`); such a view
#: also writes its edges flat.
RECADRAGE = b"moDetailCircle_c"
#: The prefix of a component's name cut by an assembly section.
SECTION_ASSEMBLAGE = re.compile(r"^.*?-SectionAssembly-\d+/")
#: The path of a part, which opens its mesh in ``VBLists``.
CHEMIN_PIECE = re.compile(r"\.sldprt$", re.I)
#: A view's name preceded by its part's: ``cube-1@Drawing View1``.
PIECE_VUE = re.compile(r"^(.*)-\d+@(Drawing View\d+)$")


def _faces_maillage(vb: bytes, debut: int, fin: int) -> list[tuple]:
    """``[(offset, end, triangles, normals)]`` of the faces between two
    offsets; strips alternate their winding, as in an assembly."""
    out = []
    i = debut
    while i < fin - 16:
        if unpack_from("<3I", vb, i) != BANDES:
            i += 1
            continue
        k = unpack_from("<I", vb, i + 12)[0]
        if not 1 <= k <= 100_000 or i + 16 + 4 * k + 16 > len(vb):
            i += 1
            continue
        longueurs = unpack_from(f"<{k}I", vb, i + 16)
        n = sum(longueurs)
        j = i + 16 + 4 * k
        if min(longueurs) < 3 or unpack_from("<4I", vb, j) != (*POINTS, n):
            i += 1
            continue
        if j + 16 + 12 * n > len(vb):
            break
        v = unpack_from(f"<{3 * n}f", vb, j + 16)
        pts = [v[3 * a : 3 * a + 3] for a in range(n)]
        j += 16 + 12 * n
        nv = None
        if j + 16 + 12 * n <= len(vb) and unpack_from("<4I", vb, j) == (*POINTS, n):
            w = unpack_from(f"<{3 * n}f", vb, j + 16)
            nv = [w[3 * a : 3 * a + 3] for a in range(n)]
            j += 16 + 12 * n
        tris, norms = [], []
        o = 0
        for L in longueurs:
            for a in range(L - 2):
                t = ((o + a, o + a + 1, o + a + 2) if a % 2 == 0
                     else (o + a + 1, o + a, o + a + 2))
                tris.append(tuple(pts[x] for x in t))
                if nv is not None:
                    norms.append(tuple(nv[x] for x in t))
            o += L
        out.append((i, j, tris, norms if nv is not None else None))
        i = j
    return out


def _couleur_apparence(vb: bytes, a: int, b: int):
    """The colour of the first appearance written between two offsets: its
    name (``defaultplastic``…) is followed by a byte 2 or 3, then the three
    bytes of the colour."""
    for m in re.finditer(re.escape(UTF16), vb[a:b]):
        lu = _cstring(vb, a + m.start())
        if lu and lu[0] and lu[1] + 4 <= b and vb[lu[1]] in (2, 3):
            return tuple(vb[lu[1] + 1 : lu[1] + 4])
    return None


def _maillages(vb: bytes, non_lus: dict,
               par_configuration: dict | None = None) -> dict[str, list[FaceOmbree]]:
    """``{part (file name without extension, lower case): faces}``.

    ``par_configuration`` also receives ``{(part, configuration): faces}``:
    in an assembly view, each instance writes its mesh followed by its
    configuration (cabinet_bath: two mouldings ``long``, two ``short``).

    Each part opens its mesh with its path; its faces follow, up to the next
    path. A face's colour is its own appearance, or the part's. An
    appearance already written comes back through an MFC back-reference: its
    number is the one taken up by the face that follows its creation; a
    reference that cannot be resolved is counted.
    """
    chemins = []
    for m in re.finditer(re.escape(UTF16), vb):
        lu = _cstring(vb, m.start())
        if lu and CHEMIN_PIECE.search(lu[0]):
            chemins.append((m.start(), PureWindowsPath(lu[0]).stem.lower()))
    if not chemins:
        return {}
    out: dict[str, list[FaceOmbree]] = {}
    defauts: dict[str, tuple[int, int, int]] = {}
    bornes = [a for a, _ in chemins] + [len(vb)]
    for x, (debut, piece) in enumerate(chemins):
        faces = _faces_maillage(vb, debut, bornes[x + 1])
        if not faces:
            continue
        chaines = []
        for m in re.finditer(re.escape(UTF16), vb[debut:faces[0][0]]):
            lu = _cstring(vb, debut + m.start())
            chaines.append(lu[0] if lu else None)
        # Path, two empty strings, description, configuration: the header of
        # an instance mesh. A part view's writes the lighting.
        config = (chaines[4] if len(chaines) > 4 and chaines[1:3] == ["", ""]
                  else None)
        cle = (piece, config)
        if piece in out and (par_configuration is None or config is None
                             or cle in par_configuration):
            continue
        # An instance that does not write its part's appearance takes the
        # one the same part wrote earlier (cabinet_bath, view 21).
        defaut = (_couleur_apparence(vb, debut, faces[0][0])
                  or defauts.get(piece) or (200, 200, 200))
        defauts.setdefault(piece, defaut)
        connues: dict[int, tuple[int, int, int]] = {}
        attendue = None
        lues = []
        limites = [f[0] for f in faces[1:]] + [bornes[x + 1]]
        for (_debut, fin, tris, norms), suivante in zip(faces, limites, strict=True):
            couleur = defaut
            j = vb.find(CHAMP_APPARENCE, fin, suivante)
            if j >= 0:
                tag = unpack_from("<H", vb, j + 8)[0]
                # 0xFFFF, the MFC marker of a new class, is not a reference
                # to an appearance: what follows is a double, not a colour
                # (box, lid).
                if tag == 0xFFFF:
                    pass
                elif tag & 0x8000:
                    couleur = tuple(vb[j + 10 : j + 13])
                    attendue = couleur
                elif tag:
                    if tag not in connues and attendue is not None:
                        connues[tag] = attendue
                    attendue = None
                    if tag in connues:
                        couleur = connues[tag]
                    else:
                        non_lus["unresolved face appearance"] = non_lus.get(
                            "unresolved face appearance", 0) + 1
            lues.append(FaceOmbree(tris, norms, couleur))
        out.setdefault(piece, lues)
        if par_configuration is not None and config is not None:
            par_configuration.setdefault(cle, lues)
    return out


def _cacher(vue: Vue, brutes: list) -> None:
    """Removes from a view the pieces of edges its mesh hides.

    A shaded view of the old format, and a hidden-lines-removed view that
    also writes its hidden edges.

    An edge of the old kind does not say whether it is visible: a shaded
    view of the old format writes **all** of them, those underneath and
    inside included (1 893 on a LEGO brick's isometric view). SolidWorks
    hides them on display, behind its mesh; so do we, with the mesh written
    in the file — the depth test of a 3D view, not a computation of the
    part. A segment is hidden when a triangle covering its midpoint lies in
    front of it.
    """
    r = vue.rotation
    axes = [tuple(r[a][j] for a in range(3)) for j in range(3)]

    def ecran(p):
        return tuple(sum(p[a] * ax[a] for a in range(3)) for ax in axes)

    tris = [tuple(ecran(p) for p in t) for f in vue.ombrage for t in f.triangles]
    if not tris:
        return
    us = [p[0] for t in tris for p in t]
    vs = [p[1] for t in tris for p in t]
    ws = [p[2] for t in tris for p in t]
    u0, v0 = min(us), min(vs)
    taille = max(max(us) - u0, max(vs) - v0, 1e-9)
    # What separates "on the face" from "behind": the mesh's chordal error.
    eps = 2e-3 * max(taille, max(ws) - min(ws))
    n = 96
    pas = taille / n
    grille: dict[tuple[int, int], list[int]] = {}
    for k, t in enumerate(tris):
        i0, i1 = (int((min(p[0] for p in t) - u0) / pas), int((max(p[0] for p in t) - u0) / pas))
        j0, j1 = (int((min(p[1] for p in t) - v0) / pas), int((max(p[1] for p in t) - v0) / pas))
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                grille.setdefault((i, j), []).append(k)

    def cache(p) -> bool:
        u, v, w = p
        for k in grille.get((int((u - u0) / pas), int((v - v0) / pas)), ()):
            (ax, ay, az), (bx, by, bz), (cx, cy, cz) = tris[k]
            d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(d) < 1e-18:
                continue
            l1 = ((by - cy) * (u - cx) + (cx - bx) * (v - cy)) / d
            l2 = ((cy - ay) * (u - cx) + (ax - cx) * (v - cy)) / d
            l3 = 1 - l1 - l2
            if min(l1, l2, l3) < -1e-9:
                continue
            if l1 * az + l2 * bz + l3 * cz > w + eps:
                return True
        return False

    gardees = []
    retirees = {id(poly) for poly, _ in brutes}
    for poly, pts in brutes:
        e = [ecran(p) for p in pts]
        vus = [not cache(tuple((a[c] + b[c]) / 2 for c in range(3)))
               for a, b in pairwise(e)]
        morceau: list = []
        for k, vu in enumerate(vus):
            if vu:
                morceau = morceau or [poly.points[k]]
                morceau.append(poly.points[k + 1])
            elif morceau:
                gardees.append(Polyligne(morceau, poly.corps, poly.silhouette, poly.nom))
                morceau = []
        if morceau:
            gardees.append(Polyligne(morceau, poly.corps, poly.silhouette, poly.nom))
    vue.aretes = [a for a in vue.aretes if id(a) not in retirees] + gardees


def _ordre_definition(defn: bytes, feuilles: list[str]) -> list[tuple[str, str]]:
    """``[(sheet, view)…]``: the order of names in ``Definition``.

    A view belongs to the last sheet named before it. Names are taken from
    the body of the stream, after the first part or assembly path; the head
    is read only as a fallback. It may keep the names of views that are
    gone: 28 on ``foodprocessor``, which has 12, all on the last sheet; two
    names shifted on ``universal beam``.
    """
    tete: list[tuple[str, str]] = []
    corps: list[tuple[str, str]] = []
    out, courante, vus = tete, None, set()
    for m in re.finditer(re.escape(UTF16), defn):
        lu = _cstring(defn, m.start())
        if not lu:
            continue
        nom = lu[0]
        if nom.lower().endswith((".sldprt", ".sldasm")):
            if out is tete:
                out, courante, vus = corps, None, set()
            continue
        if nom in feuilles:
            courante = nom
        elif re.fullmatch(r"Drawing View\d+", nom) and nom not in vus:
            vus.add(nom)
            out.append((courante, nom))
    return corps or tete


# --- assembly -------------------------------------------------------------------


def read_drawing(source: str | Path | Container,
                 assemblages: dict | None = None) -> Drawing:
    """A drawing, ready to display: sheets, primitives, views.

    ``assemblages``: the assemblies it shows, read by
    :func:`sldprt.assembly.read_assembly`, keyed by file name without
    extension. The drawing does not store where their components are;
    without them, an assembly view has no mesh.
    """
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
    sans_enregistrement = _feuille_sans_enregistrement(ordre, len(vues), feuilles)
    for at, el in dans_vues:
        k = next((x for x, (av, _) in enumerate(lues) if av > at), None)
        if k is None:
            # A view that ``Definition`` names but whose record
            # ``DisplayLists`` does not keep (the viewport of an imported
            # DWG): what it carries goes to its sheet.
            if sans_enregistrement is not None:
                sans_enregistrement.elements.append(el)
                continue
            non_lus["primitive of no known view"] = non_lus.get(
                "primitive of no known view", 0) + 1
            continue
        places[k][1].elements.append(el)
    # A view's name comes back once per body it shows; edges are merged. A
    # group under another name (a section view's) belongs to the view that
    # follows the previous group's.
    ordre_vues = [v.nom for v, _ in places]
    # Edges for which the file does not say whether they are visible, with
    # their points in the part: the old kind in a shaded view, all of them
    # in a hidden-lines-removed view.
    anciennes: dict[str, list] = {}
    bruts: dict[str, str] = {}
    aretes = _rattacher(_aretes(flux.get("Contents/VBLists", b"")), ordre_vues,
                        non_lus, bruts)
    # A detail view shows its parent, enlarged and clipped to its circle;
    # one that writes no edges takes its parent's.
    meres = _details(places, definition, non_lus)
    for detail, mere in meres.items():
        if not aretes.get(detail):
            aretes[detail] = aretes.get(mere, [])
    rompues = BRISURE in definition or RECADRAGE in definition
    for vue, cible in places:
        paquet = aretes.get(vue.nom, [])
        plate = rompues and _a_plat(paquet)
        if vue.rotation is None and not plate:
            if paquet:
                non_lus["projected view (no rotation stored)"] = non_lus.get(
                    "projected view (no rotation stored)", 0) + 1
            paquet = []
        contour = _contour_311(paquet) if vue.ombree else set()
        for at, corps, genre, pts in paquet:
            # A shaded view also writes edges the shading hides: neither the
            # DXF nor the screen shows them — except the 0x311 edges mixed
            # with its silhouettes.
            if (vue.ombree and genre not in (ARETE, SILHOUETTE, ARETE_ANCIENNE)
                    and at not in contour):
                continue
            if plate:
                q = [(1000 * (vue.position[0] + p[0]), 1000 * (vue.position[1] + p[1]))
                     for p in pts]
            else:
                q = [vue.projeter(p) for p in pts]
            if vue.cercle:
                for morceau in _decouper(q, vue.cercle):
                    vue.aretes.append(Polyligne(morceau, corps, genre == SILHOUETTE,
                                                f"A{at}"))
                continue
            if not all(-1 <= x <= cible.largeur + 1 and -1 <= y <= cible.hauteur + 1
                       for x, y in q):
                non_lus["edge off the sheet"] = non_lus.get("edge off the sheet", 0) + 1
                continue
            vue.aretes.append(Polyligne(q, corps, genre == SILHOUETTE, f"A{at}"))
            if not plate and ((vue.ombree and genre == ARETE_ANCIENNE)
                              or vue.mode == CACHEES):
                anciennes.setdefault(vue.nom, []).append((vue.aretes[-1], pts))
        cible.vues.append(vue)
    # A shaded view is painted with its part's display mesh, which VBLists
    # keeps for every view.
    asm_vue = {nom: a for nom, b in bruts.items() if (a := _assemblage_de(b))}
    places_asm: dict[str, list[FaceOmbree]] = {}
    if any(v.ombree or v.mode == CACHEES for v, _ in places):
        vb = flux.get("Contents/VBLists", b"")
        par_config: dict = {}
        maillages = _maillages(vb, non_lus, par_config)
        pieces = {}
        for m in re.finditer(re.escape(UTF16), vb):
            lu = _cstring(vb, m.start())
            nom = PIECE_VUE.match(lu[0]) if lu else None
            if nom:
                pieces.setdefault(nom.group(2), nom.group(1).lower())

        def faces_de(nom: str, placer: bool = True) -> list[FaceOmbree]:
            if nom in pieces:
                return maillages.get(pieces[nom], [])
            asm = asm_vue.get(nom)
            if placer and asm and assemblages and asm in assemblages:
                if asm not in places_asm:
                    places_asm[asm] = _placer(assemblages[asm], maillages, par_config)
                return places_asm[asm]
            if nom in meres:
                return faces_de(meres[nom], placer)
            return []

        for vue, _ in places:
            # A hidden-lines-removed view may also write the edges it hides
            # (foodprocessor): the same test. Not with the placed assembly:
            # a section view keeps its edges in the cutting plane, which the
            # whole mesh would hide entirely (bolt-assembly, View5: 57 edges
            # → 6).
            if vue.mode == CACHEES and vue.nom in anciennes:
                faces = faces_de(vue.nom, placer=False)
                if faces:
                    vue.ombrage = faces
                    _cacher(vue, anciennes[vue.nom])
                    vue.ombrage = []
            if vue.ombree:
                vue.ombrage = faces_de(vue.nom)
                if not vue.ombrage:
                    non_lus["shaded view with no mesh read"] = non_lus.get(
                        "shaded view with no mesh read", 0) + 1
                elif vue.nom in anciennes:
                    _cacher(vue, anciennes[vue.nom])
    # Centre marks are not in the display list: they live only in
    # ``Definition`` (class ``moCenterMarkSym_c``), which is not decoded.
    if b"moCenterMarkSym_c" in definition:
        non_lus[REPERE_CENTRE] = 1
    for nom_feuille, image in _images(flux, non_lus):
        cible = next((f for f in feuilles if f.nom == nom_feuille), None)
        if cible is None:
            non_lus["image on no known sheet"] = non_lus.get(
                "image on no known sheet", 0) + 1
            continue
        cible.elements.append(image)
    return Drawing(feuilles, non_lus)


def _contour_311(paquet: list) -> set[int]:
    """The offsets of the 0x311 edges of a shaded view that are visible.

    A run of 0x311 followed by a silhouette (0x331) is a piece of the
    outline; a run followed by anything else, or nothing, is hidden by the
    shading.
    """
    vues: set[int] = set()
    suite: list[int] = []
    for at, _, genre, _ in paquet:
        if genre == ARETE_311:
            suite.append(at)
            continue
        if genre == SILHOUETTE:
            vues.update(suite)
        suite = []
    return vues


def _feuille_sans_enregistrement(ordre: list, n_vues: int, feuilles: list):
    """The sheet of the first view that ``Definition`` names but whose record
    ``DisplayLists`` does not keep, or ``None``."""
    if len(ordre) <= n_vues:
        return None
    return next((f for f in feuilles if f.nom == ordre[n_vues][0]), None)


def _a_plat(paquet: list) -> bool:
    """A broken view writes its edges already flattened, in sheet metres
    around its position: z is zero everywhere. An ordinary view writes them
    in the part's frame."""
    return bool(paquet) and all(p[2] == 0.0 for _, _, _, pts in paquet for p in pts)


def _images(flux: dict, non_lus: dict) -> list[tuple[str, Image]]:
    """The drawing's OLE images.

    ``OleItemInformation/OLE_Item_Data``: a u32, the number of objects; for
    each, the sheet (CString), its holder (``DrSheet``) and two points
    (x, y, z) in metres, top-left and bottom-right corners. The image
    SolidWorks displays is ``OLE_Item_Image_<n>``, a PNG.
    """
    d = flux.get("OleItemInformation/OLE_Item_Data", b"")
    if len(d) < 4:
        return []
    out = []
    at = 4
    for n in range(unpack_from("<I", d, 0)[0]):
        feuille = _cstring(d, at)
        porteur = _cstring(d, feuille[1]) if feuille else None
        if porteur is None or porteur[1] + 48 > len(d):
            non_lus["OLE image not read"] = non_lus.get("OLE image not read", 0) + 1
            break
        xa, ya, _, xb, yb, _ = unpack_from("<6d", d, porteur[1])
        suivant = d.find(UTF16, porteur[1] + 48)
        at = suivant if suivant >= 0 else len(d)
        png = flux.get(f"OleItemInformation/OLE_Item_Image_{n}", b"")
        fin = png.find(b"IEND")
        if not png.startswith(PNG_SIGNATURE) or fin < 0:
            non_lus["OLE image not read"] = non_lus.get("OLE image not read", 0) + 1
            continue
        out.append((feuille[0], Image(_mm(min(xa, xb)), _mm(min(ya, yb)),
                                      _mm(max(xa, xb)), _mm(max(ya, yb)),
                                      png[: fin + 8], f"O{n}")))
    return out


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
    # Colours and widths written in the file, so that it reads on its
    # own, outside the page (whose stylesheet takes precedence).
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" '
        f'width="{_f(x1 - x0)}mm" height="{_f(y1 - y0)}mm" '
        f'data-feuille="{_esc(feuille.nom)}" font-family="Century Gothic, sans-serif">',
        f'<rect class="papier" x="0" y="0" width="{_f(w)}" height="{_f(h)}" fill="#fff"/>',
    ]
    for el in feuille.elements:
        if isinstance(el, Image):
            out.append(
                f'<image data-nom="{el.nom}" x="{_f(el.x0)}" y="{y(el.y1)}" '
                f'width="{_f(el.x1 - el.x0)}" height="{_f(el.y1 - el.y0)}" '
                f'preserveAspectRatio="none" href="data:image/png;base64,'
                f'{base64.b64encode(el.png).decode()}"/>')
    for k, vue in enumerate(feuille.vues):
        if vue.ombrage:
            # A detail view paints only its circle.
            if vue.cercle:
                cx, cy, r = vue.cercle
                out.append(f'<clipPath id="detail{k}"><circle cx="{_f(cx)}" '
                           f'cy="{y(cy)}" r="{_f(r)}"/></clipPath>')
            coupe = f' clip-path="url(#detail{k})"' if vue.cercle else ""
            out.append(f'<g class="ombre" data-vue="{_esc(vue.nom)}"{coupe}>')
            out.extend(_ombrage(vue, y))
            out.append("</g>")
    out.append('<g class="trait" fill="none" stroke="#000" stroke-width="0.25" '
               'stroke-linecap="round" stroke-linejoin="round">')
    for el in feuille.elements:
        c = _teinte(el, "stroke")
        motif = TRAITS.get(getattr(el, "trait", None))
        if motif:
            c += f' stroke-dasharray="{" ".join(_f(v) for v in motif)}"'
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
            plein = _teinte(el, "stroke", "fill") or ' fill="#000"'
            out.append(f'<polygon data-nom="{el.nom}" class="plein"'
                       f'{plein} points="{pts}"/>')
    for vue in feuille.vues:
        out.append(f'<g class="vue" data-vue="{_esc(vue.nom)}">')
        for a in vue.aretes if vue.avec_aretes else ():
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
            # SVG counts its angles clockwise.
            turn = (f' transform="rotate({_f(-math.degrees(el.angle))} {_f(el.x)} '
                    f'{y(el.y)})"' if abs(el.angle) > 1e-9 else "")
            out.append(f'<text data-nom="{el.nom}"{_teinte(el, "fill")}{famille} '
                       f'x="{_f(el.x)}" y="{y(el.y)}" font-size="{_f(el.hauteur)}"{turn} '
                       f'dominant-baseline="text-after-edge">{_esc(el.texte)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def _ombrage(vue: Vue, y) -> list[str]:
    """A shaded view's triangles, farthest first (painter's order). A
    triangle's tint is its stored colour, darkened by the angle of its
    normal with the view direction: a light at the eye, as the page's 3D
    view does; SolidWorks's lighting, also stored, is not replayed."""
    r = vue.rotation
    profondeur = (r[0][2], r[1][2], r[2][2])
    triangles = []
    for face in vue.ombrage:
        for k, tri in enumerate(face.triangles):
            z = sum(sum(p[a] * profondeur[a] for a in range(3)) for p in tri)
            if face.normales is not None:
                n = [sum(m[a] for m in face.normales[k]) for a in range(3)]
                long = math.sqrt(sum(c * c for c in n)) or 1.0
                lum = 0.35 + 0.65 * abs(sum(n[a] * profondeur[a] for a in range(3))) / long
            else:
                lum = 0.8
            triangles.append((z, tri, tuple(min(255, round(c * lum)) for c in face.couleur)))
    triangles.sort(key=lambda t: t[0])
    out = []
    for _, tri, (cr, cg, cb) in triangles:
        pts = " ".join(f"{_f(px)},{y(py)}" for px, py in (vue.projeter(p) for p in tri))
        teinte = f"#{cr:02x}{cg:02x}{cb:02x}"
        # A stroke of the same tint closes the slits between triangles.
        out.append(f'<polygon fill="{teinte}" stroke="{teinte}" stroke-width="0.05" '
                   f'points="{pts}"/>')
    return out


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
        elif isinstance(el, Image):
            xs += [el.x0, el.x1]
            ys += [el.y0, el.y1]
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
            # For a turned text, "above" turns with it.
            lift = 0.289 * el.hauteur
            out.extend(["0", "TEXT", "8", "TEXT", "62", c,
                        "10", g(el.x - lift * math.sin(el.angle)),
                        "20", g(el.y + lift * math.cos(el.angle)), "30", "0",
                        "40", g(el.hauteur), "1", _dxf_texte(el.texte)])
            if abs(el.angle) > 1e-9:
                out.extend(["50", g(math.degrees(el.angle))])
    for vue in feuille.vues:
        calque = _calque(vue.nom)
        for a in vue.aretes:
            polyligne(a.points, calque, 7, False)
    out.extend(["0", "ENDSEC", "0", "EOF"])
    return "\n".join(out) + "\n"
