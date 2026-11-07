# openSW

Convert SolidWorks part files (`.SLDPRT`) to Parasolid (`.x_t`) and STEP,
without SolidWorks. View parts, assemblies (`.SLDASM`) and drawings
(`.SLDDRW`) in a web page.

**Try it online: <https://cehaho.github.io/OpenSW/>** — your file is not
uploaded to any server: it is read and converted in your browser.

## What it reads, and what it doesn't

openSW shows what the file holds, as SolidWorks stored it. It recomputes
nothing: no regeneration, no retessellation, no mass computed from geometry.
It is a file reader for interoperability, not a CAD kernel or an API.

| | read | not read |
|---|---|---|
| **Part** | the exact solid of the saved configuration (Parasolid B-rep → `.x_t`, STEP), colours, face names | the feature tree (sketches, features, equations, history): what you get is the final solid, not how it was built |
| | properties: summary, custom and per-configuration properties, configuration names, material and density, preview image | geometry of configurations that were not saved with their geometry (the page says which ones were) |
| | mass, volume, area, centre of mass and inertia at the centre of mass, **when SolidWorks stored them** (it does when a property links to them) | mass properties SolidWorks did not store |
| **Assembly** | components and a bill of materials by part file (→ CSV), placements, hidden and suppressed states, the display mesh (→ STL); with its part files, an exact STEP assembly | mates; configurations; the exact geometry, which is in the parts |
| **Drawing** | sheets, lines, arcs, texts (fonts, colours), tables, model view edges, dimensions as drawn (→ SVG, DXF) | dimension values as data (tolerances, types, the edges they measure); title blocks as fields; hatches, images, centre marks, the sheet format |

What is not read is counted in the log rather than hidden.

## Install

```sh
git clone https://github.com/cehaho/OpenSW
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

Drop a part, download the `.x_t` or STEP, and view it in 3D: click a face
to read its name, `F<node>`. The page is in English, with a French option.
Here the file is sent to this local server; uploads are deleted after conversion, results
after 30 minutes. `PORT` and `OPENSW_HOST` set the address; on a public
server, put an authenticating reverse proxy in front.

## In the browser, without a server

This is the page published at <https://cehaho.github.io/OpenSW/>. To build
it yourself:

```sh
python tools/pages.py                          # writes _site/
```

`_site/` is the same page, served by any static host (GitHub Pages): the
converter runs in the browser under [Pyodide](https://pyodide.org), and the
3D view uses [occt-import-js](https://github.com/kovacsv/occt-import-js)
(OpenCASCADE in WebAssembly). The file never leaves the browser: nothing is
uploaded, only the page and its libraries are downloaded. The STEP is
the same as the desktop one except for the last digits (WebAssembly's
`sin`/`cos`) and LF line endings.

## Assemblies

The web app shows an assembly (`.SLDASM`) in 3D from the display mesh
SolidWorks stores in it: components, placements, hidden and suppressed
states. Not the exact geometry, which stays in the parts. It downloads as
STL (binary, millimetres, components placed). The file properties list its
components with a count per part file, and the list downloads as CSV.

```python
from sldprt.assembly import read_assembly, to_stl, to_view

asm = read_assembly("assembly.SLDASM")
open("assembly.stl", "wb").write(to_stl(asm))
```

Drop the assembly together with its part files (several files at once, or a
zip) and it downloads as an exact STEP assembly instead: each part written
once from its own geometry, each instance placed as the assembly says.
Sub-assemblies are flattened; a part that is not supplied is left out and
named in the log. On five test assemblies, every solid has the same volume
and centre of mass as in the STEP SolidWorks exports.

```python
from sldprt.load import load_model
from sldprt.step_assembly import write_step_assembly

parts = {name: load_model(name) for name in ("cube.SLDPRT", "cylinder.SLDPRT")}
report = write_step_assembly(read_assembly("assembly.SLDASM"), parts, "assembly.STEP")
print(report.instances, report.missing)
```

## Drawings

The web app also shows a SolidWorks drawing (`.SLDDRW`) as SVG, sheet by
sheet: lines, arcs, texts (fonts, sizes, bold, colours, Ø ± ° symbols), table
cells and model views. Click a stroke to read its name. Nothing is
recomputed: the strokes are those SolidWorks wrote in the file. It downloads
as DXF R12 in millimetres (a zip of one DXF per sheet when there are
several), and each sheet as SVG. In the DXF, colours are rounded to the
nearest plain AutoCAD colour and filled arrowheads become outlines.

```python
from sldprt.drawing import read_drawing, to_dxf, to_svg

drawing = read_drawing("plan.SLDDRW")
svg = to_svg(drawing.active)
open("plan.dxf", "w", encoding="cp1252").write(to_dxf(drawing.active))
```

Drawings from older SolidWorks versions are read too, dimensions included.
Not shown yet: centre marks, images, and the edges of projected views that
store no rotation of their own.

## File properties

For any part, assembly or drawing, the web app shows what the file says
about itself, read as stored, nothing computed: summary (title, author,
keywords…), created and saved dates and by whom, custom properties (with the
expression of a linked one), and for a part each configuration with its own
properties, its material and density, and whether its geometry was saved.
Mass, volume, centre of mass and inertia appear only when SolidWorks wrote them, which
it does when a custom property links to them. Each value was checked against
what the SolidWorks API reads back, on parts made for that purpose.

```python
from sldprt import open_container
from sldprt.properties import read_properties

info = read_properties(open_container("part.SLDPRT"))
print(info["resume"], [c["nom"] for c in info["configurations"]])
```

These properties can say more than you think (the folder the file was saved
from, who saved it): worth a look before sharing a part.

## Parasolid schemas

Not included: they are Siemens files. Built-in facts cover SolidWorks parts.
If one is asked for, copy `pschema` and `schema_18` from your SolidWorks
installation into `schemas/`.

## Licence

openSW is free software under the GNU Affero General Public License v3.0
only (AGPL-3.0-only), see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
In short, and the licence text prevails:

- You may use, study, copy and modify it, for any purpose.
- If you distribute it, modified or not, you must do so under the same
  licence, with its source code.
- If you let people use a modified version over a network (a web service,
  for instance), you must offer them its complete source code (section 13).
- It comes with no warranty.

## Legal

Interoperability work on files the author owns. This repository contains no
SolidWorks or Parasolid code. openSW is not affiliated with Dassault
Systèmes or Siemens; SolidWorks and Parasolid are trademarks of their
respective owners.
