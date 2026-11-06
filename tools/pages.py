#!/usr/bin/env python3
"""Build the published page: the web app without a server, for GitHub Pages.

    python tools/pages.py            # writes _site/
    python tools/pages.py -o folder

The folder is served as is by any static host:

- ``index.html``: the page of ``webui.py``, unchanged, followed by
  ``statique.js``, which answers the server's addresses in its place;
- ``travailleur.js``: a Web Worker running Python (Pyodide) and the
  converter, then meshing the 3D view with OpenCASCADE in WebAssembly
  (``occt-import-js``);
- ``opensw.zip``: ``src/sldprt`` and the ``tools/`` modules the page calls,
  without Parasolid schemas (built-in facts cover SolidWorks parts);
- ``version.txt``: the commit it all comes from.

The uploaded file never leaves the browser. Pyodide and occt-import-js come
from jsDelivr, at the versions pinned here.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "tools"))

import webui  # noqa: E402

PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.29.5/full/"
OCCT = "https://cdn.jsdelivr.net/npm/occt-import-js@0.0.23/dist/"

OUTILS = ("webui.py", "convert.py", "vue3d.py", "navigateur.py")


def archive(chemin: Path) -> int:
    """Write ``opensw.zip``; return the number of files packed."""
    n = 0
    with zipfile.ZipFile(chemin, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted((RACINE / "src" / "sldprt").rglob("*.py")):
            z.write(f, f.relative_to(RACINE).as_posix())
            n += 1
        for nom in OUTILS:
            z.write(RACINE / "tools" / nom, f"tools/{nom}")
            n += 1
    return n


def construire(dest: Path) -> list[Path]:
    """Write the published page into *dest*; return the files written."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    pages = RACINE / "tools" / "pages"
    if webui.HTML.count("</html>") != 1:
        raise RuntimeError("the webui.py page must have exactly one </html>")
    html = webui.HTML.replace(
        "</html>", '<script src="statique.js"></script>\n</html>')
    (dest / "index.html").write_text(html, encoding="utf-8", newline="\n")
    shutil.copyfile(pages / "statique.js", dest / "statique.js")
    travailleur = (pages / "travailleur.js").read_text(encoding="utf-8")
    (dest / "travailleur.js").write_text(
        travailleur.replace("@PYODIDE@", PYODIDE).replace("@OCCT@", OCCT),
        encoding="utf-8", newline="\n")
    archive(dest / "opensw.zip")
    (dest / "version.txt").write_text(webui._commit() + " (published page)\n",
                                      encoding="utf-8", newline="\n")
    # Without it, GitHub Pages runs Jekyll, which drops files starting with _.
    (dest / ".nojekyll").write_bytes(b"")
    return sorted(p for p in dest.iterdir())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--sortie", type=Path, default=RACINE / "_site")
    args = parser.parse_args(argv)
    for f in construire(args.sortie):
        print(f"{f.stat().st_size / 1e3:9.0f} kB  {f.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
