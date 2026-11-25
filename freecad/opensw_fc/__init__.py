"""OpenSW for FreeCAD: SolidWorks files read cold, shown in FreeCAD.

``opensw_fc.sldprt`` is the reader of the OpenSW project (``src/sldprt``
there), copied in when the addon is built. It is pure Python: FreeCAD's own
OpenCASCADE is the only one in the process. The modules next to it are
the FreeCAD side — they turn what the reader returns into document objects.
"""

__version__ = "0.1.0"
