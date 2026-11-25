"""The module FreeCAD calls for a SolidWorks file (see ``Init.py``).

``open`` and ``insert`` are the two entry points FreeCAD's import system
expects; everything else lives in :mod:`opensw_fc`.
"""

import os

import FreeCAD


def open(filename):  # the name FreeCAD looks for
    """File > Open: a new document named after the file."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    doc = FreeCAD.newDocument(_identifier(stem))
    doc.Label = stem
    return insert(filename, doc.Name)


def insert(filename, docname):
    """File > Import: into the document *docname* (created if missing)."""
    from opensw_fc import importer

    try:
        doc = FreeCAD.getDocument(docname)
    except NameError:
        doc = FreeCAD.newDocument(docname)
    if not doc.Objects and doc.Label == doc.Name:
        # A document FreeCAD has just made for this file: name it after it.
        doc.Label = os.path.splitext(os.path.basename(filename))[0]
    importer.load(filename, doc)
    return doc


def _identifier(stem):
    """A document name FreeCAD accepts: ASCII letters, digits, underscores."""
    name = "".join(c if c.isascii() and c.isalnum() else "_" for c in stem)
    return name if name[:1].isalpha() else "SW_" + name
