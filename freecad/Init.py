"""FreeCAD runs this file at start-up: it only declares the file types.

Nothing of the reader is imported here, so the addon costs nothing until a
file is actually opened.
"""

import FreeCAD

FreeCAD.addImportType(
    "SolidWorks part, assembly, drawing (*.sldprt *.sldasm *.slddrw "
    "*.SLDPRT *.SLDASM *.SLDDRW)",
    "importOpenSW",
)
# A Parasolid transmit is what a SolidWorks part keeps its bodies in: the same
# reader opens one saved on its own.
FreeCAD.addImportType(
    "Parasolid (*.x_t *.x_b *.xmt_txt *.xmt_bin *.X_T *.X_B *.XMT_TXT *.XMT_BIN)",
    "importOpenSW",
)
