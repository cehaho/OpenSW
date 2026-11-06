// The published page without a server: this script stands in for webui.py.
//
// The page is the server's own, and talks to it the same way: POST /export,
// then /journal, /vue, /plan and /download. Here those addresses never reach
// the network: a Web Worker (travailleur.js) running the same Python under
// Pyodide answers them. The uploaded file never leaves the machine.
"use strict";
(() => {
  const travailleur = new Worker("travailleur.js");
  const travaux = new Map();
  let suivant = 0;

  travailleur.onmessage = (e) => {
    const m = e.data;
    const j = travaux.get(m.id);
    if (!j) return;
    if (m.ligne) j.lignes.push(m.ligne);
    if (m.fichier) { j.fichier = m.fichier; j.resultat = m.resultat; }
    if (m.vue) j.vue = m.vue;
    if (m.plan) j.plan = m.plan;
    if (m.fini) j.fini = true;
  };
  travailleur.onerror = (e) => {
    for (const j of travaux.values()) {
      if (j.fini) continue;
      j.lignes.push({ n: j.lignes.length, ms: Math.round(performance.now() - j.début),
                      kind: "err", text: String(e.message || e) });
      j.fini = true;
    }
  };

  // POST /export: the file goes to the worker, not the network.
  envoyer = async (url, corps) => {
    const q = new URL(url, location.href).searchParams;
    const id = "j" + (++suivant);
    travaux.set(id, { lignes: [], fini: false, fichier: "", resultat: null,
                      vue: null, plan: null, début: performance.now() });
    const data = new Uint8Array(await corps.arrayBuffer());
    travailleur.postMessage({ id, route: q.get("route"), nom: q.get("nom"), data },
                            [data.buffer]);
    return { job: id };
  };

  const json = (objet) => new Response(JSON.stringify(objet),
                                       { headers: { "Content-Type": "application/json" } });
  const fetchDuReseau = window.fetch.bind(window);
  window.fetch = async (url, options) => {
    const u = new URL(url, location.href);
    const j = u.origin === location.origin && travaux.get(u.searchParams.get("job"));
    if (!j) return fetchDuReseau(url, options);
    if (u.pathname.endsWith("/journal")) {
      const depuis = Number(u.searchParams.get("depuis") || 0);
      return json({ lignes: j.lignes.slice(depuis), fini: j.fini, fichier: j.fichier,
                    vue: j.vue !== null, plan: j.plan !== null });
    }
    if (u.pathname.endsWith("/vue") && j.vue) return new Response(j.vue);
    if (u.pathname.endsWith("/plan") && j.plan) return new Response(j.plan);
    return new Response("not found", { status: 404 });
  };

  // /download: the file is already here, in the page's memory.
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("a.telecharger");
    if (!a) return;
    const j = travaux.get(a.dataset.job);
    if (!j || !j.resultat) return;
    e.preventDefault();
    const lien = document.createElement("a");
    lien.href = URL.createObjectURL(new Blob([j.resultat], { type: "application/octet-stream" }));
    lien.download = j.fichier;
    document.body.append(lien);
    lien.click();
    lien.remove();
    setTimeout(() => URL.revokeObjectURL(lien.href), 60000);
  });

  document.getElementById("local").hidden = false;

  // Load the worker right away: Python takes a few seconds.
  travailleur.postMessage({ préparer: true });
})();
