"""FreeCAD runs this file at start-up: it only declares the file types.

Nothing of the reader is imported here, so the addon costs nothing until a
SolidWorks file is actually opened.
"""

import FreeCAD

FreeCAD.addImportType(
    "SolidWorks part, assembly, drawing (*.sldprt *.sldasm *.slddrw "
    "*.SLDPRT *.SLDASM *.SLDDRW)",
    "importOpenSW",
)
