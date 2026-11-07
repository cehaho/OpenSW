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
        self.plan: bytes | None = None


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


def effacer_source(job: Job) -> None:
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
        source = job.source = dossier / job.name
        source.write_bytes(data)
        say(f"{job.name} received, {_human(len(data))}", "ok")
        say(f"reader: {version()}", "detail")
        say("drawing: sldprt.drawing.read_drawing()", "step")
        début = time.monotonic()
        from sldprt.drawing import read_drawing, to_dxf

        dessin = read_drawing(source)
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
    for nom, n in rapport.missing.items():
        say(f"part not supplied: {nom} ({n} instance(s)), left out", "note")
    job.result = sortie
    say(f"{sortie.name} ready to download, exact STEP, {rapport.instances} "
        f"instance(s) of {len(rapport.parts)} part(s), {_human(sortie.stat().st_size)}",
        "ok")


def voir_assemblage(job: Job, data: bytes) -> None:
    """Read an assembly and keep its 3D view in memory for the page.

    Alone, it gives its display mesh as STL. Uploaded with its parts (a zip,
    or several files the page zips together), it gives an exact STEP. What
    is uploaded is deleted as soon as it is read; the export stays until
    the purge.
    """
    from sldprt.assembly import read_assembly, to_stl, to_view

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
        for motif, n in asm.non_lus.items():
            say(f"not shown: {motif}, {n}", "note")
        vue = to_view(asm)
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
    job.vue = json.dumps(vue).encode()


HTML = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>openSW: SLDPRT to Parasolid and STEP, SLDDRW on screen</title>
<style>
  :root {
    --fond: #15171c; --carte: #1d2027; --trait: #2c313b;
    --texte: #e6e8ec; --doux: #9aa1ad;
    --ok: #64c27b; --err: #e2686a; --step: #7aa7e8; --note: #d9a441;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px; background: var(--fond); color: var(--texte);
    font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif;
  }
  main { max-width: 860px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 18px; font-weight: 700; letter-spacing: -0.3px;
       display: flex; align-items: center; gap: 10px; }
  h1 b { color: #e1251b; }
  #logo { width: 40px; height: 40px; flex: 0 0 auto; }
  .langues { margin-left: auto; display: flex; gap: 4px; }
  .langues button { flex: 0 0 auto; padding: 5px 9px; font-size: 12px; }
  .langues button.choisi { border-color: #e1251b; color: #fff; }
  .carte {
    background: var(--carte); border: 1px solid var(--trait);
    border-radius: 10px; padding: 16px; margin-bottom: 16px;
  }
  /* Drop zone: a click opens the file picker, a dropped file is treated as
     chosen. The <input> itself is hidden inside. */
  #depot {
    display: flex; flex-direction: column; align-items: center; gap: 8px;
    padding: 26px 16px; border: 2px dashed var(--trait); border-radius: 10px;
    color: var(--doux); text-align: center; cursor: pointer;
    transition: border-color .15s, background .15s;
  }
  #depot:hover, #depot.survol { border-color: #e1251b; background: #e1251b14; }
  #depot svg { width: 34px; height: 34px; fill: none; stroke: currentColor;
               stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }
  #depot b { color: var(--texte); }
  #depot-nom { font-size: 13px; }
  #fichier { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
  #actions { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
  #lien { display: contents; }
  .action {
    display: inline-flex; align-items: center; gap: 8px; flex: 0 0 auto;
    padding: 9px 14px; border-radius: 8px; font: 600 14px/1 inherit;
    text-decoration: none; cursor: pointer;
  }
  .action svg { width: 18px; height: 18px; fill: none; stroke: currentColor;
                stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  button {
    flex: 1 1 200px; padding: 11px 16px; border-radius: 8px; cursor: pointer;
    border: 1px solid var(--trait); background: #262b34; color: var(--texte);
    font: 600 14px/1 inherit;
  }
  button:hover:not(:disabled) { background: #2f3540; }
  button:disabled { opacity: .45; cursor: default; }
  [hidden] { display: none !important; }
  /* Progress bar: measured during upload, indeterminate while the server
     works (it cannot tell how much is left). */
  #local { margin: 10px 0 0; color: var(--doux); font-size: 13px; }
  #exemples { margin: 10px 0 0; color: var(--doux); font-size: 13px;
              display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; }
  #exemples button { flex: 0 0 auto; padding: 6px 10px; font-size: 13px; }
  #charge { margin-top: 14px; }
  .charge-piste { height: 6px; border-radius: 3px; background: #262b33; overflow: hidden; }
  #charge-barre { height: 100%; width: 0; background: #e1251b; border-radius: 3px;
                  transition: width .15s; }
  #charge.attente #charge-barre { width: 30%; transition: none;
                                  animation: va-et-vient 1.3s ease-in-out infinite; }
  @keyframes va-et-vient { from { margin-left: -30%; } to { margin-left: 100%; } }
  #charge-texte { margin-top: 6px; color: var(--doux); font-size: 12px;
                  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #log {
    font: 12.5px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace;
    height: 340px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
    margin-top: 10px;
  }
  /* The log is collapsed; it opens on click, or by itself on an error. */
  #journal summary {
    display: flex; align-items: center; gap: 10px; cursor: pointer;
    list-style: none; color: var(--doux); font-size: 14px; font-weight: 600;
  }
  #journal summary::-webkit-details-marker { display: none; }
  #journal summary::before { content: "▸"; transition: transform .15s; }
  #journal[open] summary::before { transform: rotate(90deg); }
  #journal-dernier { flex: 1; min-width: 0; font-weight: 400; font-size: 12px;
                     white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .icone { flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center;
           padding: 6px 8px; }
  .icone svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 2;
               stroke-linecap: round; stroke-linejoin: round; }
  /* Full page: the card covers the browser window (not the screen). */
  .carte.pleine-page { position: fixed; inset: 0; z-index: 50; margin: 0;
                       display: flex !important; flex-direction: column;
                       border-radius: 0; padding: 12px; }
  .carte.pleine-page .vue-cadre { flex: 1 1 auto; min-height: 0; }
  .carte.pleine-page #vue, .carte.pleine-page #plan { height: 100%; }
  body.pleine-page { overflow: hidden; }
  .plein { position: absolute; top: 8px; right: 8px; z-index: 2;
           background: #262b34cc; }
  #log div { display: flex; gap: 10px; }
  #log .ms { color: #5d646f; flex: 0 0 62px; text-align: right; }
  .ok   { color: var(--ok); }
  .err  { color: var(--err); }
  .step { color: var(--step); }
  .note { color: var(--note); }
  .vide { color: var(--doux); }
  .pied { margin: 18px 0 8px; color: var(--doux); font-size: 12px; line-height: 1.6;
          text-align: center; }
  .pied a { color: var(--texte); }
  #vue-carte { display: none; }
  /* SolidWorks background: Top/Bottom Gradient Color. */
  .vue-cadre { position: relative; }
  #vue {
    display: block; width: 100%; height: 560px; border-radius: 8px;
    background: linear-gradient(#8795aa, #c3c8d2); cursor: default;
    touch-action: none; outline: none;
  }
  #vue-triedre {
    position: absolute; left: 8px; bottom: 8px; width: 76px; height: 76px;
    pointer-events: none;
  }
  .vue-vues { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
  .vue-vues button { padding: 6px 10px; font-size: 13px; }
  .vue-barre {
    display: flex; gap: 10px; align-items: center; margin-top: 10px;
    flex-wrap: wrap; font-size: 14px;
  }
  #vue-info { flex: 1 1 260px; font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
  #vue-info b { color: #f0a35e; font-size: 15px; }
  .vue-barre button { flex: 0 0 auto; padding: 8px 12px; font-weight: 500; }
  .vue-barre input {
    width: 130px; padding: 7px 10px; border-radius: 7px; font: inherit;
    background: #262b34; color: var(--texte); border: 1px solid var(--trait);
  }
  #plan-carte { display: none; }
  .onglets { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
  .onglets button { flex: 0 0 auto; padding: 7px 12px; font-weight: 500; }
  .onglets button.choisi { background: #34405a; border-color: #4d5f86; }
  #plan {
    /* Light but distinct from the paper, so that items placed off the sheet
       stay readable and the sheet border shows. */
    width: 100%; height: 560px; border-radius: 8px; background: #b9bcc2;
    overflow: hidden; cursor: grab; touch-action: none;
  }
  #plan:active { cursor: grabbing; }
  #plan svg { width: 100%; height: 100%; display: block; }
  #plan .papier { fill: #fbfbf8; }
  /* Colour and font read from the file are SVG attributes: CSS only sets
     the default, without overriding them. */
  #plan .trait { stroke: #1b1e24; }
  #plan .trait * { stroke-width: 1.2px; vector-effect: non-scaling-stroke; }
  #plan .plein:not([fill]) { fill: #1b1e24; }
  #plan text:not([fill]) { fill: #1b1e24; }
  #plan text:not([font-family]) { font-family: "Century Gothic", ui-sans-serif, sans-serif; }
  #plan .trait .choisi { stroke: #f0a35e; stroke-width: 3px; }
  #plan .plein.choisi { fill: #f0a35e; }
  #plan text.choisi { fill: #f0a35e; }
  a.telecharger { background: #1f3a29; border: 1px solid #2f6b43; color: #a8e6bb; }
  a.telecharger:hover { background: #26482f; }
</style>
<main>
  <!-- Logo: the open box. Also used as the favicon (see below), so the page
       loads no file. -->
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
  <span class="langues"><button data-langue="fr">FR</button><button data-langue="en">EN</button></span></h1>

  <div class="carte">
    <!-- `convert.step` reads a Parasolid transmit file as well as a SolidWorks
         part, so .x_t is accepted too: comparing both outputs shows which
         one loses something. -->
    <label id="depot">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4M7 9l5-5 5 5M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/></svg>
      <span><b data-t="depot">Drop a file</b> <span data-t="depot-ou">or click</span></span>
      <span id="depot-nom">.SLDPRT · .SLDASM · .SLDDRW · .x_t</span>
      <input type="file" id="fichier"
             multiple accept=".SLDPRT,.sldprt,.x_t,.X_T,.x_b,.X_B,.xmt_txt,.xmt_bin,.SLDDRW,.slddrw,.SLDASM,.sldasm,.zip,.ZIP">
    </label>
    <!-- Shown by the published page only (statique.js): the local server
         does receive the file. -->
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
    <p id="local" data-t="local" hidden>Your file is not uploaded to any server:
      it is read and converted in your browser.</p>
    <div id="charge" hidden>
      <div class="charge-piste"><div id="charge-barre"></div></div>
      <div id="charge-texte"></div>
    </div>
    <!-- A chosen file opens by itself (STEP, drawing or assembly); only the
         .x_t export, which has no view, keeps a button. -->
    <div id="actions"><button id="btn-parasolid" class="action" hidden disabled
        data-t-title="exporter-xt" title="Also export to Parasolid"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3h7v7M21 3l-9 9M19 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h5"/></svg>.x_t</button><span id="lien"></span></div>
  </div>

  <div class="carte" id="vue-carte">
    <div class="vue-cadre">
      <canvas id="vue"></canvas>
      <canvas id="vue-triedre"></canvas>
      <button class="icone plein" data-cible="vue-carte" data-t-title="plein-écran"
              title="Full page (Esc to go back)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg></button>
    </div>
    <div class="vue-vues">
      <button data-vue="face" data-t="vue-face" title="Ctrl+1">Front</button>
      <button data-vue="arrière" data-t="vue-arrière" title="Ctrl+2">Back</button>
      <button data-vue="gauche" data-t="vue-gauche" title="Ctrl+3">Left</button>
      <button data-vue="droite" data-t="vue-droite" title="Ctrl+4">Right</button>
      <button data-vue="dessus" data-t="vue-dessus" title="Ctrl+5">Top</button>
      <button data-vue="dessous" data-t="vue-dessous" title="Ctrl+6">Bottom</button>
      <button data-vue="iso" data-t="vue-iso" title="Ctrl+7">Isometric</button>
      <button id="vue-recadrer" data-t="zoom-mieux" title="F">Zoom to fit</button>
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
    <div class="vue-cadre"><div id="plan"></div>
      <button class="icone plein" data-cible="plan-carte" data-t-title="plein-écran"
              title="Full page (Esc to go back)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg></button>
    </div>
    <div class="vue-barre">
      <span id="plan-info" class="vide" data-t="aide-plan">Click a line to read its name.
      Drag: pan · wheel: zoom.</span>
      <button id="plan-copier" data-t="copier" disabled>Copy name</button>
      <input id="plan-cherche" placeholder="E123, A456…" spellcheck="false">
      <button id="plan-trouver" data-t="trouver">Find</button>
      <button id="plan-recadrer" data-t="recadrer">Fit</button>
    </div>
  </div>

  <details class="carte" id="journal">
    <summary><span data-t="journal">Log</span><span id="journal-dernier"></span>
      <button class="icone" id="journal-copier" data-t-title="copier-journal"
              title="Copy the log"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg></button></summary>
    <div id="log"><div class="vide" data-t="journal-vide">The log will appear here.</div></div>
  </details>

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
    "vue-face": "Face", "vue-arrière": "Arrière", "vue-gauche": "Gauche",
    "vue-droite": "Droite", "vue-dessus": "Dessus", "vue-dessous": "Dessous",
    "vue-iso": "Isométrique", "zoom-mieux": "Zoom au mieux",
    "aide-vue": "Clic : choisir une face et lire son nom. Bouton du milieu : " +
      "tourner · Ctrl+milieu : déplacer · Maj+milieu ou molette : zoomer · " +
      "Alt+milieu : pivoter · double clic milieu ou F : zoom au mieux · " +
      "flèches : 15°, Maj : 90°.",
    "aide-plan": "Cliquez sur un trait pour lire son nom. Glisser : déplacer · molette : zoomer.",
    copier: "Copier le nom", trouver: "Trouver", recadrer: "Recadrer",
    "svg-feuille": "Télécharger la feuille affichée en SVG",
    "journal-vide": "Le journal s'écrira ici.",
    journal: "Journal", "copier-journal": "Copier le journal", copié: "copié",
    "plein-écran": "Pleine page (Échap pour revenir)",
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
    plusieurs: "Plusieurs fichiers : déposez un assemblage (.SLDASM) avec ses pièces.",
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
    "vue-face": "Front", "vue-arrière": "Back", "vue-gauche": "Left",
    "vue-droite": "Right", "vue-dessus": "Top", "vue-dessous": "Bottom",
    "vue-iso": "Isometric", "zoom-mieux": "Zoom to fit",
    "aide-vue": "Click: select a face and read its name. Middle button: rotate · " +
      "Ctrl+middle: pan · Shift+middle or wheel: zoom · Alt+middle: roll · " +
      "middle double-click or F: zoom to fit · arrows: 15°, Shift: 90°.",
    "aide-plan": "Click a line to read its name. Drag: pan · wheel: zoom.",
    copier: "Copy name", trouver: "Find", recadrer: "Fit",
    "svg-feuille": "Download the shown sheet as SVG",
    "journal-vide": "The log will appear here.",
    journal: "Log", "copier-journal": "Copy the log", copié: "copied",
    "plein-écran": "Full page (Esc to go back)",
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
    plusieurs: "Several files: drop an assembly (.SLDASM) together with its parts.",
    corps: "body",
    genres: { line: "segment", circle: "circle", path: "arc", polygon: "polygon",
              polyline: "edge", text: "text" },
  },
};

let LANGUE = "en";
try { LANGUE = localStorage.getItem("opensw-langue") || ""; } catch (e) { LANGUE = ""; }
if (!TEXTES[LANGUE]) LANGUE = "en";

function t(clé, ...args) {
  const v = TEXTES[LANGUE][clé] ?? TEXTES.en[clé];
  return typeof v === "function" ? v(...args) : v;
}

function traduire() {
  document.documentElement.lang = LANGUE;
  document.title = t("titre");
  document.querySelectorAll("[data-t]").forEach((e) => { e.textContent = t(e.dataset.t); });
  document.querySelectorAll("[data-t-title]").forEach((e) => { e.title = t(e.dataset.tTitle); });
  document.querySelectorAll("[data-langue]").forEach((b) => {
    b.classList.toggle("choisi", b.dataset.langue === LANGUE);
  });
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
  const plan = prêt && /\.slddrw$/i.test(f.name);
  const asm = prêt && /\.sldasm$/i.test(f.name);
  $("btn-parasolid").hidden = !f || plan || asm;
  $("btn-parasolid").disabled = !prêt || plan || asm;
}

// A chosen file opens at once: a part goes through STEP (the view reads the
// written file back); drawings and assemblies are shown as they are.
// Several files: an assembly and its parts, zipped together (stored, no
// compression) before upload; the server turns them into an exact STEP.
$("fichier").addEventListener("change", async () => {
  actifs(true);
  $("lien").innerHTML = "";
  let f = $("fichier").files[0];
  if (!f) return;
  if ($("fichier").files.length > 1) {
    const tous = [...$("fichier").files];
    const asm = tous.find((x) => /\.sldasm$/i.test(x.name));
    if (!asm) {
      ligne({ ms: 0, kind: "err", text: t("plusieurs") });
      return;
    }
    f = await zipper(tous, asm.name.replace(/\.[^.]*$/, "") + ".zip");
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
    $("vue-carte").style.display = "none";
    $("plan-carte").style.display = "none";
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
        $("lien").append(a);
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
    trièdre();
  });
}

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
      choisir(sous(c, e));
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

$("vue-recadrer").onclick = () => V.gl && ajuster();
document.querySelectorAll("[data-vue]").forEach((b) => {
  b.onclick = () => V.gl && orienter(b.dataset.vue);
});
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

function planche(plan) {
  P.feuilles = plan.feuilles;
  $("plan-carte").style.display = "block";
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
  if (carte.id === "vue-carte" && V.gl) redessiner();
}
document.querySelectorAll(".plein").forEach((b) => {
  b.onclick = () => {
    const carte = $(b.dataset.cible);
    pleinePage(carte, !carte.classList.contains("pleine-page"));
  };
});
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
                "vue": job.vue is not None,
                "plan": job.plan is not None,
            })
            return
        if route == "/vue" and job.vue:
            self._send(200, "application/json", job.vue)
            return
        if route == "/plan" and job.plan:
            self._send(200, "application/json", job.plan)
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

