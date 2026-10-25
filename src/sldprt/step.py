"""Direct STEP AP214 writer, node by node."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import evaluate
from .xt.model import Model, Node

SCALE = 1000.0

DIGITS = 17

SURFACES = {"PLANE", "CYLINDER", "CONE", "SPHERE", "TORUS", "B_SURFACE",
            "SWEPT_SURF", "OFFSET_SURF", "BLENDED_EDGE"}

REPARAMETRISED = {"BLENDED_EDGE"}

PCURVE_EXACT = {"PLANE"}
CURVES = {"LINE", "CIRCLE", "ELLIPSE", "B_CURVE", "TRIMMED_CURVE",
          "INTERSECTION"}

NULL_DOUBLE = -3.14158e13

PARAMETER_SPACE_DIM = 2


class StepError(Exception):
    pass


PARTITION_MARK = "from_partition"


@dataclass
class WriteReport:
    bodies: int = 0
    sheets: int = 0
    sheet_faces: int = 0
    partition_sheets: int = 0
    pinched_loops: int = 0
    faces: int = 0
    skipped_surfaces: dict[str, int] = field(default_factory=dict)
    skipped_curves: dict[str, int] = field(default_factory=dict)
    skipped_topology: dict[str, int] = field(default_factory=dict)
    skipped_bodies: int = 0
    skipped_body_nodes: list[tuple[int, int]] = field(default_factory=list)
    approximated_curves: int = 0
    worst_curve_error: float = 0.0
    charted_curves: int = 0
    worst_chart_error: float = 0.0
    charted_pcurves: int = 0
    approximated_without_pcurve: int = 0
    coloured_bodies: int = 0
    coloured_faces: int = 0

    def _note(self, bucket: dict[str, int], name: str) -> None:
        bucket[name] = bucket.get(name, 0) + 1

    def refusals(self) -> str:
        parts = []
        for bucket, label in (
            (self.skipped_surfaces, "surfaces"),
            (self.skipped_curves, "curves"),
            (self.skipped_topology, "topology"),
        ):
            if not bucket:
                continue
            named = sorted(bucket.items(), key=lambda pair: -pair[1])
            parts.append(
                f"{label}: "
                + ", ".join(f"{name} x{count}" for name, count in named)
            )
        return "; ".join(parts)

    def dropped_bodies(self) -> str:
        if not self.skipped_body_nodes:
            return ""
        worst = sorted(self.skipped_body_nodes, key=lambda pair: -pair[1])
        return ", ".join(f"{index} ({faces} faces)" for index, faces in worst)

    def __str__(self) -> str:
        parts = [f"{self.bodies} bodies, {self.faces} faces"]
        if self.coloured_bodies or self.coloured_faces:
            parts.append(
                f"{self.coloured_bodies} body and {self.coloured_faces} face "
                f"colours"
            )
        if self.skipped_bodies:
            parts.append(f"{self.skipped_bodies} bodies left out")
        if self.skipped_surfaces:
            parts.append(f"surfaces: {self.skipped_surfaces}")
        if self.skipped_curves:
            parts.append(f"curves: {self.skipped_curves}")
        if self.skipped_topology:
            parts.append(f"topology: {self.skipped_topology}")
        if self.charted_curves:
            parts.append(
                f"{self.charted_curves} intersections charted, declared "
                f"error up to {self.worst_chart_error * SCALE:.6f} mm"
            )
        if self.charted_pcurves:
            parts.append(
                f"{self.charted_pcurves} intersection edges carry their two "
                f"pcurves"
            )
        if self.approximated_curves:
            parts.append(
                f"{self.approximated_curves} tolerant edges approximated, "
                f"worst {self.worst_curve_error * SCALE:.6f} mm"
            )
        return "; ".join(parts)


def _real(value: float) -> str:
    text = repr(float(value))
    if "e" in text or "E" in text:
        return f"{float(value):.{DIGITS}E}"
    return text if "." in text else text + "."


def _same_sense(face_node: Node, surface_node: Node) -> str:
    face_reversed = _sense(face_node.values.get("sense")) == "-"
    surface_reversed = (
        surface_node.name != "OFFSET_SURF"
        and _sense(surface_node.values.get("sense")) == "-"
    )
    return ".F." if face_reversed != surface_reversed else ".T."


class Writer:
    def __init__(self) -> None:
        self._lines: list[str] = []
        self._cache: dict[tuple, int] = {}

    def add(self, text: str) -> int:
        self._lines.append(text)
        return len(self._lines)

    def shared(self, key: tuple, text: str) -> int:
        if key not in self._cache:
            self._cache[key] = self.add(text)
        return self._cache[key]

    def mark(self) -> int:
        return len(self._lines)

    def rewind(self, mark: int) -> None:
        if mark >= len(self._lines):
            return
        del self._lines[mark:]
        self._cache = {key: at for key, at in self._cache.items() if at <= mark}

    @property
    def lines(self) -> list[str]:
        return self._lines


    def point2d(self, vector) -> int:
        coordinates = tuple(round(v, 15) for v in vector[:2])
        return self.shared(
            ("P2", *coordinates),
            "CARTESIAN_POINT ( 'NONE', ( "
            + ", ".join(_real(c) for c in coordinates)
            + " ) )",
        )

    def point(self, vector, scale: float = SCALE) -> int:
        coordinates = tuple(round(v * scale, 12) for v in vector[:3])
        return self.shared(
            ("P", *coordinates),
            "CARTESIAN_POINT ( 'NONE', ( "
            + ", ".join(_real(c) for c in coordinates)
            + " ) )",
        )

    def direction(self, vector) -> int:
        length = math.sqrt(sum(v * v for v in vector[:3]))
        if not length:
            raise StepError(
                f"a direction vector is zero: {tuple(vector[:3])!r}"
            )
        components = tuple(round(v / length, 15) for v in vector[:3])
        return self.shared(
            ("D", *components),
            "DIRECTION ( 'NONE', ( "
            + ", ".join(_real(c) for c in components)
            + " ) )",
        )

    def placement(self, origin, axis, reference=None, scale: float = SCALE) -> int:
        parts = [f"#{self.point(origin, scale)}", f"#{self.direction(axis)}"]
        parts.append(f"#{self.direction(reference)}" if reference else "$")
        return self.add(f"AXIS2_PLACEMENT_3D ( 'NONE', {', '.join(parts)} )")


POLE_CONTACT = 1.0e-7

POLE_CANDIDATES = 500

_SPHERE_FRAMES = "_reperes_spheres"


def _circle_distance(point, values):
    centre, normal = values["centre"], _unit3(values["normal"])
    w = [point[i] - centre[i] for i in range(3)]
    height = sum(w[i] * normal[i] for i in range(3))
    radial = math.sqrt(max(0.0, sum(c * c for c in w) - height * height))
    return math.hypot(height, radial - values["radius"])


def _unit3(v):
    length = math.sqrt(sum(c * c for c in v))
    return [c / length for c in v]


def _face_boundary(model, face):
    points, circles = [], []
    for loop in model.loops_of(face):
        for halfedge in model.halfedges_of(loop):
            vertex = model.get(halfedge.values.get("vertex", 0))
            point = model.vertex_point(vertex) if vertex is not None else None
            if point is not None:
                points.append(point)
            edge = model.get(halfedge.values.get("edge", 0))
            curve = model.curve_of(edge) if edge is not None else None
            while curve is not None and curve.name == "TRIMMED_CURVE":
                curve = model.get(curve.values.get("basis_curve", 0))
            if curve is None:
                trim = model.get(halfedge.values.get("curve", 0))
                basis = model.get(trim.values.get("basis_curve", 0)) if trim else None
                if basis is None or basis.name != "SP_CURVE":
                    continue
                try:
                    spec = evaluate.read_nurbs_curve(model, model.get(basis.values["b_curve"]))
                    surface = model.get(basis.values["surface"])
                    low, high = trim.values["parm_1"], trim.values["parm_2"]
                    for k in range(33):
                        u, v = evaluate.evaluate_bspline(
                            spec["degree"], spec["poles"], spec["weights"],
                            spec["knots"], low + (high - low) * k / 32)[:2]
                        points.append(evaluate.surface_point(model, surface, u, v))
                except (evaluate.EvaluateError, KeyError, TypeError):
                    pass
            elif curve.name == "CIRCLE":
                values = curve.values
                circles.append(values)
                centre, normal = values["centre"], _unit3(values["normal"])
                x = _unit3(values["x_axis"])
                y = [normal[1] * x[2] - normal[2] * x[1], normal[2] * x[0] - normal[0] * x[2],
                     normal[0] * x[1] - normal[1] * x[0]]
                for k in range(72):
                    t = 2.0 * math.pi * k / 72
                    points.append([centre[i] + values["radius"] * (math.cos(t) * x[i] + math.sin(t) * y[i])
                                   for i in range(3)])
            elif curve.name == "INTERSECTION":
                try:
                    points.extend(evaluate.chart_points(model, curve)[0])
                except evaluate.EvaluateError:
                    pass
    return points, circles


def _sphere_frame(model, node):
    values = node.values
    axis, reference = values["axis"], values.get("x_axis")
    if model is None:
        return axis, reference
    memo = getattr(model, _SPHERE_FRAMES, None)
    if memo is None:
        memo = {}
        try:
            setattr(model, _SPHERE_FRAMES, memo)
        except AttributeError:
            pass
    if node.index in memo:
        return memo[node.index]
    centre, radius = values["centre"], values["radius"]
    unit = _unit3(axis)
    poles = [[centre[i] + s * radius * unit[i] for i in range(3)] for s in (1.0, -1.0)]
    frame = (axis, reference)
    boundaries = [(face, *_face_boundary(model, face))
                  for face in evaluate._faces_on(model, node)]
    for face, points, circles in boundaries:
        if len(list(model.loops_of(face))) < 2:
            continue
        touched = [
            min([math.dist(pole, p) for p in points]
                + [_circle_distance(pole, c) for c in circles] or [math.inf])
            <= POLE_CONTACT
            for pole in poles
        ]
        if all(touched):
            frame = _clear_frame(values, [p for _f, pts, _c in boundaries for p in pts])
            break
    memo[node.index] = frame
    return frame


def _clear_frame(values, points):
    centre = values["centre"]
    directions = []
    for point in points:
        w = [point[i] - centre[i] for i in range(3)]
        length = math.sqrt(sum(c * c for c in w))
        if length > 0.0:
            directions.append([c / length for c in w])
    best, best_clearance = _unit3(values["axis"]), -1.0
    golden = math.pi * (3.0 - math.sqrt(5.0))
    for k in range(POLE_CANDIDATES):
        z = 1.0 - (k + 0.5) / POLE_CANDIDATES
        r = math.sqrt(1.0 - z * z)
        candidate = [r * math.cos(k * golden), r * math.sin(k * golden), z]
        nearest = max(abs(sum(d[i] * candidate[i] for i in range(3))) for d in directions)
        clearance = -nearest
        if clearance > best_clearance:
            best, best_clearance = candidate, clearance
    reference = values.get("x_axis") or [1.0, 0.0, 0.0]
    along = sum(reference[i] * best[i] for i in range(3))
    reference = [reference[i] - along * best[i] for i in range(3)]
    if math.sqrt(sum(c * c for c in reference)) < 1e-6:
        reference = [0.0, 1.0, 0.0] if abs(best[0]) > 0.9 else [1.0, 0.0, 0.0]
        along = sum(reference[i] * best[i] for i in range(3))
        reference = [reference[i] - along * best[i] for i in range(3)]
    return best, _unit3(reference)


def _write_lemon(writer: Writer, values: dict) -> int:
    radius, minor = values["major_radius"], values["minor_radius"]
    centre = values["centre"]
    axis, reference = _unit3(values["axis"]), _unit3(values["x_axis"])
    normal = [-(axis[1] * reference[2] - axis[2] * reference[1]),
              -(axis[2] * reference[0] - axis[0] * reference[2]),
              -(axis[0] * reference[1] - axis[1] * reference[0])]
    tip = math.acos(max(-1.0, min(1.0, -radius / minor)))
    middle = [centre[i] + radius * reference[i] for i in range(3)]
    circle = writer.add(
        f"CIRCLE ( 'NONE', #{writer.placement(middle, normal, reference)}, "
        f"{_real(minor * SCALE)} )"
    )
    arc = writer.add(
        f"TRIMMED_CURVE ( 'NONE', #{circle}, ( PARAMETER_VALUE ( {_real(-tip)} ) ), "
        f"( PARAMETER_VALUE ( {_real(tip)} ) ), .T., .PARAMETER. )"
    )
    line = writer.add(
        f"AXIS1_PLACEMENT ( 'NONE', #{writer.point(centre)}, #{writer.direction(axis)} )"
    )
    return writer.add(f"SURFACE_OF_REVOLUTION ( 'NONE', #{arc}, #{line} )")


def _placement_of(writer: Writer, values: dict, origin_key: str, axis_key: str) -> int:
    return writer.placement(
        values[origin_key], values[axis_key], values.get("x_axis")
    )


def _knots(model, knot_index: int, mult_index: int, count):
    knot_node = model.get(knot_index)
    mult_node = model.get(mult_index)
    if knot_node is None or mult_node is None:
        raise StepError("a NURBS knot vector is missing")
    knots = knot_node.values["knots"]
    mults = mult_node.values["mult"]
    knots = knots if isinstance(knots, list) else [knots]
    mults = mults if isinstance(mults, list) else [mults]
    if isinstance(count, int) and 0 < count <= len(knots):
        knots, mults = knots[:count], mults[:count]
    if len(knots) != len(mults):
        raise StepError(
            f"a NURBS knot vector holds {len(knots)} knots but "
            f"{len(mults)} multiplicities"
        )
    kept = [(k, m) for k, m in zip(knots, mults, strict=True) if m > 0]
    return [k for k, _ in kept], [m for _, m in kept]


def _pole(vertices, offset: int, dim: int):
    weight = vertices[offset + 3] if dim == 4 else 1.0
    if not weight:
        raise StepError("a rational NURBS pole has a zero weight")
    return [vertices[offset + i] / weight for i in range(3)], weight


def _numbers(values) -> str:
    return ", ".join(_real(v) for v in values)


def _integers(values) -> str:
    return ", ".join(str(int(v)) for v in values)


def write_surface(writer: Writer, node: Node, model=None) -> int | None:
    values = node.values
    if node.name == "PLANE":
        return writer.add(
            f"PLANE ( 'NONE', #{_placement_of(writer, values, 'pvec', 'normal')} )"
        )
    if node.name == "CYLINDER":
        placement = _placement_of(writer, values, "pvec", "axis")
        radius = _real(values["radius"] * SCALE)
        return writer.add(f"CYLINDRICAL_SURFACE ( 'NONE', #{placement}, {radius} )")
    if node.name == "CONE":
        placement = _placement_of(writer, values, "pvec", "axis")
        angle = math.atan2(values["sin_half_angle"], values["cos_half_angle"])
        return writer.add(
            f"CONICAL_SURFACE ( 'NONE', #{placement}, "
            f"{_real(values['radius'] * SCALE)}, {_real(angle)} )"
        )
    if node.name == "SPHERE":
        axis, reference = _sphere_frame(model, node)
        placement = writer.placement(values["centre"], axis, reference)
        return writer.add(
            f"SPHERICAL_SURFACE ( 'NONE', #{placement}, "
            f"{_real(values['radius'] * SCALE)} )"
        )
    if node.name == "TORUS":
        if values["major_radius"] < 0.0:
            return _write_lemon(writer, values)
        placement = _placement_of(writer, values, "centre", "axis")
        return writer.add(
            f"TOROIDAL_SURFACE ( 'NONE', #{placement}, "
            f"{_real(values['major_radius'] * SCALE)}, "
            f"{_real(values['minor_radius'] * SCALE)} )"
        )
    if node.name == "B_SURFACE" and model is not None:
        return _write_bspline_surface(writer, model, node)
    if node.name == "SWEPT_SURF" and model is not None:
        section = model.get(values.get("section", 0))
        if section is None:
            return None
        curve = write_curve(writer, section, model)
        if curve is None:
            return None
        sweep = writer.add(
            f"VECTOR ( 'NONE', #{writer.direction(values['sweep'])}, "
            f"{_real(SCALE)} )"
        )
        return writer.add(
            f"SURFACE_OF_LINEAR_EXTRUSION ( 'NONE', #{curve}, #{sweep} )"
        )
    if node.name == "BLENDED_EDGE" and model is not None:
        return _write_blend(writer, model, node)
    if node.name == "OFFSET_SURF" and model is not None:
        basis_node = model.get(values.get("surface", 0))
        if basis_node is None or basis_node.name not in SURFACES:
            return None
        basis = write_surface(writer, basis_node, model)
        if basis is None:
            return None
        distance = values["offset"] * SCALE
        if _sense(values.get("sense")) == "-":
            distance = -distance
        return writer.add(
            f"OFFSET_SURFACE ( 'NONE', #{basis}, {_real(distance)}, .F. )"
        )
    return None


def _write_bspline_surface(writer: Writer, model, node: Node) -> int | None:
    nurbs = model.get(node.values.get("nurbs", 0))
    if nurbs is None or nurbs.name != "NURBS_SURF":
        return None
    vertex_node = model.get(nurbs.values["bspline_vertices"])
    if vertex_node is None:
        raise StepError("a B_SURFACE has no control points")
    vertices = vertex_node.values["vertices"]

    n_u = nurbs.values["n_u_vertices"]
    n_v = nurbs.values["n_v_vertices"]
    dim = nurbs.values["vertex_dim"]
    rational = bool(nurbs.values["rational"]) and dim == 4
    if len(vertices) != n_u * n_v * dim:
        raise StepError("a B_SURFACE control net has the wrong size")

    rows, weights, offset = [], [], 0
    for _ in range(n_u):
        row, row_weights = [], []
        for _ in range(n_v):
            point, weight = _pole(vertices, offset, dim)
            offset += dim
            row.append(f"#{writer.point(point)}")
            row_weights.append(weight)
        rows.append("( " + ", ".join(row) + " )")
        weights.append(row_weights)

    u_knots, u_mults = _knots(
        model, nurbs.values["u_knots"], nurbs.values["u_knot_mult"],
        nurbs.values.get("n_u_knots"),
    )
    v_knots, v_mults = _knots(
        model, nurbs.values["v_knots"], nurbs.values["v_knot_mult"],
        nurbs.values.get("n_v_knots"),
    )
    net = "( " + ", ".join(rows) + " )"
    degrees = f"{nurbs.values['u_degree']}, {nurbs.values['v_degree']}"
    u_closed = ".T." if nurbs.values.get("u_closed") or nurbs.values.get("u_periodic") else ".F."
    v_closed = ".T." if nurbs.values.get("v_closed") or nurbs.values.get("v_periodic") else ".F."
    knot_data = (
        f"( {_integers(u_mults)} ), ( {_integers(v_mults)} ), "
        f"( {_numbers(u_knots)} ), ( {_numbers(v_knots)} ), .UNSPECIFIED."
    )
    if not rational:
        return writer.add(
            f"B_SPLINE_SURFACE_WITH_KNOTS ( 'NONE', {degrees}, {net}, "
            f".UNSPECIFIED., {u_closed}, {v_closed}, .F., {knot_data} )"
        )
    weight_data = "( " + ", ".join(
        "( " + _numbers(row) + " )" for row in weights
    ) + " )"
    return writer.add(
        "( BOUNDED_SURFACE ( ) "
        f"B_SPLINE_SURFACE ( {degrees}, {net}, .UNSPECIFIED., "
        f"{u_closed}, {v_closed}, .F. ) "
        f"B_SPLINE_SURFACE_WITH_KNOTS ( {knot_data} ) "
        "GEOMETRIC_REPRESENTATION_ITEM ( ) "
        f"RATIONAL_B_SPLINE_SURFACE ( {weight_data} ) "
        "REPRESENTATION_ITEM ( 'NONE' ) SURFACE ( ) )"
    )


def _write_bspline_curve(writer: Writer, model, node: Node) -> int | None:
    nurbs = model.get(node.values.get("nurbs", 0))
    if nurbs is None or nurbs.name != "NURBS_CURVE":
        return None
    dim = nurbs.values["vertex_dim"]
    if dim == PARAMETER_SPACE_DIM:
        return None
    vertex_node = model.get(nurbs.values["bspline_vertices"])
    if vertex_node is None:
        raise StepError("a B_CURVE has no control points")
    vertices = vertex_node.values["vertices"]
    count = nurbs.values["n_vertices"]
    rational = bool(nurbs.values["rational"]) and dim == 4
    if len(vertices) != count * dim:
        raise StepError("a B_CURVE control polygon has the wrong size")

    points, weights = [], []
    for i in range(count):
        point, weight = _pole(vertices, i * dim, dim)
        points.append(f"#{writer.point(point)}")
        weights.append(weight)

    knots, mults = _knots(
        model, nurbs.values["knots"], nurbs.values["knot_mult"],
        nurbs.values.get("n_knots"),
    )
    polygon = "( " + ", ".join(points) + " )"
    knot_data = (
        f"( {_integers(mults)} ), ( {_numbers(knots)} ), .UNSPECIFIED."
    )
    degree = nurbs.values["degree"]
    closed = ".T." if nurbs.values.get("closed") or nurbs.values.get("periodic") else ".F."
    if not rational:
        return writer.add(
            f"B_SPLINE_CURVE_WITH_KNOTS ( 'NONE', {degree}, {polygon}, "
            f".UNSPECIFIED., {closed}, .F., {knot_data} )"
        )
    return writer.add(
        "( BOUNDED_CURVE ( ) "
        f"B_SPLINE_CURVE ( {degree}, {polygon}, .UNSPECIFIED., {closed}, .F. ) "
        f"B_SPLINE_CURVE_WITH_KNOTS ( {knot_data} ) CURVE ( ) "
        "GEOMETRIC_REPRESENTATION_ITEM ( ) "
        f"RATIONAL_B_SPLINE_CURVE ( ( {_numbers(weights)} ) ) "
        "REPRESENTATION_ITEM ( 'NONE' ) )"
    )


def write_curve(writer: Writer, node: Node, model=None) -> int | None:
    values = node.values
    if node.name == "LINE":
        origin = writer.point(values["pvec"])
        direction = writer.direction(values["direction"])
        vector = writer.add(
            f"VECTOR ( 'NONE', #{direction}, {_real(SCALE)} )"
        )
        return writer.add(f"LINE ( 'NONE', #{origin}, #{vector} )")
    if node.name == "CIRCLE":
        placement = writer.placement(
            values["centre"], values["normal"], values.get("x_axis")
        )
        return writer.add(
            f"CIRCLE ( 'NONE', #{placement}, {_real(values['radius'] * SCALE)} )"
        )
    if node.name == "ELLIPSE":
        placement = writer.placement(
            values["centre"], values["normal"], values.get("x_axis")
        )
        return writer.add(
            f"ELLIPSE ( 'NONE', #{placement}, "
            f"{_real(values['major_radius'] * SCALE)}, "
            f"{_real(values['minor_radius'] * SCALE)} )"
        )
    if model is None:
        return None
    if node.name == "B_CURVE":
        return _write_bspline_curve(writer, model, node)
    if node.name == "TRIMMED_CURVE":
        basis = model.get(values.get("basis_curve", 0))
        return write_curve(writer, basis, model) if basis is not None else None
    return None


def _write_blend(writer: Writer, model, node: Node) -> int | None:
    try:
        stations = evaluate.blend_stations(model, node)
    except evaluate.EvaluateError:
        stations = None
    try:
        net, weights, u_knots, u_mults, v_knots, v_mults = (
            evaluate.blend_surface(model, node, stations)
        )
    except evaluate.EvaluateError:
        return None
    rows = []
    for row in net:
        rows.append(
            "( " + ", ".join(f"#{writer.point(pole)}" for pole in row) + " )"
        )
    net_text = "( " + ", ".join(rows) + " )"
    weight_text = "( " + ", ".join(
        "( " + _numbers(row) + " )" for row in weights
    ) + " )"
    knot_data = (
        f"( {_integers(u_mults)} ), ( {_integers(v_mults)} ), "
        f"( {_numbers(u_knots)} ), ( {_numbers(v_knots)} ), .UNSPECIFIED."
    )
    return writer.add(
        "( BOUNDED_SURFACE ( ) "
        f"B_SPLINE_SURFACE ( 3, 2, {net_text}, .UNSPECIFIED., .F., .F., .F. ) "
        f"B_SPLINE_SURFACE_WITH_KNOTS ( {knot_data} ) "
        "GEOMETRIC_REPRESENTATION_ITEM ( ) "
        f"RATIONAL_B_SPLINE_SURFACE ( {weight_text} ) "
        "REPRESENTATION_ITEM ( 'NONE' ) SURFACE ( ) )"
    )


def parameter_map(node):
    name = node.name
    if name == "PLANE":
        return lambda u, v: (u * SCALE, v * SCALE)
    if name == "CYLINDER":
        return lambda u, v: (u, v * SCALE)
    if name == "CONE":
        cosine = node.values.get("cos_half_angle")
        if not isinstance(cosine, (int, float)) or cosine == 0.0:
            return None
        return lambda u, v: (u, v / cosine * SCALE)
    if name == "TORUS":
        if node.values.get("major_radius", 0.0) < 0.0:
            return None
        return lambda u, v: (u, v)
    if name == "B_SURFACE":
        return lambda u, v: (u, v)
    if name == "SWEPT_SURF":
        return lambda u, v: (u, v)
    if name == "OFFSET_SURF":
        basis = node.values.get("surface")
        return None if basis is None else _OFFSET_INHERITS
    return None


_OFFSET_INHERITS = object()


def parameter_map_of(model, node, depth=0):
    found = parameter_map(node)
    if found is not _OFFSET_INHERITS:
        return found
    if depth > 8:
        return None
    basis = model.get(node.values.get("surface", 0))
    return None if basis is None else parameter_map_of(model, basis, depth + 1)


def _write_bspline_curve_2d(writer: Writer, spec) -> int:
    polygon = "( " + ", ".join(
        f"#{writer.point2d(pole)}" for pole in spec["poles"]
    ) + " )"
    knot_data = (
        f"( {_integers(spec['mults'])} ), ( {_numbers(spec['distinct'])} ), "
        ".UNSPECIFIED."
    )
    if not spec["rational"]:
        return writer.add(
            f"B_SPLINE_CURVE_WITH_KNOTS ( 'NONE', {spec['degree']}, "
            f"{polygon}, .UNSPECIFIED., .F., .F., {knot_data} )"
        )
    return writer.add(
        "( BOUNDED_CURVE ( ) "
        f"B_SPLINE_CURVE ( {spec['degree']}, {polygon}, .UNSPECIFIED., "
        ".F., .F. ) "
        f"B_SPLINE_CURVE_WITH_KNOTS ( {knot_data} ) CURVE ( ) "
        "GEOMETRIC_REPRESENTATION_ITEM ( ) "
        f"RATIONAL_B_SPLINE_CURVE ( ( {_numbers(spec['weights'])} ) ) "
        "REPRESENTATION_ITEM ( 'NONE' ) )"
    )


def _on_a_blend(model, surface, depth=0) -> bool:
    if surface is None or depth > 8:
        return False
    if surface.name == "BLENDED_EDGE":
        return True
    if surface.name == "OFFSET_SURF":
        return _on_a_blend(model, model.get(surface.values.get("surface", 0)),
                           depth + 1)
    return False


def _on_the_vertices(poles, ends):
    if ends is None or ends[0] is None or ends[1] is None:
        return poles
    premier, dernier = ends
    droit = math.dist(poles[0], premier) + math.dist(poles[-1], dernier)
    inverse = math.dist(poles[0], dernier) + math.dist(poles[-1], premier)
    if inverse < droit:
        premier, dernier = dernier, premier
    poles = [list(p) for p in poles]
    poles[0], poles[-1] = list(premier), list(dernier)
    return poles


def _write_cubic_3d(writer: Writer, poles, knots, mults) -> int:
    polygon = "( " + ", ".join(f"#{writer.point(pole)}" for pole in poles) + " )"
    return writer.add(
        f"B_SPLINE_CURVE_WITH_KNOTS ( 'NONE', 3, {polygon}, .UNSPECIFIED., "
        f".F., .F., ( {_integers(mults)} ), ( {_numbers(knots)} ), "
        ".UNSPECIFIED. )"
    )


def _sense(value) -> str:
    return chr(value) if isinstance(value, int) else str(value)


def _pinched_pieces(halfedges):
    edges = [h.values.get("edge", 0) for h in halfedges if h.values.get("edge", 0)]
    if len(edges) != len(set(edges)):
        return [halfedges]
    pieces, stack, ends = [], [], {}
    for halfedge in halfedges:
        stack.append(halfedge)
        vertex = halfedge.values.get("vertex", 0)
        if not vertex:
            continue
        if vertex in ends:
            start = ends[vertex]
            piece = stack[start + 1:]
            del stack[start + 1:]
            ends = {v: k for v, k in ends.items() if k <= start}
            if any(h.values.get("edge", 0) for h in piece):
                pieces.append(piece)
        else:
            ends[vertex] = len(stack) - 1
    if stack:
        pieces.append(stack)
    return pieces


class Translator:
    def __init__(self, model: Model, writer: Writer) -> None:
        self.model = model
        self.writer = writer
        self.report = WriteReport()
        self._vertices: dict[int, int] = {}
        self._edges: dict[int, int] = {}
        self._surfaces: dict[int, int] = {}
        self._blend_charts: dict[int, list] | None = None
        self._blend_sides: dict[int, tuple] = {}
        self._vertex_slack: dict[tuple, float] = {}
        self._curves: dict[int, int] = {}
        self._trimmed: dict[tuple, int] = {}
        self._vertex_loops: set[int] = set()
        self._param_context: int | None = None
        self.faces: dict[int, int] = {}
        self.bodies: dict[int, int] = {}

    def vertex(self, node: Node) -> int:
        if node.index not in self._vertices:
            point = self.model.vertex_point(node)
            if point is None:
                raise StepError(f"vertex {node.index} has no point")
            self._vertices[node.index] = self.writer.add(
                f"VERTEX_POINT ( 'NONE', #{self.writer.point(point)} )"
            )
        return self._vertices[node.index]

    def surface(self, node: Node) -> int | None:
        if node.index not in self._surfaces:
            written = write_surface(self.writer, node, self.model)
            if written is None:
                return None
            self._surfaces[node.index] = written
        return self._surfaces[node.index]

    def curve(self, node: Node, ends=None) -> int | None:
        basis = node
        while basis is not None and basis.name == "TRIMMED_CURVE":
            basis = self.model.get(basis.values.get("basis_curve", 0))
        if basis is not None and basis.name == "SP_CURVE":
            if node.index not in self._curves:
                written = self.sp_curve(node, basis)
                if written is None:
                    return None
                self._curves[node.index] = written
            return self._curves[node.index]
        if basis is not None and basis.name == "INTERSECTION":
            key = (node.index, ends)
            if key not in self._trimmed:
                written = self.intersection(basis, ends)
                if written is None:
                    return None
                self._trimmed[key] = written
            return self._trimmed[key]
        if node.index not in self._curves:
            written = write_curve(self.writer, node, self.model)
            if written is None:
                return None
            self._curves[node.index] = written
        return self._curves[node.index]

    def sp_curve(self, node: Node, basis: Node) -> int | None:
        span = None
        trim = node
        seen: set[int] = set()
        while trim is not None and trim.name == "TRIMMED_CURVE":
            if trim.index in seen:
                return None
            seen.add(trim.index)
            first = trim.values.get("parm_1")
            last = trim.values.get("parm_2")
            if isinstance(first, (int, float)) and isinstance(last, (int, float)):
                span = (first, last)
            trim = self.model.get(trim.values.get("basis_curve", 0))

        surface_node = self.model.get(basis.values.get("surface", 0))
        b_curve = self.model.get(basis.values.get("b_curve", 0))
        if surface_node is None or b_curve is None:
            return None
        if surface_node.name not in SURFACES:
            return None
        try:
            spec = evaluate.read_nurbs_curve(self.model, b_curve)
        except evaluate.EvaluateError:
            return None
        if spec["dim"] != PARAMETER_SPACE_DIM:
            return None
        try:
            spec, span = evaluate.unrolled_span(self.model, surface_node, spec, span)
        except evaluate.EvaluateError:
            self.report._note(
                self.report.skipped_curves, "(SP_CURVE will not evaluate)"
            )
            return None
        if span is not None and span[0] > span[1]:
            mirror = spec["distinct"][0] + spec["distinct"][-1]
            spec = evaluate.reversed_curve(spec)
            span = (mirror - span[1], mirror - span[0])
        try:
            poles, knots, mults, error = evaluate.adaptive_cubic(
                self.model, surface_node, spec, span=span
            )
        except evaluate.EvaluateError:
            self.report._note(
                self.report.skipped_curves, "(SP_CURVE will not evaluate)"
            )
            return None
        self.report.approximated_curves += 1
        self.report.worst_curve_error = max(self.report.worst_curve_error, error)
        return _write_cubic_3d(self.writer, poles, knots, mults)

    def intersection(self, node: Node, ends=None) -> int | None:
        blend = self._blend_boundary(node)
        if blend:
            _points, declared, _read = evaluate.dense_chart(self.model, node)
            support, side = self._blend_sides[node.index]
            points = evaluate.refined_blend_boundary(
                self.model, support, side, blend, ends, declared)
        else:
            points, declared, _read = evaluate.clipped_chart(
                self.model, node, ends, self._slack(ends))
        if len(points) < 2:
            self.report._note(
                self.report.skipped_curves, "INTERSECTION (chart too short)"
            )
            return None
        try:
            poles, knots, mults = evaluate.interpolating_cubic(points)
        except evaluate.EvaluateError:
            self.report._note(
                self.report.skipped_curves, "INTERSECTION (chart degenerate)"
            )
            return None
        self.report.charted_curves += 1
        self.report.worst_chart_error = max(self.report.worst_chart_error, declared)
        return _write_cubic_3d(self.writer, poles, knots, mults)

    def _slack(self, ends):
        if ends is None:
            return (0.0, 0.0)
        return tuple(self._vertex_slack.get(point, 0.0) for point in ends)

    def _blend_boundary(self, node: Node):
        if self._blend_charts is None:
            self._blend_charts = self._read_blend_boundaries()
        return self._blend_charts.get(node.index)

    def _read_blend_boundaries(self) -> dict[int, list]:
        found: dict[int, list] = {}
        for face in self.model.of_type("FACE"):
            blend = self.model.surface_of(face)
            if blend is None or blend.name != "BLENDED_EDGE":
                continue
            for loop in self.model.loops_of(face):
                for halfedge in self.model.halfedges_of(loop):
                    edge = self.model.get(halfedge.values.get("edge", 0))
                    if edge is None:
                        continue
                    curve = self.model.curve_of(edge)
                    if curve is None:
                        continue
                    if curve.name == "TRIMMED_CURVE":
                        curve = self.model.get(
                            curve.values.get("basis_curve", 0)) or curve
                    if curve.name != "INTERSECTION" or curve.index in found:
                        continue
                    side = evaluate.blend_boundary_side(
                        self.model, curve, blend.index)
                    if side is None:
                        continue
                    try:
                        chart, _declared = evaluate.chart_points(
                            self.model, curve)
                        found[curve.index] = evaluate.blend_touches(
                            self.model, blend, side, chart)
                        self._blend_sides[curve.index] = (blend, side)
                    except evaluate.EvaluateError:
                        continue
        return found

    def _parameter_space(self) -> int:
        if self._param_context is None:
            self._param_context = self.writer.add(
                "( GEOMETRIC_REPRESENTATION_CONTEXT ( 2 ) "
                "PARAMETRIC_REPRESENTATION_CONTEXT ( ) "
                "REPRESENTATION_CONTEXT ( '2D SPACE', '' ) )"
            )
        return self._param_context

    def _sp_geometry(self, halfedge: Node):
        node = self.model.get(halfedge.values.get("curve", 0))
        span = None
        if node is not None and node.name == "TRIMMED_CURVE":
            first = node.values.get("parm_1")
            last = node.values.get("parm_2")
            if isinstance(first, (int, float)) and isinstance(last, (int, float)):
                span = (first, last)
            node = self.model.get(node.values.get("basis_curve", 0))
        if node is None or node.name != "SP_CURVE":
            return None
        along = ((_sense(halfedge.values.get("sense")) == "+")
                 == (_sense(node.values.get("sense")) == "+"))
        surface_node = self.model.get(node.values.get("surface", 0))
        b_curve = self.model.get(node.values.get("b_curve", 0))
        if surface_node is None or b_curve is None:
            return None
        if surface_node.name not in SURFACES:
            return None
        try:
            spec = evaluate.read_nurbs_curve(self.model, b_curve)
        except evaluate.EvaluateError:
            return None
        if spec["dim"] != PARAMETER_SPACE_DIM:
            return None
        try:
            spec, span = evaluate.unrolled_span(self.model, surface_node, spec, span)
        except evaluate.EvaluateError:
            return None
        if not along:
            mirror = spec["distinct"][0] + spec["distinct"][-1]
            spec = evaluate.reversed_curve(spec)
            if span is not None:
                span = (mirror - span[1], mirror - span[0])
        return surface_node, spec, span, along

    def _pcurve(self, halfedge: Node) -> tuple[int, Node, dict] | None:
        read = self._sp_geometry(halfedge)
        if read is None:
            return None
        surface_node, spec, span, along = read
        if surface_node.name in REPARAMETRISED:
            return None
        surface = self.surface(surface_node)
        if surface is None:
            return None
        onto = parameter_map_of(self.model, surface_node)
        if onto is None:
            return None
        written = dict(spec, poles=[onto(u, v) for u, v in spec["poles"]])
        curve2d = _write_bspline_curve_2d(self.writer, written)
        definition = self.writer.add(
            f"DEFINITIONAL_REPRESENTATION ( 'NONE', ( #{curve2d} ), "
            f"#{self._parameter_space()} )"
        )
        pcurve = self.writer.add(
            f"PCURVE ( 'NONE', #{surface}, #{definition} )"
        )
        return pcurve, surface_node, spec, span, along

    def surface_curve(self, halfedge: Node, ends=None) -> int | None:
        partner = self.model.get(halfedge.values.get("other", 0))
        reads = [self._sp_geometry(halfedge)]
        if partner is not None:
            reads.append(self._sp_geometry(partner))
        reads = [read for read in reads if read is not None]
        if not reads:
            return None
        exact = [read for read in reads if not _on_a_blend(self.model, read[0])]
        surface_node, spec, span, _along = (exact or reads)[0]
        try:
            poles, knots, mults, error = evaluate.adaptive_cubic(
                self.model, surface_node, spec, span=span
            )
        except evaluate.EvaluateError:
            self.report._note(
                self.report.skipped_curves, "(SP_CURVE will not evaluate)"
            )
            return None
        self.report.approximated_curves += 1
        self.report.worst_curve_error = max(self.report.worst_curve_error, error)
        poles = _on_the_vertices(poles, ends)
        curve3d = _write_cubic_3d(self.writer, poles, knots, mults)

        sides = [self._pcurve(halfedge)]
        if partner is not None:
            sides.append(self._pcurve(partner))
        written = [one[0] for one in sides if one is not None]
        if not written:
            self.report.approximated_without_pcurve += 1
            return curve3d
        geometry = ", ".join(f"#{one}" for one in written)
        return self.writer.add(
            f"SURFACE_CURVE ( 'NONE', #{curve3d}, ( {geometry} ), "
            ".PCURVE_S1. )"
        )

    def edge(self, halfedge: Node) -> int | None:
        edge_node = self.model.get(halfedge.values.get("edge", 0))
        if edge_node is None:
            self.report._note(
                self.report.skipped_topology, "(halfedge without edge)"
            )
            return None
        if edge_node.index in self._edges:
            return self._edges[edge_node.index]

        forward = halfedge
        if _sense(halfedge.values.get("sense")) != "-":
            other = self.model.get(halfedge.values.get("other", 0))
            forward = other if other is not None else halfedge
        partner = self.model.get(forward.values.get("other", 0))
        start = self.model.get(forward.values.get("vertex", 0))
        end = self.model.get(partner.values.get("vertex", 0)) if partner else None
        ends = None
        if start is not None and end is not None:
            here = self.model.vertex_point(start)
            there = self.model.vertex_point(end)
            if here is not None and there is not None:
                ends = (tuple(here), tuple(there))
                for vertex, point in ((start, ends[0]), (end, ends[1])):
                    self._vertex_slack[point] = vertex.values.get("tolerance") or 0.0

        curve_node = self.model.curve_of(edge_node)
        seam_point = None
        if curve_node is None and (start is None or end is None):
            seam_point = self._fin_seam(halfedge)
            if seam_point is not None:
                ends = (seam_point, seam_point)
        if curve_node is None:
            curve = self.surface_curve(halfedge, ends)
            if curve is None:
                self.report._note(
                    self.report.skipped_curves, "(edge without curve)"
                )
                return None
        elif curve_node.name not in CURVES:
            self.report._note(self.report.skipped_curves, curve_node.name)
            return None
        else:
            curve = self.curve(curve_node, ends)
            if curve is None:
                self.report._note(self.report.skipped_curves, curve_node.name)
                return None

        if start is None or end is None:
            if curve_node is not None:
                seam = self._seam_vertex(curve_node)
            elif seam_point is not None:
                seam = self._point_vertex(seam_point)
            else:
                seam = None
            if seam is None:
                self.report._note(
                    self.report.skipped_curves,
                    f"closed {curve_node.name}" if curve_node
                    else "(closed tolerant edge)",
                )
                return None
            first = last = seam
        else:
            first, last = self.vertex(start), self.vertex(end)
        basis = curve_node
        while basis is not None and basis.name == "TRIMMED_CURVE":
            basis = self.model.get(basis.values.get("basis_curve", 0))
        if curve_node is not None:
            if basis is not None and basis.name == "INTERSECTION":
                pair = self._intersection_pcurves(basis, halfedge, ends)
                if pair:
                    joined = ", ".join(f"#{one}" for one in pair)
                    curve = self.writer.add(
                        f"SURFACE_CURVE ( 'NONE', #{curve}, "
                        f"( {joined} ), .CURVE_3D. )"
                    )
        oriented = basis is not None and basis.name == "INTERSECTION" and ends is not None
        sense = ".T." if oriented else self._curve_sense(curve_node)
        written = self.writer.add(
            f"EDGE_CURVE ( 'NONE', #{first}, #{last}, #{curve}, {sense} )"
        )
        self._edges[edge_node.index] = written
        return written

    def _face_of(self, halfedge):
        if halfedge is None:
            return None
        loop = self.model.get(halfedge.values.get("loop", 0))
        return self.model.get(loop.values.get("face", 0)) if loop else None

    def _intersection_pcurves(self, basis, halfedge, ends):
        points, _declared, read = evaluate.dense_chart(self.model, basis)
        if read is None:
            return None
        surfaces, samples = read
        partner = self.model.get(halfedge.values.get("other", 0))
        wanted = [self._face_of(halfedge), self._face_of(partner)]
        if not all(wanted):
            return None
        carried = [self.model.surface_of(face) for face in wanted]
        if not all(carried):
            return None
        order = []
        for surface in carried:
            found = [i for i, s in enumerate(surfaces) if s.index == surface.index]
            if len(found) != 1 or found[0] in order:
                return None
            order.append(found[0])

        run = evaluate.clip_run(points, ends)
        if run is not None and not self._blend_boundary(basis):
            _clipped, _declared, cut = evaluate.clipped_chart(
                self.model, basis, ends, self._slack(ends))
            if cut is not None:
                samples = [pairs[1:-1] for pairs in cut[1]]
                run = list(range(len(samples[0])))
        written = []
        for side in order:
            one = self._one_pcurve(surfaces[side], samples[side], run, ends)
            if one is not None:
                written.append(one)
        return written or None

    def _one_pcurve(self, surface, pairs, run, ends):
        if pairs is None or surface.name not in PCURVE_EXACT:
            return None
        onto = parameter_map_of(self.model, surface)
        if onto is None:
            return None
        if run is None:
            kept = list(pairs)
        else:
            stops = [
                evaluate.surface_parameters(self.model, surface, list(end))
                for end in ends
            ]
            if not all(stops):
                return None
            kept = [stops[0]] + [pairs[i] for i in run] + [stops[1]]
        periods = evaluate.PERIODS.get(surface.name, (None, None))
        across = evaluate.unwrap([u for u, _v in kept], periods[0])
        along = evaluate.unwrap([v for _u, v in kept], periods[1])
        try:
            poles, knots, mults = evaluate.interpolating_cubic(
                [onto(u, v) for u, v in zip(across, along, strict=True)]
            )
        except evaluate.EvaluateError:
            return None
        self.report.charted_pcurves += 1
        curve2d = _write_bspline_curve_2d(self.writer, {
            "poles": poles, "degree": 3, "mults": mults,
            "distinct": knots, "rational": False,
        })
        definition = self.writer.add(
            f"DEFINITIONAL_REPRESENTATION ( 'NONE', ( #{curve2d} ), "
            f"#{self._parameter_space()} )"
        )
        return self.writer.add(
            f"PCURVE ( 'NONE', #{self.surface(surface)}, #{definition} )"
        )

    def _curve_sense(self, curve_node) -> str:
        basis = curve_node
        while basis is not None and basis.name == "TRIMMED_CURVE":
            basis = self.model.get(basis.values.get("basis_curve", 0))
        if basis is None:
            return ".T."
        return ".F." if _sense(basis.values.get("sense")) == "-" else ".T."

    def _seam_vertex(self, curve_node: Node) -> int | None:
        basis = curve_node
        while basis is not None and basis.name == "TRIMMED_CURVE":
            basis = self.model.get(basis.values.get("basis_curve", 0))
        if basis is not None and basis.name == "INTERSECTION":
            points, _ = evaluate.chart_points(self.model, basis)
            if not points:
                return None
            return self.writer.add(
                f"VERTEX_POINT ( 'NONE', #{self.writer.point(points[0])} )"
            )
        values = curve_node.values
        if curve_node.name == "TRIMMED_CURVE":
            one, two = values.get("point_1"), values.get("point_2")
            if one is None or two is None:
                return None
            if math.dist(one, two) > evaluate.CLOSURE:
                return None
            return self._point_vertex(one)
        if curve_node.name == "B_CURVE":
            try:
                spec = evaluate.read_nurbs_curve(self.model, curve_node)
            except evaluate.EvaluateError:
                return None
            first, last = evaluate.base_range(spec)
            start = evaluate.curve_point(self.model, curve_node, first)
            end = evaluate.curve_point(self.model, curve_node, last)
            if math.dist(start, end) > evaluate.CLOSURE:
                return None
            return self._point_vertex(start)
        if curve_node.name not in ("CIRCLE", "ELLIPSE"):
            return None
        centre = values.get("centre")
        reference = values.get("x_axis")
        if centre is None or reference is None:
            return None
        radius = values.get("radius", values.get("major_radius"))
        if radius is None:
            return None
        point = [centre[i] + reference[i] * radius for i in range(3)]
        return self._point_vertex(point)

    def _point_vertex(self, point) -> int:
        return self.writer.add(
            f"VERTEX_POINT ( 'NONE', #{self.writer.point(point)} )"
        )

    def _fin_seam(self, halfedge: Node):
        partner = self.model.get(halfedge.values.get("other", 0))
        for fin in (halfedge, partner):
            if fin is None:
                continue
            trim = self.model.get(fin.values.get("curve", 0))
            if trim is None or trim.name != "TRIMMED_CURVE":
                continue
            one, two = trim.values.get("point_1"), trim.values.get("point_2")
            if one is None or two is None:
                continue
            if math.dist(one, two) <= evaluate.CLOSURE:
                return tuple(one)
        return None

    def _sphere_seam(self, surface_node: Node) -> int | None:
        values = surface_node.values
        centre = values.get("centre")
        axis = values.get("axis")
        reference = values.get("x_axis")
        radius = values.get("radius")
        if centre is None or axis is None or reference is None or not radius:
            return None
        normal = [
            reference[1] * axis[2] - reference[2] * axis[1],
            reference[2] * axis[0] - reference[0] * axis[2],
            reference[0] * axis[1] - reference[1] * axis[0],
        ]
        placement = self.writer.placement(centre, normal, reference)
        circle = self.writer.add(
            f"CIRCLE ( 'NONE', #{placement}, {_real(radius * SCALE)} )"
        )
        poles = []
        for side in (-1.0, 1.0):
            pole = [centre[i] + side * radius * axis[i] for i in range(3)]
            poles.append(
                self.writer.add(
                    f"VERTEX_POINT ( 'NONE', #{self.writer.point(pole)} )"
                )
            )
        edge = self.writer.add(
            f"EDGE_CURVE ( 'NONE', #{poles[0]}, #{poles[1]}, #{circle}, .T. )"
        )
        up = self.writer.add(f"ORIENTED_EDGE ( 'NONE', *, *, #{edge}, .T. )")
        down = self.writer.add(f"ORIENTED_EDGE ( 'NONE', *, *, #{edge}, .F. )")
        return self.writer.add(f"EDGE_LOOP ( 'NONE', ( #{up}, #{down} ) )")

    def loops(self, loop_node: Node) -> list[int] | None:
        halfedges = list(self.model.halfedges_of(loop_node))
        pieces = _pinched_pieces(halfedges)
        if len(pieces) == 1:
            written = self.loop(loop_node)
            return None if written is None else [written]
        out = []
        for piece in pieces:
            written = self._edge_loop(piece)
            if written is None:
                return None
            out.append(written)
        self.report.pinched_loops += 1
        return out

    def loop(self, loop_node: Node) -> int | None:
        halfedges = list(self.model.halfedges_of(loop_node))
        if len(halfedges) == 1 and not halfedges[0].values.get("edge", 0):
            apex = self.model.get(halfedges[0].values.get("vertex", 0))
            if apex is not None and self.model.vertex_point(apex) is not None:
                written = self.writer.add(
                    f"VERTEX_LOOP ( 'NONE', #{self.vertex(apex)} )"
                )
                self._vertex_loops.add(written)
                return written
        return self._edge_loop(halfedges)

    def _edge_loop(self, halfedges: list[Node]) -> int | None:
        oriented = []
        for halfedge in halfedges:
            if not halfedge.values.get("edge", 0):
                continue
            edge = self.edge(halfedge)
            if edge is None:
                return None
            way = ".T." if _sense(halfedge.values.get("sense")) != "-" else ".F."
            entity = self.writer.add(
                f"ORIENTED_EDGE ( 'NONE', *, *, #{edge}, {way} )"
            )
            oriented.append(f"#{entity}")
        if not oriented:
            self.report._note(self.report.skipped_topology, "(empty loop)")
            return None
        return self.writer.add(
            f"EDGE_LOOP ( 'NONE', ( {', '.join(oriented)} ) )"
        )

    def face(self, face_node: Node) -> int | None:
        surface_node = self.model.surface_of(face_node)
        if surface_node is None or surface_node.name not in SURFACES:
            self.report._note(
                self.report.skipped_surfaces,
                surface_node.name if surface_node else "(face without surface)",
            )
            return None
        surface = self.surface(surface_node)
        if surface is None:
            self.report._note(self.report.skipped_surfaces, surface_node.name)
            return None

        bounds = []
        outer_taken = False
        for loop_node in self.model.loops_of(face_node):
            written = self.loops(loop_node)
            if written is None:
                return None
            for loop in written:
                apex = loop in self._vertex_loops
                plain = apex or outer_taken or len(written) > 1
                kind = "FACE_BOUND" if plain else "FACE_OUTER_BOUND"
                outer_taken = outer_taken or not apex
                entity = self.writer.add(f"{kind} ( 'NONE', #{loop}, .T. )")
                bounds.append(f"#{entity}")
        if not bounds and surface_node.name == "SPHERE":
            seam = self._sphere_seam(surface_node)
            if seam is not None:
                entity = self.writer.add(
                    f"FACE_OUTER_BOUND ( 'NONE', #{seam}, .T. )"
                )
                bounds.append(f"#{entity}")
        if not bounds:
            self.report._note(
                self.report.skipped_topology, "(face without bounds)"
            )
            return None

        same = _same_sense(face_node, surface_node)
        written = self.writer.add(
            f"ADVANCED_FACE ( 'F{face_node.index}', ( {', '.join(bounds)} ), "
            f"#{surface}, {same} )"
        )
        self.report.faces += 1
        self.faces[face_node.index] = written
        return written

    def _forget_since(self, mark: int, faces_written: int) -> None:
        self.writer.rewind(mark)
        for memo in (
            self._vertices, self._edges, self._surfaces, self._curves,
            self.faces,
        ):
            for index in [i for i, at in memo.items() if at > mark]:
                del memo[index]
        for key in [k for k, at in self._trimmed.items() if at > mark]:
            del self._trimmed[key]
        self._vertex_loops -= {n for n in self._vertex_loops if n > mark}
        if self._param_context is not None and self._param_context > mark:
            self._param_context = None
        self.report.faces = faces_written

    def body(self, body_node: Node) -> int | None:
        faces = self._body_faces(body_node)
        if faces is None:
            return None
        shell = self.writer.add(
            f"CLOSED_SHELL ( 'NONE', ( {', '.join(faces)} ) )"
        )
        self.report.bodies += 1
        brep = self.writer.add(f"MANIFOLD_SOLID_BREP ( 'NONE', #{shell} )")
        self.bodies[body_node.index] = brep
        return brep

    def sheet(self, body_node: Node) -> int | None:
        faces = self._body_faces(body_node)
        if faces is None:
            return None
        self.report.sheets += 1
        self.report.sheet_faces += len(faces)
        return self.writer.add(
            f"OPEN_SHELL ( 'NONE', ( {', '.join(faces)} ) )"
        )

    def _body_faces(self, body_node: Node) -> list[str] | None:
        mark = self.writer.mark()
        faces_written = self.report.faces
        faces = []
        refused = False
        walked = 0
        for shell in self.model.shells_of(body_node):
            for face_node in self.model.faces_of(shell):
                walked += 1
                face = self.face(face_node)
                if face is None:
                    refused = True
                    continue
                faces.append(f"#{face}")
        if refused:
            self.report.skipped_bodies += 1
            self.report.skipped_body_nodes.append((body_node.index, walked))
            self._forget_since(mark, faces_written)
            return None
        if not faces:
            self._forget_since(mark, faces_written)
            return None
        return faces

    def style(self, rgb: tuple[float, float, float]) -> int:
        colour = self.writer.shared(
            ("RGB", *tuple(rgb)),
            "COLOUR_RGB ( '', " + ", ".join(_real(c) for c in rgb) + " )",
        )
        fill = self.writer.shared(
            ("FASC", colour), f"FILL_AREA_STYLE_COLOUR ( '', #{colour} )"
        )
        area = self.writer.shared(
            ("FAS", fill), f"FILL_AREA_STYLE ( '', ( #{fill} ) )"
        )
        surface = self.writer.shared(
            ("SSFA", area), f"SURFACE_STYLE_FILL_AREA ( #{area} )"
        )
        side = self.writer.shared(
            ("SSS", surface), f"SURFACE_SIDE_STYLE ( '', ( #{surface} ) )"
        )
        usage = self.writer.shared(
            ("SSU", side), f"SURFACE_STYLE_USAGE ( .BOTH. , #{side} )"
        )
        return self.writer.shared(
            ("PSA", usage), f"PRESENTATION_STYLE_ASSIGNMENT (( #{usage} ) )"
        )

    def styled_items(self) -> list[int]:
        colours = self.model.colours()
        items = []
        for owner, entities in ((self.bodies, "bodies"), (self.faces, "faces")):
            for index, entity in owner.items():
                rgb = colours.get(index)
                if rgb is None:
                    continue
                items.append(
                    self.writer.add(
                        f"STYLED_ITEM ( 'NONE', ( #{self.style(rgb)} ), #{entity} )"
                    )
                )
                if entities == "bodies":
                    self.report.coloured_bodies += 1
                else:
                    self.report.coloured_faces += 1
        return items


HEADER = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION (( 'STEP AP214' ), '1' );
FILE_NAME ('%s', '%s', ( '' ), ( '' ), 'openSW', 'openSW', '' );
FILE_SCHEMA (( 'AUTOMOTIVE_DESIGN' ));
ENDSEC;

DATA;
"""


def _context(writer: Writer) -> tuple[int, int]:
    millimetre = writer.add(
        "( LENGTH_UNIT ( ) NAMED_UNIT ( * ) SI_UNIT ( .MILLI., .METRE. ) )"
    )
    radian = writer.add(
        "( NAMED_UNIT ( * ) PLANE_ANGLE_UNIT ( ) SI_UNIT ( $, .RADIAN. ) )"
    )
    steradian = writer.add(
        "( NAMED_UNIT ( * ) SI_UNIT ( $, .STERADIAN. ) SOLID_ANGLE_UNIT ( ) )"
    )
    uncertainty = writer.add(
        f"UNCERTAINTY_MEASURE_WITH_UNIT ( LENGTH_MEASURE( {_real(1e-5)} ), "
        f"#{millimetre}, 'distance_accuracy_value', 'NONE' )"
    )
    context = writer.add(
        "( GEOMETRIC_REPRESENTATION_CONTEXT ( 3 ) "
        f"GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT ( ( #{uncertainty} ) ) "
        f"GLOBAL_UNIT_ASSIGNED_CONTEXT ( ( #{millimetre}, #{radian}, #{steradian} ) ) "
        "REPRESENTATION_CONTEXT ( 'NONE', 'WORKASPACE' ) )"
    )
    return context, millimetre


def _product(writer: Writer, name: str, representation: int) -> None:
    application = writer.add("APPLICATION_CONTEXT ( 'automotive_design' )")
    writer.add(
        "APPLICATION_PROTOCOL_DEFINITION ( 'draft international standard', "
        f"'automotive_design', 1998, #{application} )"
    )
    product_context = writer.add(
        f"PRODUCT_CONTEXT ( 'NONE', #{application}, 'mechanical' )"
    )
    product = writer.add(
        f"PRODUCT ( '{name}', '{name}', '', ( #{product_context} ) )"
    )
    writer.add(
        f"PRODUCT_RELATED_PRODUCT_CATEGORY ( 'part', '', ( #{product} ) )"
    )
    formation = writer.add(
        "PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE ( 'ANY', '', "
        f"#{product}, .NOT_KNOWN. )"
    )
    definition_context = writer.add(
        f"PRODUCT_DEFINITION_CONTEXT ( 'detailed design', #{application}, 'design' )"
    )
    definition = writer.add(
        "PRODUCT_DEFINITION ( 'UNKNOWN', '', "
        f"#{formation}, #{definition_context} )"
    )
    shape = writer.add(f"PRODUCT_DEFINITION_SHAPE ( 'NONE', 'NONE', #{definition} )")
    writer.add(
        f"SHAPE_DEFINITION_REPRESENTATION ( #{shape}, #{representation} )"
    )


def _string_literal(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "''")


def _iso_text(text: str) -> str:
    out: list[str] = []
    run: list[str] = []
    wide = False

    def flush():
        if run:
            out.append(("\\X4\\" if wide else "\\X2\\") + "".join(run) + "\\X0\\")
            run.clear()

    for ch in text:
        code = ord(ch)
        if code < 128:
            flush()
            out.append(ch)
            continue
        astral = code > 0xFFFF
        if run and wide != astral:
            flush()
        wide = astral
        run.append(f"{code:08X}" if astral else f"{code:04X}")
    flush()
    return "".join(out)


def write_step(model: Model, path: str | Path, *, name: str = "part") -> WriteReport:
    name = _string_literal(name)
    writer = Writer()
    context, _ = _context(writer)
    translator = Translator(model, writer)

    solids, sheets = [], []
    partition = getattr(model, PARTITION_MARK, False)
    for body_node in model.bodies:
        if not model.is_solid_body(body_node):
            if partition:
                translator.report.partition_sheets += 1
                continue
            sheet = translator.sheet(body_node)
            if sheet is not None:
                sheets.append(f"#{sheet}")
            continue
        solid = translator.body(body_node)
        if solid is not None:
            solids.append(f"#{solid}")
    if not solids:
        why = translator.report.refusals()
        error = StepError(
            f"no body could be written — {why}" if why
            else "no body could be written, and nothing was refused either: "
                 "this model states no solid body"
        )
        error.report = translator.report
        raise error

    origin = writer.point((0.0, 0.0, 0.0), 1.0)
    axis = writer.add(f"AXIS2_PLACEMENT_3D ( 'NONE', #{origin}, $, $ )")
    representation = writer.add(
        "ADVANCED_BREP_SHAPE_REPRESENTATION ( 'NONE', "
        f"( #{axis}, {', '.join(solids)} ), #{context} )"
    )
    if sheets:
        model_item = writer.add(
            f"SHELL_BASED_SURFACE_MODEL ( 'NONE', ( {', '.join(sheets)} ) )"
        )
        surfaces = writer.add(
            "MANIFOLD_SURFACE_SHAPE_REPRESENTATION ( 'NONE', "
            f"( #{axis}, #{model_item} ), #{context} )"
        )
        writer.add(
            f"SHAPE_REPRESENTATION_RELATIONSHIP ( 'NONE', '', #{representation}, #{surfaces} )"
        )
    _product(writer, name, representation)

    items = translator.styled_items()
    if items:
        writer.add(
            "MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION ( '', ( "
            + ", ".join(f"#{item}" for item in items)
            + f" ), #{context} )"
        )

    body = "\n".join(
        f"#{number} = {text} ;" for number, text in enumerate(writer.lines, start=1)
    )
    final = Path(path)
    partial = final.with_name(final.name + ".part")
    partial.write_text(
        _iso_text(
            (HEADER % (_string_literal(final.name), "2026-01-01T00:00:00"))
            + body
            + "\nENDSEC;\nEND-ISO-10303-21;\n"
        ),
        encoding="ascii",
    )
    os.replace(partial, final)
    return translator.report

