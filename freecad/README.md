# OpenSW for FreeCAD

Open SolidWorks files in FreeCAD — **without SolidWorks**.

| File | What arrives in FreeCAD |
|---|---|
| `.SLDPRT` | The exact bodies (B-rep, from the file's own Parasolid data), one object per body, their colours, and every sketch |
| `.SLDASM` | The assembly with each instance placed. Exact when the part files sit next to it; otherwise the display mesh the assembly keeps of each part |
| `.SLDDRW` | Each sheet as flat geometry: lines, arcs, dimensions as drawn, texts, the projected edges of every view |

After installing, SolidWorks files simply show up in **File > Open** and
**File > Import**.

## What to expect

- **Geometry is exact, features are not replayed.** A part arrives as solids,
  like a STEP import — not as a FreeCAD Pad/Pocket history, and the
  FeatureManager tree is not rebuilt.
- **Sketches come without constraints.** A planar sketch becomes a real
  Sketcher sketch, placed on its plane, with its lines, arcs, circles,
  ellipses and splines — but the file is read for where the points *are*, not
  for the constraints that put them there. A 3D sketch becomes plain edges
  (FreeCAD has no 3D sketch). All sketches are in one `Sketches` folder; one
  that a feature absorbs carries that feature's name: `Sketch1 (Boss-Extrude1)`.
- **An assembly needs its parts for exact geometry.** An `.SLDASM` does not
  contain the geometry of its parts. Put the `.SLDPRT` files in the same
  folder (or one folder below): the parts found are read exactly, the others
  arrive as meshes, and the report view names them. Suppressed and hidden
  components are left out. Mates are not converted.
- **A drawing is a picture you can measure, not a TechDraw page.** It is what
  SolidWorks drew, in millimetres on the XY plane, sheets side by side.
  Shaded views arrive by their edges only, pictures are not drawn.

The report view says what was read and — always — what was not.

## Install

Addon Manager, or by hand: copy this folder into FreeCAD's `Mod` directory
and restart FreeCAD. FreeCAD 1.0 or later.

The only dependency is the Python module `olefile`, which FreeCAD's own
builds ship. Nothing else is installed, no second OpenCASCADE is loaded.

## How it works

`opensw_fc/sldprt/` is the reader of the
[OpenSW](https://github.com/cehaho/OpenSW) project, in pure Python. It
decodes the file, writes the geometry as STEP into a temporary folder, and
FreeCAD's own STEP importer reads it back; sketches, meshes and drawing
geometry are built directly as FreeCAD objects. Nothing is sent anywhere:
everything runs in your FreeCAD.

The addon is built from OpenSW (`python tools/freecad_addon.py` there):
fixes to the reader and to the importer both go to that repository.

## Licence

AGPL-3.0-only — see `LICENSE` and `NOTICE`. SolidWorks is a trademark of
Dassault Systèmes; this project is not affiliated with it and contains no
SolidWorks code.
