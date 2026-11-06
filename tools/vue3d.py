"""Triangle mesh of a STEP file for the web page's 3D view.

Each face keeps the name the converter wrote in its ``ADVANCED_FACE``
(``F<node>``), read back from the STEP through OpenCASCADE's XCAF layer, and
the colour the STEP sets on it or on its body.
"""

from __future__ import annotations

import base64
import contextlib
import io
import math
import re
import sys
from array import array
from pathlib import Path

FLECHE_RELATIVE = 1.0e-3

ANGLE = 0.35


_ENTITE = re.compile(rb"#(\d+)\s*=\s*([A-Z_0-9]+)\s*\((.*?)\)\s*;", re.S)
_LABEL = re.compile(rb"(?m)^\s*#(\d+)\s*=")
_REF = re.compile(rb"#(\d+)")
_NOM = re.compile(rb"'((?:[^']|'')*)'")

_REEL = re.compile(rb"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def couleurs(chemin: Path | str) -> dict[int, tuple[float, float, float]]:
    """Colours set by the STEP: entity number → (r, g, b)."""
    return _teintes(_entites(Path(chemin).read_bytes()))


def _entites(texte: bytes) -> dict[int, tuple[bytes, bytes]]:
    """Simple entities of the text: label → (type, arguments)."""
    debut = texte.find(b"DATA;")
    return {int(m[1]): (m[2], m[3]) for m in _ENTITE.finditer(texte, max(debut, 0))}


def _teintes(entites) -> dict[int, tuple[float, float, float]]:
    """:func:`couleurs` on entities already read: each ``STYLED_ITEM``
    followed down to its ``COLOUR_RGB``."""

    def rgb(n: int, profondeur: int = 0):
        e = entites.get(n)
        if e is None or profondeur > 12:
            return None
        genre, args = e
        if genre == b"COLOUR_RGB":
            v = [float(x) for x in _REEL.findall(args.split(b",", 1)[1])]
            return tuple(v[:3]) if len(v) >= 3 else None
        for r in _REF.findall(args):
            c = rgb(int(r), profondeur + 1)
            if c is not None:
                return c
        return None

    out = {}
    for genre, args in entites.values():
        if genre not in (b"STYLED_ITEM", b"OVER_RIDING_STYLED_ITEM"):
            continue
        refs = [int(r) for r in _REF.findall(args)]
        if len(refs) < 2:
            continue
        c = rgb(refs[0])
        if c is not None:
            out.setdefault(refs[-1], c)  # the target is the last argument
    return out


def _corps(forme):
    """Bodies in order: solids, then free shells and faces."""
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    corps = []
    for genre, sauf in ((TopAbs_SOLID, None), (TopAbs_SHELL, TopAbs_SOLID),
                        (TopAbs_FACE, TopAbs_SHELL)):
        explorateur = (TopExp_Explorer(forme, genre) if sauf is None
                       else TopExp_Explorer(forme, genre, sauf))
        while explorateur.More():
            corps.append(explorateur.Current())
            explorateur.Next()
    return corps


def _faces(corps):
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    explorateur = TopExp_Explorer(corps, TopAbs_FACE)
    while explorateur.More():
        yield TopoDS.Face_s(explorateur.Current())
        explorateur.Next()


def _lire(chemin):
    """The STEP read through XCAF, face names included.

    ``read.stepcaf.subshapes.name`` gives one label per named face, so the
    shape → name table is built in one pass (12 s on a 6,309-face part,
    against 34 s looking each face up in the transfer). Naming faces by
    their order would be wrong: OpenCASCADE reorders a shell's faces.
    """
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Controller, STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_LabelSequence
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopTools import TopTools_IndexedMapOfShape
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    # The parameter only exists once the XCAF controller is declared.
    STEPCAFControl_Controller.Init_s()
    Interface_Static.SetIVal_s("read.stepcaf.subshapes.name", 1)
    document = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    XCAFApp_Application.GetApplication_s().InitDocument(document)
    lecteur = STEPCAFControl_Reader()
    lecteur.SetNameMode(True)
    lecteur.SetColorMode(False)  # read from the text, see `couleurs`
    lecteur.SetLayerMode(False)
    with contextlib.redirect_stdout(io.StringIO()):
        if lecteur.ReadFile(str(chemin)) != IFSelect_ReturnStatus.IFSelect_RetDone:
            raise RuntimeError(f"OpenCASCADE cannot read {Path(chemin).name}")
        if not lecteur.Transfer(document):
            raise RuntimeError(f"OpenCASCADE cannot transfer {Path(chemin).name}")
    outil = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    libres = TDF_LabelSequence()
    outil.GetFreeShapes(libres)
    if libres.Length() != 1:
        raise RuntimeError(f"{libres.Length()} free shapes in the STEP, one expected")

    connues, noms = TopTools_IndexedMapOfShape(), []

    def parcourir(etiquette):
        dessous = TDF_LabelSequence()
        outil.GetSubShapes_s(etiquette, dessous)
        for i in range(1, dessous.Length() + 1):
            sous = dessous.Value(i)
            forme = outil.GetShape_s(sous)
            nom = TDataStd_Name()
            if (forme.ShapeType() == TopAbs_FACE and not connues.Contains(forme)
                    and sous.FindAttribute(TDataStd_Name.GetID_s(), nom)):
                connues.Add(forme)
                noms.append(nom.Get().ToExtString())
            parcourir(sous)

    parcourir(libres.Value(1))

    def nom_de(face):
        k = connues.FindIndex(face)
        return noms[k - 1] if k else ""

    # The document keeps the shape alive: return it along.
    return outil.GetShape_s(libres.Value(1)), lecteur.Reader(), nom_de, document


def maillage(chemin: Path | str) -> dict:
    """The STEP at *chemin*, meshed face by face, each face under its name."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location

    forme, lecteur, nom_de, _document = _lire(chemin)
    transfert = lecteur.WS().TransferReader()
    modele = lecteur.WS().Model()
    # A colour targets a face or a body by its label. A face is found by
    # its unique name; a body by the entity the transfer gives for it.
    # ``Model().Number`` returns 0 under OCP, so an entity is found by its
    # rank, which follows the order of labels in the file. Python wrappers
    # are kept alive so that ``id`` is a stable key.
    texte = Path(chemin).read_bytes()
    entites = _entites(texte)
    teintes = _teintes(entites)
    par_nom, par_id, tenues = {}, {}, []
    if teintes:
        # A name designates a face only if unique: SolidWorks leaves them empty.
        compte = {}
        for etiquette, (genre, args) in entites.items():
            if genre == b"ADVANCED_FACE":
                m = _NOM.search(args)
                nom = m[1].decode("latin-1") if m else ""
                compte[nom] = compte.get(nom, 0) + 1
                par_nom[nom] = teintes.get(etiquette)
        par_nom = {n: c for n, c in par_nom.items() if n and compte[n] == 1}
        rang_de = {int(m[1]): i for i, m in enumerate(
            _LABEL.finditer(texte, max(texte.find(b"DATA;"), 0)), start=1)}
        for etiquette, c in teintes.items():
            i = rang_de.get(etiquette)
            if i is not None and i <= modele.NbEntities():
                v = modele.Value(i)
                tenues.append(v)
                par_id[id(v)] = c

    boite = Bnd_Box()
    BRepBndLib.Add_s(forme, boite)
    if boite.IsVoid():
        raise RuntimeError("the STEP has no geometry")
    x0, y0, z0, x1, y1, z1 = boite.Get()
    centre = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
    diagonale = math.dist((x0, y0, z0), (x1, y1, z1)) or 1.0
    BRepMesh_IncrementalMesh(forme, FLECHE_RELATIVE * diagonale, False, ANGLE, True)

    faces, sommets = [], array("f")
    for rang, corps in enumerate(_corps(forme), start=1):
        du_corps = None
        if par_id:
            entite = transfert.EntityFromShapeResult(corps, -1)
            du_corps = par_id.get(id(entite)) if entite is not None else None
        for face in _faces(corps):
            nom = nom_de(face)
            lieu = TopLoc_Location()
            triangulation = BRep_Tool.Triangulation_s(face, lieu)
            n = 0
            if triangulation is not None:
                trsf = lieu.Transformation()
                points = [None]
                for i in range(1, triangulation.NbNodes() + 1):
                    p = triangulation.Node(i).Transformed(trsf)
                    points.append((p.X() - centre[0], p.Y() - centre[1],
                                   p.Z() - centre[2]))
                inverse = face.Orientation() == TopAbs_REVERSED
                for i in range(1, triangulation.NbTriangles() + 1):
                    a, b, c = triangulation.Triangle(i).Get()
                    if inverse:
                        b, c = c, b
                    sommets.extend((*points[a], *points[b], *points[c]))
                n = triangulation.NbTriangles()
            f = {"nom": nom or "?", "corps": rang, "triangles": n}
            if nom in par_nom:
                peinte = par_nom[nom] or du_corps
            elif par_id:
                # No unique name: look the entity up in the transfer (slow).
                entite = transfert.EntityFromShapeResult(face, -1)
                peinte = (par_id.get(id(entite)) if entite is not None else None) or du_corps
            else:
                peinte = du_corps
            if peinte is not None:
                f["couleur"] = [round(x, 4) for x in peinte]
            faces.append(f)
    if sys.byteorder != "little":
        sommets.byteswap()
    return {
        "faces": faces,
        "corps": max((f["corps"] for f in faces), default=0),
        "rayon": diagonale / 2,
        "sommets": base64.b64encode(sommets.tobytes()).decode("ascii"),
    }
