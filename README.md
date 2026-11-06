# openSW

Convert SolidWorks part files (`.SLDPRT`) to Parasolid (`.x_t`) and STEP,
without SolidWorks.

## Install

```sh
git clone https://github.com/Emilien-Etadam/OpenSW
cd OpenSW
pip install -e ".[occ]"
```

Python 3.10+. The `occ` extra (OpenCASCADE) is only needed for the 3D view of
the web app.

## Convert

```sh
python tools/convert.py part.SLDPRT -o out/    # .x_t and .STEP
python tools/convert.py --help
```

## Web app

```sh
python tools/webui.py                          # http://127.0.0.1:8765
```

Upload a part, download the `.x_t` or STEP, and view it in 3D: click a face
to read its name, `F<node>`. Uploads are deleted after conversion, results
after 30 minutes. `PORT` and `OPENSW_HOST` set the address; on a public
server, put an authenticating reverse proxy in front.

## In the browser, without a server

```sh
python tools/pages.py                          # writes _site/
```

`_site/` is the same page, served by any static host (GitHub Pages): the
converter runs in the browser under [Pyodide](https://pyodide.org), and the
3D view uses [occt-import-js](https://github.com/kovacsv/occt-import-js)
(OpenCASCADE in WebAssembly). The file never leaves the browser. The STEP is
the same as the desktop one except for the last digits (WebAssembly's
`sin`/`cos`) and LF line endings.

## Assemblies

The web app shows an assembly (`.SLDASM`) in 3D from the display mesh
SolidWorks stores in it: components, placements, hidden and suppressed
states. Not the exact geometry, which stays in the parts.

```python
from sldprt.assembly import read_assembly, to_view

asm = read_assembly("assembly.SLDASM")
```

## Drawings

The web app also shows a SolidWorks drawing (`.SLDDRW`) as SVG, sheet by
sheet: lines, arcs, texts (fonts, sizes, bold, colours, Ø ± ° symbols), table
cells and model views. Click a stroke to read its name. Nothing is
recomputed: the strokes are those SolidWorks wrote in the file.

```python
from sldprt.drawing import read_drawing, to_svg

drawing = read_drawing("plan.SLDDRW")
svg = to_svg(drawing.active)
```

Not shown yet: centre marks, hatches, images, sketches drawn inside a view.

## Parasolid schemas

Not included: they are Siemens files. Built-in facts cover SolidWorks parts.
If one is asked for, copy `pschema` and `schema_18` from your SolidWorks
installation into `schemas/`.

## Legal

Interoperability work on files the author owns. This repository contains no
SolidWorks or Parasolid code. AGPL-3.0-only, see [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE).
