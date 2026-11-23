#!/usr/bin/env python3
"""Local web page to convert a part to Parasolid or STEP, or view a drawing
or an assembly.

A drawing (``.SLDDRW``) is not converted: it is read by
:mod:`sldprt.drawing` and shown as SVG, sheet by sheet. An assembly
(``.SLDASM``) is read by :mod:`sldprt.assembly` and shown in 3D from its
display mesh.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import convert

PORT = 8765
HOST = "127.0.0.1"

GARDE_S = 30 * 60
MAX_UPLOAD = 200 * 1024 * 1024


def safe_name(raw: str) -> str:
    """An uploaded file name reduced to harmless characters."""
    name = re.sub(r"[^\w.()+ -]", "_", os.path.basename(raw))[:120].lstrip(". ")
    return name or "part.SLDPRT"


def _commit() -> str:
    racine = Path(__file__).resolve().parent.parent
    try:
        vu = subprocess.run(
            ["git", "-C", str(racine), "log", "-1", "--format=%h %cd",
             "--date=format:%Y-%m-%d %H:%M"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown version (no git)"
    sortie = vu.stdout.strip()
    if vu.returncode != 0 or not sortie:
        return "unknown version (not a git checkout)"
    return sortie


def _empreinte() -> str:
    racine = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for dossier in (racine / "src" / "sldprt", racine / "tools"):
        for fichier in sorted(dossier.rglob("*.py")):
            digest.update(str(fichier.relative_to(racine)).encode("utf-8"))
            try:
                digest.update(fichier.read_bytes())
            except OSError:
                digest.update(b"(unreadable)")
    return digest.hexdigest()


_AU_DEMARRAGE = (_commit(), _empreinte())


def version() -> str:
    commit, empreinte = _AU_DEMARRAGE
    if _empreinte() != empreinte:
        return f"{commit} (sources changed since start: restart the page)"
    return commit


ROUTES = {
    "parasolid": ("Parasolid", ".x_t", convert.parasolid),
    "step": ("STEP", ".STEP", convert.step),
}

CONVERSION = threading.Lock()


class Journal:
    def __init__(self) -> None:
        self._lines: list[dict] = []
        self._lock = threading.Lock()
        self._start = time.monotonic()

    def say(self, text: str, kind: str = "detail") -> None:
        with self._lock:
            self._lines.append({
                "n": len(self._lines),
                "ms": int((time.monotonic() - self._start) * 1000),
                "text": text,
                "kind": kind,
            })

    def since(self, mark: int) -> list[dict]:
        with self._lock:
            return self._lines[mark:]


class Job:
    def __init__(self, name: str, route: str) -> None:
        self.id = uuid.uuid4().hex
        self.name = name
        self.route = route
        self.journal = Journal()
        self.done = False
        self.failed = False
        self.result: Path | None = None
        self.dossier: Path | None = None
        self.source: Path | None = None
        self.fini_a: float | None = None
        self.vue: bytes | None = None
        #: The STEP of the sketches alone, written with a part's view.
        self.esquisses_step: Path | None = None
        self.plan: bytes | None = None
        self.fiche: bytes | None = None
        self.composants: list | None = None
        #: The records of the parts supplied with the assembly, by file.
        self.pieces: dict = {}
        self.contraintes: list | None = None


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _human(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} kB"


def run(job: Job, data: bytes) -> None:
    say = job.journal.say
    label, suffix, écrire = ROUTES[job.route]
    try:
        dossier = job.dossier = Path(tempfile.mkdtemp(prefix="opensw-"))
        source = job.source = dossier / job.name
        source.write_bytes(data)
        say(f"{job.name} received, {_human(len(data))}", "ok")
        say(f"converter: {version()}", "detail")
        say(f"{label}: {écrire.__module__}.{écrire.__name__}()", "step")

        sortie = dossier / (source.stem + suffix)
        bavardage = io.StringIO()
        début = time.monotonic()
        if CONVERSION.locked():
            say("another conversion is running, waiting", "detail")
        with CONVERSION, contextlib.redirect_stdout(bavardage), \
                contextlib.redirect_stderr(bavardage):
            dit = écrire(source, sortie)
        for ligne in bavardage.getvalue().splitlines():
            if ligne.strip():
                say(ligne.rstrip(), "detail")

        if not sortie.exists():
            raise RuntimeError(f"{écrire.__name__} wrote nothing")
        job.result = sortie
        say(f"{dit}, {_human(sortie.stat().st_size)} "
            f"in {time.monotonic() - début:.1f} s", "ok")
        say(f"{sortie.name} ready to download", "ok")
        if job.route == "step":
            préparer_vue(job)
    except Exception as échec:
        job.failed = True
        say(f"{type(échec).__name__}: {échec}", "err")
        for ligne in traceback.format_exc().splitlines()[-6:]:
            say(ligne.rstrip(), "detail")
    finally:
        effacer_source(job)
        job.fini_a = time.monotonic()
        job.done = True
        if job.result is not None:
            say(f"files deleted from the server {GARDE_S // 60} min "
                "after this conversion", "detail")


def _annotations_assemblage(job: Job, asm, fichiers: dict[str, Path], vue: dict) -> None:
    """The sketches and PMI of the supplied parts, placed where the assembly
    places each visible instance: read once per part, in its frame (mm),
    then sent through the component's matrix. A name says the instance:
    ``cube-1 · Sketch1``. An unreadable part does not break the view."""
    from sldprt import sketch_view
    from sldprt.dimxpert import pmi_en_vue

    say = job.journal.say
    lues: dict[str, tuple[list, list]] = {}
    for nom in dict.fromkeys(c.fichier for c in asm.composants):
        chemin = fichiers.get(nom.lower())
        if chemin is None or chemin.suffix.lower() != ".sldprt":
            continue
        esq, pmi = [], []
        try:
            esq = sketch_view.strokes(chemin)
        except Exception as échec:
            say(f"{nom}: sketches not read — {type(échec).__name__}: {échec}", "note")
        try:
            pmi = pmi_en_vue(chemin)
        except Exception as échec:
            say(f"{nom}: PMI not read — {type(échec).__name__}: {échec}", "note")
        lues[nom] = (esq, pmi)

    def placer(c, xyz):
        q = c.placer([v / 1000 for v in xyz])
        return [1000 * v for v in q]

    esquisses, annotations = [], []
    for c in asm.composants:
        if c.cache or c.supprime or c.fichier not in lues:
            continue
        esq, pmi = lues[c.fichier]
        for e in esq:
            placé = dict(e, nom=f"{c.nom} · {e['nom']}")
            for clé in ("trait", "construction"):
                pts = e[clé]
                placé[clé] = [v for i in range(0, len(pts), 3)
                              for v in placer(c, pts[i:i + 3])]
            esquisses.append(placé)
        for a in pmi:
            annotations.append(dict(a, nom=f"{c.nom} · {a['nom']}", ancre=placer(c, a["ancre"])))
    if esquisses:
        vue["esquisses"] = esquisses_en_vue(esquisses, vue["centre"])
        say(f"sketches: {len(esquisses)} in the view, placed per instance", "ok")
    if annotations:
        vue["pmi"] = annotations
        say(f"PMI: {len(annotations)} DimXpert annotations in the view, placed per instance", "ok")


def lire_fiche(job: Job) -> None:
    """Read the uploaded file's own record (properties, configurations,
    material, preview) while it is still there. A file that is not a
    SolidWorks document (an ``.x_t``) has none, and that is not an error."""
    if job.source is None or not job.source.exists():
        return
    try:
        import base64

        from sldprt import open_container
        from sldprt.properties import read_properties as fiche

        f = fiche(open_container(job.source))
    except Exception:
        return
    for cle, png in (("apercu", f.pop("apercu_png")),
                     ("apercu_grand", f.pop("apercu_grand_png"))):
        f[cle] = base64.b64encode(png).decode() if png else None
    for fe in f["feuilles"]:
        fe["png"] = base64.b64encode(fe["png"]).decode() if fe["png"] else None
    if job.composants is not None:
        f["composants"] = job.composants
        f["contraintes"] = job.contraintes or []
        f["pieces"] = job.pieces
    job.fiche = json.dumps(f).encode()


def effacer_source(job: Job) -> None:
    lire_fiche(job)
    if job.dossier is None:
        return
    if job.result is None:
        shutil.rmtree(job.dossier, ignore_errors=True)
    elif job.source is not None and job.source != job.result:
        job.source.unlink(missing_ok=True)


def purger(maintenant: float | None = None) -> int:
    maintenant = time.monotonic() if maintenant is None else maintenant
    with JOBS_LOCK:
        échus = [j for j in JOBS.values()
                 if j.done and j.fini_a is not None
                 and maintenant - j.fini_a > GARDE_S]
        for job in échus:
            del JOBS[job.id]
    for job in échus:
        if job.dossier is not None:
            shutil.rmtree(job.dossier, ignore_errors=True)
    return len(échus)


def _balayer() -> None:
    while True:
        time.sleep(60)
        with contextlib.suppress(Exception):
            purger()


#: The route that converts nothing: it reads a drawing to show it.
ROUTE_PLAN = "plan"


def plan_json(source) -> dict:
    """A drawing (a path, or one already read) ready for the page: each
    sheet as SVG."""
    from sldprt.drawing import Drawing, read_drawing, to_svg

    dessin = source if isinstance(source, Drawing) else read_drawing(source)
    return {
        "feuilles": [
            {
                "nom": f.nom,
                "active": f.active,
                "svg": to_svg(f),
                "elements": len(f.elements),
                "vues": [{"nom": v.nom, "echelle": v.echelle,
                          "aretes": len(v.aretes)} for v in f.vues],
            }
            for f in dessin.feuilles
        ],
        "non_lus": dessin.non_lus,
    }


def voir(job: Job, data: bytes) -> None:
    """Read a drawing and keep its SVG in memory for the page.

    The uploaded file is written only while it is read, then deleted; the
    DXF export stays until the purge.
    """
    say = job.journal.say
    try:
        dossier = job.dossier = Path(tempfile.mkdtemp(prefix="opensw-"))
        say(f"{job.name} received, {_human(len(data))}", "ok")
        assemblages = {}
        if data[:4] == b"PK\x03\x04":
            source, chemins = _deballer_plan(data, dossier, job.name)
            from sldprt.assembly import read_assembly

            for a in chemins:
                try:
                    assemblages[a.stem.lower()] = read_assembly(a)
                except Exception as échec:
                    say(f"{a.name} not read: {type(échec).__name__}: {échec}", "note")
            say(f"zip: {source.name} and {len(assemblages)} assembly(ies) read, "
                "which place the parts of shaded views", "detail")
        else:
            source = dossier / job.name
            source.write_bytes(data)
        job.source = source
        say(f"reader: {version()}", "detail")
        say("drawing: sldprt.drawing.read_drawing()", "step")
        début = time.monotonic()
        from sldprt.drawing import read_drawing, to_dxf

        dessin = read_drawing(source, assemblages or None)
        plan = plan_json(dessin)
        for f in plan["feuilles"]:
            vues = ", ".join(
                f"{v['nom']} ({v['aretes']} edges, scale {v['echelle']:g})"
                for v in f["vues"]) or "no view"
            actif = " (active)" if f["active"] else ""
            say(f"{f['nom']}{actif}: {f['elements']} elements, {vues}", "detail")
        for motif, n in plan["non_lus"].items():
            say(f"not shown: {motif}, {n}", "note")
        say(f"{len(plan['feuilles'])} sheet(s) read in "
            f"{time.monotonic() - début:.1f} s", "ok")
        job.plan = json.dumps(plan).encode()
        job.result = exporter_plan(dessin, source, to_dxf)
        say(f"{job.result.name} ready to download (DXF R12, mm)", "ok")
    except Exception as échec:
        job.failed = True
        say(f"{type(échec).__name__}: {échec}", "err")
        for ligne in traceback.format_exc().splitlines()[-6:]:
            say(ligne.rstrip(), "detail")
    finally:
        effacer_source(job)
        job.fini_a = time.monotonic()
        job.done = True


def exporter_plan(dessin, source: Path, to_dxf) -> Path:
    """The drawing as DXF next to *source*: one file per sheet, zipped when
    there are several."""
    if len(dessin.feuilles) == 1:
        sortie = source.with_suffix(".dxf")
        sortie.write_bytes(to_dxf(dessin.feuilles[0]).encode("cp1252"))
        return sortie
    sortie = source.with_suffix(".zip")
    with zipfile.ZipFile(sortie, "w", zipfile.ZIP_DEFLATED) as z:
        for i, f in enumerate(dessin.feuilles, start=1):
            nom = re.sub(r"[^\w -]", "_", f.nom).strip() or str(i)
            z.writestr(f"{source.stem}-{i:02d}-{nom}.dxf",
                       to_dxf(f).encode("cp1252"))
    return sortie


#: The other route that converts nothing: an assembly, shown in 3D from the
#: display mesh it keeps of its parts.
ROUTE_ASSEMBLAGE = "assemblage"


def _deballer(data: bytes, dossier: Path, nom: str) -> tuple[Path, dict[str, Path]]:
    """An uploaded zip: the assembly, and the parts by file name.

    Only the name of each entry counts, never its path: nothing is written
    outside ``dossier/pieces``. Several assemblies: the one named like the
    zip, else the largest (a sub-assembly is smaller than its parent).
    """
    pieces = dossier / "pieces"
    pieces.mkdir()
    assemblages, fichiers = [], {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            base = PurePosixPath(info.filename.replace("\\", "/")).name
            if info.is_dir() or not base or base.startswith("~$"):
                continue
            cible = pieces / base
            cible.write_bytes(z.read(info))
            if base.lower().endswith(".sldasm"):
                assemblages.append(cible)
            elif base.lower().endswith(".sldprt"):
                fichiers[base.lower()] = cible
    if not assemblages:
        raise ValueError("the zip holds no .SLDASM")
    tete = next((a for a in assemblages if a.stem.lower() == Path(nom).stem.lower()),
                max(assemblages, key=lambda a: a.stat().st_size))
    return tete, fichiers


def _step_assemblage(job: Job, asm, fichiers: dict[str, Path], sortie: Path) -> None:
    """The exact STEP of the assembly, from the parts supplied."""
    from sldprt.load import load_model
    from sldprt.step_assembly import write_step_assembly

    say = job.journal.say
    voulus = {c.fichier.lower(): c.fichier for c in asm.composants if not c.supprime}
    modeles = {}
    with CONVERSION:
        for cle, nom in voulus.items():
            if cle not in fichiers:
                continue
            try:
                modeles[nom] = load_model(fichiers[cle])
            except Exception as échec:
                say(f"{nom}: unreadable, {type(échec).__name__}: {échec}", "note")
        rapport = write_step_assembly(asm, modeles, sortie, name=sortie.stem)
    for nom, r in rapport.parts.items():
        say(f"{nom}: {r.bodies} bodies, {r.faces} faces", "detail")
    for nom, pourquoi in rapport.refused.items():
        say(f"{nom}: no body written, {pourquoi}", "note")
    if rapport.suppressed or rapport.hidden:
        say(f"not exported, as in SolidWorks: {rapport.suppressed} suppressed, "
            f"{rapport.hidden} hidden", "detail")
    for nom, n in rapport.missing.items():
        say(f"part not supplied: {nom} ({n} instance(s)), left out", "note")
    job.result = sortie
    say(f"{sortie.name} ready to download, exact STEP, {rapport.instances} "
        f"instance(s) of {len(rapport.parts)} part(s), {_human(sortie.stat().st_size)}",
        "ok")


def _fiches_pieces(asm, fichiers: dict[str, Path]) -> dict:
    """What each supplied part says about itself, for the bill of
    materials: its properties and, per configuration, its properties and
    material. Read in the part, never computed; a missing part has none."""
    from sldprt import open_container
    from sldprt.properties import read_properties

    out = {}
    for nom in dict.fromkeys(c.fichier for c in asm.composants):
        chemin = fichiers.get(nom.lower())
        if chemin is None:
            continue
        try:
            f = read_properties(open_container(chemin))
        except Exception:
            continue
        out[nom] = {
            "proprietes": [[p["nom"], p["valeur"]] for p in f["proprietes"]],
            "configurations": {
                c["nom"]: {"proprietes": [[p["nom"], p["valeur"]] for p in c["proprietes"]],
                           "materiau": (c.get("materiau") or {}).get("nom") or ""}
                for c in f["configurations"]},
        }
    return out


def _plan_dans_zip(data: bytes) -> bool:
    """A zip that holds a drawing: the drawing is what is looked at."""
    if data[:4] != b"PK\x03\x04":
        return False
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return any(n.lower().endswith(".slddrw") for n in z.namelist())


def _deballer_plan(data: bytes, dossier: Path, nom: str) -> tuple[Path, list[Path]]:
    """A dropped zip: the drawing, and the assemblies its views show. The
    drawing keeps each part's mesh, not where the assembly places it. Only
    the name of each entry counts, never its path."""
    plans, assemblages = [], []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            base = PurePosixPath(info.filename.replace("\\", "/")).name
            if info.is_dir() or not base or base.startswith("~$"):
                continue
            if base.lower().endswith((".slddrw", ".sldasm")):
                cible = dossier / base
                cible.write_bytes(z.read(info))
                (plans if base.lower().endswith(".slddrw") else assemblages).append(cible)
    if not plans:
        raise ValueError("the zip holds no .SLDDRW")
    tete = next((p for p in plans if p.stem.lower() == Path(nom).stem.lower()), plans[0])
    return tete, assemblages


def voir_assemblage(job: Job, data: bytes) -> None:
    """Read an assembly and keep its 3D view in memory for the page.

    Alone, it gives its display mesh as STL. Uploaded with its parts (a zip,
    or several files the page zips together), it gives an exact STEP. What
    is uploaded is deleted as soon as it is read; the export stays until
    the purge.
    """
    from sldprt.assembly import read_assembly, to_stl, to_view

    if _plan_dans_zip(data):
        voir(job, data)
        return
    say = job.journal.say
    try:
        dossier = job.dossier = Path(tempfile.mkdtemp(prefix="opensw-"))
        say(f"{job.name} received, {_human(len(data))}", "ok")
        fichiers: dict[str, Path] = {}
        if data[:4] == b"PK\x03\x04":
            source, fichiers = _deballer(data, dossier, job.name)
            say(f"zip: {source.name} and {len(fichiers)} part(s)", "detail")
        else:
            source = dossier / job.name
            source.write_bytes(data)
        job.source = source
        say(f"reader: {version()}", "detail")
        say("assembly: sldprt.assembly.read_assembly()", "step")
        début = time.monotonic()
        asm = read_assembly(source)
        for c in asm.composants:
            etat = " (suppressed)" if c.supprime else " (hidden)" if c.cache else ""
            triangles = sum(len(f) for f in c.faces)
            say(f"{c.nom}{etat}: {c.fichier}, {len(c.faces)} faces, "
                f"{triangles} triangles", "detail")
        # The component list, as the tree gives it: the page groups it
        # into a bill of materials by file.
        job.composants = [{"nom": c.nom, "fichier": c.fichier, "cache": c.cache,
                           "supprime": c.supprime,
                           "hors_nomenclature": c.hors_nomenclature,
                           "configuration": c.configuration} for c in asm.composants]
        job.pieces = _fiches_pieces(asm, fichiers)
        job.contraintes = [{"nom": c.nom, "type": c.type, "libelle": c.libelle,
                            "valeur_m": c.valeur_m} for c in asm.contraintes]
        for motif, n in asm.non_lus.items():
            say(f"not shown: {motif}, {n}", "note")
        vue = to_view(asm)
        if fichiers:
            _annotations_assemblage(job, asm, fichiers, vue)
        say(f"{len(asm.composants)} component(s) read in "
            f"{time.monotonic() - début:.1f} s (SolidWorks display mesh, "
            "not the exact geometry)", "ok")
        job.vue = json.dumps(vue).encode()
        if fichiers:
            say("assembly STEP: sldprt.step_assembly.write_step_assembly()", "step")
            _step_assemblage(job, asm, fichiers, dossier / (source.stem + ".STEP"))
        else:
            sortie = dossier / (source.stem + ".stl")
            sortie.write_bytes(to_stl(asm))
            job.result = sortie
            say(f"{sortie.name} ready to download, {_human(sortie.stat().st_size)} "
                "(display mesh, mm)", "ok")
            say("for an exact STEP, drop the assembly together with its parts "
                "(several files, or a zip)", "detail")
    except Exception as échec:
        job.failed = True
        say(f"{type(échec).__name__}: {échec}", "err")
        for ligne in traceback.format_exc().splitlines()[-6:]:
            say(ligne.rstrip(), "detail")
    finally:
        effacer_source(job)
        if job.dossier is not None:
            shutil.rmtree(job.dossier / "pieces", ignore_errors=True)
        job.fini_a = time.monotonic()
        job.done = True


def préparer_vue(job: Job) -> None:
    say = job.journal.say
    try:
        import OCP  # noqa: F401
        import vue3d
    except ImportError:
        say("3D view unavailable: OpenCASCADE is not installed "
            "(pip install -e \".[occ]\")", "note")
        return
    début = time.monotonic()
    say("3D view: reading the STEP with OpenCASCADE", "step")
    try:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            vue = vue3d.maillage(job.result)
    except Exception as échec:
        say(f"3D view failed: {type(échec).__name__}: {échec}", "note")
        return
    faces = vue["faces"]
    say(f"3D view: {len(faces)} faces, {vue['corps']} bodies, "
        f"in {time.monotonic() - début:.1f} s", "ok")
    sans_nom = sum(f["nom"] == "?" for f in faces)
    if sans_nom:
        say(f"{sans_nom} unnamed faces (split by OpenCASCADE on reading)",
            "note")
    vides = sum(f["triangles"] == 0 for f in faces)
    if vides:
        say(f"{vides} faces OpenCASCADE cannot mesh, not shown", "note")
    _esquisses_dans_la_vue(job, vue)
    _pmi_dans_la_vue(job, vue)
    job.vue = json.dumps(vue).encode()


def lire_esquisses(job: Job) -> list[dict] | None:
    """The uploaded part's sketches, placed in the model, as strokes
    (:func:`sldprt.sketch_view.strokes`), read in this process, under
    Pyodide too. The STEP of the sketches alone, in pure Python, is written
    from the same reading, next to the part's, and offered for download.
    Returns ``None``, after saying so in the log, when there is nothing to
    show: a reading error does not break the view."""
    say = job.journal.say
    if job.source is None or job.source.suffix.lower() != ".sldprt":
        return None
    début = time.monotonic()
    try:
        from sldprt import sketch_view

        lecture = sketch_view.read(job.source)
        liste = sketch_view.strokes(lecture)
    except Exception as échec:
        say(f"sketches not read: {type(échec).__name__}: {échec}", "note")
        return None
    if not liste:
        say("sketches: none read in this part", "note")
        return None
    say(f"sketches: {len(liste)} in the view ({sum(e['3d'] for e in liste)} in 3D), "
        f"in {time.monotonic() - début:.1f} s", "ok")
    # SolidWorks opens the STEP of the sketches as 3D sketches; the
    # published page writes it too.
    esq = job.result.with_name(job.result.stem + ".sketches.step")
    try:
        sketch_view.write_step(lecture, esq, f"{job.source.stem} — sketches")
    except Exception as échec:
        say(f"{esq.name} not written: {type(échec).__name__}: {échec}", "note")
        return liste
    if esq.exists():
        job.esquisses_step = esq
        say(f"{esq.name} ready to download: sketches only, no solid, "
            f"{_human(esq.stat().st_size)}", "ok")
    return liste


def esquisses_en_vue(liste: list[dict], centre) -> dict:
    """``vue["esquisses"]``: the sketch table (``trait`` and
    ``construction`` as ``[first, count]`` vertices) and the vertices as
    little-endian ``float32``, base64, relative to ``centre`` like the
    faces'."""
    import base64
    from array import array

    flottants, fiches = array("f"), []
    for e in liste:
        fiche = {"nom": e["nom"], "3d": e["3d"], "fonction": e.get("fonction")}
        for clé in ("trait", "construction"):
            pts = e[clé]
            fiche[clé] = [len(flottants) // 3, len(pts) // 3]
            flottants.extend(v - centre[i % 3] for i, v in enumerate(pts))
        fiches.append(fiche)
    if sys.byteorder != "little":
        flottants.byteswap()
    return {"liste": fiches, "sommets": base64.b64encode(flottants.tobytes()).decode("ascii")}


def _esquisses_dans_la_vue(job: Job, vue: dict) -> None:
    """Add the part's sketches to the view as strokes (:func:`lire_esquisses`).
    If they are not read, the view goes without them."""
    if "centre" not in vue:
        return
    liste = lire_esquisses(job)
    if liste:
        vue["esquisses"] = esquisses_en_vue(liste, vue["centre"])


def lire_pmi(job: Job) -> list[dict] | None:
    """The uploaded part's DimXpert annotations, placed on their features
    (:func:`sldprt.dimxpert.pmi_en_vue`, mm in the model), under Pyodide
    too. ``None`` when there are none: a read error does not break the
    view."""
    say = job.journal.say
    if job.source is None or job.source.suffix.lower() != ".sldprt":
        return None
    try:
        from sldprt.dimxpert import lire_dimxpert, pmi_en_vue

        liste = pmi_en_vue(job.source)
        total = sum(len(d.annotations) for d in lire_dimxpert(job.source)[:1])
    except Exception as échec:
        say(f"PMI: not read — {type(échec).__name__}: {échec}", "note")
        return None
    if not liste:
        return None
    say(f"PMI: {len(liste)} DimXpert annotations in the view"
        + (f" ({total - len(liste)} with no point to place them)" if total > len(liste) else ""),
        "ok")
    return liste


def _pmi_dans_la_vue(job: Job, vue: dict) -> None:
    """PMI added to the view, anchors in the model frame: the page moves
    them relative to ``vue["centre"]``."""
    liste = lire_pmi(job)
    if liste:
        vue["pmi"] = liste


HTML = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>openSW: SLDPRT to Parasolid and STEP, SLDDRW on screen</title>
<style>
  /* The author's « VSS » v7 design system tokens, the ones openPDF3D uses:
     grey neutrals (openPDF3D's warm ones looked brown), 3 px corners, a single accent (red here, the logo's,
     where openPDF3D is blue). Light by default, dark when the system asks.
     The older names (--fond, --carte…) remain, set on the new ones. */
  :root {
    --bg: #f4f4f5; --surface: #ffffff; --raised: #ffffff; --soft: #eeeef0;
    --border: #e2e2e6; --text: #26272b; --muted: #6a6b72; --action-text: #ffffff;
    --control: #f0f0f2; --hover: #e8e8eb;
    --accent: #d2231a; --accent-soft: #fbe9e7; --focus-ring: rgba(210,35,26,.35);
    --green: #357f31; --green-soft: #e7f3e7;
    --red: #c53030; --red-soft: #fbe9e9;
    --amber: #a87908; --amber-soft: #faf3df;
    --radius: 3px; --radius-sm: 2px; --radius-full: 999px;
    --s1: 4px; --s2: 8px; --s3: 12px; --s4: 16px; --s5: 20px; --s6: 24px; --s8: 32px;
    --choix: #c96a0c;
    --fond: var(--bg); --carte: var(--surface); --trait: var(--border);
    --texte: var(--text); --doux: var(--muted);
    --ok: var(--green); --err: var(--red); --step: var(--text); --note: var(--amber);
  }
  @media (prefers-color-scheme: dark) { :root {
    --bg: #18191b; --surface: #202124; --raised: #27282b; --soft: #2e2f33;
    --border: #3a3b40; --text: #d9dade; --muted: #9a9ba1;
    --control: #2b2c30; --hover: rgba(255,255,255,.06);
    --accent: #f0645c; --accent-soft: rgba(240,100,92,.16); --focus-ring: rgba(240,100,92,.45);
    --green: #83cd7f; --green-soft: rgba(131,205,127,.14);
    --red: #ef8574; --red-soft: rgba(239,133,116,.14);
    --amber: #d3af2a; --amber-soft: rgba(211,175,42,.13);
    --choix: #f0a35e;
  } }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  body {
    margin: 0; padding: var(--s8) var(--s4) 64px; background: var(--bg); color: var(--text);
    font: 13px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  :focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
  main { max-width: 1040px; margin: 0 auto; }
  h1 { font-size: 18px; margin: 0; font-weight: 700; flex: 0 0 auto;
       display: flex; align-items: center; gap: var(--s3); }
  h1 > span:first-of-type { font-weight: 400; color: var(--muted); }
  h1 b { color: var(--text); font-weight: 700; }
  #logo { width: 34px; height: 34px; flex: 0 0 auto; }
  /* The six most spoken languages, two rows of three: the ribbon's height, not its width. */
  .langues { margin-inline-start: auto; align-self: center;
             display: grid; grid-template-columns: repeat(3, auto); gap: 4px; }
  .langues button { flex: 0 0 auto; height: 24px; min-width: 32px; padding: 0 6px; font-size: 11.5px; }
  .langues button.choisi, .onglets button.choisi {
    background: var(--accent-soft); color: var(--accent); box-shadow: none;
    border-color: color-mix(in srgb, var(--accent) 40%, transparent);
  }
  .carte {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: var(--s4); margin-bottom: var(--s4);
  }
  /* The drop zone: a click opens the picker, a file dropped on it goes as
     if it had been chosen. The input itself is hidden inside. */
  #depot {
    display: flex; flex-direction: column; align-items: center; gap: var(--s1);
    padding: var(--s5) var(--s6); text-align: center; cursor: pointer; color: var(--muted);
    border: 1.5px dashed color-mix(in srgb, var(--muted) 50%, transparent);
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--control) 60%, transparent);
    transition: border-color .12s, background .12s, color .12s;
  }
  #depot:hover, #depot.survol { border-color: var(--accent); color: var(--text);
                                background: var(--accent-soft); }
  #depot svg { width: 22px; height: 22px; fill: none; stroke: currentColor;
               stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
  #depot b { color: var(--accent); font-weight: 600; }
  #depot-nom { font-size: 11px; }
  #fichier { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
  /* Buttons: the VSS « secondary » recipe by default. */
  button {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    flex: 1 1 200px; height: 32px; padding: 0 14px; border-radius: var(--radius);
    font: inherit; font-weight: 500; color: var(--text); cursor: pointer; white-space: nowrap;
    background: linear-gradient(180deg, color-mix(in srgb, var(--raised) 92%, #fff),
                                        color-mix(in srgb, var(--raised) 96%, #000));
    border: 1px solid color-mix(in srgb, var(--muted) 45%, transparent);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.10), 0 1px 1px rgba(0,0,0,.12);
    transition: background .12s, border-color .12s, color .12s, filter .12s;
  }
  button:hover:not(:disabled) { filter: brightness(1.04); }
  button:active:not(:disabled) { filter: none; box-shadow: inset 0 1px 3px rgba(0,0,0,.22); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  /* The drop zone does not need the full width: the ribbon sits to its
     right, below it when the window is narrow. */
  #depot-ligne { display: flex; gap: var(--s3); align-items: center; flex-wrap: wrap; }
  .carte:has(> #depot-ligne) { padding: var(--s3); }
  #depot-ligne { align-items: stretch; }
  #depot-ligne #depot {
    flex: 0 0 auto; max-width: 250px; display: grid; grid-template-columns: auto minmax(0, 1fr);
    column-gap: var(--s3); row-gap: 2px; align-content: center; text-align: left;
    padding: var(--s3) var(--s4);
  }
  #depot-ligne #depot svg { grid-row: span 2; }
  #depot-ligne #depot > span:not(#depot-nom) { white-space: nowrap; }
  #depot-ligne #depot-nom { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #actions { display: flex; gap: var(--s2); flex-wrap: wrap; align-items: center; }
  #actions button { flex: 0 0 auto; }
  /* The ribbon: groups side by side, a quiet title above each, a rule
     between them. */
  #ruban { flex: 0 1 auto; display: flex; flex-wrap: wrap; align-items: stretch;
           gap: var(--s2) 0; min-width: 0; }
  .ruban-groupe { display: flex; flex-direction: column; justify-content: space-between;
                  gap: 5px; padding-inline-end: var(--s3); }
  .ruban-groupe:not([hidden]) ~ .ruban-groupe { border-inline-start: 1px solid var(--border);
                                               padding-inline-start: var(--s3); }
  .ruban-groupe:last-child { padding-inline-end: 0; }
  .ruban-titre { color: var(--muted); font-size: 10.5px; font-weight: 600;
                 text-transform: uppercase; letter-spacing: .05em; line-height: 1; }
  #ruban .action { height: 30px; padding: 0 10px; font-size: 12px; }
  #ruban .groupe { gap: 6px; }
  /* The group title already says « File »; the sketches link says
     « Sketches ». */
  #ruban .groupe-titre { display: none !important; }
  #ruban #groupe-esq { margin-left: 0; padding-left: 0; border-left: 0; }
  #ruban #actions { gap: 6px; }
  .ruban-ligne { display: flex; gap: var(--s3); align-items: center; flex-wrap: wrap;
                 min-height: 32px; }
  .ruban-ligne { gap: 6px; }
  .ruban-ligne button { flex: 0 0 auto; height: 30px; padding: 0 10px; font-size: 12px; }
  .ruban-ligne .icone-r { width: 30px; padding: 0; }
  .icone-r svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.7;
                 stroke-linecap: round; stroke-linejoin: round; }
  /* In the ribbon, a switch is a button that stays pressed. */
  .ruban-ligne .interrupteur {
    height: 30px; padding: 0 10px; font-size: 12px; font-weight: 500;
    border: 1px solid color-mix(in srgb, var(--muted) 45%, transparent);
    border-radius: var(--radius); background: var(--control);
  }
  .ruban-ligne .interrupteur .piste { display: none; }
  .ruban-ligne .interrupteur.icone-r { width: 30px; padding: 0; justify-content: center; }
  .ruban-ligne .interrupteur:hover { border-color: var(--muted); }
  .ruban-ligne .interrupteur:has(input:checked) {
    background: var(--accent-soft); color: var(--accent);
    border-color: color-mix(in srgb, var(--accent) 45%, transparent);
  }
  .ruban-ligne .interrupteur:has(input:focus-visible) { box-shadow: 0 0 0 2px var(--focus-ring); }
  /* Full page: the ribbon floats at the top right of the view. */
  body.pleine-page #ruban {
    position: fixed; top: var(--s2); right: var(--s2); z-index: 60; flex: none;
    padding: var(--s2) var(--s3); background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); box-shadow: 0 4px 16px rgba(0,0,0,.18);
  }
  #lien, #lien-esq { display: contents; }
  /* A group of outputs, its title first: "3D", then "Sketch" after a rule. */
  .groupe { display: inline-flex; align-items: center; gap: var(--s2); flex-wrap: wrap; }
  .groupe-titre { color: var(--muted); font-size: 12px; font-weight: 600; }
  .groupe-titre::after { content: " :"; }
  #groupe-esq { margin-left: var(--s3); padding-left: var(--s4);
                border-left: 1px solid var(--border); }
  .action {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    flex: 0 0 auto; height: 32px; padding: 0 14px; border-radius: var(--radius);
    font: inherit; font-weight: 600; text-decoration: none; cursor: pointer;
  }
  .action svg { width: 14px; height: 14px; fill: none; stroke: currentColor;
                stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  /* Download: the « primary » recipe. */
  a.telecharger {
    color: var(--action-text); border: 1px solid color-mix(in srgb, var(--accent) 70%, #000);
    background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 90%, #fff) 0%,
                var(--accent) 55%, color-mix(in srgb, var(--accent) 92%, #000) 100%);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.28); text-shadow: 0 -1px 0 rgba(0,0,0,.22);
  }
  a.telecharger:hover { filter: brightness(1.06); }
  /* The banner, edge to edge, at the top: SolidWorks red, white text, the
     promise first, in bold, then the examples. */
  #bandeau { margin: calc(-1 * var(--s8)) calc(-1 * var(--s4)) var(--s5);
             background: #c8201a; color: #fff; }
  #bandeau:not(:has(p:not([hidden]))) { display: none; }
  .bandeau-ligne { padding: 7px var(--s4); display: flex; flex-wrap: wrap;
                   align-items: center; justify-content: center; gap: var(--s1) var(--s6); }
  #local { margin: 0; font-size: 12px; font-weight: 700; }
  #exemples button { background: transparent; color: #fff; box-shadow: none;
                     border: 1px solid rgba(255,255,255,.55); }
  #exemples button:hover { background: rgba(255,255,255,.14); border-color: #fff; filter: none; }
  @media (max-width: 560px) { #bandeau { margin-top: calc(-1 * var(--s5)); } }
  #exemples { margin: 0; font-size: 12px;
              display: flex; flex-wrap: wrap; align-items: center; gap: 6px var(--s2); }
  #exemples button { flex: 0 0 auto; height: 26px; padding: 0 10px; font-size: 12px; }
  /* The progress bar: measured during upload, back and forth while the
     server works, since it cannot tell how much is left. */
  #charge { margin-top: var(--s3); }
  .charge-piste { height: 5px; border-radius: var(--radius-full); background: var(--soft);
                  overflow: hidden; box-shadow: inset 0 1px 2px rgba(0,0,0,.16); }
  #charge-barre { height: 100%; width: 0; border-radius: inherit; transition: width .15s;
    background: linear-gradient(90deg, color-mix(in srgb, var(--accent) 88%, #fff), var(--accent)); }
  #charge.attente #charge-barre { width: 40%; transition: none;
                                  animation: va-et-vient 1.2s ease-in-out infinite; }
  @keyframes va-et-vient { from { margin-left: -42%; } to { margin-left: 102%; } }
  #charge-texte { margin-top: 6px; color: var(--muted); font-size: 11px;
                  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  /* The file properties. */
  #fiche-carte { display: none; }
  /* On a wide screen the properties sit right of the view instead of
     below it; the whole page widens, every card keeps the same edges. */
  @media (min-width: 1200px) {
    main.avec-fiche { max-width: 1480px; }
    main.avec-fiche #espace { display: grid; grid-template-columns: minmax(0, 1fr) 400px;
                              column-gap: var(--s4); align-items: start; }
    main.avec-fiche #vue-carte, main.avec-fiche #plan-carte,
    main.avec-fiche #journal { grid-column: 1; }
    main.avec-fiche #fiche-carte { grid-column: 2; grid-row: 1 / span 3;
      position: sticky; top: var(--s4); max-height: calc(100vh - 2 * var(--s4)); overflow: auto; }
  }
  .fiche-titre { display: flex; flex-wrap: wrap; gap: var(--s1) 10px; align-items: baseline;
                 margin-bottom: var(--s3); font-size: 12px; }
  #fiche-export { margin-left: auto; display: flex; gap: 6px; }
  #fiche-export a { font-size: 11px; color: var(--text); text-decoration: none;
                    border: 1px solid var(--border); border-radius: var(--radius);
                    padding: 1px 8px; }
  #fiche-export a:hover { border-color: var(--muted); }
  .fiche-titre small, #fiche-table small { color: var(--muted); font-size: 11px; }
  .fiche { display: flex; gap: var(--s4); flex-wrap: wrap; align-items: flex-start; }
  #fiche-apercu { max-width: 160px; width: 100%; border-radius: var(--radius);
                  border: 1px solid var(--border); background: #fff; cursor: zoom-in; }
  /* The previews at their stored size, pixel for pixel. */
  #apercus { max-width: calc(100vw - 32px); max-height: calc(100vh - 32px);
             padding: var(--s4); border: 1px solid var(--border);
             border-radius: var(--radius); background: var(--surface); color: var(--text); }
  #apercus::backdrop { background: rgba(0, 0, 0, .5); }
  #apercus figure { margin: 0 0 var(--s4); }
  #apercus figcaption { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
  #apercus img { display: block; max-width: none; background: #fff;
                 border: 1px solid var(--border); }
  #fiche-table { flex: 1 1 360px; min-width: 0; font-size: 12px; }
  /* A section: collapsible, its lines as a table. */
  .fiche-kv { border-collapse: collapse; }
  .fiche-kv th { text-align: left; color: var(--muted); font-weight: 400; max-width: 16em;
                 padding: 3px var(--s3) 3px 0; vertical-align: top; overflow-wrap: anywhere; }
  .fiche-kv td { overflow-wrap: anywhere; padding: 3px 0; }
  .fiche-bloc { margin-top: var(--s3); }
  .fiche-bloc > summary { cursor: pointer; font-weight: 600; padding: 4px 0;
                          border-bottom: 1px solid var(--border); }
  .fiche-bloc > summary small { font-weight: 400; margin-left: 6px; }
  .fiche-defile { max-height: 340px; overflow: auto; }
  .fiche-tab { border-collapse: collapse; }
  .fiche-tab th { position: sticky; top: 0; background: var(--surface); text-align: left;
                  color: var(--muted); font-weight: 400; white-space: nowrap;
                  padding: 4px var(--s3) 4px 0; border-bottom: 1px solid var(--border); }
  .fiche-tab td { padding: 3px var(--s3) 3px 0; vertical-align: top; white-space: nowrap; }
  .fiche-tab tbody tr:nth-child(even) td { background: var(--soft); }
  .fiche-tab .num { text-align: right; font-variant-numeric: tabular-nums; }
  .fiche-tab .doux { color: var(--muted); }
  /* The log, folded: it opens on a click, or by itself on an error. */
  #journal { padding: 0; overflow: hidden; }
  #journal summary {
    display: flex; align-items: center; gap: var(--s2); cursor: pointer; user-select: none;
    list-style: none; color: var(--muted); font-size: 12px; font-weight: 600;
    padding: 9px var(--s3);
  }
  #journal summary:hover { background: var(--hover); }
  #journal summary::-webkit-details-marker { display: none; }
  #journal summary::before { content: ""; flex: none; width: 7px; height: 7px; margin: 0 2px;
    border-right: 1.5px solid var(--muted); border-bottom: 1.5px solid var(--muted);
    transform: rotate(-45deg); transition: transform .12s; }
  #journal[open] summary::before { transform: rotate(45deg); }
  #journal-dernier { flex: 1; min-width: 0; font-weight: 400; font-size: 11px;
                     white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #log {
    font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    height: 340px; overflow-y: auto; white-space: pre-wrap; overflow-wrap: anywhere;
    padding: var(--s3) var(--s4); border-top: 1px solid var(--border); color: var(--muted);
    background: color-mix(in srgb, var(--control) 70%, transparent);
    box-shadow: inset 0 1px 2px rgba(0,0,0,.10);
  }
  .icone { flex: 0 0 auto; width: 26px; height: 26px; padding: 0; }
  .icone svg { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 2;
               stroke-linecap: round; stroke-linejoin: round; }
  /* Full page: the card covers the browser window (not the screen), and
     the view takes whatever the bars leave. */
  .carte.pleine-page { position: fixed; inset: 0; z-index: 50; margin: 0;
                       display: flex !important; flex-direction: column;
                       border-radius: 0; padding: var(--s3); }
  .carte.pleine-page .vue-cadre { flex: 1 1 auto; min-height: 0; }
  .carte.pleine-page #vue, .carte.pleine-page #plan { height: 100%; }
  body.pleine-page { overflow: hidden; }
  .plein { position: absolute; top: 8px; right: 8px; z-index: 2; }
  #log div { display: flex; gap: 10px; }
  #log .ms { color: color-mix(in srgb, var(--muted) 70%, transparent); flex: 0 0 62px;
             text-align: right; font-variant-numeric: tabular-nums; }
  .ok   { color: var(--green); }
  .err  { color: var(--red); }
  .step { color: var(--text); font-weight: 600; }
  .note { color: var(--amber); }
  .vide { color: var(--muted); }
  .pied { margin: var(--s8) 0 var(--s2); color: var(--muted); font-size: 11px; line-height: 1.6;
          text-align: center; }
  .pied a { color: inherit; }
  #vue-carte { display: none; }
  /* SolidWorks' gradient background: Top/Bottom Gradient Color. */
  .vue-cadre { position: relative; }
  #vue {
    display: block; width: 100%; height: 560px; border-radius: var(--radius);
    background: linear-gradient(#8795aa, #c3c8d2); cursor: default;
    touch-action: none; outline: none;
  }
  #vue-triedre {
    position: absolute; left: 8px; bottom: 8px; width: 76px; height: 76px;
    pointer-events: none;
  }
  /* The view cube, top left, bare: six CSS 3D faces. */
  #vue-cube { position: absolute; left: 12px; top: 12px; z-index: 2; width: 92px;
              display: flex; flex-direction: column; align-items: center; gap: 4px; }
  .cube-scene { width: 92px; height: 92px; perspective: 520px;
                display: grid; place-items: center; }
  #vue-cube-corps { width: 56px; height: 56px; position: relative; transform-style: preserve-3d; }
  #vue-cube-corps > div {
    position: absolute; inset: 0; display: grid; place-items: center; cursor: pointer;
    font-size: 9px; font-weight: 700; letter-spacing: .01em; text-transform: uppercase;
    color: #4a5160; background: rgba(246,247,249,.94); border: 1px solid #8a93a3;
    user-select: none; transition: background .12s, color .12s;
  }
  #vue-cube-corps > div:hover { background: #fbe0dd; color: #a8160f; border-color: var(--accent); }
  .c-face { transform: translateZ(28px); }
  .c-arriere { transform: rotateY(180deg) translateZ(28px); }
  .c-droite { transform: rotateY(90deg) translateZ(28px); }
  .c-gauche { transform: rotateY(-90deg) translateZ(28px); }
  .c-dessus { transform: rotateX(90deg) translateZ(28px); }
  .c-dessous { transform: rotateX(-90deg) translateZ(28px); }
  .vue-vues { display: flex; gap: 6px; flex-wrap: wrap; margin-top: var(--s3); }
  .vue-vues button { flex: 0 0 auto; height: 26px; padding: 0 10px; font-size: 12px; }
  .vue-barre {
    display: flex; gap: var(--s2); align-items: center; margin-top: var(--s3);
    flex-wrap: wrap;
  }
  #vue-info, #plan-info { flex: 1 1 260px;
    font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  #vue-info b, #plan-info b { color: var(--choix); font-size: 13px; }
  .vue-barre button { flex: 0 0 auto; }
  .vue-barre input {
    width: 130px; height: 32px; padding: 0 10px; border-radius: var(--radius); font: inherit;
    background: var(--surface); color: var(--text); border: 1px solid var(--border);
    box-shadow: inset 0 1px 2px rgba(0,0,0,.08);
  }
  .vue-barre input:focus { border-color: var(--accent); outline: none;
                           box-shadow: 0 0 0 2px var(--focus-ring); }
  /* A switch: the checkbox stays for the keyboard and screen readers,
     invisible; the track and its knob show it. */
  .interrupteur { display: inline-flex; align-items: center; gap: var(--s2);
                  cursor: pointer; user-select: none; color: var(--text); }
  .interrupteur input { position: absolute; opacity: 0; width: 1px; height: 1px;
                        margin: 0; padding: 0; border: 0; box-shadow: none; }
  .interrupteur .piste {
    position: relative; flex: 0 0 auto; width: 30px; height: 18px;
    border-radius: var(--radius-full); background: var(--control);
    border: 1px solid var(--border); box-shadow: inset 0 1px 2px rgba(0,0,0,.08);
    transition: background .15s, border-color .15s;
  }
  .interrupteur .piste::after {
    content: ""; position: absolute; top: 1px; left: 1px; width: 14px; height: 14px;
    border-radius: 50%; background: var(--surface);
    box-shadow: 0 1px 2px rgba(0,0,0,.25); transition: transform .15s;
  }
  .interrupteur:hover .piste { border-color: var(--muted); }
  .interrupteur input:checked + .piste::after { transform: translateX(12px); background: #fff; }
  .interrupteur input:focus-visible + .piste { box-shadow: 0 0 0 2px var(--focus-ring); }
  .interrupteur input:disabled + .piste { opacity: .45; }
  /* PMI: a 2D layer over the view, which clicks go through. */
  #vue-pmi {
    position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none;
  }
  /* On: the « primary » recipe, the downloads'. */
  .interrupteur input:checked + .piste {
    background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 90%, #fff) 0%,
                var(--accent) 55%, color-mix(in srgb, var(--accent) 92%, #000) 100%);
    border-color: color-mix(in srgb, var(--accent) 70%, #000);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.28);
  }
  #plan-carte { display: none; }
  .onglets { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: var(--s3); }
  .onglets button { flex: 0 0 auto; height: 26px; padding: 0 10px; font-size: 12px; }
  #plan {
    /* Light, and distinct from the paper: what lies off the sheet stays
       readable, and the sheet's edge shows. */
    width: 100%; height: 560px; border-radius: var(--radius); background: #b9bcc2;
    overflow: hidden; cursor: grab; touch-action: none;
  }
  #plan:active { cursor: grabbing; }
  #plan svg { width: 100%; height: 100%; display: block; }
  #plan .papier { fill: #fbfbf8; }
  /* The colour and font read from the file are attributes: CSS only gives
     the default, without overriding them. */
  #plan .trait { stroke: #1b1e24; }
  #plan .trait * { stroke-width: 1.2px; vector-effect: non-scaling-stroke; }
  #plan .plein:not([fill]) { fill: #1b1e24; }
  #plan text:not([fill]) { fill: #1b1e24; }
  #plan text:not([font-family]) { font-family: "Century Gothic", ui-sans-serif, sans-serif; }
  #plan .trait .choisi { stroke: #e07b16; stroke-width: 3px; }
  #plan .plein.choisi { fill: #e07b16; }
  #plan text.choisi { fill: #e07b16; }
  @media (max-width: 560px) {
    body { padding: var(--s5) var(--s4) 48px; }
    #vue, #plan { height: 380px; }
  }
</style>
<!-- The banner, at the top on a red background: the promise that the file
     stays in the browser (published page only), then the examples. -->
<div id="bandeau"><div class="bandeau-ligne">
    <!-- Shown by the published page only (statique.js): the local server
         does receive the file. -->
    <p id="local" data-t="local" hidden>Your file is not uploaded to any server:
      it is read and converted in your browser.</p>
    <!-- Examples fetched from GitHub, which allows it: NIST models (public domain)
         and a drawing from andyeske/How-to-CAD-Almost-Anything (MIT). -->
    <p id="exemples"><span data-t="exemples">No file at hand? Open an example:</span>
      <button class="exemple" data-t="ex-piece" data-url="https://raw.githubusercontent.com/cadmpeg/cadmpeg/3dc401df603b99f61f7800b05f7027e54f111eea/corpus/nist-pmi/sldprt/nist_ftc_08_asme1_rc_sw1802.SLDPRT"
              data-nom="nist_ftc_08_asme1_rc_sw1802.SLDPRT">NIST part</button>
      <button class="exemple" data-t="ex-asm" data-url="https://raw.githubusercontent.com/cehaho/OpenSW/main/examples/nist_mtc_crada_assembly_rev-D.SLDASM"
              data-nom="nist_mtc_crada_assembly_rev-D.SLDASM">NIST assembly</button>
      <button class="exemple" data-t="ex-plan" data-url="https://raw.githubusercontent.com/andyeske/How-to-CAD-Almost-Anything/f2bd87ed5b267ba4f962919ba3ab73641bfca5ad/Session%20Files/Session%206/2x4%20LEGO%20brick.SLDDRW"
              data-nom="2x4 LEGO brick.SLDDRW">LEGO drawing</button>
    </p>
</div></div>
<main>
  <!-- Logo: the open box. Also used as the favicon (see below), so the page
       loads no file. -->

  <div class="carte">
    <!-- `convert.step` reads a Parasolid transmit file as well as a SolidWorks
         part, so .x_t is accepted too: comparing both outputs shows which
         one loses something. -->
    <div id="depot-ligne">
    <h1><svg id="logo" viewBox="0 0 100 100" role="img" aria-label="openSW">
      <defs>
        <linearGradient id="logo-lueur" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#ff6a5c"/><stop offset="1" stop-color="#e1251b"/>
        </linearGradient>
      </defs>
      <polygon points="19,44 50,62 50,94 19,76" fill="#6d6e71"/>
      <polygon points="50,62 81,44 81,76 50,94" fill="#4d4e51"/>
      <polygon points="19,44 50,26 81,44 50,62" fill="url(#logo-lueur)"
               stroke="#1d1d1f" stroke-width="2"/>
      <polygon points="19,24 50,6 81,24 50,42" fill="#e6e7e8" stroke="#1d1d1f"
               stroke-width="2" transform="rotate(-8 50 24)"/>
      <path d="M19,44 L50,62 L81,44 M50,62 L50,94" stroke="#1d1d1f" stroke-width="2"
            fill="none"/>
    </svg><span>open<b>SW</b></span>
</h1>
    <label id="depot">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4M7 9l5-5 5 5M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/></svg>
      <span><b data-t="depot">Drop a file</b> <span data-t="depot-ou">or click</span></span>
      <span id="depot-nom">.SLDPRT · .SLDASM · .SLDDRW · .x_t</span>
      <input type="file" id="fichier"
             multiple accept=".SLDPRT,.sldprt,.x_t,.X_T,.x_b,.X_B,.xmt_txt,.xmt_bin,.SLDDRW,.slddrw,.SLDASM,.sldasm,.zip,.ZIP">
    </label>
    <!-- A chosen file opens by itself (STEP, drawing or assembly); only the
         .x_t export, which has no view, keeps a button. The ribbon holds
         every button, in groups; the view's appear with it. -->
    <div id="ruban">
      <section class="ruban-groupe" id="ruban-fichier" hidden>
        <div class="ruban-titre" data-t="r-fichier">File</div>
        <div id="actions"><span class="groupe" id="groupe-3d"><span class="groupe-titre" hidden>3D</span><button id="btn-parasolid" class="action" hidden disabled
        data-t-title="exporter-xt" title="Also export to Parasolid"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3h7v7M21 3l-9 9M19 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h5"/></svg>.x_t</button><button id="btn-step-asm" class="action" hidden
        data-t-title="step-asm" title="Export to STEP: pick the assembly's parts"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3h7v7M21 3l-9 9M19 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h5"/></svg>.STEP</button><input id="pieces-asm" type="file" multiple accept=".SLDPRT,.sldprt" hidden><span id="lien"></span></span><span class="groupe" id="groupe-esq" hidden><span class="groupe-titre" data-t="groupe-esq">Sketch</span><span id="lien-esq"></span></span></div>
      </section>
      <section class="ruban-groupe" id="ruban-vue" hidden>
        <div class="ruban-titre" data-t="r-affichage">Display</div>
        <div class="ruban-ligne">
      <label class="interrupteur" id="vue-pmi-bascule" hidden><input type="checkbox" role="switch"
        id="vue-pmi-voir"><span class="piste" aria-hidden="true"></span><span data-t="pmi">PMI</span></label>
      <label class="interrupteur" id="vue-esq-bascule" hidden><input type="checkbox" role="switch"
        id="vue-esq"><span class="piste" aria-hidden="true"></span><span data-t="esquisses">Sketches</span></label>
        </div>
      </section>
      <section class="ruban-groupe" id="ruban-orienter" hidden>
        <div class="ruban-titre" data-t="r-vue">View</div>
        <div class="ruban-ligne">
          <button class="icone-r" data-cube="iso" data-t-title="vue-iso" title="Isometric"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 20 7.5v9L12 21l-8-4.5v-9ZM4 7.5l8 4.5 8-4.5M12 12v9"/></svg></button>
          <button class="icone-r" id="vue-recadrer" data-t-title="zoom-mieux" title="Zoom to fit"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/><rect x="9" y="9" width="6" height="6" rx="1"/></svg></button>
          <label class="interrupteur icone-r" data-t-title="plein-écran" title="Full page (Esc to go back)"><input
            type="checkbox" role="switch" id="vue-plein" data-t-aria="pleine-page" aria-label="Full page"><svg
            viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/></svg></label>
        </div>
      </section>
    </div>
    <span class="langues" role="group" aria-label="Langue · Language"><button data-langue="en" title="English">EN</button><button
      data-langue="zh" title="中文">中文</button><button data-langue="hi" title="हिन्दी">हिं</button><button
      data-langue="es" title="Español">ES</button><button data-langue="fr" title="Français">FR</button><button
      data-langue="ar" title="العربية">ع</button></span>
    </div>
    <div id="charge" hidden>
      <div class="charge-piste"><div id="charge-barre"></div></div>
      <div id="charge-texte"></div>
    </div>
  </div>

  <div id="espace">
  <div class="carte" id="vue-carte">
    <div class="vue-cadre">
      <canvas id="vue"></canvas>
      <canvas id="vue-triedre"></canvas>
      <div id="vue-cube"><div class="cube-scene"><div id="vue-cube-corps">
        <div data-cube="face" class="c-face" data-t="vue-face">Front</div>
        <div data-cube="arrière" class="c-arriere" data-t="vue-arrière">Back</div>
        <div data-cube="droite" class="c-droite" data-t="vue-droite">Right</div>
        <div data-cube="gauche" class="c-gauche" data-t="vue-gauche">Left</div>
        <div data-cube="dessus" class="c-dessus" data-t="vue-dessus">Top</div>
        <div data-cube="dessous" class="c-dessous" data-t="vue-dessous">Bottom</div>
      </div></div></div>
      <canvas id="vue-pmi"></canvas>
    </div>
    <div class="vue-barre">
      <span id="vue-info" class="vide" data-t="aide-vue">Click: select a face and read its name.
      Middle button: rotate · Ctrl+middle: pan · Shift+middle or
      wheel: zoom · Alt+middle: roll · middle double-click or F: zoom
      to fit · arrows: 15°, Shift: 90°.</span>
      <button id="vue-copier" data-t="copier" disabled>Copy name</button>
      <input id="vue-cherche" placeholder="F123…" spellcheck="false">
      <button id="vue-trouver" data-t="trouver">Find</button>
    </div>
  </div>

  <div class="carte" id="plan-carte">
    <div class="onglets" id="plan-onglets"></div>
    <div class="vue-cadre"><div id="plan"></div></div>
    <div class="vue-barre">
      <span id="plan-info" class="vide" data-t="aide-plan">Click a line to read its name.
      Drag: pan · wheel: zoom.</span>
      <button id="plan-copier" data-t="copier" disabled>Copy name</button>
      <input id="plan-cherche" placeholder="E123, A456…" spellcheck="false">
      <button id="plan-trouver" data-t="trouver">Find</button>
      <button id="plan-recadrer" data-t="recadrer">Fit</button>
    </div>
  </div>

  <div class="carte" id="fiche-carte">
    <div class="fiche-titre"><b data-t="fiche">File properties</b>
      <small data-t="fiche-note">What the file says about itself, read as is: nothing is computed.</small><span id="fiche-export"></span></div>
    <div class="fiche"><img id="fiche-apercu" alt="" hidden><div id="fiche-table"></div></div>
    <dialog id="apercus"><form method="dialog"><button data-t="fermer">Close</button></form>
      <figure><figcaption id="apercu-png-legende"></figcaption><img id="apercu-png" alt=""></figure>
      <figure id="apercu-dib-figure"><figcaption id="apercu-dib-legende"></figcaption><img id="apercu-dib" alt=""></figure>
    </dialog>
  </div>

  <details class="carte" id="journal">
    <summary><span data-t="journal">Log</span><span id="journal-dernier"></span>
      <button class="icone" id="journal-copier" data-t-title="copier-journal"
              title="Copy the log"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg></button></summary>
    <div id="log"><div class="vide" data-t="journal-vide">The log will appear here.</div></div>
  </details>
  </div>

  <!-- AGPL-3.0, section 13: users interacting with the page over a network
       must be able to get its source code. -->
  <footer class="pied">
    <a href="https://github.com/cehaho/OpenSW">github.com/cehaho/OpenSW</a>
    · <span data-t="libre">free software under the</span>
    <a href="https://github.com/cehaho/OpenSW/blob/main/LICENSE">GNU AGPL v3.0</a>
    · <span data-t="marques">not affiliated with Dassault Systèmes; SolidWorks and
    Parasolid are trademarks of their respective owners.</span>
  </footer>
</main>
<script>
const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------ languages
// English by default, or French if chosen (kept in this browser only).
// The log comes from the server, in English.
const TEXTES = {
  fr: {
    titre: "openSW — pièces, assemblages et mises en plan SolidWorks",
    depot: "Déposez un fichier", "depot-ou": "ou cliquez",
    exemples: "Pas de fichier sous la main ? Ouvrez un exemple :",
    "ex-piece": "Pièce NIST", "ex-asm": "Assemblage NIST", "ex-plan": "Mise en plan LEGO",
    "ex-charge": (n) => `Téléchargement de l'exemple ${n}`,
    local: "Votre fichier n'est envoyé sur aucun serveur : il est lu et converti " +
      "dans votre navigateur.",
    "exporter-xt": "Exporter aussi en Parasolid",
    télécharger: (n) => `Télécharger ${n}`,
    "télécharger-esq": (n) => `Télécharger ${n} — uniquement les esquisses, sans le solide`,
    "vue-face": "Face", "vue-arrière": "Arrière", "vue-gauche": "Gauche",
    "vue-droite": "Droite", "vue-dessus": "Dessus", "vue-dessous": "Dessous",
    "vue-iso": "Isométrique", "zoom-mieux": "Zoom au mieux",
    esquisses: "Esquisses", "groupe-esq": "Esquisse", pmi: "PMI",
    "r-fichier": "Fichier", "r-affichage": "Affichage", "r-vue": "Vue",
    "pmi-info": (n, f, c) => `${n} — ${f} : ${c}`,
    "esq-info": (n, f) => f ? `${n} — ${f}` : `${n} (esquisse libre)`,
    "aide-vue": "Clic : choisir une face et lire son nom. Bouton du milieu : " +
      "tourner · Ctrl+milieu : déplacer · Maj+milieu ou molette : zoomer · " +
      "Alt+milieu : pivoter · double clic milieu ou F : zoom au mieux · " +
      "flèches : 15°, Maj : 90°.",
    "aide-plan": "Cliquez sur un trait pour lire son nom. Glisser : déplacer · molette : zoomer.",
    copier: "Copier le nom", trouver: "Trouver", recadrer: "Recadrer",
    "svg-feuille": "Télécharger la feuille affichée en SVG",
    "journal-vide": "Le journal s'écrira ici.",
    journal: "Journal", "copier-journal": "Copier le journal", copié: "copié",
    "plein-écran": "Pleine page (Échap pour revenir)", "pleine-page": "Pleine page",
    libre: "logiciel libre sous licence",
    marques: "sans lien avec Dassault Systèmes ; SolidWorks et Parasolid sont " +
      "des marques de leurs propriétaires.",
    envoi: (p) => `Envoi du fichier — ${p} %`,
    lecture: "Lecture du fichier",
    "aff-vue": (s) => `Affichage de la vue 3D — ${s} s`,
    "aff-plan": (s) => `Affichage de la mise en plan — ${s} s`,
    "serveur-répond": (c) => `le serveur répond ${c}`,
    "envoi-coupé": "envoi interrompu",
    "pas-webgl": "Ce navigateur n'offre pas WebGL : pas de vue 3D.",
    "vue-prête": (f, c) => `${f} faces, ${c} corps. Cliquez sur une face pour lire son nom.`,
    "aucune-face": "Aucune face choisie.",
    "face-info": (c, t) => ` · corps ${c} · ${t} triangles`,
    "pas-pièce": (n) => `${n} : pas dans cette pièce.`,
    "feuille-info": (n, e, v) => `${n} : ${e} éléments, ${v} vue(s). ` +
      "Cliquez sur un trait pour lire son nom.",
    "aucun-trait": "Aucun trait sous la souris.",
    "pas-plan": (n) => `${n} : pas dans cette mise en plan.`,
    "step-asm": "Exporter en STEP : choisir les pièces de l'assemblage",
    fiche: "Propriétés du fichier",
    "fiche-note": "Ce que le fichier dit de lui-même, lu tel quel : rien n'est calculé.",
    "r-titre": "Titre", "r-sujet": "Sujet", "r-auteur": "Auteur",
    "r-mots_cles": "Mots clés", "r-commentaires": "Commentaires",
    "f-cree": "Créé", "f-enregistre": "Enregistré", "f-par": "Enregistré par",
    "f-perso": "Propriétés personnalisées", "f-config": (n) => `Configuration « ${n} »`,
    "f-geo": (oui) => oui ? "géométrie enregistrée dans le fichier"
                          : "géométrie non enregistrée dans le fichier",
    "f-materiau": "Matériau", "f-densite": "densité",
    "f-masse": "Masse", "f-volume": "Volume", "f-aire": "Aire",
    "f-centre": "Centre de gravité",
    "f-masse-note": "écrites par SolidWorks parce qu'une propriété y est liée",
    "h-qte": "Qté", "h-fichier": "Fichier", "h-config": "Configuration",
    "h-materiau": "Matériau", "h-composant": "Composant", "h-etat": "État",
    "h-nom": "Nom", "h-type": "Type", "h-valeur": "Valeur", "h-tolerance": "Tolérance",
    "h-composants": "Composants",
    "f-nomenclature": "Nomenclature", "f-composants": "Composants",
    "f-cache": "caché", "f-supprime": "supprimé, hors nomenclature",
    "f-hors": "exclu de la nomenclature",
    "f-inertie": "Inertie au centre de masse",
    "f-liens": "Textes liés",
    "f-general": "Général",
    "f-export-csv": "Exporter les propriétés en CSV, une ligne par valeur",
    "f-export-json": "Exporter tout ce qui est lu, en JSON",
    "f-cotes": "Cotes",
    "f-contraintes": "Contraintes", "m-0": "coïncidente", "m-5": "distance",
    "f-csv": "Télécharger la liste des composants en CSV",
    "apercu-agrandir": "Voir les aperçus à leur taille",
    "apercu-png": "PreviewPNG, l'aperçu PNG",
    "apercu-dib": "Preview, l'image Windows (256 couleurs)",
    "feuille-sans-image": "SolidWorks n'a pas gardé d'image de cette feuille : aperçu du fichier",
    fermer: "Fermer",
    plusieurs: "Plusieurs fichiers : déposez un assemblage (.SLDASM) avec ses pièces, ou une mise en plan (.SLDDRW) avec ses assemblages.",
    corps: "corps",
    genres: { line: "segment", circle: "cercle", path: "arc", polygon: "polygone",
              polyline: "arête", text: "texte" },
  },
  en: {
    titre: "openSW — SolidWorks parts, assemblies and drawings",
    depot: "Drop a file", "depot-ou": "or click",
    exemples: "No file at hand? Open an example:",
    "ex-piece": "NIST part", "ex-asm": "NIST assembly", "ex-plan": "LEGO drawing",
    "ex-charge": (n) => `Downloading the example ${n}`,
    local: "Your file is not uploaded to any server: it is read and converted " +
      "in your browser.",
    "exporter-xt": "Also export to Parasolid",
    télécharger: (n) => `Download ${n}`,
    "télécharger-esq": (n) => `Download ${n} — sketches only, no solid`,
    "vue-face": "Front", "vue-arrière": "Back", "vue-gauche": "Left",
    "vue-droite": "Right", "vue-dessus": "Top", "vue-dessous": "Bottom",
    "vue-iso": "Isometric", "zoom-mieux": "Zoom to fit",
    esquisses: "Sketches", "groupe-esq": "Sketch", pmi: "PMI",
    "r-fichier": "File", "r-affichage": "Display", "r-vue": "View",
    "pmi-info": (n, f, c) => `${n} — ${f}: ${c}`,
    "esq-info": (n, f) => f ? `${n} — ${f}` : `${n} (free sketch)`,
    "aide-vue": "Click: select a face and read its name. Middle button: rotate · " +
      "Ctrl+middle: pan · Shift+middle or wheel: zoom · Alt+middle: roll · " +
      "middle double-click or F: zoom to fit · arrows: 15°, Shift: 90°.",
    "aide-plan": "Click a line to read its name. Drag: pan · wheel: zoom.",
    copier: "Copy name", trouver: "Find", recadrer: "Fit",
    "svg-feuille": "Download the shown sheet as SVG",
    "journal-vide": "The log will appear here.",
    journal: "Log", "copier-journal": "Copy the log", copié: "copied",
    "plein-écran": "Full page (Esc to go back)", "pleine-page": "Full page",
    libre: "free software under the",
    marques: "not affiliated with Dassault Systèmes; SolidWorks and Parasolid " +
      "are trademarks of their respective owners.",
    envoi: (p) => `Uploading — ${p} %`,
    lecture: "Reading the file",
    "aff-vue": (s) => `Showing the 3D view — ${s} s`,
    "aff-plan": (s) => `Showing the drawing — ${s} s`,
    "serveur-répond": (c) => `the server answered ${c}`,
    "envoi-coupé": "upload interrupted",
    "pas-webgl": "This browser has no WebGL: no 3D view.",
    "vue-prête": (f, c) => `${f} faces, ${c} ${c > 1 ? "bodies" : "body"}. ` +
      "Click a face to read its name.",
    "aucune-face": "No face selected.",
    "face-info": (c, t) => ` · body ${c} · ${t} triangles`,
    "pas-pièce": (n) => `${n}: not in this part.`,
    "feuille-info": (n, e, v) => `${n}: ${e} elements, ${v} view(s). ` +
      "Click a line to read its name.",
    "aucun-trait": "No line under the mouse.",
    "pas-plan": (n) => `${n}: not in this drawing.`,
    "step-asm": "Export to STEP: pick the assembly's parts",
    fiche: "File properties",
    "fiche-note": "What the file says about itself, read as is: nothing is computed.",
    "r-titre": "Title", "r-sujet": "Subject", "r-auteur": "Author",
    "r-mots_cles": "Keywords", "r-commentaires": "Comments",
    "f-cree": "Created", "f-enregistre": "Saved", "f-par": "Saved by",
    "f-perso": "Custom properties", "f-config": (n) => `Configuration "${n}"`,
    "f-geo": (oui) => oui ? "geometry stored in the file"
                          : "geometry not stored in the file",
    "f-materiau": "Material", "f-densite": "density",
    "f-masse": "Mass", "f-volume": "Volume", "f-aire": "Area",
    "f-centre": "Centre of mass",
    "f-masse-note": "written by SolidWorks because a property links to them",
    "h-qte": "Qty", "h-fichier": "File", "h-config": "Configuration",
    "h-materiau": "Material", "h-composant": "Component", "h-etat": "State",
    "h-nom": "Name", "h-type": "Type", "h-valeur": "Value", "h-tolerance": "Tolerance",
    "h-composants": "Components",
    "f-nomenclature": "Bill of materials", "f-composants": "Components",
    "f-cache": "hidden", "f-supprime": "suppressed, left out of the bill",
    "f-hors": "excluded from the bill",
    "f-inertie": "Inertia at the centre of mass",
    "f-liens": "Linked texts",
    "f-general": "General",
    "f-export-csv": "Export the properties as CSV, one line per value",
    "f-export-json": "Export everything read, as JSON",
    "f-cotes": "Dimensions",
    "f-contraintes": "Mates", "m-0": "coincident", "m-5": "distance",
    "f-csv": "Download the component list as CSV",
    "apercu-agrandir": "Show the previews at their stored size",
    "apercu-png": "PreviewPNG, the PNG preview",
    "apercu-dib": "Preview, the Windows bitmap (256 colours)",
    "feuille-sans-image": "SolidWorks kept no image of this sheet: file preview",
    fermer: "Close",
    plusieurs: "Several files: drop an assembly (.SLDASM) together with its parts, or a drawing (.SLDDRW) with its assemblies.",
    corps: "body",
    genres: { line: "segment", circle: "circle", path: "arc", polygon: "polygon",
              polyline: "edge", text: "text" },
  },
  es: {
    titre: "openSW — piezas, ensamblajes y dibujos de SolidWorks",
    local: "Su archivo no se envía a ningún servidor: se lee y se convierte " +
      "en su navegador.",
    exemples: "¿No tiene un archivo a mano? Abra un ejemplo:",
    "ex-piece": "Pieza NIST", "ex-asm": "Ensamblaje NIST", "ex-plan": "Dibujo LEGO",
    "ex-charge": (n) => `Descargando el ejemplo ${n}`,
    "svg-feuille": "Descargar la hoja mostrada en SVG",
    depot: "Suelte un archivo", "depot-ou": "o haga clic",
    "exporter-xt": "Exportar también a Parasolid",
    télécharger: (n) => `Descargar ${n}`,
    "télécharger-esq": (n) => `Descargar ${n} — solo los croquis, sin el sólido`,
    "vue-face": "Frontal", "vue-arrière": "Posterior", "vue-gauche": "Izquierda",
    "vue-droite": "Derecha", "vue-dessus": "Superior", "vue-dessous": "Inferior",
    "vue-iso": "Isométrica", "zoom-mieux": "Ajustar a la vista",
    esquisses: "Croquis", "groupe-esq": "Croquis", pmi: "PMI",
    "r-fichier": "Archivo", "r-affichage": "Mostrar", "r-vue": "Vista",
    "pmi-info": (n, f, c) => `${n} — ${f}: ${c}`,
    "esq-info": (n, f) => f ? `${n} — ${f}` : `${n} (croquis libre)`,
    "aide-vue": "Clic: seleccionar una cara y leer su nombre. Botón central: girar · " +
      "Ctrl+central: desplazar · Mayús+central o rueda: zoom · Alt+central: rodar · " +
      "doble clic central o F: ajustar · flechas: 15°, Mayús: 90°.",
    "aide-plan": "Haga clic en una línea para leer su nombre. Arrastrar: desplazar · rueda: zoom.",
    copier: "Copiar nombre", trouver: "Buscar", recadrer: "Ajustar",
    "journal-vide": "El registro aparecerá aquí.",
    journal: "Registro", "copier-journal": "Copiar el registro", copié: "copiado",
    "plein-écran": "Página completa (Esc para volver)", "pleine-page": "Página completa",
    libre: "software libre bajo la licencia",
    marques: "sin relación con Dassault Systèmes; SolidWorks y Parasolid " +
      "son marcas de sus respectivos propietarios.",
    envoi: (p) => `Enviando — ${p} %`,
    lecture: "Leyendo el archivo",
    "aff-vue": (s) => `Mostrando la vista 3D — ${s} s`,
    "aff-plan": (s) => `Mostrando el dibujo — ${s} s`,
    "serveur-répond": (c) => `el servidor respondió ${c}`,
    "envoi-coupé": "envío interrumpido",
    "pas-webgl": "Este navegador no tiene WebGL: sin vista 3D.",
    "vue-prête": (f, c) => `${f} caras, ${c} ${c > 1 ? "cuerpos" : "cuerpo"}. ` +
      "Haga clic en una cara para leer su nombre.",
    "aucune-face": "Ninguna cara seleccionada.",
    "face-info": (c, t) => ` · cuerpo ${c} · ${t} triángulos`,
    "pas-pièce": (n) => `${n}: no está en esta pieza.`,
    "feuille-info": (n, e, v) => `${n}: ${e} elementos, ${v} vista(s). ` +
      "Haga clic en una línea para leer su nombre.",
    "aucun-trait": "Ninguna línea bajo el ratón.",
    "pas-plan": (n) => `${n}: no está en este dibujo.`,
    "step-asm": "Exportar a STEP: elegir las piezas del ensamblaje",
    fiche: "Propiedades del archivo",
    "fiche-note": "Lo que el archivo dice de sí mismo, leído tal cual: nada se calcula.",
    "r-titre": "Título", "r-sujet": "Asunto", "r-auteur": "Autor",
    "r-mots_cles": "Palabras clave", "r-commentaires": "Comentarios",
    "f-cree": "Creado", "f-enregistre": "Guardado", "f-par": "Guardado por",
    "f-perso": "Propiedades personalizadas", "f-config": (n) => `Configuración «${n}»`,
    "f-geo": (oui) => oui ? "geometría guardada en el archivo"
                          : "geometría no guardada en el archivo",
    "f-materiau": "Material", "f-densite": "densidad",
    "f-masse": "Masa", "f-volume": "Volumen", "f-aire": "Área",
    "f-centre": "Centro de masa",
    "f-masse-note": "escritas por SolidWorks porque una propiedad está vinculada a ellas",
    "h-qte": "Cant.", "h-fichier": "Archivo", "h-config": "Configuración",
    "h-materiau": "Material", "h-composant": "Componente", "h-etat": "Estado",
    "h-nom": "Nombre", "h-type": "Tipo", "h-valeur": "Valor", "h-tolerance": "Tolerancia",
    "h-composants": "Componentes",
    "f-nomenclature": "Lista de materiales", "f-composants": "Componentes",
    "f-cache": "oculto", "f-supprime": "suprimido, fuera de la lista",
    "f-hors": "excluido de la lista",
    "f-inertie": "Inercia en el centro de masa",
    "f-liens": "Textos vinculados",
    "f-general": "General",
    "f-export-csv": "Exportar las propiedades en CSV, una línea por valor",
    "f-export-json": "Exportar todo lo leído, en JSON",
    "f-cotes": "Cotas",
    "f-contraintes": "Relaciones de posición", "m-0": "coincidente", "m-5": "distancia",
    "f-csv": "Descargar la lista de componentes en CSV",
    "apercu-agrandir": "Mostrar las vistas previas a su tamaño guardado",
    "apercu-png": "PreviewPNG, la vista previa PNG",
    "apercu-dib": "Preview, el mapa de bits de Windows (256 colores)",
    "feuille-sans-image": "SolidWorks no guardó imagen de esta hoja: vista previa del archivo",
    fermer: "Cerrar",
    plusieurs: "Varios archivos: suelte un ensamblaje (.SLDASM) con sus piezas, o un dibujo (.SLDDRW) con sus ensamblajes.",
    corps: "cuerpo",
    genres: { line: "segmento", circle: "círculo", path: "arco", polygon: "polígono",
              polyline: "arista", text: "texto" },
  },
  zh: {
    titre: "openSW — SolidWorks 零件、装配体和工程图",
    local: "您的文件不会上传到任何服务器：它在您的浏览器中读取和转换。",
    exemples: "手头没有文件？打开一个示例：",
    "ex-piece": "NIST 零件", "ex-asm": "NIST 装配体", "ex-plan": "LEGO 工程图",
    "ex-charge": (n) => `正在下载示例 ${n}`,
    "svg-feuille": "将当前图纸下载为 SVG",
    depot: "拖放文件", "depot-ou": "或点击",
    "exporter-xt": "同时导出为 Parasolid",
    télécharger: (n) => `下载 ${n}`,
    "télécharger-esq": (n) => `下载 ${n} — 仅草图，不含实体`,
    "vue-face": "前视", "vue-arrière": "后视", "vue-gauche": "左视",
    "vue-droite": "右视", "vue-dessus": "上视", "vue-dessous": "下视",
    "vue-iso": "等轴测", "zoom-mieux": "整屏显示",
    esquisses: "草图", "groupe-esq": "草图", pmi: "PMI",
    "r-fichier": "文件", "r-affichage": "显示", "r-vue": "视图",
    "pmi-info": (n, f, c) => `${n} — ${f}：${c}`,
    "esq-info": (n, f) => f ? `${n} — ${f}` : `${n}（独立草图）`,
    "aide-vue": "单击：选择一个面并读取其名称。中键：旋转 · Ctrl+中键：平移 · " +
      "Shift+中键或滚轮：缩放 · Alt+中键：滚转 · 双击中键或 F：整屏显示 · " +
      "方向键：15°，Shift：90°。",
    "aide-plan": "单击线条以读取其名称。拖动：平移 · 滚轮：缩放。",
    copier: "复制名称", trouver: "查找", recadrer: "适应",
    "journal-vide": "日志将显示在这里。",
    journal: "日志", "copier-journal": "复制日志", copié: "已复制",
    "plein-écran": "全页（按 Esc 返回）", "pleine-page": "全页",
    libre: "自由软件，许可证为",
    marques: "与 Dassault Systèmes 无关；SolidWorks 和 Parasolid 是其各自所有者的商标。",
    envoi: (p) => `正在上传 — ${p} %`,
    lecture: "正在读取文件",
    "aff-vue": (s) => `正在显示 3D 视图 — ${s} 秒`,
    "aff-plan": (s) => `正在显示工程图 — ${s} 秒`,
    "serveur-répond": (c) => `服务器返回 ${c}`,
    "envoi-coupé": "上传中断",
    "pas-webgl": "此浏览器不支持 WebGL：无法显示 3D 视图。",
    "vue-prête": (f, c) => `${f} 个面，${c} 个实体。单击一个面以读取其名称。`,
    "aucune-face": "未选择面。",
    "face-info": (c, t) => ` · 实体 ${c} · ${t} 个三角形`,
    "pas-pièce": (n) => `${n}：不在此零件中。`,
    "feuille-info": (n, e, v) => `${n}：${e} 个元素，${v} 个视图。单击线条以读取其名称。`,
    "aucun-trait": "鼠标下没有线条。",
    "pas-plan": (n) => `${n}：不在此工程图中。`,
    "step-asm": "导出为 STEP：选择装配体的零件",
    fiche: "文件属性",
    "fiche-note": "文件对自身的描述，按原样读取：不做任何计算。",
    "r-titre": "标题", "r-sujet": "主题", "r-auteur": "作者",
    "r-mots_cles": "关键词", "r-commentaires": "备注",
    "f-cree": "创建时间", "f-enregistre": "保存时间", "f-par": "保存者",
    "f-perso": "自定义属性", "f-config": (n) => `配置“${n}”`,
    "f-geo": (oui) => oui ? "几何体已保存在文件中" : "几何体未保存在文件中",
    "f-materiau": "材料", "f-densite": "密度",
    "f-masse": "质量", "f-volume": "体积", "f-aire": "面积",
    "f-centre": "质心",
    "f-masse-note": "由 SolidWorks 写入，因为有属性引用了它们",
    "h-qte": "数量", "h-fichier": "文件", "h-config": "配置",
    "h-materiau": "材料", "h-composant": "零部件", "h-etat": "状态",
    "h-nom": "名称", "h-type": "类型", "h-valeur": "值", "h-tolerance": "公差",
    "h-composants": "零部件",
    "f-nomenclature": "材料明细表", "f-composants": "零部件",
    "f-cache": "隐藏", "f-supprime": "已压缩，不计入明细表",
    "f-hors": "不计入明细表",
    "f-inertie": "质心处的惯性",
    "f-liens": "链接文本",
    "f-general": "常规",
    "f-export-csv": "将属性导出为 CSV，每个值一行",
    "f-export-json": "将读取的全部内容导出为 JSON",
    "f-cotes": "尺寸",
    "f-contraintes": "配合", "m-0": "重合", "m-5": "距离",
    "f-csv": "将零部件列表下载为 CSV",
    "apercu-agrandir": "按保存尺寸显示预览图",
    "apercu-png": "PreviewPNG，PNG 预览图",
    "apercu-dib": "Preview，Windows 位图（256 色）",
    "feuille-sans-image": "SolidWorks 未保存此图纸的图像：文件预览图",
    fermer: "关闭",
    plusieurs: "多个文件：拖放一个装配体（.SLDASM）及其零件，或一个工程图（.SLDDRW）及其装配体。",
    corps: "实体",
    genres: { line: "线段", circle: "圆", path: "圆弧", polygon: "多边形",
              polyline: "边线", text: "文字" },
  },
  hi: {
    titre: "openSW — SolidWorks पार्ट, असेंबली और ड्रॉइंग",
    local: "आपकी फ़ाइल किसी सर्वर पर नहीं भेजी जाती: वह आपके ब्राउज़र में ही पढ़ी और बदली जाती है।",
    exemples: "पास में कोई फ़ाइल नहीं? एक उदाहरण खोलें:",
    "ex-piece": "NIST पार्ट", "ex-asm": "NIST असेंबली", "ex-plan": "LEGO ड्रॉइंग",
    "ex-charge": (n) => `उदाहरण ${n} डाउनलोड हो रहा है`,
    "svg-feuille": "दिखाई गई शीट SVG में डाउनलोड करें",
    depot: "फ़ाइल यहाँ छोड़ें", "depot-ou": "या क्लिक करें",
    "exporter-xt": "Parasolid में भी निर्यात करें",
    télécharger: (n) => `${n} डाउनलोड करें`,
    "télécharger-esq": (n) => `${n} डाउनलोड करें — केवल स्केच, ठोस के बिना`,
    "vue-face": "सामने", "vue-arrière": "पीछे", "vue-gauche": "बाएँ",
    "vue-droite": "दाएँ", "vue-dessus": "ऊपर", "vue-dessous": "नीचे",
    "vue-iso": "आइसोमेट्रिक", "zoom-mieux": "पूरा दिखाएँ",
    esquisses: "स्केच", "groupe-esq": "स्केच", pmi: "PMI",
    "r-fichier": "फ़ाइल", "r-affichage": "प्रदर्शन", "r-vue": "दृश्य",
    "pmi-info": (n, f, c) => `${n} — ${f}: ${c}`,
    "esq-info": (n, f) => f ? `${n} — ${f}` : `${n} (स्वतंत्र स्केच)`,
    "aide-vue": "क्लिक: एक सतह चुनें और उसका नाम पढ़ें। बीच का बटन: घुमाएँ · " +
      "Ctrl+बीच: खिसकाएँ · Shift+बीच या व्हील: ज़ूम · Alt+बीच: लुढ़काएँ · " +
      "बीच का डबल-क्लिक या F: पूरा दिखाएँ · तीर: 15°, Shift: 90°।",
    "aide-plan": "रेखा का नाम पढ़ने के लिए उस पर क्लिक करें। खींचें: खिसकाएँ · व्हील: ज़ूम।",
    copier: "नाम कॉपी करें", trouver: "खोजें", recadrer: "फ़िट करें",
    "journal-vide": "लॉग यहाँ दिखाई देगा।",
    journal: "लॉग", "copier-journal": "लॉग कॉपी करें", copié: "कॉपी हो गया",
    "plein-écran": "पूरा पेज (वापस जाने के लिए Esc)", "pleine-page": "पूरा पेज",
    libre: "मुक्त सॉफ़्टवेयर, लाइसेंस:",
    marques: "Dassault Systèmes से संबद्ध नहीं; SolidWorks और Parasolid अपने-अपने " +
      "स्वामियों के ट्रेडमार्क हैं।",
    envoi: (p) => `भेजा जा रहा है — ${p} %`,
    lecture: "फ़ाइल पढ़ी जा रही है",
    "aff-vue": (s) => `3D दृश्य दिखाया जा रहा है — ${s} से.`,
    "aff-plan": (s) => `ड्रॉइंग दिखाई जा रही है — ${s} से.`,
    "serveur-répond": (c) => `सर्वर ने ${c} लौटाया`,
    "envoi-coupé": "भेजना बीच में रुक गया",
    "pas-webgl": "इस ब्राउज़र में WebGL नहीं है: 3D दृश्य उपलब्ध नहीं।",
    "vue-prête": (f, c) => `${f} सतहें, ${c} ठोस। किसी सतह का नाम पढ़ने के लिए उस पर क्लिक करें।`,
    "aucune-face": "कोई सतह नहीं चुनी गई।",
    "face-info": (c, t) => ` · ठोस ${c} · ${t} त्रिभुज`,
    "pas-pièce": (n) => `${n}: इस पार्ट में नहीं है।`,
    "feuille-info": (n, e, v) => `${n}: ${e} तत्व, ${v} दृश्य। रेखा का नाम पढ़ने के लिए उस पर क्लिक करें।`,
    "aucun-trait": "माउस के नीचे कोई रेखा नहीं।",
    "pas-plan": (n) => `${n}: इस ड्रॉइंग में नहीं है।`,
    "step-asm": "STEP में निर्यात करें: असेंबली के पार्ट चुनें",
    fiche: "फ़ाइल गुण",
    "fiche-note": "फ़ाइल अपने बारे में जो कहती है, वैसा ही पढ़ा गया: कुछ भी गणना नहीं की गई।",
    "r-titre": "शीर्षक", "r-sujet": "विषय", "r-auteur": "लेखक",
    "r-mots_cles": "कीवर्ड", "r-commentaires": "टिप्पणियाँ",
    "f-cree": "बनाया गया", "f-enregistre": "सहेजा गया", "f-par": "सहेजने वाले",
    "f-perso": "कस्टम गुण", "f-config": (n) => `कॉन्फ़िगरेशन "${n}"`,
    "f-geo": (oui) => oui ? "ज्यामिति फ़ाइल में सहेजी गई है"
                          : "ज्यामिति फ़ाइल में सहेजी नहीं गई",
    "f-materiau": "सामग्री", "f-densite": "घनत्व",
    "f-masse": "द्रव्यमान", "f-volume": "आयतन", "f-aire": "क्षेत्रफल",
    "f-centre": "द्रव्यमान केंद्र",
    "f-masse-note": "SolidWorks ने लिखे क्योंकि कोई गुण इनसे जुड़ा है",
    "h-qte": "संख्या", "h-fichier": "फ़ाइल", "h-config": "कॉन्फ़िगरेशन",
    "h-materiau": "सामग्री", "h-composant": "घटक", "h-etat": "स्थिति",
    "h-nom": "नाम", "h-type": "प्रकार", "h-valeur": "मान", "h-tolerance": "टॉलरेंस",
    "h-composants": "घटक",
    "f-nomenclature": "सामग्री सूची", "f-composants": "घटक",
    "f-cache": "छिपा हुआ", "f-supprime": "दबाया गया, सूची से बाहर",
    "f-hors": "सूची से बाहर",
    "f-inertie": "द्रव्यमान केंद्र पर जड़त्व",
    "f-liens": "जुड़े हुए पाठ",
    "f-general": "सामान्य",
    "f-export-csv": "गुण CSV में निर्यात करें, हर मान एक पंक्ति",
    "f-export-json": "पढ़ा गया सब कुछ JSON में निर्यात करें",
    "f-cotes": "आयाम",
    "f-contraintes": "मेट", "m-0": "संपाती", "m-5": "दूरी",
    "f-csv": "घटकों की सूची CSV में डाउनलोड करें",
    "apercu-agrandir": "पूर्वावलोकन उनके सहेजे आकार में दिखाएँ",
    "apercu-png": "PreviewPNG, PNG पूर्वावलोकन",
    "apercu-dib": "Preview, Windows बिटमैप (256 रंग)",
    "feuille-sans-image": "SolidWorks ने इस शीट की कोई छवि नहीं रखी: फ़ाइल का पूर्वावलोकन",
    fermer: "बंद करें",
    plusieurs: "कई फ़ाइलें: एक असेंबली (.SLDASM) उसके पार्ट के साथ, या एक ड्रॉइंग (.SLDDRW) उसकी असेंबली के साथ छोड़ें।",
    corps: "ठोस",
    genres: { line: "रेखाखंड", circle: "वृत्त", path: "चाप", polygon: "बहुभुज",
              polyline: "किनारा", text: "पाठ" },
  },
  ar: {
    titre: "openSW — قطع وتجميعات ورسومات SolidWorks",
    local: "لا يُرسَل ملفك إلى أي خادم: يُقرأ ويُحوَّل داخل متصفحك.",
    exemples: "ليس لديك ملف؟ افتح مثالًا:",
    "ex-piece": "قطعة NIST", "ex-asm": "تجميعة NIST", "ex-plan": "رسم LEGO",
    "ex-charge": (n) => `جارٍ تنزيل المثال ${n}`,
    "svg-feuille": "تنزيل الورقة المعروضة بصيغة SVG",
    depot: "أفلت ملفًا هنا", "depot-ou": "أو انقر",
    "exporter-xt": "التصدير أيضًا إلى Parasolid",
    télécharger: (n) => `تنزيل ${n}`,
    "télécharger-esq": (n) => `تنزيل ${n} — الرسومات التخطيطية فقط، دون المجسم`,
    "vue-face": "أمامي", "vue-arrière": "خلفي", "vue-gauche": "يسار",
    "vue-droite": "يمين", "vue-dessus": "علوي", "vue-dessous": "سفلي",
    "vue-iso": "متساوي القياس", "zoom-mieux": "ملاءمة العرض",
    esquisses: "المخططات", "groupe-esq": "مخطط", pmi: "PMI",
    "r-fichier": "ملف", "r-affichage": "عرض", "r-vue": "منظور",
    "pmi-info": (n, f, c) => `${n} — ${f}: ${c}`,
    "esq-info": (n, f) => f ? `${n} — ${f}` : `${n} (رسم تخطيطي حر)`,
    "aide-vue": "نقرة: اختر وجهًا واقرأ اسمه. الزر الأوسط: تدوير · " +
      "Ctrl+الأوسط: تحريك · Shift+الأوسط أو العجلة: تكبير · Alt+الأوسط: لفّ · " +
      "نقرة مزدوجة بالأوسط أو F: ملاءمة · الأسهم: 15°، Shift: 90°.",
    "aide-plan": "انقر على خط لقراءة اسمه. السحب: تحريك · العجلة: تكبير.",
    copier: "نسخ الاسم", trouver: "بحث", recadrer: "ملاءمة",
    "journal-vide": "سيظهر السجل هنا.",
    journal: "السجل", "copier-journal": "نسخ السجل", copié: "تم النسخ",
    "plein-écran": "صفحة كاملة (Esc للعودة)", "pleine-page": "صفحة كاملة",
    libre: "برنامج حر بموجب رخصة",
    marques: "لا علاقة له بـ Dassault Systèmes؛ SolidWorks وParasolid " +
      "علامتان تجاريتان لمالكيهما.",
    envoi: (p) => `جارٍ الإرسال — ${p} %`,
    lecture: "جارٍ قراءة الملف",
    "aff-vue": (s) => `جارٍ عرض المنظور ثلاثي الأبعاد — ${s} ث`,
    "aff-plan": (s) => `جارٍ عرض الرسم — ${s} ث`,
    "serveur-répond": (c) => `ردّ الخادم بـ ${c}`,
    "envoi-coupé": "انقطع الإرسال",
    "pas-webgl": "هذا المتصفح لا يدعم WebGL: لا يوجد عرض ثلاثي الأبعاد.",
    "vue-prête": (f, c) => `${f} وجهًا، ${c} مجسم. انقر على وجه لقراءة اسمه.`,
    "aucune-face": "لم يُختر أي وجه.",
    "face-info": (c, t) => ` · المجسم ${c} · ${t} مثلثًا`,
    "pas-pièce": (n) => `${n}: غير موجود في هذه القطعة.`,
    "feuille-info": (n, e, v) => `${n}: ${e} عنصرًا، ${v} منظور. انقر على خط لقراءة اسمه.`,
    "aucun-trait": "لا يوجد خط تحت المؤشر.",
    "pas-plan": (n) => `${n}: غير موجود في هذا الرسم.`,
    "step-asm": "التصدير إلى STEP: اختر قطع التجميعة",
    fiche: "خصائص الملف",
    "fiche-note": "ما يقوله الملف عن نفسه، مقروءًا كما هو: لا يُحسب شيء.",
    "r-titre": "العنوان", "r-sujet": "الموضوع", "r-auteur": "المؤلف",
    "r-mots_cles": "الكلمات المفتاحية", "r-commentaires": "التعليقات",
    "f-cree": "أُنشئ", "f-enregistre": "حُفظ", "f-par": "حفظه",
    "f-perso": "خصائص مخصصة", "f-config": (n) => `التكوين «${n}»`,
    "f-geo": (oui) => oui ? "الهندسة محفوظة في الملف" : "الهندسة غير محفوظة في الملف",
    "f-materiau": "المادة", "f-densite": "الكثافة",
    "f-masse": "الكتلة", "f-volume": "الحجم", "f-aire": "المساحة",
    "f-centre": "مركز الكتلة",
    "f-masse-note": "كتبها SolidWorks لأن خاصيةً ترتبط بها",
    "h-qte": "الكمية", "h-fichier": "الملف", "h-config": "التكوين",
    "h-materiau": "المادة", "h-composant": "المكوّن", "h-etat": "الحالة",
    "h-nom": "الاسم", "h-type": "النوع", "h-valeur": "القيمة", "h-tolerance": "التفاوت",
    "h-composants": "المكوّنات",
    "f-nomenclature": "قائمة المواد", "f-composants": "المكوّنات",
    "f-cache": "مخفي", "f-supprime": "مُعطَّل، خارج القائمة",
    "f-hors": "مستبعد من القائمة",
    "f-inertie": "العطالة عند مركز الكتلة",
    "f-liens": "نصوص مرتبطة",
    "f-general": "عام",
    "f-export-csv": "تصدير الخصائص بصيغة CSV، سطر لكل قيمة",
    "f-export-json": "تصدير كل ما قُرئ بصيغة JSON",
    "f-cotes": "الأبعاد",
    "f-contraintes": "قيود التجميع", "m-0": "متطابق", "m-5": "مسافة",
    "f-csv": "تنزيل قائمة المكوّنات بصيغة CSV",
    "apercu-agrandir": "عرض المعاينات بحجمها المحفوظ",
    "apercu-png": "PreviewPNG، معاينة PNG",
    "apercu-dib": "Preview، صورة Windows النقطية (256 لونًا)",
    "feuille-sans-image": "لم يحفظ SolidWorks صورة لهذه الورقة: معاينة الملف",
    fermer: "إغلاق",
    plusieurs: "عدة ملفات: أفلت تجميعة (.SLDASM) مع قطعها، أو رسمًا (.SLDDRW) مع تجميعاته.",
    corps: "مجسم",
    genres: { line: "قطعة مستقيمة", circle: "دائرة", path: "قوس", polygon: "مضلع",
              polyline: "حافة", text: "نص" },
  },
};

let LANGUE = "en";
try { LANGUE = localStorage.getItem("opensw-langue") || ""; } catch (e) { LANGUE = ""; }
if (!TEXTES[LANGUE]) {
  const nav = (navigator.language || "").slice(0, 2).toLowerCase();
  LANGUE = TEXTES[nav] ? nav : "en";
}

function t(clé, ...args) {
  const v = TEXTES[LANGUE][clé] ?? TEXTES.en[clé] ?? TEXTES.fr[clé];
  return typeof v === "function" ? v(...args) : v;
}

// The properties shown, to rewrite them in a language chosen later.
let ficheMontrée = null;

function traduire() {
  document.documentElement.lang = LANGUE;
  // Arabic is written right to left; the 3D view does not flip.
  document.documentElement.dir = LANGUE === "ar" ? "rtl" : "ltr";
  document.title = t("titre");
  document.querySelectorAll("[data-t]").forEach((e) => { e.textContent = t(e.dataset.t); });
  document.querySelectorAll("[data-t-title]").forEach((e) => { e.title = t(e.dataset.tTitle); });
  document.querySelectorAll("[data-t-aria]").forEach((e) => {
    e.setAttribute("aria-label", t(e.dataset.tAria));
  });
  document.querySelectorAll("[data-langue]").forEach((b) => {
    b.classList.toggle("choisi", b.dataset.langue === LANGUE);
  });
  if (ficheMontrée) {
    // Sections keep their state, open or folded.
    const ouvertes = [...$("fiche-table").querySelectorAll("details")].map((d) => d.open);
    fiche(ficheMontrée);
    $("fiche-table").querySelectorAll("details").forEach((d, i) => {
      if (i < ouvertes.length) d.open = ouvertes[i];
    });
  }
}

document.querySelectorAll("[data-langue]").forEach((b) => {
  b.onclick = () => {
    LANGUE = b.dataset.langue;
    try { localStorage.setItem("opensw-langue", LANGUE); } catch (e) { /* ignore */ }
    traduire();
  };
});
traduire();

// The favicon is the header logo, serialized rather than duplicated.
(() => {
  const icône = document.createElement("link");
  icône.rel = "icon";
  icône.href = "data:image/svg+xml," +
    encodeURIComponent(new XMLSerializer().serializeToString($("logo")));
  document.head.append(icône);
})();
const log = $("log");
let vide = true;

function ligne(l) {
  if (vide) { log.innerHTML = ""; vide = false; }
  const d = document.createElement("div");
  const ms = document.createElement("span");
  ms.className = "ms";
  ms.textContent = (l.ms / 1000).toFixed(1) + "s";
  const t = document.createElement("span");
  t.className = l.kind;
  t.textContent = l.text;
  d.append(ms, t);
  log.append(d);
  $("journal-dernier").textContent = l.text;
  if (l.kind === "err") $("journal").open = true;
  log.scrollTop = log.scrollHeight;
}

function actifs(oui) {
  const f = $("fichier").files[0];
  const prêt = oui && !!f;
  // Drawings and assemblies are viewed, not converted.
  // Several files dropped: the browser does not guarantee their order, the
  // assembly or drawing among them decides. Dropped with its parts, an
  // assembly has become a zip that replaced the files.
  const noms = [...$("fichier").files].map((x) => x.name);
  const plan = prêt && noms.some((n) => /\.slddrw$/i.test(n));
  const asm = prêt && !plan && noms.some((n) => /\.(sldasm|zip)$/i.test(n));
  // Once the .x_t is written, its download link takes the button's place.
  const xtPrêt = !!$("lien").querySelector("a.sortie-xt");
  $("btn-parasolid").hidden = !f || plan || asm || xtPrêt;
  $("btn-parasolid").disabled = !prêt || plan || asm;
  // Dropped with its parts, the assembly already has its STEP.
  $("btn-step-asm").hidden = !asm || noms.some((n) => /\.zip$/i.test(n));
  $("btn-step-asm").disabled = !prêt;
  grouper();
}

// The "3D" title shows when the group holds a 3D output; the "Sketch" group
// when it holds a link.
function grouper() {
  const g = $("groupe-3d");
  g.querySelector(".groupe-titre").hidden =
    !g.querySelector("#btn-parasolid:not([hidden]), #btn-step-asm:not([hidden]), a.sortie-3d");
  $("groupe-esq").hidden = !$("lien-esq").children.length;
  // The ribbon's « File » group shows only with something in it.
  $("ruban-fichier").hidden = !$("actions").querySelector(
    "#btn-parasolid:not([hidden]), #btn-step-asm:not([hidden]), a");
}

// A chosen file opens at once: a part goes through STEP (the view reads the
// written file back); drawings and assemblies are shown as they are.
// Several files: an assembly and its parts, zipped together (stored, no
// compression) before upload; the server turns them into an exact STEP.
$("fichier").addEventListener("change", async () => {
  actifs(true);
  $("lien").innerHTML = "";
  $("lien-esq").innerHTML = "";
  grouper();
  let f = $("fichier").files[0];
  if (!f) return;
  if ($("fichier").files.length > 1) {
    const tous = [...$("fichier").files];
    // A drawing with its assemblies: they place the parts of its shaded views.
    const tete = tous.find((x) => /\.slddrw$/i.test(x.name))
              || tous.find((x) => /\.sldasm$/i.test(x.name));
    if (!tete) {
      ligne({ ms: 0, kind: "err", text: t("plusieurs") });
      return;
    }
    f = await zipper(tous, tete.name.replace(/\.[^.]*$/, "") + ".zip");
    const dt = new DataTransfer();
    dt.items.add(f);
    $("fichier").files = dt.files;
  }
  $("depot-nom").textContent = f.name;
  exporter(/\.slddrw$/i.test(f.name) ? "plan"
           : /\.(sldasm|zip)$/i.test(f.name) ? "assemblage" : "step");
});

// A "stored" zip (method 0): headers and CRC-32, nothing more.
const CRC = Array.from({ length: 256 }, (_, n) => {
  for (let k = 0; k < 8; k++) n = n & 1 ? 0xedb88320 ^ (n >>> 1) : n >>> 1;
  return n >>> 0;
});
async function zipper(fichiers, nom) {
  const morceaux = [], central = [];
  let decalage = 0;
  for (const f of fichiers) {
    const octets = new Uint8Array(await f.arrayBuffer());
    let crc = 0xffffffff;
    for (const b of octets) crc = CRC[(crc ^ b) & 0xff] ^ (crc >>> 8);
    crc = (crc ^ 0xffffffff) >>> 0;
    const n = new TextEncoder().encode(f.name);
    const tete = new DataView(new ArrayBuffer(30));
    [[0, 0x04034b50, 4], [4, 20, 2], [6, 0x0800, 2], [14, crc, 4],
     [18, octets.length, 4], [22, octets.length, 4], [26, n.length, 2]]
      .forEach(([a, v, l]) => (l === 4 ? tete.setUint32(a, v, true) : tete.setUint16(a, v, true)));
    const c = new DataView(new ArrayBuffer(46));
    [[0, 0x02014b50, 4], [4, 20, 2], [6, 20, 2], [8, 0x0800, 2], [16, crc, 4],
     [20, octets.length, 4], [24, octets.length, 4], [28, n.length, 2], [42, decalage, 4]]
      .forEach(([a, v, l]) => (l === 4 ? c.setUint32(a, v, true) : c.setUint16(a, v, true)));
    morceaux.push(tete, n, octets);
    central.push(c, n);
    decalage += 30 + n.length + octets.length;
  }
  const taille = central.reduce((s, x) => s + x.byteLength, 0);
  const fin = new DataView(new ArrayBuffer(22));
  [[0, 0x06054b50, 4], [8, fichiers.length, 2], [10, fichiers.length, 2],
   [12, taille, 4], [16, decalage, 4]]
    .forEach(([a, v, l]) => (l === 4 ? fin.setUint32(a, v, true) : fin.setUint16(a, v, true)));
  return new File([...morceaux, ...central, fin], nom, { type: "application/zip" });
}

// An example is downloaded, then opened as if it had been dropped.
document.querySelectorAll("button.exemple").forEach((b) => b.addEventListener("click", async () => {
  charge(t("ex-charge", b.dataset.nom));
  try {
    const r = await fetch(b.dataset.url);
    if (!r.ok) throw new Error(t("serveur-répond", r.status));
    const dt = new DataTransfer();
    dt.items.add(new File([await r.blob()], b.dataset.nom));
    $("fichier").files = dt.files;
    $("fichier").dispatchEvent(new Event("change"));
  } catch (e) {
    $("charge").hidden = true;
    ligne({ ms: 0, kind: "err", text: String(e.message || e) });
  }
}));

// `part` in [0, 1], or undefined when progress cannot be measured.
function charge(texte, part) {
  $("charge").hidden = false;
  $("charge").classList.toggle("attente", part === undefined);
  $("charge-barre").style.width = part === undefined ? "" : (100 * part).toFixed(1) + "%";
  $("charge-texte").textContent = texte;
}

// XMLHttpRequest, unlike fetch, reports upload progress.
function envoyer(url, corps) {
  return new Promise((ok, échec) => {
    const x = new XMLHttpRequest();
    x.open("POST", url);
    x.upload.onprogress = (e) => {
      if (e.lengthComputable)
        charge(t("envoi", Math.round(100 * e.loaded / e.total)), e.loaded / e.total);
    };
    x.onload = () => (x.status === 200 ? ok(JSON.parse(x.responseText))
                                        : échec(new Error(t("serveur-répond", x.status))));
    x.onerror = () => échec(new Error(t("envoi-coupé")));
    x.send(corps);
  });
}

(() => {
  const zone = $("depot");
  ["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => {
    e.preventDefault(); zone.classList.add("survol");
  }));
  ["dragleave", "drop"].forEach((t) => zone.addEventListener(t, () => zone.classList.remove("survol")));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    if (!e.dataTransfer.files.length) return;
    const dt = new DataTransfer();
    for (const f of e.dataTransfer.files) dt.items.add(f);
    $("fichier").files = dt.files;
    $("fichier").dispatchEvent(new Event("change"));
  });
  // A file dropped outside the zone must not replace the page.
  ["dragover", "drop"].forEach((t) => window.addEventListener(t, (e) => e.preventDefault()));
})();

async function exporter(route) {
  const f = $("fichier").files[0];
  if (!f) return;
  actifs(false);
  // The .x_t export adds to what is open: the view and STEP stay.
  const enPlus = route === "parasolid";
  if (!enPlus) {
    $("lien").innerHTML = "";
    $("lien-esq").innerHTML = "";
    grouper();
    $("vue-carte").style.display = "none";
    $("ruban-vue").hidden = $("ruban-orienter").hidden = true;
    $("plan-carte").style.display = "none";
    $("fiche-carte").style.display = "none";
    ficheMontrée = null;
    document.querySelector("main").classList.remove("avec-fiche");
  }
  log.innerHTML = ""; vide = true;
  const début = performance.now();
  try {
    charge(t("envoi", 0), 0);
    const { job } = await envoyer("/export?route=" + route +
                                  "&nom=" + encodeURIComponent(f.name), f);
    let depuis = 0, dernière = t("lecture");
    while (true) {
      const état = await (await fetch(`/journal?job=${job}&depuis=${depuis}`)).json();
      état.lignes.forEach(ligne);
      depuis += état.lignes.length;
      if (état.lignes.length) dernière = état.lignes[état.lignes.length - 1].text;
      const s = ((performance.now() - début) / 1000).toFixed(0);
      // The download link appears as soon as the file is written, before
      // the 3D view is ready.
      if (état.fichier && !document.querySelector(`a[data-job="${job}"]`)) {
        const a = document.createElement("a");
        a.className = "action telecharger";
        a.href = `/download?job=${job}`;
        a.dataset.job = job;
        a.title = t("télécharger", état.fichier);
        a.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12M7 10l5 5 5-5M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>`;
        a.append(état.fichier.split(".").pop());
        if (/\.(step|x_t|x_b|stl)$/i.test(état.fichier)) a.classList.add("sortie-3d");
        if (/\.(x_t|x_b)$/i.test(état.fichier)) {
          a.classList.add("sortie-xt");
          $("btn-parasolid").hidden = true;
          $("lien").prepend(a);  // where the button was
        } else {
          $("lien").append(a);
        }
        grouper();
      }
      if (état.esquisses_step && !document.querySelector(`a[data-job-esq="${job}"]`)) {
        const a = document.createElement("a");
        a.className = "action telecharger";
        a.href = `/download?job=${job}&quoi=esquisses`;
        a.dataset.jobEsq = job;
        a.title = t("télécharger-esq", état.esquisses_step);
        a.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12M7 10l5 5 5-5M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>`;
        // « Sketches » rather than a second « STEP »: you know which one you take.
        const mot = document.createElement("span");
        mot.dataset.t = "esquisses";
        mot.textContent = t("esquisses");
        a.append(mot);
        $("lien-esq").append(a);
        grouper();
      }
      if (état.fini) {
        if (état.vue && !enPlus) {
          charge(t("aff-vue", s));
          montrer(await (await fetch(`/vue?job=${job}`)).json());
        }
        if (état.plan) {
          charge(t("aff-plan", s));
          planche(await (await fetch(`/plan?job=${job}`)).json());
        }
        if (état.fiche && !enPlus) fiche(await (await fetch(`/fiche?job=${job}`)).json());
        break;
      }
      charge(`${dernière} — ${s} s`);
      await new Promise((r) => setTimeout(r, 250));
    }
  } catch (e) {
    ligne({ ms: performance.now() - début, kind: "err", text: String(e.message || e) });
  }
  $("charge").hidden = true;
  actifs(true);
}

// ---------------------------------------------------------------- properties
// What the file says about itself. The values come from the file: they are
// set as text, never as HTML.
function fiche(d) {
  ficheMontrée = d;
  const corps = $("fiche-table");
  corps.replaceChildren();
  const cellule = (tag, texte, classe) => {
    const e = document.createElement(tag);
    e.textContent = texte ?? "";
    if (classe) e.className = classe;
    return e;
  };
  // A section: collapsible, open by default when short.
  const bloc = (titre, n) => {
    const det = document.createElement("details");
    det.className = "fiche-bloc";
    det.open = !(n > 12);
    const resume = cellule("summary", titre);
    if (n != null) resume.append(cellule("small", String(n)));
    det.append(resume);
    corps.append(det);
    return det;
  };
  // A table: one column per header, one row per item; columns empty
  // everywhere are dropped.
  const tableau = (titre, entetes, lignes, classes = []) => {
    const garde = entetes.map((_, k) => k === 0 || lignes.some((l) => l[k] !== "" && l[k] != null));
    const det = bloc(titre, lignes.length);
    for (const l of lignes) {
      const champs = l.map((v, k) => [entetes[k], v]).slice(1).filter(([, v]) => v !== "" && v != null);
      if (!champs.length) sortie.push([titre, l[0], entetes[0], l[0], ""]);
      for (const [h, v] of champs) sortie.push([titre, l[0], h, v, ""]);
    }
    const tab = document.createElement("table");
    tab.className = "fiche-tab";
    const tete = document.createElement("tr");
    entetes.forEach((h, k) => garde[k] && tete.append(cellule("th", h, classes[k])));
    const thead = document.createElement("thead");
    thead.append(tete);
    const tbody = document.createElement("tbody");
    for (const l of lignes) {
      const tr = document.createElement("tr");
      l.forEach((v, k) => garde[k] && tr.append(cellule("td", v, classes[k])));
      tbody.append(tr);
    }
    tab.append(thead, tbody);
    const boite = document.createElement("div");
    boite.className = "fiche-defile";
    boite.append(tab);
    det.append(boite);
  };
  // What the export carries: one line per value shown, under its section.
  const sortie = [];
  let rubrique = t("f-general");
  let table = document.createElement("table");
  table.className = "fiche-kv";
  corps.append(table);
  const rang = (titre, valeur, note) => {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.textContent = titre;
    const td = document.createElement("td");
    td.textContent = valeur;
    if (note) {
      const s = document.createElement("small");
      s.textContent = " " + note;
      td.append(s);
    }
    tr.append(th, td);
    table.append(tr);
    sortie.push([rubrique, "", titre, valeur, note || ""]);
  };
  // A section of name / value pairs.
  const section = (titre, n) => {
    table = document.createElement("table");
    table.className = "fiche-kv";
    bloc(titre, n).append(table);
    rubrique = titre;
  };
  const date = (iso) => { const x = new Date(iso); return isNaN(x) ? iso : x.toLocaleString(); };
  for (const [cle, v] of Object.entries(d.resume || {})) if (v) rang(t("r-" + cle), v);
  if (d.cree) rang(t("f-cree"), date(d.cree));
  if (d.enregistre) rang(t("f-enregistre"), date(d.enregistre));
  if (d.enregistre_par) rang(t("f-par"), d.enregistre_par);
  if ((d.proprietes || []).length) {
    section(t("f-perso"), d.proprietes.length);
    d.proprietes.forEach((p) => rang(p.nom, p.valeur, p.expression || ""));
  }
  // Linked texts (a title block, a note): the value shown, and its expression.
  if ((d.liens || []).length) {
    section(t("f-liens"), d.liens.length);
    d.liens.forEach((l) => rang(l.valeur, "", l.expression));
  }
  // Dimensions as numbers, in mm: the value and the tolerance as stored.
  if ((d.cotes || []).length) {
    const mm = (v) => Number((v * 1000).toPrecision(10)).toLocaleString();
    // An angle is written in radians: shown in degrees, a unit change
    // like metres to millimetres.
    tableau(t("f-cotes"), [t("h-nom"), t("h-valeur"), t("h-tolerance")],
      d.cotes.map((c) => [c.nom,
        c.genre === "angle"
          ? `${Number((c.valeur_rad * 180 / Math.PI).toPrecision(10)).toLocaleString()}°`
          : `${mm(c.valeur_m)} mm`,
        c.tolerance ? `+${mm(c.tolerance.haut_m)} / ${mm(c.tolerance.bas_m)} mm` : ""]),
      ["", "num", "num"]);
  }
  const nombre = (x, n) => Number(x).toLocaleString(undefined, { maximumSignificantDigits: n });
  for (const c of d.configurations || []) {
    section(t("f-config", c.nom));
    rang("", t("f-geo", c.geometrie));
    if (c.materiau) {
      const dens = (c.materiau.physiques || {}).DENS;
      rang(t("f-materiau"), c.materiau.nom,
           (dens ? `${t("f-densite")} ${nombre(dens.valeur, 6)} kg/m³ · ` : "") + c.materiau.base);
    }
    if (c.masse) {
      const m = c.masse;
      rang(t("f-masse"), `${nombre(m.masse_kg * 1000, 8)} g`, t("f-masse-note"));
      rang(t("f-volume"), `${nombre(m.volume_m3 * 1e9, 8)} mm³`);
      rang(t("f-aire"), `${nombre(m.aire_m2 * 1e6, 8)} mm²`);
      rang(t("f-centre"), m.centre_m.map((v) => nombre(v * 1000, 8)).join(" ; ") + " mm");
      // At the centre of mass, model axes, in g·mm² as SolidWorks shows it.
      if (m.inertie_kg_m2) {
        const g = m.inertie_kg_m2.map((v) => nombre(v * 1e9, 8));
        rang(t("f-inertie"), `Lxx ${g[0]} ; Lyy ${g[1]} ; Lzz ${g[2]}`, "g·mm²");
        rang("", `Lxy ${g[3]} ; Lxz ${g[4]} ; Lyz ${g[5]}`, "g·mm²");
      }
    }
    (c.proprietes || []).forEach((p) => rang(p.nom, p.valeur, p.expression || ""));
  }
  // An assembly: its bill of materials (one row per part file, suppressed
  // components left out), then each component; a CSV of the former.
  if ((d.composants || []).length) {
    // What each supplied part says about itself: material and the
    // properties of the configuration shown, over the document's.
    const pieces = d.pieces || {};
    const specs = (c) => {
      const p = pieces[c.fichier];
      if (!p) return { materiau: "", props: new Map() };
      const conf = p.configurations[c.configuration] ||
        Object.values(p.configurations)[0] || { proprietes: [], materiau: "" };
      return { materiau: conf.materiau, props: new Map([...p.proprietes, ...conf.proprietes]) };
    };
    const lus = d.composants.map(specs);
    const noms = [...new Set(lus.flatMap((s) => [...s.props.keys()]))];
    const etat = (c) => c.supprime ? t("f-supprime") : c.hors_nomenclature ? t("f-hors")
      : c.cache ? t("f-cache") : "";
    // The bill of materials: one line per file and configuration,
    // suppressed or excluded components left out.
    const par = new Map();
    d.composants.forEach((c, i) => {
      if (c.supprime || c.hors_nomenclature) return;
      const cle = `${c.fichier}\u0000${c.configuration || ""}`;
      const x = par.get(cle) || { c, s: lus[i], n: 0 };
      x.n += 1;
      par.set(cle, x);
    });
    tableau(t("f-nomenclature"),
      [t("h-fichier"), t("h-qte"), t("h-config"), t("h-materiau"), ...noms],
      [...par.values()].map(({ c, s, n }) => [c.fichier, String(n), c.configuration || "",
        s.materiau, ...noms.map((k) => s.props.get(k) ?? "")]),
      ["", "num"]);
    tableau(t("f-composants"), [t("h-composant"), t("h-fichier"), t("h-config"), t("h-etat")],
      d.composants.map((c) => [c.nom, c.fichier, c.configuration || "", etat(c)]),
      ["", "", "", "doux"]);
    // The mates, as the tree names them; the type when measured.
    if ((d.contraintes || []).length) {
      tableau(t("f-contraintes"), [t("h-nom"), t("h-type"), t("h-valeur"), t("h-composants")],
        d.contraintes.map((c) => {
          const lib = c.libelle || "";
          const p = lib.indexOf(" (");
          return [p > 0 ? lib.slice(0, p) : (lib || c.nom), t("m-" + c.type) || `type ${c.type}`,
            c.valeur_m == null ? "" : `${+(c.valeur_m * 1000).toPrecision(12)} mm`,
            p > 0 ? lib.slice(p + 2, -1).replace(/,/g, ", ") : ""];
        }),
        ["", "", "num", "doux"]);
    }
    // The CSV is the list as the file gives it, one component per line: a
    // button next to the other downloads.
    const champ = (x) => `"${String(x).replace(/"/g, '""')}"`;
    const ligne = (xs) => xs.map(champ).join(";");
    // Each line also carries what its part says about itself, one column
    // per property name, in the order met.
    const csv = [ligne(["component", "file", "configuration", "state", "in_bom",
                        "material", ...noms])]
      .concat(d.composants.map((c, i) => ligne([c.nom, c.fichier, c.configuration || "",
        c.supprime ? "suppressed" : c.cache ? "hidden" : "",
        c.supprime || c.hors_nomenclature ? "no" : "yes",
        lus[i].materiau, ...noms.map((n) => lus[i].props.get(n) ?? "")])))
      .join("\r\n") + "\r\n";
    $("lien").querySelector("a.csv")?.remove();
    const a = document.createElement("a");
    a.className = "action telecharger csv";
    a.href = URL.createObjectURL(new Blob(["\ufeff" + csv], { type: "text/csv" }));
    a.download = ($("depot-nom").textContent || "assembly").replace(/\.[^.]*$/, "") +
      "-components.csv";
    a.title = t("f-csv");
    a.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12M7 10l5 5 5-5M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>`;
    a.append("csv");
    $("lien").append(a);
  }
  // Export what is shown, one line per value (CSV), or everything read (JSON).
  const base = ($("depot-nom").textContent || "document").replace(/\.[^.]*$/, "");
  const lien = (texte, titre, contenu, type, nom) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([contenu], { type }));
    a.download = nom;
    a.title = titre;
    a.textContent = texte;
    return a;
  };
  const cases = (xs) => xs.map((x) => `"${String(x ?? "").replace(/"/g, '""')}"`).join(";");
  const { apercu, apercu_grand, feuilles, ...lu } = d;
  lu.feuilles = (feuilles || []).map((f) => f.nom);
  $("fiche-export").replaceChildren(
    lien("csv", t("f-export-csv"),
      "\ufeff" + [cases(["section", "item", "field", "value", "note"])].concat(sortie.map(cases)).join("\r\n") + "\r\n",
      "text/csv", `${base}-properties.csv`),
    lien("json", t("f-export-json"), JSON.stringify(lu, null, 1) + "\n",
      "application/json", `${base}-properties.json`));
  const img = $("fiche-apercu");
  img.hidden = !d.apercu;
  img.title = t("apercu-agrandir");
  if (d.apercu) img.src = "data:image/png;base64," + d.apercu;
  // A click: the previews the file keeps, at their size.
  const légende = (id, cle) => (e) =>
    ($(id).textContent = `${t(cle)} — ${e.target.naturalWidth} × ${e.target.naturalHeight}`);
  $("apercu-png").onload = (e) => ($("apercu-png-legende").textContent =
    `${FICHE.legende} — ${e.target.naturalWidth} × ${e.target.naturalHeight}`);
  $("apercu-dib").onload = légende("apercu-dib-legende", "apercu-dib");
  $("apercu-dib-figure").hidden = !d.apercu_grand;
  $("apercu-dib").src = d.apercu_grand ? "data:image/png;base64," + d.apercu_grand : "";
  img.onclick = () => $("apercus").showModal();
  // A drawing's preview follows the sheet shown: SolidWorks keeps an
  // image of each.
  FICHE.apercu = d.apercu;
  FICHE.feuilles = feuilles || [];
  apercuFeuille();
  $("fiche-carte").style.display = "block";
  document.querySelector("main").classList.add("avec-fiche");
}

// ---------------------------------------------------------------- 3D view
// Plain WebGL, no library: the page loads nothing from the network.
//
// The view imitates SolidWorks, using the settings SOLIDWORKS 2026 keeps in
// the registry (HKCU\Software\SolidWorks\SOLIDWORKS 2026, keys Colors and
// General): orthographic projection, shaded with black edges ("HLR Edges in
// Shaded Mode"), gradient background ("Top/Bottom Gradient Color"), hovered
// face outlined in orange ("Dynamic Highlight"), selected face in blue
// ("Selected Face, Shaded"), triad X red, Y green, Z blue, 15° rotation steps
// ("View Rotate Increment"), zoom towards the cursor ("Zoom About Screen
// Center" = 0) and rotation about the part centre ("Rotate About Screen
// Center" = 0).
//
// Each vertex carries its face index; picking renders the scene offscreen
// with each face painted with its index, and reads the pixel under the mouse.

const SW = {
  défaut: [0.894, 0.894, 0.894],  // face with no colour in the file
  survol: [1.0, 0.502, 0.0],      // Dynamic Highlight, 0x0080FF in BGR
  choisie: [0.0, 0.502, 1.0],     // Selected Face, Shaded, 0xFF8000 in BGR
  arête: [0.0, 0.0, 0.0],         // HLR Edges in Shaded Mode
  pas: Math.PI / 12,              // View Rotate Increment: 15°
  axes: ["#ff0000", "#008000", "#0000ff"],  // X, Y, Z axis of Reference Triad
};

const SOMMET = `
attribute vec3 aPos; attribute vec3 aNor; attribute float aFace; attribute vec4 aCoul;
uniform mat4 uMVP;
varying vec3 vNor; varying vec3 vCoul; varying float vFace;
void main() {
  gl_Position = uMVP * vec4(aPos, 1.0);
  vNor = aNor; vCoul = aCoul.rgb; vFace = aFace;
}`;
// Three view-bound lights, as in SolidWorks, plus a specular highlight.
const COULEUR = `
precision highp float;
uniform vec3 uDroite; uniform vec3 uHaut; uniform vec3 uArriere;
uniform float uSel; uniform vec3 uChoisie;
varying vec3 vNor; varying vec3 vCoul; varying float vFace;
void main() {
  vec3 n = normalize(vNor);
  if (!gl_FrontFacing) n = -n;
  vec3 l1 = normalize(-0.45 * uDroite + 0.6 * uHaut + uArriere);
  vec3 l2 = normalize(0.8 * uDroite - 0.2 * uHaut + 0.5 * uArriere);
  vec3 l3 = normalize(0.1 * uDroite - 0.9 * uHaut + 0.3 * uArriere);
  float d = 0.55 * max(dot(n, l1), 0.0) + 0.25 * max(dot(n, l2), 0.0)
          + 0.10 * max(dot(n, l3), 0.0);
  float s = 0.28 * pow(max(dot(n, normalize(l1 + uArriere)), 0.0), 40.0);
  vec3 c = abs(vFace - uSel) < 0.5 ? uChoisie : vCoul;
  gl_FragColor = vec4(c * (0.32 + d) + s, 1.0);
}`;
const RANG = `
precision highp float;
varying vec3 vNor; varying vec3 vCoul; varying float vFace;
void main() {
  float i = floor(vFace + 0.5) + 1.0;
  gl_FragColor = vec4(mod(i, 256.0), mod(floor(i / 256.0), 256.0),
                      floor(i / 65536.0), 255.0) / 255.0;
}`;
const TRAIT = `
attribute vec3 aPos; attribute float aFace;
uniform mat4 uMVP;
varying float vFace;
void main() { gl_Position = uMVP * vec4(aPos, 1.0); vFace = aFace; }`;
const ENCRE = `
precision highp float;
uniform float uSeul; uniform vec3 uEncre;
varying float vFace;
void main() {
  if (uSeul >= 0.0 && abs(vFace - uSeul) > 0.5) discard;
  gl_FragColor = vec4(uEncre, 1.0);
}`;

const V = { gl: null, tampons: [] };

function programme(gl, sommet, fragment) {
  const p = gl.createProgram();
  for (const [genre, source] of [[gl.VERTEX_SHADER, sommet], [gl.FRAGMENT_SHADER, fragment]]) {
    const sh = gl.createShader(genre);
    gl.shaderSource(sh, source);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw gl.getShaderInfoLog(sh);
    gl.attachShader(p, sh);
  }
  ["aPos", "aNor", "aFace", "aCoul"].forEach((nom, i) => gl.bindAttribLocation(p, i, nom));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw gl.getProgramInfoLog(p);
  return p;
}

function tampon(gl, données) {
  const b = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, b);
  gl.bufferData(gl.ARRAY_BUFFER, données, gl.STATIC_DRAW);
  V.tampons.push(b);
  return b;
}

function lier(i, b, taille, genre, normé) {
  const gl = V.gl;
  gl.bindBuffer(gl.ARRAY_BUFFER, b);
  gl.enableVertexAttribArray(i);
  gl.vertexAttribPointer(i, taille, genre || gl.FLOAT, !!normé, 0, 0);
}

// Normals are smoothed within each face (a B-rep face is smooth) and broken
// between faces; drawn edges are face boundaries, i.e. edges belonging to a
// single facet. Shared vertices are matched by their bits: a mesh point is
// always written with the same three floats.
function préparer(vue, pos) {
  const n = pos.length / 3;
  const bits = new Uint32Array(pos.buffer, pos.byteOffset, pos.length);
  const nor = new Float32Array(pos.length);
  const face = new Float32Array(n);
  const coul = new Uint8Array(4 * n);
  const traits = [], traitsFace = [];
  const boites = [];
  let k = 0;
  vue.faces.forEach((f, rang) => {
    const c = (f.couleur || SW.défaut).map((x) => Math.round(255 * x));
    const ids = new Map(), somme = [], local = new Int32Array(3 * f.triangles);
    const plat = new Float32Array(3 * f.triangles);
    const bords = new Map();
    const b = [Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity];
    for (let t = 0; t < f.triangles; t++) {
      const v0 = k + 3 * t, a = 3 * v0;
      const ux = pos[a + 3] - pos[a], uy = pos[a + 4] - pos[a + 1], uz = pos[a + 5] - pos[a + 2];
      const vx = pos[a + 6] - pos[a], vy = pos[a + 7] - pos[a + 1], vz = pos[a + 8] - pos[a + 2];
      const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
      const l = Math.hypot(nx, ny, nz) || 1;
      plat[3 * t] = nx / l; plat[3 * t + 1] = ny / l; plat[3 * t + 2] = nz / l;
      for (let j = 0; j < 3; j++) {
        const v = v0 + j, s = 3 * v;
        const clé = bits[s] + "," + bits[s + 1] + "," + bits[s + 2];
        let id = ids.get(clé);
        if (id === undefined) { id = ids.size; ids.set(clé, id); somme.push(0, 0, 0); }
        local[3 * t + j] = id;
        somme[3 * id] += nx; somme[3 * id + 1] += ny; somme[3 * id + 2] += nz;
        face[v] = rang;
        coul.set(c, 4 * v); coul[4 * v + 3] = 255;
        for (let q = 0; q < 3; q++) {
          b[q] = Math.min(b[q], pos[s + q]);
          b[q + 3] = Math.max(b[q + 3], pos[s + q]);
        }
      }
      for (let j = 0; j < 3; j++) {
        const p = local[3 * t + j], q = local[3 * t + (j + 1) % 3];
        if (p === q) continue;
        const clé = p < q ? p * 4194304 + q : q * 4194304 + p;
        const e = bords.get(clé);
        if (e) e[0]++;
        else bords.set(clé, [1, v0 + j, v0 + (j + 1) % 3]);
      }
    }
    for (let t = 0; t < f.triangles; t++) {
      for (let j = 0; j < 3; j++) {
        const id = local[3 * t + j], s = 3 * (k + 3 * t + j);
        let x = somme[3 * id], y = somme[3 * id + 1], z = somme[3 * id + 2];
        const l = Math.hypot(x, y, z);
        const px = plat[3 * t], py = plat[3 * t + 1], pz = plat[3 * t + 2];
        // An average far from the facet normal means a crease: use flat.
        if (l < 1e-30 || (x * px + y * py + z * pz) / l < 0.5) { x = px; y = py; z = pz; }
        else { x /= l; y /= l; z /= l; }
        nor[s] = x; nor[s + 1] = y; nor[s + 2] = z;
      }
    }
    for (const [compte, p, q] of bords.values()) {
      if (compte !== 1) continue;
      traits.push(pos[3 * p], pos[3 * p + 1], pos[3 * p + 2],
                  pos[3 * q], pos[3 * q + 1], pos[3 * q + 2]);
      traitsFace.push(rang, rang);
    }
    boites.push(b);
    k += 3 * f.triangles;
  });
  return { nor, face, coul, traits: new Float32Array(traits),
           traitsFace: new Float32Array(traitsFace), boites };
}

function montrer(vue) {
  $("vue-carte").style.display = "block";
  $("ruban-vue").hidden = $("ruban-orienter").hidden = false;
  $("ruban-orienter").querySelector("[data-cube]").hidden = false;
  const canvas = $("vue");
  if (!V.gl) {
    V.gl = canvas.getContext("webgl", { antialias: true, alpha: true });
    if (!V.gl) {
      $("vue-info").textContent = t("pas-webgl");
      return;
    }
    const gl = V.gl;
    V.dessin = programme(gl, SOMMET, COULEUR);
    V.rang = programme(gl, SOMMET, RANG);
    V.trait = programme(gl, TRAIT, ENCRE);
    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    brancher(canvas);
  }
  const gl = V.gl;
  V.tampons.forEach((b) => gl.deleteBuffer(b));
  V.tampons = [];
  const brut = atob(vue.sommets);
  const octets = new Uint8Array(brut.length);
  for (let i = 0; i < brut.length; i++) octets[i] = brut.charCodeAt(i);
  const pos = new Float32Array(octets.buffer);
  const m = préparer(vue, pos);
  V.b = { pos: tampon(gl, pos), nor: tampon(gl, m.nor), face: tampon(gl, m.face),
          coul: tampon(gl, m.coul), traits: tampon(gl, m.traits),
          traitsFace: tampon(gl, m.traitsFace) };
  V.n = pos.length / 3;
  V.nTraits = m.traits.length / 3;
  V.faces = vue.faces;
  V.boites = m.boites;
  V.rayon = vue.rayon || 1;
  V.sel = -1; V.survol = -1;
  préparerEsquisses(gl, vue.esquisses);
  préparerPmi(vue.pmi, vue.centre);
  orienter("iso");
  delete $("vue-info").dataset.t;  // status replaces the help text: no longer translated
  $("vue-info").textContent = t("vue-prête", vue.faces.length, vue.corps);
  $("vue-copier").disabled = true;
}

// ------------------------------------------------------- camera
// An orthonormal frame (right, up, back) in part coordinates, a target at
// the screen centre and a visible half-height, in mm.

const ajout = (u, v, k = 1) => u.map((x, i) => x + k * v[i]);
const scal = (u, v) => u[0] * v[0] + u[1] * v[1] + u[2] * v[2];
const vect = (u, v) => [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
                        u[0] * v[1] - u[1] * v[0]];
const unité = (u) => { const l = Math.hypot(...u) || 1; return u.map((x) => x / l); };

// Rodrigues: v rotated by `angle` about the unit axis `axe`.
function rodrigues(v, axe, angle) {
  const c = Math.cos(angle), s = Math.sin(angle), k = scal(axe, v) * (1 - c);
  const x = vect(axe, v);
  return v.map((vi, i) => vi * c + x[i] * s + axe[i] * k);
}

// Rotate the camera about the part centre.
function tourner(axe, angle) {
  const r = (v) => rodrigues(v, axe, angle);
  V.droite = unité(r(V.droite)); V.haut = unité(r(V.haut));
  V.arrière = vect(V.droite, V.haut);
  V.cible = r(V.cible);  // the part centre is the origin (centred view)
  redessiner();
}

// SolidWorks standard views, Y up.
const VUES = {
  face: [[1, 0, 0], [0, 1, 0]], arrière: [[-1, 0, 0], [0, 1, 0]],
  gauche: [[0, 0, 1], [0, 1, 0]], droite: [[0, 0, -1], [0, 1, 0]],
  dessus: [[1, 0, 0], [0, 0, -1]], dessous: [[1, 0, 0], [0, 0, 1]],
  iso: [unité([1, 0, -1]), unité([-1, 2, -1])],
};

function orienter(nom) {
  const [d, h] = VUES[nom];
  V.droite = d.slice(); V.haut = h.slice(); V.arrière = vect(d, h);
  ajuster();
}

// Zoom to fit: the part's bounding boxes projected on the screen plane.
function ajuster() {
  const c = $("vue"), aspect = (c.clientWidth || 1) / (c.clientHeight || 1);
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const b of V.boites) {
    if (!(b[0] <= b[3])) continue;
    for (let i = 0; i < 8; i++) {
      const p = [b[(i & 1) ? 3 : 0], b[(i & 2) ? 4 : 1], b[(i & 4) ? 5 : 2]];
      const x = scal(p, V.droite), y = scal(p, V.haut);
      x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
    }
  }
  if (!(x0 <= x1)) { x0 = y0 = -V.rayon; x1 = y1 = V.rayon; }
  V.cible = ajout(ajout([0, 0, 0], V.droite, (x0 + x1) / 2), V.haut, (y0 + y1) / 2);
  V.demi = 1.08 * Math.max((y1 - y0) / 2, (x1 - x0) / 2 / aspect, V.rayon * 1e-3);
  redessiner();
}

// Column-major matrices, as WebGL reads them.
function produit(a, b) {
  const m = new Array(16).fill(0);
  for (let c = 0; c < 4; c++)
    for (let r = 0; r < 4; r++)
      for (let k = 0; k < 4; k++) m[4 * c + r] += a[4 * k + r] * b[4 * c + k];
  return m;
}
function matrice() {
  const c = $("vue"), aspect = c.width / c.height;
  const { droite: d, haut: h, arrière: a } = V;
  const recul = 4 * V.rayon + Math.hypot(...V.cible);
  const œil = ajout(V.cible, a, recul);
  const vueM = [d[0], h[0], a[0], 0, d[1], h[1], a[1], 0, d[2], h[2], a[2], 0,
                -scal(d, œil), -scal(h, œil), -scal(a, œil), 1];
  const sy = 1 / V.demi, sx = sy / aspect, loin = 2 * recul;
  const ortho = [sx, 0, 0, 0, 0, sy, 0, 0, 0, 0, -2 / loin, 0, 0, 0, -1, 1];
  return produit(ortho, vueM);
}

function uniformes(prog, valeurs) {
  const gl = V.gl;
  for (const [nom, v] of Object.entries(valeurs)) {
    const l = gl.getUniformLocation(prog, nom);
    if (l === null) continue;
    if (typeof v === "number") gl.uniform1f(l, v);
    else if (v.length === 3) gl.uniform3fv(l, v);
    else gl.uniformMatrix4fv(l, false, new Float32Array(v));
  }
}

function faces(prog, mvp) {
  const gl = V.gl;
  gl.useProgram(prog);
  uniformes(prog, { uMVP: mvp, uSel: V.sel, uChoisie: SW.choisie,
                    uDroite: V.droite, uHaut: V.haut, uArriere: V.arrière });
  lier(0, V.b.pos, 3); lier(1, V.b.nor, 3); lier(2, V.b.face, 1);
  lier(3, V.b.coul, 4, gl.UNSIGNED_BYTE, true);
  gl.drawArrays(gl.TRIANGLES, 0, V.n);
}

function arêtes(mvp, seule, encre) {
  const gl = V.gl;
  if (!V.nTraits) return;
  gl.useProgram(V.trait);
  uniformes(V.trait, { uMVP: mvp, uSeul: seule, uEncre: encre });
  gl.disableVertexAttribArray(1); gl.disableVertexAttribArray(3);
  lier(0, V.b.traits, 3); lier(2, V.b.traitsFace, 1);
  gl.drawArrays(gl.LINES, 0, V.nTraits);
}

// ------------------------------------------------------- the sketches
// Strokes placed in the model, drawn by the edge program (aFace absent:
// nothing is left out). A switch shows them all; a click on one of their
// strokes picks the sketch, and the status line says which.
function préparerEsquisses(gl, esq) {
  V.esq = null;
  $("vue-esq-bascule").hidden = !esq;
  if (!esq) return;
  const brut = atob(esq.sommets);
  const octets = new Uint8Array(brut.length);
  for (let i = 0; i < brut.length; i++) octets[i] = brut.charCodeAt(i);
  const pos = new Float32Array(octets.buffer);
  V.esq = { liste: esq.liste, pos, b: tampon(gl, pos), choisie: -1 };
}

// A sketch's stroke ranges: [first, count] in vertices.
function plages(e) { return [e.trait, e.construction].filter(([, n]) => n); }

// The sketch with a stroke within 6 px of the click, or -1.
function esquisseSous(c, ev) {
  if (!V.esq || !$("vue-esq").checked) return -1;
  const r = c.getBoundingClientRect();
  const mx = ev.clientX - r.left, my = ev.clientY - r.top;
  const m = matrice(), p = V.esq.pos;
  const écran = (k) => {
    const x = p[3 * k], y = p[3 * k + 1], z = p[3 * k + 2];
    const w = m[3] * x + m[7] * y + m[11] * z + m[15];
    return [((m[0] * x + m[4] * y + m[8] * z + m[12]) / w + 1) / 2 * r.width,
            (1 - (m[1] * x + m[5] * y + m[9] * z + m[13]) / w) / 2 * r.height];
  };
  let meilleure = -1, d2min = 36;
  V.esq.liste.forEach((e, i) => {
    for (const [début, n] of plages(e)) {
      for (let k = début; k + 1 < début + n; k += 2) {
        const [ax, ay] = écran(k), [bx, by] = écran(k + 1);
        const dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy;
        const s = l2 ? Math.max(0, Math.min(1, ((mx - ax) * dx + (my - ay) * dy) / l2)) : 0;
        const ex = ax + s * dx - mx, ey = ay + s * dy - my, d2 = ex * ex + ey * ey;
        if (d2 < d2min) { d2min = d2; meilleure = i; }
      }
    }
  });
  return meilleure;
}

function choisirEsquisse(i) {
  V.esq.choisie = i;
  const e = V.esq.liste[i];
  delete $("vue-info").dataset.t;
  $("vue-info").textContent = t("esq-info", e.nom, e.fonction);
  redessiner();
}

function esquisses(mvp) {
  if (!V.esq || !$("vue-esq").checked) return;
  const gl = V.gl;
  gl.useProgram(V.trait);
  gl.disableVertexAttribArray(1); gl.disableVertexAttribArray(2);
  gl.disableVertexAttribArray(3);
  gl.vertexAttrib1f(2, 0);
  lier(0, V.esq.b, 3);
  const choisie = V.esq.choisie;
  const ordre = V.esq.liste.map((_, i) => i).filter((i) => i !== choisie);
  if (choisie >= 0) ordre.push(choisie);  // on top of the others
  for (const i of ordre) {
    const e = V.esq.liste[i], vive = i === choisie;
    for (const [clé, encre] of [["construction", vive ? [0.8, 0.45, 0.1] : [0.55, 0.58, 0.64]],
                                ["trait", vive ? [0.95, 0.55, 0.1] : [0.1, 0.45, 0.9]]]) {
      const [début, n] = e[clé];
      if (!n) continue;
      uniformes(V.trait, { uMVP: mvp, uSeul: -1, uEncre: encre });
      gl.drawArrays(gl.LINES, début, n);
    }
  }
}

$("vue-esq").onchange = () => {
  if (V.esq && !$("vue-esq").checked) V.esq.choisie = -1;
  if (V.gl) redessiner();
};

// ------------------------------------------------------------ PMI
// DimXpert annotations (sldprt.dimxpert.pmi_en_vue), drawn on a 2D layer:
// each leads from its feature's point, by a leader line, to its frame —
// geometric tolerance and datum in cells, dimension as a label. Those of
// one feature stack. A click on a frame selects it, and the status line
// says which.
function préparerPmi(liste, centre) {
  V.pmi = null;
  $("vue-pmi-bascule").hidden = !liste;
  effacerPmi();
  if (!liste) return;
  const c = centre || [0, 0, 0];
  const groupes = new Map();
  liste.forEach((a, i) => {
    const clé = a.ancre.join(",");
    if (!groupes.has(clé)) groupes.set(clé, { p: a.ancre.map((v, k) => v - c[k]), membres: [] });
    groupes.get(clé).membres.push(i);
  });
  V.pmi = { liste, groupes: [...groupes.values()], choisie: -1, cadres: [] };
}

function effacerPmi() {
  const t = $("vue-pmi");
  t.getContext("2d").clearRect(0, 0, t.width, t.height);
}

function pmi(mvp) {
  const t = $("vue-pmi");
  const r = window.devicePixelRatio || 1;
  const l = t.clientWidth, h = t.clientHeight;
  if (t.width !== Math.round(l * r) || t.height !== Math.round(h * r)) {
    t.width = Math.round(l * r); t.height = Math.round(h * r);
  }
  const g = t.getContext("2d");
  g.setTransform(r, 0, 0, r, 0, 0);
  g.clearRect(0, 0, l, h);
  if (!V.pmi || !$("vue-pmi-voir").checked) return;
  const m = mvp;
  const écran = ([x, y, z]) => {
    const w = m[3] * x + m[7] * y + m[11] * z + m[15];
    return [((m[0] * x + m[4] * y + m[8] * z + m[12]) / w + 1) / 2 * l,
            (1 - (m[1] * x + m[5] * y + m[9] * z + m[13]) / w) / 2 * h];
  };
  const H = 20, pad = 5, encre = "#1b2433", vive = "#e07b0c";
  const TEXTE = "12.5px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
  const SYMBOLE = "15px 'Segoe UI Symbol', 'Cambria Math', 'DejaVu Sans', 'Noto Sans Symbols', sans-serif";
  // The first cell of a tolerance frame is its symbol.
  const police = (a, k) => (k === 0 && a.cadre && a.genre !== "Datum" ? SYMBOLE : TEXTE);
  g.textBaseline = "middle";
  V.pmi.cadres = [];
  const dessinées = [];
  // Each block (a feature's annotations, stacked) looks for a free spot
  // around its point: first away from the screen centre, then turning and
  // moving further out. Blocks already placed, and anchor points, are
  // obstacles. With no free spot, the first try stays.
  const posés = V.pmi.groupes.map((gr) => écran(gr.p)).map(([x, y]) => ({ x: x - 4, y: y - 4, w: 8, h: 8 }));
  const touche = (r) => posés.some((q) => r.x < q.x + q.w + 3 && q.x < r.x + r.w + 3 &&
                                           r.y < q.y + q.h + 3 && q.y < r.y + r.h + 3);
  const ESSAIS = [];
  for (const dist of [34, 58, 88, 124]) {
    for (const angle of [0, 25, -25, 50, -50, 80, -80, 115, -115, 150, -150, 180]) {
      ESSAIS.push([dist, angle * Math.PI / 180]);
    }
  }
  for (const gr of V.pmi.groupes) {
    const [ax, ay] = écran(gr.p);
    const blocs = gr.membres.map((i) => {
      const a = V.pmi.liste[i];
      const largeurs = a.cases.map((c, k) => {
        g.font = police(a, k);
        return Math.max(g.measureText(c).width + 2 * pad, k === 0 && a.cadre ? H : 0);
      });
      return { i, a, largeurs, total: largeurs.reduce((s, w) => s + w, 0) };
    });
    const larg = Math.max(...blocs.map((b) => b.total)), haut = blocs.length * H;
    let dx0 = ax - l / 2, dy0 = ay - h / 2;
    const n = Math.hypot(dx0, dy0);
    if (n < 1) { dx0 = 1; dy0 = -1; } else { dx0 /= n; dy0 /= n; }
    let choix = null;
    for (const [dist, angle] of ESSAIS) {
      const c = Math.cos(angle), s = Math.sin(angle);
      const dx = dx0 * c - dy0 * s, dy = dx0 * s + dy0 * c;
      const ox = ax + dx * dist, oy = ay + dy * dist;
      const r = { x: dx < 0 ? ox - larg : ox, y: dy < 0 ? oy - haut : oy, w: larg, h: haut };
      const dedans = r.x >= 0 && r.y >= 0 && r.x + r.w <= l && r.y + r.h <= h;
      if (!choix) choix = { r, dx };
      if (dedans && !touche(r)) { choix = { r, dx }; break; }
    }
    posés.push(choix.r);
    blocs.forEach((b, rang) => {
      const x = choix.dx < 0 ? choix.r.x + larg - b.total : choix.r.x, y = choix.r.y + rang * H;
      dessinées.push({ ...b, x, y, ax, ay, premier: rang === 0, bloc: choix.r });
      V.pmi.cadres.push({ i: b.i, x, y, w: b.total, h: H });
    });
  }
  // Leaders first, frames on top, the selected one last.
  g.lineWidth = 1;
  for (const d of dessinées) {
    if (!d.premier) continue;
    g.strokeStyle = "rgba(27,36,51,.7)";
    g.beginPath(); g.moveTo(d.ax, d.ay);
    const r = d.bloc;
    const bx = Math.max(r.x, Math.min(d.ax, r.x + r.w)), by = Math.max(r.y, Math.min(d.ay, r.y + r.h));
    g.lineTo(bx, by); g.stroke();
    g.fillStyle = encre;
    g.beginPath(); g.arc(d.ax, d.ay, 2.5, 0, 2 * Math.PI); g.fill();
  }
  dessinées.sort((p, q) => (p.i === V.pmi.choisie) - (q.i === V.pmi.choisie));
  for (const d of dessinées) {
    const choisie = d.i === V.pmi.choisie, trait = choisie ? vive : encre;
    g.fillStyle = d.a.cadre ? "rgba(255,255,255,.95)" : "rgba(255,255,255,.8)";
    g.fillRect(d.x, d.y, d.total, H);
    g.strokeStyle = trait; g.lineWidth = choisie ? 2 : 1;
    if (d.a.cadre) {
      g.strokeRect(d.x + .5, d.y + .5, d.total - 1, H - 1);
      let x = d.x;
      for (const w of d.largeurs.slice(0, -1)) {
        x += w; g.beginPath(); g.moveTo(x + .5, d.y); g.lineTo(x + .5, d.y + H); g.stroke();
      }
    } else if (choisie) {
      g.strokeRect(d.x + .5, d.y + .5, d.total - 1, H - 1);
    }
    g.fillStyle = trait;
    let x = d.x;
    d.a.cases.forEach((c, k) => {
      g.font = police(d.a, k);
      g.textAlign = "center";
      g.fillText(c, x + d.largeurs[k] / 2, d.y + H / 2 + .5);
      x += d.largeurs[k];
    });
    g.textAlign = "left";
  }
}

// The annotation whose frame is under the click, or -1.
function pmiSous(c, ev) {
  if (!V.pmi || !$("vue-pmi-voir").checked) return -1;
  const r = c.getBoundingClientRect();
  const mx = ev.clientX - r.left, my = ev.clientY - r.top;
  for (let k = V.pmi.cadres.length - 1; k >= 0; k--) {
    const f = V.pmi.cadres[k];
    if (mx >= f.x && mx <= f.x + f.w && my >= f.y && my <= f.y + f.h) return f.i;
  }
  return -1;
}

function choisirPmi(i) {
  V.pmi.choisie = i;
  const a = V.pmi.liste[i];
  delete $("vue-info").dataset.t;
  $("vue-info").textContent = t("pmi-info", a.nom, a.fonction, a.cases.join(" | "));
  redessiner();
}

$("vue-pmi-voir").onchange = () => {
  if (V.pmi && !$("vue-pmi-voir").checked) V.pmi.choisie = -1;
  if (V.gl) redessiner();
};

function redessiner() {
  if (V.attente) return;
  V.attente = requestAnimationFrame(() => {
    V.attente = 0;
    const c = $("vue"), r = window.devicePixelRatio || 1;
    const l = Math.round(c.clientWidth * r), h = Math.round(c.clientHeight * r);
    if (c.width !== l || c.height !== h) { c.width = l; c.height = h; }
    const gl = V.gl, mvp = matrice();
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, c.width, c.height);
    gl.clearColor(0, 0, 0, 0);  // the background gradient comes from CSS
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    // Push faces back slightly so edges draw on top.
    gl.enable(gl.POLYGON_OFFSET_FILL);
    gl.polygonOffset(1, 1);
    faces(V.dessin, mvp);
    gl.disable(gl.POLYGON_OFFSET_FILL);
    arêtes(mvp, -1, SW.arête);
    if (V.survol >= 0 && V.survol !== V.sel) arêtes(mvp, V.survol, SW.survol);
    esquisses(mvp);
    pmi(mvp);
    trièdre();
    cube();
  });
}

// The view cube, top left, as in SolidWorks: six CSS 3D faces that turn
// with the part. A click on a face gives its view.
// The cube is built the CSS way (y down): its local frame is the model's,
// Y flipped. The matrix sends its axes onto the screen (x right, y down,
// z towards the eye).
function cube() {
  const c = $("vue-cube-corps");
  if (!c || !V.droite) return;
  const d = V.droite, h = V.haut, a = V.arrière;
  const m = [d[0], -h[0], a[0], 0, -d[1], h[1], -a[1], 0, d[2], -h[2], a[2], 0, 0, 0, 0, 1];
  c.style.transform = `matrix3d(${m.map((v) => v.toFixed(6)).join(",")})`;
}

document.querySelectorAll("[data-cube]").forEach((f) => {
  f.onclick = () => V.gl && orienter(f.dataset.cube);
});

// Reference triad, bottom left, as in SolidWorks.
function trièdre() {
  const t = $("vue-triedre"), r = window.devicePixelRatio || 1;
  const taille = 76;
  if (t.width !== taille * r) { t.width = t.height = taille * r; }
  const g = t.getContext("2d");
  g.setTransform(r, 0, 0, r, 0, 0);
  g.clearRect(0, 0, taille, taille);
  const o = taille / 2, long = 26;
  const axes = [[1, 0, 0], [0, 1, 0], [0, 0, 1]].map((u, i) => ({
    x: scal(u, V.droite), y: scal(u, V.haut), z: scal(u, V.arrière), i }));
  axes.sort((p, q) => p.z - q.z);
  g.lineWidth = 2;
  g.font = "bold 11px system-ui, sans-serif";
  for (const a of axes) {
    g.strokeStyle = g.fillStyle = SW.axes[a.i];
    g.beginPath(); g.moveTo(o, o); g.lineTo(o + long * a.x, o - long * a.y); g.stroke();
    g.fillText("XYZ"[a.i], o + (long + 8) * a.x - 4, o - (long + 8) * a.y + 4);
  }
}

// Index of the face under canvas pixel (x, y), or -1.
function viser(x, y) {
  const gl = V.gl, c = $("vue");
  if (!V.fbo || V.fboL !== c.width || V.fboH !== c.height) {
    V.fbo = gl.createFramebuffer();
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, c.width, c.height, 0, gl.RGBA,
                  gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    const prof = gl.createRenderbuffer();
    gl.bindRenderbuffer(gl.RENDERBUFFER, prof);
    gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT16, c.width, c.height);
    gl.bindFramebuffer(gl.FRAMEBUFFER, V.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, prof);
    V.fboL = c.width; V.fboH = c.height;
  }
  gl.bindFramebuffer(gl.FRAMEBUFFER, V.fbo);
  gl.viewport(0, 0, c.width, c.height);
  gl.clearColor(0, 0, 0, 0);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  faces(V.rang, matrice());
  const px = new Uint8Array(4);
  gl.readPixels(x, c.height - 1 - y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
  gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  return px[0] + 256 * px[1] + 65536 * px[2] - 1;
}

function sous(c, e) {
  const r = c.getBoundingClientRect(), d = c.width / r.width;
  return viser(Math.floor((e.clientX - r.left) * d), Math.floor((e.clientY - r.top) * d));
}

function choisir(rang) {
  V.sel = rang;
  const info = $("vue-info");
  if (rang < 0) {
    info.textContent = t("aucune-face");
    $("vue-copier").disabled = true;
  } else {
    const f = V.faces[rang];
    info.innerHTML = "";
    const b = document.createElement("b");
    b.textContent = f.nom;
    info.append(b, t("face-info", f.corps, f.triangles));
    $("vue-copier").disabled = f.nom === "?";
  }
  redessiner();
}

// Zoom keeps the point under the cursor fixed (nx, ny in [−1, 1]).
function zoomer(f, nx = 0, ny = 0) {
  const c = $("vue"), aspect = c.clientWidth / c.clientHeight;
  V.cible = ajout(ajout(V.cible, V.droite, (1 - f) * V.demi * nx * aspect),
                  V.haut, (1 - f) * V.demi * ny);
  V.demi *= f;
  redessiner();
}

function déplacer(dx, dy) {
  const k = 2 * V.demi / $("vue").clientHeight;
  V.cible = ajout(ajout(V.cible, V.droite, -k * dx), V.haut, k * dy);
  redessiner();
}

// SolidWorks mouse: middle button rotates, with Ctrl pans, with Shift zooms,
// with Alt rolls in the screen plane; middle double-click fits. The wheel
// zooms towards the cursor (backwards to enlarge). Left click selects a face.
// Without a middle button (touchpad), left drag rotates and right drag pans.
function brancher(c) {
  let glisse = null, milieu = 0;
  c.tabIndex = 0;
  c.addEventListener("contextmenu", (e) => e.preventDefault());
  // Prevent middle-click autoscroll.
  c.addEventListener("mousedown", (e) => { if (e.button === 1) e.preventDefault(); });
  c.addEventListener("pointerdown", (e) => {
    c.focus();
    c.setPointerCapture(e.pointerId);
    if (e.button === 1) {
      e.preventDefault();  // no browser autoscroll
      const t = performance.now();
      if (t - milieu < 350) { ajuster(); milieu = 0; return; }
      milieu = t;
    }
    const mode = e.button === 1
      ? (e.ctrlKey ? "déplacer" : e.shiftKey ? "zoomer" : e.altKey ? "pivoter" : "tourner")
      : e.button === 2 || e.shiftKey ? "déplacer" : "tourner";
    glisse = { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY, mode,
               bouton: e.button };
    V.survol = -1;
  });
  c.addEventListener("pointermove", (e) => {
    if (!glisse) { survoler(c, e); return; }
    const dx = e.clientX - glisse.x, dy = e.clientY - glisse.y;
    glisse.x = e.clientX; glisse.y = e.clientY;
    if (glisse.mode === "déplacer") déplacer(dx, dy);
    else if (glisse.mode === "zoomer") zoomer(Math.exp(dy * 0.005));
    else if (glisse.mode === "pivoter") tourner(V.arrière, dx * 0.008);
    else {
      const haut = V.haut.slice(), droite = V.droite.slice();
      if (dx) tourner(haut, -dx * 0.008);
      if (dy) tourner(droite, -dy * 0.008);
    }
  });
  c.addEventListener("pointerup", (e) => {
    if (glisse && glisse.bouton === 0 &&
        Math.hypot(e.clientX - glisse.x0, e.clientY - glisse.y0) < 4) {
      // A PMI frame under the click wins over everything behind it, a
      // sketch stroke over the face behind it.
      const a = pmiSous(c, e);
      const k = a >= 0 ? -1 : esquisseSous(c, e);
      if (a >= 0) choisirPmi(a);
      else if (k >= 0) choisirEsquisse(k);
      else choisir(sous(c, e));
    }
    glisse = null;
  });
  c.addEventListener("pointerleave", () => {
    if (V.survol >= 0 && !glisse) { V.survol = -1; redessiner(); }
  });
  c.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = c.getBoundingClientRect();
    zoomer(Math.exp(-e.deltaY * 0.0015),
           (e.clientX - r.left) / r.width * 2 - 1, 1 - (e.clientY - r.top) / r.height * 2);
  }, { passive: false });
  // SolidWorks keyboard shortcuts, while the canvas has focus.
  c.addEventListener("keydown", (e) => {
    const flèches = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    const vues = { 1: "face", 2: "arrière", 3: "gauche", 4: "droite", 5: "dessus",
                   6: "dessous", 7: "iso" };
    let fait = true;
    if (e.key in flèches) {
      const [x, y] = flèches[e.key];
      const pas = e.shiftKey ? Math.PI / 2 : SW.pas;
      if (e.ctrlKey) déplacer(-x * 20, -y * 20);
      else if (e.altKey && x) tourner(V.arrière, x * pas);
      else if (x) tourner(V.haut.slice(), -x * pas);
      else tourner(V.droite.slice(), -y * pas);
    } else if (e.key === "f" || e.key === "F") ajuster();
    else if (e.key === "z") zoomer(1.25);
    else if (e.key === "Z") zoomer(0.8);
    else if (e.ctrlKey && vues[e.key]) orienter(vues[e.key]);
    else if (e.key === "Escape") choisir(-1);
    else fait = false;
    if (fait) e.preventDefault();
  });
  new ResizeObserver(redessiner).observe(c);
}

// Dynamic highlight. An offscreen pass per mouse move is costly, so wait
// for the mouse to settle.
function survoler(c, e) {
  clearTimeout(V.minuteur);
  V.minuteur = setTimeout(() => {
    const rang = sous(c, e);
    if (rang !== V.survol) { V.survol = rang; redessiner(); }
  }, 40);
}

// Zoom to fit: on the drawing when it is the one shown.
$("vue-recadrer").onclick = () => {
  if ($("plan-carte").style.display === "block") P.plein && cadrer({ ...P.plein });
  else if (V.gl) ajuster();
};
$("vue-copier").onclick = () => {
  if (V.sel >= 0) navigator.clipboard.writeText(V.faces[V.sel].nom);
};
function trouver() {
  if (!V.faces) return;
  let nom = $("vue-cherche").value.trim().toUpperCase();
  if (!nom) return;
  // An assembly face is named "component:index"; a part face, F<n>.
  if (!nom.startsWith("F") && !nom.includes(":")) nom = "F" + nom;
  const rang = V.faces.findIndex((f) => f.nom.toUpperCase() === nom);
  if (rang < 0) { $("vue-info").textContent = t("pas-pièce", nom); return; }
  const b = V.boites[rang];
  if (b[0] <= b[3]) {
    const centre = [0, 1, 2].map((i) => (b[i] + b[i + 3]) / 2);
    // Keep the target in the screen plane: project the centre onto it.
    V.cible = ajout(ajout([0, 0, 0], V.droite, scal(centre, V.droite)),
                    V.haut, scal(centre, V.haut));
    V.demi = Math.max(2 * Math.hypot(b[3] - b[0], b[4] - b[1], b[5] - b[2]), V.rayon * 1e-3);
  }
  choisir(rang);
}
$("vue-trouver").onclick = trouver;
$("vue-cherche").addEventListener("keydown", (e) => { if (e.key === "Enter") trouver(); });

// ------------------------------------------------------- drawing
// The SVG comes from the server as is. Zoom and pan rewrite the viewBox;
// strokes keep their screen width (vector-effect) at any scale.

const P = { feuilles: [], rang: 0, vb: null, plein: null, sel: null };

// The properties preview: the file's, or the image of the sheet shown.
const FICHE = { apercu: null, feuilles: [], legende: "" };
function apercuFeuille() {
  const nom = P.feuilles[P.rang] && $("plan-carte").style.display === "block"
    ? P.feuilles[P.rang].nom : null;
  const f = FICHE.feuilles.find((x) => x.nom === nom);
  const png = (f && f.png) || FICHE.apercu;
  const img = $("fiche-apercu");
  img.hidden = !png;
  if (png) img.src = "data:image/png;base64," + png;
  img.title = f && !f.png ? t("feuille-sans-image") : t("apercu-agrandir");
  FICHE.legende = f && f.png ? `Images/Sheet_${FICHE.feuilles.indexOf(f)} — ${f.nom}`
    : t("apercu-png");
  $("apercu-png").src = png ? "data:image/png;base64," + png : "";
}

function planche(plan) {
  P.feuilles = plan.feuilles;
  $("plan-carte").style.display = "block";
  // The ribbon's View group serves the drawing too: zoom to fit and full
  // page, without isometric, which means nothing on a sheet.
  $("ruban-orienter").hidden = false;
  $("ruban-orienter").querySelector("[data-cube]").hidden = true;
  const onglets = $("plan-onglets");
  onglets.innerHTML = "";
  plan.feuilles.forEach((f, i) => {
    const b = document.createElement("button");
    b.textContent = f.nom + (f.active ? " ●" : "");
    b.onclick = () => feuille(i);
    onglets.append(b);
  });
  feuille(Math.max(0, plan.feuilles.findIndex((f) => f.active)));
  lienSvg();
}

function feuille(i) {
  P.rang = i;
  apercuFeuille();
  [...$("plan-onglets").children].forEach((b, j) => b.classList.toggle("choisi", i === j));
  $("plan").innerHTML = P.feuilles[i].svg;
  const svg = $("plan").querySelector("svg");
  svg.removeAttribute("width"); svg.removeAttribute("height");
  const vb = svg.viewBox.baseVal;
  P.plein = { x: vb.x, y: vb.y, w: vb.width, h: vb.height };
  P.sel = null;
  cadrer({ ...P.plein });
  const f = P.feuilles[i];
  delete $("plan-info").dataset.t;
  $("plan-info").textContent = t("feuille-info", f.nom, f.elements, f.vues.length);
  $("plan-copier").disabled = true;
  if (!P.branché) { brancherPlan($("plan")); P.branché = true; }
}

function cadrer(vb) {
  P.vb = vb;
  const svg = $("plan").querySelector("svg");
  if (svg) svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
}

// Sheet point under the mouse, in viewBox units.
function sousSouris(e) {
  const svg = $("plan").querySelector("svg");
  const pt = svg.createSVGPoint();
  pt.x = e.clientX; pt.y = e.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}

function désigner(el) {
  if (P.sel) P.sel.classList.remove("choisi");
  P.sel = el;
  const info = $("plan-info");
  if (!el) {
    info.textContent = t("aucun-trait");
    $("plan-copier").disabled = true;
    return;
  }
  el.classList.add("choisi");
  info.innerHTML = "";
  const b = document.createElement("b");
  b.textContent = el.dataset.nom;
  const vue = el.closest("[data-vue]");
  const quoi = t("genres")[el.tagName] || el.tagName;
  info.append(b, ` · ${quoi}` + (vue ? ` · ${vue.dataset.vue}` : "") +
              (el.dataset.corps ? ` · ${t("corps")} ${el.dataset.corps}` : ""));
  $("plan-copier").disabled = false;
}

function brancherPlan(zone) {
  let glisse = null;
  zone.addEventListener("pointerdown", (e) => {
    zone.setPointerCapture(e.pointerId);
    glisse = { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY };
  });
  zone.addEventListener("pointermove", (e) => {
    if (!glisse || !P.vb) return;
    const r = zone.getBoundingClientRect();
    const k = Math.max(P.vb.w / r.width, P.vb.h / r.height);
    P.vb.x -= (e.clientX - glisse.x) * k;
    P.vb.y -= (e.clientY - glisse.y) * k;
    glisse.x = e.clientX; glisse.y = e.clientY;
    cadrer(P.vb);
  });
  zone.addEventListener("pointerup", (e) => {
    if (glisse && Math.hypot(e.clientX - glisse.x0, e.clientY - glisse.y0) < 4) {
      // Thin lines are hard to hit: search a few pixels around the click.
      // Release pointer capture first, or elementFromPoint returns the zone.
      zone.releasePointerCapture(e.pointerId);
      let el = null;
      for (let d = 0; d <= 4 && !el; d += 2)
        for (const [dx, dy] of [[0, 0], [d, 0], [-d, 0], [0, d], [0, -d]]) {
          const t = document.elementFromPoint(e.clientX + dx, e.clientY + dy);
          el = t && t.closest ? t.closest("[data-nom]") : null;
          if (el) break;
        }
      désigner(el);
    }
    glisse = null;
  });
  zone.addEventListener("wheel", (e) => {
    e.preventDefault();
    if (!P.vb) return;
    const f = Math.exp(e.deltaY * 0.0015);
    const p = sousSouris(e);
    cadrer({ x: p.x - (p.x - P.vb.x) * f, y: p.y - (p.y - P.vb.y) * f,
             w: P.vb.w * f, h: P.vb.h * f });
  }, { passive: false });
}

function trouverTrait() {
  const nom = $("plan-cherche").value.trim().toUpperCase();
  if (!nom || !P.feuilles.length) return;
  // The name may be on another sheet.
  for (let i = 0; i < P.feuilles.length; i++) {
    if (!P.feuilles[i].svg.includes(`data-nom="${nom}"`)) continue;
    if (i !== P.rang) feuille(i);
    const el = $("plan").querySelector(`[data-nom="${nom}"]`);
    const b = el.getBBox(), marge = Math.max(b.width, b.height, 2) * 1.5;
    cadrer({ x: b.x - marge, y: b.y - marge, w: b.width + 2 * marge,
             h: b.height + 2 * marge });
    désigner(el);
    return;
  }
  $("plan-info").textContent = t("pas-plan", nom);
}

$("plan-recadrer").onclick = () => P.plein && cadrer({ ...P.plein });
// The shown sheet as SVG, next to the other downloads: as the server
// drew it, before zoom and pan touched its viewBox.
function lienSvg() {
  $("lien").querySelector("a.svg")?.remove();
  const lien = document.createElement("a");
  lien.className = "action telecharger svg";
  lien.href = "#";
  lien.title = t("svg-feuille");
  lien.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12M7 10l5 5 5-5M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>`;
  lien.append("svg");
  lien.onclick = (e) => { e.preventDefault(); enregistrerSvg(); };
  $("lien").append(lien);
}

function enregistrerSvg() {
  const f = P.feuilles[P.rang];
  if (!f) return;
  const base = ($("fichier").files[0]?.name || "drawing").replace(/\.[^.]*$/, "");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([f.svg], { type: "image/svg+xml" }));
  a.download = `${base}-${f.nom}.svg`.replace(/[\\/:*?"<>|]/g, "_");
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 60000);
};

$("plan-copier").onclick = () => {
  if (P.sel) navigator.clipboard.writeText(P.sel.dataset.nom);
};
$("plan-trouver").onclick = trouverTrait;
$("plan-cherche").addEventListener("keydown", (e) => { if (e.key === "Enter") trouverTrait(); });

$("btn-parasolid").onclick = () => exporter("parasolid");
// Un assemblage montré seul : ses pièces, choisies ensuite, et le tout part
// en un zip qui revient en STEP exact.
$("btn-step-asm").onclick = () => $("pieces-asm").click();
$("pieces-asm").addEventListener("change", async () => {
  const asm = $("fichier").files[0];
  const pieces = [...$("pieces-asm").files];
  $("pieces-asm").value = "";
  if (!asm || !pieces.length) return;
  const dt = new DataTransfer();
  dt.items.add(await zipper([asm, ...pieces], asm.name.replace(/\.[^.]*$/, "") + ".zip"));
  $("fichier").files = dt.files;
  $("depot-nom").textContent = asm.name;
  exporter("assemblage");
});

// Copy the log without toggling it open or closed.
$("journal-copier").onclick = (e) => {
  e.preventDefault();
  const lignes = [...log.querySelectorAll("div")].filter((d) => d.children.length === 2)
    .map((d) => d.children[0].textContent.padStart(7) + "  " + d.children[1].textContent);
  navigator.clipboard.writeText(lignes.join("\n")).then(() => {
    const avant = $("journal-dernier").textContent;
    $("journal-dernier").textContent = t("copié");
    setTimeout(() => { $("journal-dernier").textContent = avant; }, 1200);
  });
};

// Full page: the same button or Esc restores the card.
function pleinePage(carte, oui) {
  carte.classList.toggle("pleine-page", oui);
  document.body.classList.toggle("pleine-page", oui);
  $("vue-plein").checked = oui;
  if (carte.id === "vue-carte" && V.gl) redessiner();
}
// One switch, in the View group: it enlarges what is shown, the 3D view or
// the drawing.
$("vue-plein").onchange = () => {
  const carte = $("plan-carte").style.display === "block" ? $("plan-carte") : $("vue-carte");
  pleinePage(carte, $("vue-plein").checked);
};
document.addEventListener("keydown", (e) => {
  const ouverte = document.querySelector(".carte.pleine-page");
  if (e.key === "Escape" && ouverte) pleinePage(ouverte, false);
});
</script>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def _send(self, code: int, kind: str, body: bytes, name: str = "") -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if name:
            from urllib.parse import quote

            ascii_name = name.encode("ascii", "replace").decode().replace("?", "_")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
            )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict) -> None:
        self._send(200, "application/json", json.dumps(payload).encode())

    def _query(self) -> dict[str, str]:
        from urllib.parse import parse_qs, unquote, urlparse

        raw = parse_qs(urlparse(self.path).query)
        return {k: unquote(v[0]) for k, v in raw.items()}

    def do_GET(self) -> None:
        route = self.path.split("?", 1)[0]
        if route == "/":
            self._send(200, "text/html; charset=utf-8", HTML.encode())
            return
        q = self._query()
        job = JOBS.get(q.get("job", ""))
        if job is None:
            self._send(404, "text/plain; charset=utf-8",
                       b"unknown, or deleted after expiry")
            return
        if route == "/journal":
            try:
                depuis = int(q.get("depuis", 0))
            except ValueError:
                self._send(400, "text/plain", b"bad request")
                return
            self._json({
                "lignes": job.journal.since(depuis),
                "fini": job.done,
                "fichier": job.result.name if job.result else "",
                "esquisses_step": job.esquisses_step.name if job.esquisses_step else "",
                "vue": job.vue is not None,
                "plan": job.plan is not None,
                "fiche": job.fiche is not None,
            })
            return
        if route == "/vue" and job.vue:
            self._send(200, "application/json", job.vue)
            return
        if route == "/plan" and job.plan:
            self._send(200, "application/json", job.plan)
            return
        if route == "/fiche" and job.fiche:
            self._send(200, "application/json", job.fiche)
            return
        if route == "/download" and q.get("quoi") == "esquisses" and job.esquisses_step:
            self._send(200, "application/octet-stream", job.esquisses_step.read_bytes(),
                       job.esquisses_step.name)
            return
        if route == "/download" and job.result:
            self._send(
                200, "application/octet-stream",
                job.result.read_bytes(), job.result.name,
            )
            return
        self._send(404, "text/plain", b"not found")

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/export":
            self._send(404, "text/plain", b"not found")
            return
        q = self._query()
        route = q.get("route", "")
        if route not in ROUTES and route not in (ROUTE_PLAN, ROUTE_ASSEMBLAGE):
            self._send(400, "text/plain", b"unknown route")
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send(400, "text/plain", b"bad request")
            return
        if size > MAX_UPLOAD:
            self._send(413, "text/plain", b"file too large (200 MB max)")
            return
        nom = safe_name(q.get("nom", ""))
        data = self.rfile.read(size)

        job = Job(nom, route)
        with JOBS_LOCK:
            JOBS[job.id] = job
        cible = {ROUTE_PLAN: voir, ROUTE_ASSEMBLAGE: voir_assemblage}.get(route, run)
        threading.Thread(target=cible, args=(job, data), daemon=True).start()
        self._json({"job": job.id})


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def adresse(argv: list[str], environ) -> tuple[str, int]:
    host = environ.get("OPENSW_HOST") or HOST
    port = int(argv[0] if argv else environ.get("PORT") or PORT)
    return host, port


def main(argv=None) -> int:
    host, port = adresse(sys.argv[1:] if argv is None else argv, os.environ)
    url = f"http://{host}:{port}"
    print(f"openSW — {url}")
    threading.Thread(target=_balayer, daemon=True).start()
    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        with Server((host, port), Handler) as server:
            if host in ("127.0.0.1", "localhost"):
                with contextlib.suppress(Exception):
                    webbrowser.open(url)
            with contextlib.suppress(KeyboardInterrupt):
                server.serve_forever()
    finally:
        purger(float("inf"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

