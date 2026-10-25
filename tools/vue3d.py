"""Triangle mesh of a STEP file for the web page's 3D view."""

from __future__ import annotations

import base64
import contextlib
import io
import math
import sys
from array import array
from pathlib import Path

FLECHE_RELATIVE = 1.0e-3

ANGLE = 0.35


def _corps(forme):
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


def maillage(chemin: Path | str) -> dict:
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location

    lecteur = STEPControl_Reader()
    with contextlib.redirect_stdout(io.StringIO()):
        if lecteur.ReadFile(str(chemin)) != IFSelect_ReturnStatus.IFSelect_RetDone:
            raise RuntimeError(f"OpenCASCADE cannot read {Path(chemin).name}")
        lecteur.TransferRoots()
        forme = lecteur.OneShape()
    transfert = lecteur.WS().TransferReader()

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
        for face in _faces(corps):
            entite = transfert.EntityFromShapeResult(face, -1)
            nom = entite.Name().ToCString() if entite is not None else ""
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
            faces.append({"nom": nom or "?", "corps": rang, "triangles": n})
    if sys.byteorder != "little":
        sommets.byteswap()
    return {
        "faces": faces,
        "corps": max((f["corps"] for f in faces), default=0),
        "rayon": diagonale / 2,
        "sommets": base64.b64encode(sommets.tobytes()).decode("ascii"),
    }

