"""The published page, Python side: what the browser runs under Pyodide.

The static page (``tools/pages.py``) has no server: the uploaded file never
leaves the user's machine. The converting code is nevertheless the same as
``webui.py``'s: its ``run``, ``voir`` and ``voir_assemblage`` are called as
they are, in a Web Worker, and their journal is sent back line by line.

Only the 3D view of a part differs. OpenCASCADE does not exist under
Pyodide; the page meshes the STEP with ``occt-import-js`` (OpenCASCADE in
WebAssembly), which does not return face names but does return each face's
colour. :func:`etiqueter` writes, for the view only, a STEP where each face
carries a colour that encodes its index, and the page reads the face's name
back from the table. The downloaded file is untouched.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable

import vue3d
import webui

#: Levels per channel of the colour encoding an index: 64³ faces.
NIVEAUX = 64

_SOLIDES = (b"MANIFOLD_SOLID_BREP", b"BREP_WITH_VOIDS")
_COQUES = {b"CLOSED_SHELL", b"OPEN_SHELL", b"ORIENTED_CLOSED_SHELL",
           b"ORIENTED_OPEN_SHELL"}
_REPRESENTATIONS = (b"ADVANCED_BREP_SHAPE_REPRESENTATION",
                    b"MANIFOLD_SURFACE_SHAPE_REPRESENTATION")
_STYLES = re.compile(
    rb"(?m)^#\d+\s*=\s*(?:STYLED_ITEM|OVER_RIDING_STYLED_ITEM|"
    rb"MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION)\s*\(.*;[ \t]*\r?\n"
)


def code(rang: int) -> tuple[float, float, float]:
    """The colour that designates the face at index *rang*."""
    n = NIVEAUX
    if not 0 <= rang < n ** 3:
        raise ValueError(f"at most {n ** 3} faces, not {rang + 1}")
    return (rang // (n * n) / (n - 1), rang // n % n / (n - 1), rang % n / (n - 1))


def etiqueter(texte: bytes) -> tuple[bytes, list[dict]]:
    """The STEP *texte* with each face painted the colour of its index, and
    the face table: ``{"nom", "corps", "couleur"?}`` per index.

    Bodies follow :func:`vue3d._corps` (solids, then each shell of a surface
    model); a face takes the colour the file gives it, or its body's.
    """
    entites = vue3d._entites(texte)
    teintes = vue3d._teintes(entites)

    def descendre(etiquette, ancetres, profondeur=0):
        genre, args = entites.get(etiquette, (b"", b""))
        if genre == b"ADVANCED_FACE":
            yield etiquette, ancetres
        elif (genre in _COQUES or genre in _SOLIDES) and profondeur < 6:
            for r in vue3d._REF.findall(args):
                yield from descendre(int(r), (*ancetres, etiquette), profondeur + 1)

    corps = [[e] for e, (g, _a) in sorted(entites.items()) if g in _SOLIDES]
    for e, (g, a) in sorted(entites.items()):
        if g == b"SHELL_BASED_SURFACE_MODEL":
            corps += [[e, int(r)] for r in vue3d._REF.findall(a)]

    table, vues = [], set()
    for rang, chaine in enumerate(corps, start=1):
        for face, ancetres in descendre(chaine[-1], tuple(chaine[:-1])):
            if face in vues:
                continue
            vues.add(face)
            m = vue3d._NOM.search(entites[face][1])
            f = {"etiquette": face, "nom": m[1].decode("latin-1") if m else "",
                 "corps": rang}
            peinte = teintes.get(face)
            for a in reversed(ancetres):
                peinte = peinte or teintes.get(a)
            if peinte is not None:
                f["couleur"] = [round(x, 4) for x in peinte]
            table.append(f)

    contexte = None
    for _e, (g, a) in sorted(entites.items()):
        if g in _REPRESENTATIONS:
            contexte = vue3d._REF.findall(a)[-1].decode()
            break
    if contexte is None or not table:
        return texte, table

    fin = texte.rfind(b"ENDSEC;")
    corps_texte = _STYLES.sub(b"", texte[:fin])
    n = max(entites) + 1
    lignes, items = [], []
    for rang, f in enumerate(table):
        r, g, b = code(rang)
        lignes += [
            f"#{n} = COLOUR_RGB ( '', {r!r}, {g!r}, {b!r} ) ;",
            f"#{n + 1} = FILL_AREA_STYLE_COLOUR ( '', #{n} ) ;",
            f"#{n + 2} = FILL_AREA_STYLE ( '', ( #{n + 1} ) ) ;",
            f"#{n + 3} = SURFACE_STYLE_FILL_AREA ( #{n + 2} ) ;",
            f"#{n + 4} = SURFACE_SIDE_STYLE ( '', ( #{n + 3} ) ) ;",
            f"#{n + 5} = SURFACE_STYLE_USAGE ( .BOTH. , #{n + 4} ) ;",
            f"#{n + 6} = PRESENTATION_STYLE_ASSIGNMENT (( #{n + 5} ) ) ;",
            f"#{n + 7} = STYLED_ITEM ( 'NONE', ( #{n + 6} ), #{f['etiquette']} ) ;",
        ]
        items.append(f"#{n + 7}")
        n += 8
    lignes.append(
        f"#{n} = MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION ( '', ( "
        + ", ".join(items) + f" ), #{contexte} ) ;"
    )
    for f in table:
        del f["etiquette"]
    return corps_texte + ("\n".join(lignes) + "\n").encode("ascii") + texte[fin:], table


def _vue_navigateur(job: webui.Job) -> None:
    """``webui.préparer_vue`` without OpenCASCADE: the labelled STEP, which
    the page will mesh. Never fails the job."""
    try:
        job.etiquete = etiqueter(job.result.read_bytes())
    except Exception as echec:
        job.journal.say(f"3D view failed: {type(echec).__name__}: {echec}", "note")
        return
    job.journal.say("3D view: meshing in the browser (OpenCASCADE in WebAssembly)",
                    "step")


#: What the server says and the browser does not do.
_HORS_SUJET = ("files deleted from the server",)


def lancer(route: str, nom: str, data: bytes, dire: Callable[[str], None],
           version: str = "") -> dict:
    """A page job, run as ``webui`` runs it, journal included.

    *dire* receives each journal line as JSON, as it comes. Returns
    ``{"fichier", "resultat", "vue", "plan", "etiquete", "faces"}``.
    """
    if hasattr(data, "to_bytes"):  # a Uint8Array from JavaScript
        data = data.to_bytes()
    # webui is borrowed, not modified: what is replaced is restored on exit.
    avant = webui.version, webui.préparer_vue
    if version:
        webui.version = lambda: version
    webui.préparer_vue = _vue_navigateur
    try:
        return _lancer(route, nom, data, dire)
    finally:
        webui.version, webui.préparer_vue = avant


def _lancer(route, nom, data, dire) -> dict:
    job = webui.Job(webui.safe_name(nom), route)
    job.etiquete = None
    dit = job.journal.say

    def say(text: str, kind: str = "detail") -> None:
        if text.startswith(_HORS_SUJET):
            return
        dit(text, kind)
        dire(json.dumps(job.journal.since(len(job.journal._lines) - 1)[0]))

    job.journal.say = say
    cible = {webui.ROUTE_PLAN: webui.voir,
             webui.ROUTE_ASSEMBLAGE: webui.voir_assemblage}.get(route)
    if cible is None and route not in webui.ROUTES:
        raise ValueError(f"unknown route: {route}")
    (cible or webui.run)(job, data)

    sortie = {"fichier": "", "resultat": None, "vue": None, "plan": None,
              "etiquete": None, "faces": None, "fiche": None}
    if job.result is not None and job.result.exists():
        sortie["fichier"] = job.result.name
        sortie["resultat"] = job.result.read_bytes()
    if job.vue is not None:
        sortie["vue"] = job.vue.decode()
    if job.plan is not None:
        sortie["plan"] = job.plan.decode()
    if job.fiche is not None:
        sortie["fiche"] = job.fiche.decode()
    if job.etiquete is not None:
        sortie["etiquete"], faces = job.etiquete
        sortie["faces"] = json.dumps(faces)
    if job.dossier is not None:
        shutil.rmtree(job.dossier, ignore_errors=True)
    return sortie
