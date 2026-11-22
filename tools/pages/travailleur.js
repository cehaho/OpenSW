// The published page's worker: Python (Pyodide) and the converter, then
// OpenCASCADE in WebAssembly (occt-import-js) for the 3D view.
//
// The Python is the repository's, packed by tools/pages.py into opensw.zip
// and driven by tools/navigateur.py as webui.py drives it. This file only
// carries messages: journal lines, the written file, and the view.
"use strict";
const PYODIDE = "@PYODIDE@";
const OCCT = "@OCCT@";

let python = null;   // promise: { nav, version }
let occt = null;     // promise: the occt-import-js module

function préparer(dire) {
  if (python) return python;
  python = (async () => {
    const t0 = performance.now();
    importScripts(PYODIDE + "pyodide.js");
    const py = await loadPyodide({ indexURL: PYODIDE });
    const [archive, version] = await Promise.all([
      fetch("opensw.zip").then((r) => r.arrayBuffer()),
      fetch("version.txt").then((r) => (r.ok ? r.text() : "")).catch(() => ""),
    ]);
    py.unpackArchive(archive, "zip", { extractDir: "/opensw" });
    // Server modules Pyodide lacks: webui.py imports them, the page does not use them.
    py.runPython(`
import sys, types
sys.path[:0] = ["/opensw/tools", "/opensw/src"]
for nom in ("webbrowser", "signal", "http.server", "socketserver"):
    try:
        __import__(nom)
    except Exception:
        sys.modules[nom] = types.ModuleType(nom)
`);
    try {
      await py.loadPackage("micropip");
      await py.pyimport("micropip").install("olefile");   // pre-2015 .SLDPRT files
    } catch (_) { /* recent files do not need it */ }
    const nav = py.pyimport("navigateur");
    dire(`Python ${py.runPython("import sys; sys.version.split()[0]")} (Pyodide ` +
         `${py.version}) loaded in the browser in ` +
         `${((performance.now() - t0) / 1000).toFixed(1)} s`, "detail");
    return { nav, version: version.trim() };
  })();
  return python;
}

function chargerOcct() {
  if (!occt) {
    importScripts(OCCT + "occt-import-js.js");
    occt = occtimportjs({ locateFile: (f) => OCCT + f });
  }
  return occt;
}

// A face's index, read back from its colour (navigateur.code). OpenCASCADE
// reads COLOUR_RGB as sRGB and returns linear components: back to sRGB first.
const NIVEAUX = 64;
const srgb = (x) => (x <= 0.0031308 ? x * 12.92 : 1.055 * x ** (1 / 2.4) - 0.055);
function rang(c) {
  if (!c) return -1;
  for (const lu of [c.map(srgb), c]) {
    const v = lu.map((x) => x * (NIVEAUX - 1));
    const r = v.map(Math.round);
    if (v.every((x, i) => Math.abs(x - r[i]) <= 0.02))
      return (r[0] * NIVEAUX + r[1]) * NIVEAUX + r[2];
  }
  return -1;
}

// Float32 as base64, in slices: String.fromCharCode cannot take a large array.
function base64(flottants) {
  const octets = new Uint8Array(flottants.buffer);
  let binaire = "";
  for (let i = 0; i < octets.length; i += 0x8000)
    binaire += String.fromCharCode.apply(null, octets.subarray(i, i + 0x8000));
  return btoa(binaire);
}

// vue.esquisses, as webui.esquisses_en_vue writes it on the server: the
// strokes of navigateur.lancer (mm, in the model) relative to the mesh
// centre, as float32.
function esquissesEnVue(liste, centre) {
  let n = 0;
  for (const e of liste) n += e.trait.length + e.construction.length;
  const f = new Float32Array(n);
  const fiches = [];
  let k = 0;
  for (const e of liste) {
    const fiche = { nom: e.nom, "3d": e["3d"], fonction: e.fonction ?? null };
    for (const clé of ["trait", "construction"]) {
      const pts = e[clé];
      fiche[clé] = [k / 3, pts.length / 3];
      for (let i = 0; i < pts.length; i++) f[k++] = pts[i] - centre[i % 3];
    }
    fiches.push(fiche);
  }
  return { liste: fiches, sommets: base64(f) };
}

// The labelled STEP, meshed: the same JSON as tools/vue3d.py returns,
// sketches included when navigateur.lancer read some.
async function mailler(octets, table, dire, esquisses) {
  const t0 = performance.now();
  const o = await chargerOcct();
  const res = o.ReadStepFile(octets, {
    linearUnit: "millimeter", linearDeflectionType: "bounding_box_ratio",
    linearDeflection: 0.001, angularDeflection: 0.35,
  });
  if (!res.success) throw new Error("OpenCASCADE cannot read the STEP");
  let min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
  let total = 0;
  for (const m of res.meshes) {
    const p = m.attributes.position.array;
    for (let i = 0; i < p.length; i += 3)
      for (let k = 0; k < 3; k++) {
        if (p[i + k] < min[k]) min[k] = p[i + k];
        if (p[i + k] > max[k]) max[k] = p[i + k];
      }
    total += m.index.array.length / 3;
  }
  if (!total) throw new Error("the STEP has no geometry");
  const centre = min.map((v, k) => (v + max[k]) / 2);
  const sommets = new Float32Array(total * 9);
  const faces = [];
  let s = 0, inconnues = 0, corpsMax = 0;
  res.meshes.forEach((m, rangMaillage) => {
    const p = m.attributes.position.array, ix = m.index.array;
    for (const bf of m.brep_faces) {
      const k = rang(bf.color);
      const f = k >= 0 && k < table.length ? table[k] : null;
      if (!f) inconnues++;
      for (let t = bf.first; t <= bf.last; t++)
        for (let c = 0; c < 3; c++) {
          const v = ix[3 * t + c] * 3;
          for (let a = 0; a < 3; a++) sommets[s++] = p[v + a] - centre[a];
        }
      const face = { nom: f ? f.nom || "?" : "?", corps: f ? f.corps : rangMaillage + 1,
                     triangles: bf.last - bf.first + 1 };
      if (f && f.couleur) face.couleur = f.couleur;
      corpsMax = Math.max(corpsMax, face.corps);
      faces.push(face);
    }
  });
  const diagonale = Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]) || 1;
  dire(`3D view: ${faces.length} faces, ${corpsMax} bodies, in ` +
       `${((performance.now() - t0) / 1000).toFixed(1)} s`, "ok");
  if (inconnues) dire(`${inconnues} unnamed faces: their colour designates no face`, "note");
  const vue = { faces, corps: corpsMax, rayon: diagonale / 2, centre, sommets: base64(sommets) };
  if (esquisses && esquisses.length) vue.esquisses = esquissesEnVue(esquisses, centre);
  return JSON.stringify(vue);
}

onmessage = async (e) => {
  const m = e.data;
  if (m.préparer) {
    préparer(() => {}).catch(() => {});
    return;
  }
  const { id } = m;
  const t0 = performance.now();
  let n = 0;
  const dire = (text, kind) =>
    postMessage({ id, ligne: { n: n++, ms: Math.round(performance.now() - t0), text, kind } });
  try {
    if (!python) dire("loading Python in the browser…", "step");
    const { nav, version } = await préparer(dire);
    const r = nav.lancer(m.route, m.nom, m.data, (json) => {
      const l = JSON.parse(json);
      dire(l.text, l.kind);
    }, version).toJs({ dict_converter: Object.fromEntries });
    if (r.fichier) {
      const resultat = new Uint8Array(r.resultat);
      postMessage({ id, fichier: r.fichier, resultat }, [resultat.buffer]);
    }
    if (r.plan) postMessage({ id, plan: r.plan });
    if (r.fiche) postMessage({ id, fiche: r.fiche });
    if (r.vue) postMessage({ id, vue: r.vue });
    if (r.etiquete) {
      try {
        dire("3D view: loading OpenCASCADE (WebAssembly)", "step");
        const vue = await mailler(new Uint8Array(r.etiquete), JSON.parse(r.faces), dire,
                                  r.esquisses ? JSON.parse(r.esquisses) : null);
        postMessage({ id, vue });
      } catch (échec) {
        dire(`3D view failed: ${échec.message || échec}`, "note");
      }
    }
  } catch (échec) {
    dire(String(échec.message || échec), "err");
  }
  postMessage({ id, fini: true });
};
