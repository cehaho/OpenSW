#!/usr/bin/env python3
"""Local web page to convert a part to Parasolid or STEP."""

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
from pathlib import Path

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
<title>openSW: SLDPRT to Parasolid and STEP</title>
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
  h1 { font-size: 20px; margin: 0 0 4px; font-weight: 600; }
  p.sous { margin: 0 0 20px; color: var(--doux); font-size: 13px; }
  .carte {
    background: var(--carte); border: 1px solid var(--trait);
    border-radius: 10px; padding: 16px; margin-bottom: 16px;
  }
  input[type=file] { width: 100%; color: var(--doux); font-size: 14px; }
  input[type=file]::file-selector-button {
    background: #262b34; color: var(--texte); border: 1px solid var(--trait);
    border-radius: 7px; padding: 8px 14px; margin-right: 12px; cursor: pointer;
    font: inherit;
  }
  .boutons { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
  button {
    flex: 1 1 200px; padding: 11px 16px; border-radius: 8px; cursor: pointer;
    border: 1px solid var(--trait); background: #262b34; color: var(--texte);
    font: 600 14px/1 inherit;
  }
  button:hover:not(:disabled) { background: #2f3540; }
  button:disabled { opacity: .45; cursor: default; }
  #log {
    font: 12.5px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace;
    height: 340px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
  }
  #log div { display: flex; gap: 10px; }
  #log .ms { color: #5d646f; flex: 0 0 62px; text-align: right; }
  .ok   { color: var(--ok); }
  .err  { color: var(--err); }
  .step { color: var(--step); }
  .note { color: var(--note); }
  .vide { color: var(--doux); }
  #vue-carte { display: none; }
  #vue {
    display: block; width: 100%; height: 520px; border-radius: 8px;
    background: #101216; cursor: grab; touch-action: none;
  }
  #vue:active { cursor: grabbing; }
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
  a.telecharger {
    display: inline-block; margin-top: 12px; padding: 10px 16px;
    background: #1f3a29; border: 1px solid #2f6b43; border-radius: 8px;
    color: #a8e6bb; text-decoration: none; font-weight: 600;
  }
</style>
<main>
  <h1>openSW</h1>
  <p class="sous">SolidWorks part to Parasolid or STEP.</p>

  <div class="carte">
    <input type="file" id="fichier"
           accept=".SLDPRT,.sldprt,.x_t,.X_T,.x_b,.X_B,.xmt_txt,.xmt_bin">
    <div class="boutons">
      <button id="btn-parasolid" disabled>Export to Parasolid (.x_t)</button>
      <button id="btn-step" disabled>Export to STEP</button>
    </div>
    <div id="lien"></div>
  </div>

  <div class="carte" id="vue-carte">
    <canvas id="vue"></canvas>
    <div class="vue-barre">
      <span id="vue-info" class="vide">Click a face to read its name.
      Drag: rotate · right-click or Shift: pan · wheel: zoom.</span>
      <button id="vue-copier" disabled>Copy name</button>
      <input id="vue-cherche" placeholder="F123…" spellcheck="false">
      <button id="vue-trouver">Find</button>
      <button id="vue-recadrer">Reset view</button>
    </div>
  </div>

  <div class="carte">
    <div id="log"><div class="vide">The log will appear here.</div></div>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
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
  log.scrollTop = log.scrollHeight;
}

function actifs(oui) {
  const prêt = oui && $("fichier").files.length;
  $("btn-parasolid").disabled = !prêt;
  $("btn-step").disabled = !prêt;
}

$("fichier").addEventListener("change", () => {
  actifs(true);
  $("lien").innerHTML = "";
});

async function exporter(route) {
  const f = $("fichier").files[0];
  if (!f) return;
  actifs(false);
  $("lien").innerHTML = "";
  log.innerHTML = ""; vide = true;

  $("vue-carte").style.display = "none";
  const r = await fetch("/export?route=" + route +
                        "&nom=" + encodeURIComponent(f.name),
                       { method: "POST", body: await f.arrayBuffer() });
  if (!r.ok) {
    ligne({ ms: 0, text: await r.text(), kind: "err" });
    actifs(true);
    return;
  }
  const { job } = await r.json();

  let depuis = 0;
  while (true) {
    const état = await (await fetch(`/journal?job=${job}&depuis=${depuis}`)).json();
    état.lignes.forEach(ligne);
    depuis += état.lignes.length;
    if (état.fichier && !$("lien").firstChild) {
      const a = document.createElement("a");
      a.className = "telecharger";
      a.href = `/download?job=${job}`;
      a.textContent = `Download ${état.fichier}`;
      $("lien").append(a);
    }
    if (état.fini) {
      if (état.vue) montrer(await (await fetch(`/vue?job=${job}`)).json());
      break;
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  actifs(true);
}

const SOMMET = `
attribute vec3 aPos; attribute vec3 aNor; attribute float aFace; attribute float aCorps;
uniform mat4 uMVP; uniform float uSel;
varying vec3 vNor; varying float vFace; varying float vCorps; varying float vSel;
void main() {
  gl_Position = uMVP * vec4(aPos, 1.0);
  vNor = aNor; vFace = aFace; vCorps = aCorps;
  vSel = abs(aFace - uSel) < 0.5 ? 1.0 : 0.0;
}`;
const COULEUR = `
precision mediump float;
uniform vec3 uLum;
varying vec3 vNor; varying float vFace; varying float vCorps; varying float vSel;
void main() {
  float l = 0.35 + 0.65 * abs(dot(normalize(vNor), uLum));
  vec3 teinte = 0.62 + 0.16 * cos(6.2831 * (vec3(0.0, 0.33, 0.67) + vCorps * 0.618));
  vec3 c = vSel > 0.5 ? vec3(0.95, 0.55, 0.22) : teinte;
  gl_FragColor = vec4(c * l, 1.0);
}`;
const RANG = `
precision highp float;
varying vec3 vNor; varying float vFace; varying float vCorps; varying float vSel;
void main() {
  float i = floor(vFace + 0.5) + 1.0;
  gl_FragColor = vec4(mod(i, 256.0), mod(floor(i / 256.0), 256.0),
                      floor(i / 65536.0), 255.0) / 255.0;
}`;

const V = { gl: null };

function programme(gl, sommet, fragment) {
  const p = gl.createProgram();
  for (const [genre, source] of [[gl.VERTEX_SHADER, sommet], [gl.FRAGMENT_SHADER, fragment]]) {
    const sh = gl.createShader(genre);
    gl.shaderSource(sh, source);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw gl.getShaderInfoLog(sh);
    gl.attachShader(p, sh);
  }
  ["aPos", "aNor", "aFace", "aCorps"].forEach((nom, i) => gl.bindAttribLocation(p, i, nom));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw gl.getProgramInfoLog(p);
  return p;
}

function tampon(gl, i, données, taille) {
  const b = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, b);
  gl.bufferData(gl.ARRAY_BUFFER, données, gl.STATIC_DRAW);
  gl.enableVertexAttribArray(i);
  gl.vertexAttribPointer(i, taille, gl.FLOAT, false, 0, 0);
}

function montrer(vue) {
  $("vue-carte").style.display = "block";
  const canvas = $("vue");
  if (!V.gl) {
    V.gl = canvas.getContext("webgl", { antialias: true });
    if (!V.gl) {
      $("vue-info").textContent = "This browser has no WebGL: no 3D view.";
      return;
    }
    const gl = V.gl;
    V.dessin = programme(gl, SOMMET, COULEUR);
    V.rang = programme(gl, SOMMET, RANG);
    gl.enable(gl.DEPTH_TEST);
    brancher(canvas);
  }
  const gl = V.gl;
  const brut = atob(vue.sommets);
  const octets = new Uint8Array(brut.length);
  for (let i = 0; i < brut.length; i++) octets[i] = brut.charCodeAt(i);
  const pos = new Float32Array(octets.buffer);
  const n = pos.length / 3;
  const nor = new Float32Array(pos.length);
  const face = new Float32Array(n), corps = new Float32Array(n);
  V.faces = vue.faces;
  V.boites = [];
  let k = 0;
  vue.faces.forEach((f, rang) => {
    const b = [Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity];
    for (let t = 0; t < f.triangles; t++, k += 3) {
      const a = 3 * k;
      const ux = pos[a + 3] - pos[a], uy = pos[a + 4] - pos[a + 1], uz = pos[a + 5] - pos[a + 2];
      const vx = pos[a + 6] - pos[a], vy = pos[a + 7] - pos[a + 1], vz = pos[a + 8] - pos[a + 2];
      let nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
      const l = Math.hypot(nx, ny, nz) || 1;
      nx /= l; ny /= l; nz /= l;
      for (let j = 0; j < 3; j++) {
        const s = a + 3 * j;
        nor[s] = nx; nor[s + 1] = ny; nor[s + 2] = nz;
        face[k + j] = rang; corps[k + j] = f.corps;
        for (let c = 0; c < 3; c++) {
          b[c] = Math.min(b[c], pos[s + c]);
          b[c + 3] = Math.max(b[c + 3], pos[s + c]);
        }
      }
    }
    V.boites.push(b);
  });
  tampon(gl, 0, pos, 3);
  tampon(gl, 1, nor, 3);
  tampon(gl, 2, face, 1);
  tampon(gl, 3, corps, 1);
  V.n = n;
  V.rayon = vue.rayon || 1;
  V.sel = -1;
  recadrer();
  $("vue-info").textContent = `${vue.faces.length} faces, ${vue.corps} bodies. ` +
    "Click a face to read its name.";
  $("vue-copier").disabled = true;
}

function recadrer() {
  V.cible = [0, 0, 0];
  V.dist = 2.6 * V.rayon;
  V.lacet = 0.6; V.tangage = 0.45;
  redessiner();
}

function perspective(fov, aspect, près, loin) {
  const f = 1 / Math.tan(fov / 2), d = près - loin;
  return [f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (loin + près) / d, -1,
          0, 0, 2 * loin * près / d, 0];
}
function produit(a, b) {
  const m = new Array(16).fill(0);
  for (let c = 0; c < 4; c++)
    for (let r = 0; r < 4; r++)
      for (let k = 0; k < 4; k++) m[4 * c + r] += a[4 * k + r] * b[4 * c + k];
  return m;
}
function repère() {
  const cp = Math.cos(V.tangage), sp = Math.sin(V.tangage);
  const arrière = [cp * Math.sin(V.lacet), sp, cp * Math.cos(V.lacet)];
  const droite = [Math.cos(V.lacet), 0, -Math.sin(V.lacet)];
  const haut = [
    arrière[1] * droite[2] - arrière[2] * droite[1],
    arrière[2] * droite[0] - arrière[0] * droite[2],
    arrière[0] * droite[1] - arrière[1] * droite[0],
  ];
  return { arrière, droite, haut };
}
function matrice() {
  const { arrière, droite, haut } = repère();
  const œil = V.cible.map((c, i) => c + V.dist * arrière[i]);
  const pt = (u) => -(u[0] * œil[0] + u[1] * œil[1] + u[2] * œil[2]);
  const vueM = [droite[0], haut[0], arrière[0], 0, droite[1], haut[1], arrière[1], 0,
                droite[2], haut[2], arrière[2], 0, pt(droite), pt(haut), pt(arrière), 1];
  const c = $("vue");
  const près = Math.max(V.dist - 2 * V.rayon, V.dist * 1e-3);
  return { mvp: produit(perspective(0.7, c.width / c.height, près, V.dist + 2 * V.rayon), vueM),
           lum: arrière };
}

function dessiner(prog) {
  const gl = V.gl, c = $("vue");
  const { mvp, lum } = matrice();
  gl.viewport(0, 0, c.width, c.height);
  gl.useProgram(prog);
  gl.uniformMatrix4fv(gl.getUniformLocation(prog, "uMVP"), false, new Float32Array(mvp));
  gl.uniform1f(gl.getUniformLocation(prog, "uSel"), V.sel);
  const l = gl.getUniformLocation(prog, "uLum");
  if (l) gl.uniform3fv(l, lum);
  gl.drawArrays(gl.TRIANGLES, 0, V.n);
}

function redessiner() {
  if (V.attente) return;
  V.attente = requestAnimationFrame(() => {
    V.attente = 0;
    const c = $("vue"), r = window.devicePixelRatio || 1;
    const l = Math.round(c.clientWidth * r), h = Math.round(c.clientHeight * r);
    if (c.width !== l || c.height !== h) { c.width = l; c.height = h; }
    const gl = V.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.clearColor(0.063, 0.071, 0.086, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    dessiner(V.dessin);
  });
}

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
  gl.clearColor(0, 0, 0, 0);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  dessiner(V.rang);
  const px = new Uint8Array(4);
  gl.readPixels(x, c.height - 1 - y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
  gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  return px[0] + 256 * px[1] + 65536 * px[2] - 1;
}

function choisir(rang) {
  V.sel = rang;
  const info = $("vue-info");
  if (rang < 0) {
    info.textContent = "No face under the pointer.";
    $("vue-copier").disabled = true;
  } else {
    const f = V.faces[rang];
    info.innerHTML = "";
    const b = document.createElement("b");
    b.textContent = f.nom;
    info.append(b, ` · body ${f.corps} · ${f.triangles} triangles`);
    $("vue-copier").disabled = f.nom === "?";
  }
  redessiner();
}

function brancher(c) {
  let glisse = null;
  c.addEventListener("contextmenu", (e) => e.preventDefault());
  c.addEventListener("pointerdown", (e) => {
    c.setPointerCapture(e.pointerId);
    glisse = { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY,
               déplace: e.button === 2 || e.shiftKey };
  });
  c.addEventListener("pointermove", (e) => {
    if (!glisse) return;
    const dx = e.clientX - glisse.x, dy = e.clientY - glisse.y;
    glisse.x = e.clientX; glisse.y = e.clientY;
    if (glisse.déplace) {
      const { droite, haut } = repère();
      const k = 2 * V.dist * Math.tan(0.35) / c.clientHeight;
      V.cible = V.cible.map((v, i) => v - k * (dx * droite[i] - dy * haut[i]));
    } else {
      V.lacet -= dx * 0.008;
      V.tangage = Math.max(-1.55, Math.min(1.55, V.tangage + dy * 0.008));
    }
    redessiner();
  });
  c.addEventListener("pointerup", (e) => {
    if (glisse && Math.hypot(e.clientX - glisse.x0, e.clientY - glisse.y0) < 4) {
      const r = c.getBoundingClientRect(), d = c.width / r.width;
      choisir(viser(Math.floor((e.clientX - r.left) * d), Math.floor((e.clientY - r.top) * d)));
    }
    glisse = null;
  });
  c.addEventListener("wheel", (e) => {
    e.preventDefault();
    const f = Math.exp(e.deltaY * 0.0015);
    const r = c.getBoundingClientRect();
    const nx = (e.clientX - r.left) / r.width * 2 - 1, ny = 1 - (e.clientY - r.top) / r.height * 2;
    const { droite, haut } = repère();
    const t = Math.tan(0.35) * V.dist, a = r.width / r.height;
    V.cible = V.cible.map((v, i) => v + (1 - f) * t * (nx * a * droite[i] + ny * haut[i]));
    V.dist *= f;
    redessiner();
  }, { passive: false });
  new ResizeObserver(redessiner).observe(c);
}

$("vue-recadrer").onclick = () => V.gl && recadrer();
$("vue-copier").onclick = () => {
  if (V.sel >= 0) navigator.clipboard.writeText(V.faces[V.sel].nom);
};
function trouver() {
  if (!V.faces) return;
  let nom = $("vue-cherche").value.trim().toUpperCase();
  if (!nom) return;
  if (!nom.startsWith("F")) nom = "F" + nom;
  const rang = V.faces.findIndex((f) => f.nom === nom);
  if (rang < 0) { $("vue-info").textContent = `${nom}: not in this part.`; return; }
  const b = V.boites[rang];
  if (b[0] <= b[3]) {
    V.cible = [0, 1, 2].map((i) => (b[i] + b[i + 3]) / 2);
    V.dist = Math.max(4 * Math.hypot(b[3] - b[0], b[4] - b[1], b[5] - b[2]), V.rayon * 1e-3);
  }
  choisir(rang);
}
$("vue-trouver").onclick = trouver;
$("vue-cherche").addEventListener("keydown", (e) => { if (e.key === "Enter") trouver(); });

$("btn-parasolid").onclick = () => exporter("parasolid");
$("btn-step").onclick = () => exporter("step");
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
            })
            return
        if route == "/vue" and job.vue:
            self._send(200, "application/json", job.vue)
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
        if route not in ROUTES:
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
        threading.Thread(target=run, args=(job, data), daemon=True).start()
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

