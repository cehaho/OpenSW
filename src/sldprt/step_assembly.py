"""An assembly as STEP: its parts, exact, placed where it says.

Nothing is recomputed here. The structure comes from the assembly's XML tree
(:mod:`sldprt.assembly`): which parts, how many times, where. The geometry of
each part comes from its Parasolid partition, through the part writer
(:func:`sldprt.step.write_step`: same translator, same entities).

The layout is the one SolidWorks writes itself (AP214):

* each **distinct** part is written once: a ``SHAPE_REPRESENTATION`` that
  carries its frame, linked to its ``ADVANCED_BREP_SHAPE_REPRESENTATION``;
* the assembly has its own ``SHAPE_REPRESENTATION``, with one frame per
  instance;
* each instance is a ``NEXT_ASSEMBLY_USAGE_OCCURRENCE``, with an
  ``ITEM_DEFINED_TRANSFORMATION`` from the part frame to the instance frame.

Sub-assemblies are flattened: :mod:`sldprt.assembly` already composes their
matrices, so every part is placed directly under the assembly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .assembly import Assembly
from .step import (
    HEADER,
    PARTITION_MARK,
    StepError,
    Translator,
    Writer,
    WriteReport,
    _context,
    _iso_text,
    _string_literal,
)
from .xt.model import Model


@dataclass
class AssemblyReport:
    """What was written, and what was not."""

    #: The instances written.
    instances: int = 0
    #: The report of each part written, by file name.
    parts: dict[str, WriteReport] = field(default_factory=dict)
    #: Components whose file was not supplied, by file.
    missing: dict[str, int] = field(default_factory=dict)
    #: Supplied parts of which no body could be written, and why.
    refused: dict[str, str] = field(default_factory=dict)
    #: Suppressed components, which a STEP does not carry.
    suppressed: int = 0
    #: Hidden components, which SolidWorks does not export either.
    hidden: int = 0


def write_step_assembly(asm: Assembly, parts: dict[str, Model], path: str | Path,
                        *, name: str = "assembly") -> AssemblyReport:
    """Write *asm* as STEP. *parts*: one model per part file name
    (``cube.SLDPRT``), compared case-insensitively. A missing part is
    counted, not made up; if none is left, :class:`StepError`."""
    report = AssemblyReport()
    by_name = {k.lower(): (k, v) for k, v in parts.items()}
    writer = Writer()
    context, _ = _context(writer)
    application = writer.add("APPLICATION_CONTEXT ( 'automotive_design' )")
    writer.add(
        "APPLICATION_PROTOCOL_DEFINITION ( 'draft international standard', "
        f"'automotive_design', 1998, #{application} )"
    )
    product_ctx = writer.add(f"PRODUCT_CONTEXT ( 'NONE', #{application}, 'mechanical' )")
    definition_ctx = writer.add(
        f"PRODUCT_DEFINITION_CONTEXT ( 'detailed design', #{application}, 'design' )"
    )

    def product(label: str, representation: int, category: str) -> int:
        label = _string_literal(label)
        p = writer.add(f"PRODUCT ( '{label}', '{label}', '', ( #{product_ctx} ) )")
        writer.add(f"PRODUCT_RELATED_PRODUCT_CATEGORY ( '{category}', '', ( #{p} ) )")
        formation = writer.add(
            "PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE ( 'ANY', '', "
            f"#{p}, .NOT_KNOWN. )"
        )
        definition = writer.add(
            f"PRODUCT_DEFINITION ( 'UNKNOWN', '', #{formation}, #{definition_ctx} )"
        )
        shape = writer.add(f"PRODUCT_DEFINITION_SHAPE ( 'NONE', 'NONE', #{definition} )")
        writer.add(f"SHAPE_DEFINITION_REPRESENTATION ( #{shape}, #{representation} )")
        return definition

    origin = writer.point((0.0, 0.0, 0.0), 1.0)
    styles: list[int] = []
    # A distinct part: (definition, representation, frame), or None if it
    # could not be written.
    written: dict[str, tuple[int, int, int] | None] = {}

    def part(filename: str):
        key = filename.lower()
        if key in written:
            return written[key]
        label, model = by_name[key]
        mark = writer.mark()
        translator = Translator(model, writer)
        solids, sheets = [], []
        partition = getattr(model, PARTITION_MARK, False)
        for body in model.bodies:
            if not model.is_solid_body(body):
                # As for a single part: the sheets of a partition are left
                # out, those of a transmit are written.
                if partition:
                    translator.report.partition_sheets += 1
                    continue
                n = translator.sheet(body)
                if n is not None:
                    sheets.append(f"#{n}")
                continue
            n = translator.body(body)
            if n is not None:
                solids.append(f"#{n}")
        if not solids:
            writer.rewind(mark)
            report.refused[label] = translator.report.refusals() or "no solid body"
            written[key] = None
            return None
        frame = writer.add(f"AXIS2_PLACEMENT_3D ( 'NONE', #{origin}, $, $ )")
        brep = writer.add(
            "ADVANCED_BREP_SHAPE_REPRESENTATION ( 'NONE', "
            f"( #{frame}, {', '.join(solids)} ), #{context} )"
        )
        if sheets:
            model_item = writer.add(
                f"SHELL_BASED_SURFACE_MODEL ( 'NONE', ( {', '.join(sheets)} ) )"
            )
            surfaces = writer.add(
                "MANIFOLD_SURFACE_SHAPE_REPRESENTATION ( 'NONE', "
                f"( #{frame}, #{model_item} ), #{context} )"
            )
            writer.add(
                f"SHAPE_REPRESENTATION_RELATIONSHIP ( 'NONE', '', #{brep}, #{surfaces} )"
            )
        shape = writer.add(
            f"SHAPE_REPRESENTATION ( '{_string_literal(Path(label).stem)}', "
            f"( #{frame} ), #{context} )"
        )
        writer.add(f"SHAPE_REPRESENTATION_RELATIONSHIP ( 'NONE', 'NONE', #{shape}, #{brep} )")
        definition = product(Path(label).stem, shape, "part")
        styles.extend(translator.styled_items())
        report.parts[label] = translator.report
        written[key] = (definition, shape, frame)
        return written[key]

    instances = []
    for c in asm.composants:
        if c.supprime:
            report.suppressed += 1
            continue
        if c.cache:
            report.hidden += 1
            continue
        if c.fichier.lower() not in by_name:
            report.missing[c.fichier] = report.missing.get(c.fichier, 0) + 1
            continue
        done = part(c.fichier)
        if done is not None:
            instances.append((c, done))
    if not instances:
        raise StepError("no part of the assembly could be written"
                        + (f"; missing: {', '.join(report.missing)}"
                           if report.missing else ""))

    # The assembly: one frame per instance, the component matrix. A point is
    # placed at p·R + t, so the part x axis goes to row 0 of R, z to row 2.
    asm_frame = writer.add(f"AXIS2_PLACEMENT_3D ( 'NONE', #{origin}, $, $ )")
    places = []
    for c, _ in instances:
        m = c.matrice
        places.append(writer.add(
            f"AXIS2_PLACEMENT_3D ( 'NONE', #{writer.point(m[9:12])}, "
            f"#{writer.direction(m[6:9])}, #{writer.direction(m[0:3])} )"
        ))
    asm_shape = writer.add(
        f"SHAPE_REPRESENTATION ( '{_string_literal(name)}', "
        f"( #{asm_frame}, {', '.join(f'#{p}' for p in places)} ), #{context} )"
    )
    asm_definition = product(name, asm_shape, "assembly")
    for k, ((c, (definition, shape, frame)), place) in enumerate(
            zip(instances, places, strict=True), start=1):
        transformation = writer.add(
            f"ITEM_DEFINED_TRANSFORMATION ( 'NONE', 'NONE', #{place}, #{frame} )"
        )
        # SolidWorks' order: the assembly representation first, the part's
        # second, like the two frames above.
        relation = writer.add(
            f"( REPRESENTATION_RELATIONSHIP ( 'NONE', 'NONE', #{asm_shape}, #{shape} ) "
            f"REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION ( #{transformation} ) "
            "SHAPE_REPRESENTATION_RELATIONSHIP ( ) )"
        )
        usage = writer.add(
            f"NEXT_ASSEMBLY_USAGE_OCCURRENCE ( 'NAUO{k}', "
            f"'{_string_literal(c.nom)}', ' ', #{asm_definition}, #{definition}, $ )"
        )
        usage_shape = writer.add(f"PRODUCT_DEFINITION_SHAPE ( 'NONE', 'NONE', #{usage} )")
        writer.add(f"CONTEXT_DEPENDENT_SHAPE_REPRESENTATION ( #{relation}, #{usage_shape} )")
        report.instances += 1
    if styles:
        writer.add(
            "MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION ( '', ( "
            + ", ".join(f"#{s}" for s in styles) + f" ), #{context} )"
        )

    body = "\n".join(f"#{n} = {text} ;" for n, text in enumerate(writer.lines, start=1))
    final = Path(path)
    partial = final.with_name(final.name + ".part")
    partial.write_text(
        _iso_text((HEADER % (_string_literal(final.name), "2026-01-01T00:00:00"))
                  + body + "\nENDSEC;\nEND-ISO-10303-21;\n"),
        encoding="ascii",
    )
    os.replace(partial, final)
    return report
