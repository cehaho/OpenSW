"""A part's DimXpert annotations: dimensions, tolerances, datums.

DimXpert is the SolidWorks tool that places PMI (*Product and Manufacturing
Information*) on the 3D model: toleranced dimensions, geometric tolerances
(flatness, position, profile…), A/B/C datums. It writes its state in one
stream per configuration, ``SWIFT/Config<n>-Schema<m>``.

That stream has two layers:

1. an MFC object graph (``moSwiftSchema_c``, ``moDisplayGtol_c``…) — the
   display wrapping;
2. from the string ``Entity`` + ``PrizMetrik.GdtAnalysisSupport.GdtPart``
   on, the **semantics**: a tree of entities written by the .NET library
   ``PrizMetrik.GdtAnalysis`` (``gdtanalysis.net``), the DimXpert engine.

This module reads the second layer, which is self-describing. Strings are
those of .NET's ``BinaryWriter`` (length as a 7-bit encoded integer, then
UTF-8), integers are ``int32`` and reals ``double``, little-endian ::

    entity   := "Entity" type assembly version:i32 section* "EndEntity"
    section  := "Strings"  n (key value:string)ⁿ          "EndStrings"
              | "Integers" n (key value:i32)ⁿ             "EndIntegers"
              | "Doubles"  n (key value:f64)ⁿ             "EndDoubles"
              | list       n (key type)ⁿ entity*          "End"list
    list     := "Features" | "RelatedObjects" | "Annotations"

In a list, the entities that follow the ``n`` pairs are those of the pairs,
in order — or none, when the list only **refers** to entities written
elsewhere (an annotation's ``Features``, which names the feature it targets
by its ``F…`` identifier). Measured on the NIST DimXpert test parts: no list
has a number of entities other than 0 or n, and reading ends **exactly** on
the last byte of each stream.

Values are in **millimetres** (and radians for an angle), whatever the
part's unit: 0.254 on an inch part for 0.01 in, as the API reports.

A dimension's nominal value is **not** written (``Nominal`` is 0):
SolidWorks measures it on the geometry. This module does not compute it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from struct import unpack_from

#: What opens the semantic part of the stream: the string ``Entity`` (6 bytes
#: announced) followed by the root's type, ``PrizMetrik.GdtAnalysisSupport.GdtPart``
#: (37 bytes announced, ``%``).
RACINE = b"\x06Entity%PrizMetrik.GdtAnalysisSupport.GdtPart"

#: Prefix of the DimXpert streams in the container.
PREFIXE_FLUX = "SWIFT/"

#: The sections that carry references and, possibly, their entities.
LISTES = ("Features", "RelatedObjects", "Annotations")


class DimXpertError(ValueError):
    """The stream does not follow the grammar at the point being read."""


@dataclass
class Entite:
    """A node of the ``PrizMetrik`` tree, as written."""

    type: str
    assemblage: str
    version: int
    chaines: dict[str, str] = field(default_factory=dict)
    entiers: dict[str, int] = field(default_factory=dict)
    reels: dict[str, float] = field(default_factory=dict)
    #: Per list: the ``(key, type)`` pairs in stream order.
    renvois: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    #: Per list: the entities written in place (empty for a pure reference).
    enfants: dict[str, list[Entite]] = field(default_factory=dict)

    @property
    def genre(self) -> str:
        """The class's short name: ``GdtPosition`` → ``Position``."""
        nom = self.type.rsplit(".", 1)[-1]
        return nom[3:] if nom.startswith(("Gdt", "Geo")) else nom

    def objet(self, cle: str) -> Entite | None:
        """The related object named ``cle`` in ``RelatedObjects``."""
        for (nom, _), ent in zip(
            self.renvois.get("RelatedObjects", ()),
            self.enfants.get("RelatedObjects", ()),
            strict=False,
        ):
            if nom == cle:
                return ent
        return None


class _Lecteur:
    def __init__(self, data: bytes, at: int) -> None:
        self.data, self.at = data, at

    def _besoin(self, n: int) -> None:
        if self.at + n > len(self.data):
            raise DimXpertError(f"reading {n} bytes at {self.at} past {len(self.data)}")

    def chaine(self) -> str:
        n = decalage = 0
        while True:
            self._besoin(1)
            octet = self.data[self.at]
            self.at += 1
            n |= (octet & 0x7F) << decalage
            decalage += 7
            if octet < 0x80:
                break
            if decalage > 28:
                raise DimXpertError(f"invalid string length at {self.at}")
        self._besoin(n)
        try:
            valeur = self.data[self.at : self.at + n].decode("utf-8")
        except UnicodeDecodeError as err:
            raise DimXpertError(f"non-UTF-8 string at {self.at}") from err
        self.at += n
        return valeur

    def suivante(self) -> str:
        at = self.at
        try:
            return self.chaine()
        finally:
            self.at = at

    def i32(self) -> int:
        self._besoin(4)
        v = unpack_from("<i", self.data, self.at)[0]
        self.at += 4
        return v

    def f64(self) -> float:
        self._besoin(8)
        v = unpack_from("<d", self.data, self.at)[0]
        self.at += 8
        return v

    def entite(self) -> Entite:
        debut = self.at
        if (mot := self.chaine()) != "Entity":
            raise DimXpertError(f"« Entity » expected at {debut}, read {mot!r}")
        e = Entite(self.chaine(), self.chaine(), self.i32())
        while (section := self.chaine()) != "EndEntity":
            n = self.i32()
            if n < 0:
                raise DimXpertError(f"negative count for {section} at {self.at}")
            if section == "Strings":
                e.chaines = {self.chaine(): self.chaine() for _ in range(n)}
            elif section == "Integers":
                e.entiers = {self.chaine(): self.i32() for _ in range(n)}
            elif section == "Doubles":
                e.reels = {self.chaine(): self.f64() for _ in range(n)}
            elif section in LISTES:
                e.renvois[section] = [(self.chaine(), self.chaine()) for _ in range(n)]
                enfants = []
                while self.suivante() == "Entity":
                    enfants.append(self.entite())
                if len(enfants) not in (0, n):
                    raise DimXpertError(
                        f"{section} of {e.type}: {n} references but {len(enfants)} entities"
                    )
                e.enfants[section] = enfants
            else:
                raise DimXpertError(f"unknown section {section!r} at {self.at}")
            if (fin := self.chaine()) != "End" + section:
                raise DimXpertError(f"« End{section} » expected at {self.at}, read {fin!r}")
        return e


def lire_arbre(flux: bytes) -> Entite | None:
    """The stream's ``GdtPart`` root, or ``None`` if it has none.

    A ``SWIFT/…-MachiningWrapper0`` stream has none: it is something other
    than DimXpert. A stream that has one but cannot be read to the end
    raises :class:`DimXpertError`.
    """
    debut = flux.find(RACINE)
    if debut < 0:
        return None
    lecteur = _Lecteur(flux, debut)
    racine = lecteur.entite()
    if lecteur.at != len(flux):
        raise DimXpertError(
            f"the tree ends at {lecteur.at}, the stream at {len(flux)}: {len(flux) - lecteur.at} bytes unread"
        )
    return racine


@dataclass(frozen=True)
class Fonction:
    """A manufacturing feature recognised by DimXpert (plane, hole…)."""

    ident: str
    genre: str
    nom: str


@dataclass(frozen=True)
class Annotation:
    """A DimXpert dimension, geometric tolerance or datum."""

    ident: str
    genre: str
    nom: str
    #: The names of the targeted features, in stream order.
    fonctions: tuple[str, ...]
    #: Tolerance zone of a geometric tolerance (mm).
    tolerance: float | None = None
    #: Deviations of a dimension (mm, or rad for an angle); ``moins`` is negative.
    plus: float | None = None
    moins: float | None = None
    #: Outer part of a surface profile (mm).
    tolerance_exterieure: float | None = None
    #: A datum's letter.
    identifiant: str | None = None
    #: Letters of the primary, secondary, tertiary datums.
    references: tuple[tuple[str, ...], ...] = ()
    entite: Entite | None = field(default=None, compare=False, repr=False)

    @property
    def nom_api(self) -> str:
        """The ``Name`` the API returns, without the ``(value)`` suffix.

        The API writes ``<name>@<feature>``; a datum's name there is
        generated (``Datum205``) and is not in the stream.
        """
        return f"{self.nom}@{self.fonctions[0]}" if self.fonctions else self.nom


@dataclass(frozen=True)
class DimXpert:
    """The DimXpert content of one ``SWIFT/`` stream — one configuration."""

    flux: str
    fonctions: tuple[Fonction, ...]
    annotations: tuple[Annotation, ...]
    racine: Entite = field(compare=False, repr=False)


def _references(ent: Entite, lettres: dict[str, str]) -> tuple[tuple[str, ...], ...]:
    sortie = []
    for cle in ("PrimaryDatums", "SecondaryDatums", "TertiaryDatums"):
        coll = ent.objet(cle)
        if coll is None:
            break
        rang = []
        for appliquee in coll.enfants.get("RelatedObjects", ()):
            for ident, _ in appliquee.renvois.get("Annotations", ()):
                rang.append(lettres.get(ident, ident))
        sortie.append(tuple(rang))
    while sortie and not sortie[-1]:
        sortie.pop()
    return tuple(sortie)


def interpreter(nom_flux: str, racine: Entite) -> DimXpert:
    """The named features and annotations, references resolved."""
    fonctions = {
        ident: Fonction(ident, ent.genre, ent.chaines.get("ObjectName", ""))
        for (ident, _), ent in zip(
            racine.renvois.get("Features", ()), racine.enfants.get("Features", ()), strict=True
        )
    }
    paires = list(
        zip(racine.renvois.get("Annotations", ()), racine.enfants.get("Annotations", ()), strict=True)
    )
    lettres = {
        ident: ent.chaines.get("DatumIdentifier", "")
        for (ident, _), ent in paires
        if "DatumIdentifier" in ent.chaines
    }
    annotations = []
    for (ident, _), ent in paires:
        cibles = tuple(
            fonctions[f].nom if f in fonctions else f for f, _ in ent.renvois.get("Features", ())
        )
        r = ent.reels
        geometrique = "Tolerance" in r
        annotations.append(
            Annotation(
                ident=ident,
                genre=ent.genre,
                nom=ent.chaines.get("ObjectName", ""),
                fonctions=cibles,
                tolerance=r.get("Tolerance") if geometrique else None,
                plus=None if geometrique else r.get("PlusTolerance"),
                moins=None if geometrique else r.get("MinusTolerance"),
                tolerance_exterieure=r.get("OuterToleranceValue"),
                identifiant=ent.chaines.get("DatumIdentifier"),
                references=_references(ent, lettres),
                entite=ent,
            )
        )
    return DimXpert(nom_flux, tuple(fonctions.values()), tuple(annotations), racine)


def lire_dimxpert(source) -> list[DimXpert]:
    """The DimXpert of each ``SWIFT/`` stream of a part that has one.

    ``source`` is a path or an already opened container. A part without
    DimXpert gives an empty list.
    """
    from .container import open_container

    conteneur = open_container(source) if isinstance(source, (str, Path)) else source
    sortie = []
    for blob in conteneur.blobs:
        if not blob.name.startswith(PREFIXE_FLUX):
            continue
        racine = lire_arbre(blob.data)
        if racine is not None:
            sortie.append(interpreter(blob.name, racine))
    return sortie


# -------------------------------------------------------------- for the view
#
# Each feature carries its nominal geometry in its ``RelatedObjects``, in mm
# in the model frame: ``NomPlane`` (a point of the plane and its normal),
# ``NomCylinder``/``NomCone`` (a point of the axis, the axis) bounded by
# ``NomTop``/``NomBottom``, ``NomSphere``, ``NomCompoundWidth``… A compound
# feature (pattern, compound hole) carries none: it refers to its members
# through ``SubFeatures``. A fillet carries none at all.

#: The ASME Y14.5 symbol of each geometric tolerance.
SYMBOLES = {
    "Position": "⌖", "Concentricity": "◎", "Symmetry": "⌯",
    "SurfaceProfile": "⌓", "CompositeSurfaceProfile": "⌓", "LineProfile": "⌒",
    "Angularity": "∠", "Parallelism": "∥", "Perpendicularity": "⊥",
    "CircularRunout": "↗", "TotalRunout": "⌰", "Flatness": "⏥",
    "Circularity": "○", "Cylindricity": "⌭", "Straightness": "⏤",
    "Tangency": "T",
}

#: A dimension's prefix, before its deviations.
PREFIXES = {
    "Diameter": "⌀", "CounterBore": "⌴⌀", "CounterSinkDiameter": "⌵⌀",
    "Radius": "R", "AngleBetween": "∠", "CounterSinkAngle": "⌵∠", "Depth": "↧",
}

#: The dimensions whose deviations are angles (rad in the stream).
ANGULAIRES = {"AngleBetween", "CounterSinkAngle"}


def _xyz(e: Entite) -> tuple[float, float, float] | None:
    r = e.reels
    return (r["X"], r["Y"], r["Z"]) if {"X", "Y", "Z"} <= r.keys() else None


def _ancre_propre(f: Entite) -> tuple[float, float, float] | None:
    """The point where a feature's annotation goes, if it is written."""
    haut, bas = f.objet("NomTop"), f.objet("NomBottom")
    if haut is not None and bas is not None and _xyz(haut) and _xyz(bas):
        a, b = _xyz(haut), _xyz(bas)
        return tuple((u + v) / 2 for u, v in zip(a, b, strict=True))
    for (cle, _), g in zip(
        f.renvois.get("RelatedObjects", ()), f.enfants.get("RelatedObjects", ()), strict=False
    ):
        if cle.startswith("Nom") and cle not in ("NomOrigin",) and _xyz(g):
            return _xyz(g)
    return None


def ancres(dx: DimXpert) -> dict[str, tuple[float, float, float]]:
    """The point of each feature that has one, by identifier.

    A compound feature takes that of its first member that has one.
    """
    par_ident = dict(
        zip(
            (i for i, _ in dx.racine.renvois.get("Features", ())),
            dx.racine.enfants.get("Features", ()),
            strict=True,
        )
    )
    sortie: dict[str, tuple[float, float, float]] = {}

    def ancre(ident: str, vus: frozenset = frozenset()) -> tuple[float, float, float] | None:
        if ident in sortie:
            return sortie[ident]
        f = par_ident.get(ident)
        if f is None or ident in vus:
            return None
        point = _ancre_propre(f)
        if point is None and (membres := f.objet("SubFeatures")) is not None:
            for appliquee in membres.enfants.get("RelatedObjects", ()):
                for cible, _ in appliquee.renvois.get("Features", ()):
                    if (point := ancre(cible, vus | {ident})) is not None:
                        break
                if point is not None:
                    break
        if point is not None:
            sortie[ident] = point
        return point

    for ident in par_ident:
        ancre(ident)
    return sortie


def _nombre(v: float) -> str:
    texte = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if texte in ("-0", "") else texte.replace("-", "−")


def cellules(a: Annotation) -> list[str]:
    """What the view writes of an annotation, cell by cell.

    A datum: its letter. A geometric tolerance: its symbol, its zone, its
    datums. A dimension: its prefix and its deviations — **without** the
    nominal value, which is not written in the file.
    Values are in mm, angles in degrees.
    """
    if a.genre == "Datum":
        return [a.identifiant or "?"]
    if a.tolerance is not None or a.genre in SYMBOLES:
        cases = [SYMBOLES.get(a.genre, a.genre)]
        if a.tolerance is not None:
            cases.append(_nombre(a.tolerance))
        cases += ["".join(rang) for rang in a.references]
        return cases
    plus, moins = a.plus or 0.0, a.moins or 0.0
    unite = ""
    if a.genre in ANGULAIRES:
        plus, moins, unite = math.degrees(plus), math.degrees(moins), "°"
    if abs(plus + moins) < 1e-9:
        ecarts = f"±{_nombre(plus)}{unite}"
    else:
        haut = f"+{_nombre(plus)}" if plus else "0"
        ecarts = f"{haut}{unite}/{_nombre(moins)}{unite}"
    prefixe = PREFIXES.get(a.genre, "")
    return [f"{prefixe} {ecarts}".strip()]


def pmi_en_vue(source) -> list[dict]:
    """The annotations to place in the 3D view, in stream order.

    Each one: ``nom``, ``genre``, ``fonction`` (the name of the first
    targeted feature), ``cases`` (:func:`cellules`), ``cadre`` (true for a
    geometric tolerance and a datum) and ``ancre`` (mm, model frame). An
    annotation none of whose features has a point (a fillet) is left out.
    """
    sortie = []
    for dx in lire_dimxpert(source)[:1]:  # the active configuration
        points = ancres(dx)
        for a in dx.annotations:
            idents = [i for i, _ in a.entite.renvois.get("Features", ())] if a.entite else []
            point = next((points[i] for i in idents if i in points), None)
            if point is None:
                continue
            sortie.append({
                "nom": a.nom,
                "genre": a.genre,
                "fonction": a.fonctions[0] if a.fonctions else "",
                "cases": cellules(a),
                "cadre": a.genre == "Datum" or a.tolerance is not None or a.genre in SYMBOLES,
                "ancre": [round(v, 6) for v in point],
            })
    return sortie
