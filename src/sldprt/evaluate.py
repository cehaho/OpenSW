"""Evaluate Parasolid curves and surfaces in pure Python."""

from __future__ import annotations

import math

NULL_DOUBLE = -3.14158e13


class EvaluateError(Exception):
    pass


def _expanded(knots, mults):
    out = []
    for knot, mult in zip(knots, mults, strict=True):
        out.extend([knot] * mult)
    return out


def _span(knots, degree, count, t):
    if t >= knots[count]:
        return count - 1
    if t <= knots[degree]:
        return degree
    low, high = degree, count
    mid = (low + high) // 2
    while t < knots[mid] or t >= knots[mid + 1]:
        if t < knots[mid]:
            high = mid
        else:
            low = mid
        mid = (low + high) // 2
    return mid


def _basis(knots, span, degree, t):
    left = [0.0] * (degree + 1)
    right = [0.0] * (degree + 1)
    values = [1.0] + [0.0] * degree
    for j in range(1, degree + 1):
        left[j] = t - knots[span + 1 - j]
        right[j] = knots[span + j] - t
        saved = 0.0
        for r in range(j):
            denominator = right[r + 1] + left[j - r]
            if denominator == 0.0:
                temp = 0.0
            else:
                temp = values[r] / denominator
            values[r] = saved + right[r + 1] * temp
            saved = left[j - r] * temp
        values[j] = saved
    return values


def evaluate_bspline(degree, poles, weights, knots, t):
    count = len(poles)
    if len(knots) != count + degree + 1:
        raise EvaluateError(
            f"a knot vector of {len(knots)} for {count} poles of degree {degree}"
        )
    span = _span(knots, degree, count, t)
    basis = _basis(knots, span, degree, t)
    dim = len(poles[0])
    total = [0.0] * dim
    divisor = 0.0
    for i, value in enumerate(basis):
        pole = poles[span - degree + i]
        weight = weights[span - degree + i] if weights else 1.0
        scaled = value * weight
        for k in range(dim):
            total[k] += scaled * pole[k]
        divisor += scaled
    if divisor == 0.0:
        raise EvaluateError("a rational curve evaluates to a zero weight")
    return [c / divisor for c in total]


def evaluate_bspline_surface(spec, u, v):
    u_count, v_count = spec["n_u"], spec["n_v"]
    u_span = _span(spec["u_knots"], spec["u_degree"], u_count, u)
    v_span = _span(spec["v_knots"], spec["v_degree"], v_count, v)
    u_basis = _basis(spec["u_knots"], u_span, spec["u_degree"], u)
    v_basis = _basis(spec["v_knots"], v_span, spec["v_degree"], v)
    net, weights = spec["net"], spec["weights"]
    total = [0.0, 0.0, 0.0]
    divisor = 0.0
    for i, u_value in enumerate(u_basis):
        row = u_span - spec["u_degree"] + i
        for j, v_value in enumerate(v_basis):
            column = v_span - spec["v_degree"] + j
            weight = weights[row][column] if weights else 1.0
            scaled = u_value * v_value * weight
            point = net[row][column]
            for k in range(3):
                total[k] += scaled * point[k]
            divisor += scaled
    if divisor == 0.0:
        raise EvaluateError("a rational surface evaluates to a zero weight")
    return [c / divisor for c in total]


def _basis_and_derivative(knots, span, degree, t):
    ndu = [[0.0] * (degree + 1) for _ in range(degree + 1)]
    ndu[0][0] = 1.0
    left = [0.0] * (degree + 1)
    right = [0.0] * (degree + 1)
    for j in range(1, degree + 1):
        left[j] = t - knots[span + 1 - j]
        right[j] = knots[span + j] - t
        saved = 0.0
        for r in range(j):
            ndu[j][r] = right[r + 1] + left[j - r]
            temp = ndu[r][j - 1] / ndu[j][r] if ndu[j][r] != 0.0 else 0.0
            ndu[r][j] = saved + right[r + 1] * temp
            saved = left[j - r] * temp
        ndu[j][j] = saved
    values = [ndu[r][degree] for r in range(degree + 1)]
    if degree == 0:
        return values, [0.0]
    derivatives = []
    for r in range(degree + 1):
        d = 0.0
        if r >= 1 and ndu[degree][r - 1] != 0.0:
            d += ndu[r - 1][degree - 1] / ndu[degree][r - 1]
        if r <= degree - 1 and ndu[degree][r] != 0.0:
            d -= ndu[r][degree - 1] / ndu[degree][r]
        derivatives.append(degree * d)
    return values, derivatives


def bspline_surface_partials(spec, u, v):
    u_count, v_count = spec["n_u"], spec["n_v"]
    p, q = spec["u_degree"], spec["v_degree"]
    u_span = _span(spec["u_knots"], p, u_count, u)
    v_span = _span(spec["v_knots"], q, v_count, v)
    nu, du = _basis_and_derivative(spec["u_knots"], u_span, p, u)
    nv, dv = _basis_and_derivative(spec["v_knots"], v_span, q, v)
    net, weights = spec["net"], spec["weights"]
    a = [0.0, 0.0, 0.0]
    a_u = [0.0, 0.0, 0.0]
    a_v = [0.0, 0.0, 0.0]
    w = w_u = w_v = 0.0
    for i in range(p + 1):
        row = u_span - p + i
        for j in range(q + 1):
            column = v_span - q + j
            weight = weights[row][column] if weights else 1.0
            point = net[row][column]
            b = nu[i] * nv[j] * weight
            b_u = du[i] * nv[j] * weight
            b_v = nu[i] * dv[j] * weight
            for k in range(3):
                a[k] += b * point[k]
                a_u[k] += b_u * point[k]
                a_v[k] += b_v * point[k]
            w += b
            w_u += b_u
            w_v += b_v
    if w == 0.0:
        raise EvaluateError("a rational surface evaluates to a zero weight")
    point = [c / w for c in a]
    return (
        point,
        [(a_u[k] - w_u * point[k]) / w for k in range(3)],
        [(a_v[k] - w_v * point[k]) / w for k in range(3)],
    )


EXTENSION_STEP = 1.0e-4


def _inward(function, at, low, high, width):
    step = EXTENSION_STEP * width
    sign = 1.0 if at <= low else -1.0
    f0 = function(at)
    f1 = function(at + sign * step)
    f2 = function(at + 2.0 * sign * step)
    return [(-3.0 * f0[k] + 4.0 * f1[k] - f2[k]) / (2.0 * sign * step)
            for k in range(3)]


def extended_bspline_surface(spec, u, v):
    u_low, u_high = spec["u_knots"][spec["u_degree"]], spec["u_knots"][spec["n_u"]]
    v_low, v_high = spec["v_knots"][spec["v_degree"]], spec["v_knots"][spec["n_v"]]
    u_width, v_width = u_high - u_low, v_high - v_low
    if spec.get("u_periodic") and u_width > 0.0 and not u_low <= u <= u_high:
        u = u_low + (u - u_low) % u_width
    if spec.get("v_periodic") and v_width > 0.0 and not v_low <= v <= v_high:
        v = v_low + (v - v_low) % v_width
    u_in = min(max(u, u_low), u_high)
    v_in = min(max(v, v_low), v_high)
    a, c = u - u_in, v - v_in
    if a == 0.0 and c == 0.0:
        return evaluate_bspline_surface(spec, u, v)
    point, du, dv = bspline_surface_partials(spec, u_in, v_in)
    out = list(point)
    if a != 0.0:
        out = [out[k] + a * du[k] for k in range(3)]
    if c != 0.0:
        out = [out[k] + c * dv[k] for k in range(3)]
    if a != 0.0 and c != 0.0:
        cross = _inward(
            lambda s: bspline_surface_partials(spec, u_in, s)[1],
            v_in, v_low, v_high, v_width,
        )
        out = [out[k] + a * c * cross[k] for k in range(3)]
    return out


def extended_bspline_curve(spec, t):
    low, high = base_range(spec)
    width = high - low
    if spec.get("periodic") and width > 0.0 and not low <= t <= high:
        t = low + (t - low) % width

    def point(s):
        return evaluate_bspline(
            spec["degree"], spec["poles"], spec["weights"], spec["knots"], s
        )

    if low <= t <= high or width <= 0.0:
        return point(t)
    edge = low if t < low else high
    start, derivative = bspline_curve_derivative(spec, edge)
    return [start[k] + (t - edge) * derivative[k] for k in range(3)]


def bspline_curve_derivative(spec, t):
    degree, poles, weights, knots = (
        spec["degree"], spec["poles"], spec["weights"], spec["knots"])
    count = len(poles)
    span = _span(knots, degree, count, t)
    values, derivatives = _basis_and_derivative(knots, span, degree, t)
    dim = len(poles[0])
    a, a_t = [0.0] * dim, [0.0] * dim
    w = w_t = 0.0
    for i in range(degree + 1):
        pole = poles[span - degree + i]
        weight = weights[span - degree + i] if weights else 1.0
        b, b_t = values[i] * weight, derivatives[i] * weight
        for k in range(dim):
            a[k] += b * pole[k]
            a_t[k] += b_t * pole[k]
        w += b
        w_t += b_t
    if w == 0.0:
        raise EvaluateError("a rational curve evaluates to a zero weight")
    point = [c / w for c in a]
    return point, [(a_t[k] - w_t * point[k]) / w for k in range(dim)]


def _cut(knot_node, mult_node, count):
    if knot_node is None or mult_node is None:
        raise EvaluateError("a NURBS knot vector is missing")
    knots = knot_node.values["knots"]
    mults = mult_node.values["mult"]
    knots = knots if isinstance(knots, list) else [knots]
    mults = mults if isinstance(mults, list) else [mults]
    if isinstance(count, int) and 0 < count <= len(knots):
        knots, mults = knots[:count], mults[:count]
    if len(knots) != len(mults):
        raise EvaluateError("a NURBS knot vector disagrees with its multiplicities")
    kept = [(k, m) for k, m in zip(knots, mults, strict=True) if m > 0]
    return [k for k, _ in kept], [m for _, m in kept]


_NURBS_CURVE_CACHE = "_courbe_nurbs"


def read_nurbs_curve(model, node):
    nurbs = model.get(node.values.get("nurbs", 0))
    if nurbs is None or nurbs.name != "NURBS_CURVE":
        raise EvaluateError("a B_CURVE has no NURBS_CURVE")
    dim = nurbs.values["vertex_dim"]
    count = nurbs.values["n_vertices"]
    vertex_node = model.get(nurbs.values["bspline_vertices"])
    if vertex_node is None:
        raise EvaluateError("a B_CURVE has no control points")
    values = vertex_node.values["vertices"]
    if len(values) != count * dim:
        raise EvaluateError("a B_CURVE control polygon has the wrong size")
    rational = bool(nurbs.values["rational"])
    spatial = dim - 1 if rational else dim
    poles, weights = [], ([] if rational else None)
    for i in range(count):
        offset = i * dim
        if rational:
            weight = values[offset + spatial]
            if not weight:
                raise EvaluateError("a rational NURBS pole has a zero weight")
            poles.append([values[offset + k] / weight for k in range(spatial)])
            weights.append(weight)
        else:
            poles.append([values[offset + k] for k in range(spatial)])
    knots, mults = _cut(
        model.get(nurbs.values["knots"]), model.get(nurbs.values["knot_mult"]),
        nurbs.values.get("n_knots"),
    )
    return {
        "degree": nurbs.values["degree"],
        "dim": spatial,
        "poles": poles,
        "weights": weights,
        "knots": _expanded(knots, mults),
        "distinct": knots,
        "mults": mults,
        "rational": rational,
        "periodic": bool(nurbs.values.get("periodic")),
    }


_NURBS_SURFACE_CACHE = "_releve_nurbs"


def read_nurbs_surface(model, node):
    garde = getattr(node, _NURBS_SURFACE_CACHE, None)
    if garde is not None:
        return garde
    releve = _read_nurbs_surface(model, node)
    try:
        setattr(node, _NURBS_SURFACE_CACHE, releve)
    except AttributeError:
        pass
    return releve


def _read_nurbs_surface(model, node):
    nurbs = model.get(node.values.get("nurbs", 0))
    if nurbs is None or nurbs.name != "NURBS_SURF":
        raise EvaluateError("a B_SURFACE has no NURBS_SURF")
    vertex_node = model.get(nurbs.values["bspline_vertices"])
    if vertex_node is None:
        raise EvaluateError("a B_SURFACE has no control points")
    values = vertex_node.values["vertices"]
    n_u, n_v = nurbs.values["n_u_vertices"], nurbs.values["n_v_vertices"]
    dim = nurbs.values["vertex_dim"]
    rational = bool(nurbs.values["rational"]) and dim == 4
    if len(values) != n_u * n_v * dim:
        raise EvaluateError("a B_SURFACE control net has the wrong size")
    net, weights, offset = [], ([] if rational else None), 0
    for _ in range(n_u):
        row, row_weights = [], []
        for _ in range(n_v):
            weight = values[offset + 3] if rational else 1.0
            if rational and not weight:
                raise EvaluateError("a rational NURBS pole has a zero weight")
            row.append([values[offset + k] / weight for k in range(3)])
            row_weights.append(weight)
            offset += dim
        net.append(row)
        if rational:
            weights.append(row_weights)
    u_knots, u_mults = _cut(
        model.get(nurbs.values["u_knots"]), model.get(nurbs.values["u_knot_mult"]),
        nurbs.values.get("n_u_knots"),
    )
    v_knots, v_mults = _cut(
        model.get(nurbs.values["v_knots"]), model.get(nurbs.values["v_knot_mult"]),
        nurbs.values.get("n_v_knots"),
    )
    return {
        "u_degree": nurbs.values["u_degree"],
        "v_degree": nurbs.values["v_degree"],
        "n_u": n_u, "n_v": n_v,
        "net": net, "weights": weights,
        "u_knots": _expanded(u_knots, u_mults),
        "v_knots": _expanded(v_knots, v_mults),
        "u_periodic": bool(nurbs.values.get("u_periodic")),
        "v_periodic": bool(nurbs.values.get("v_periodic")),
    }


def _frame(values, origin_key, axis_key):
    origin = values[origin_key]
    axis = _unit(values[axis_key])
    reference = values.get("x_axis")
    if reference is None or all(c == 0.0 for c in reference):
        reference = _perpendicular(axis)
    x = _unit(_reject(reference, axis))
    y = _cross(axis, x)
    return origin, axis, x, y


def _unit(v):
    n = math.sqrt(sum(c * c for c in v))
    if n == 0.0:
        raise EvaluateError("a direction of zero length")
    return [c / n for c in v]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def _reject(v, axis):
    d = _dot(v, axis)
    return [v[i] - d * axis[i] for i in range(3)]


def _perpendicular(axis):
    smallest = min(range(3), key=lambda i: abs(axis[i]))
    basis = [0.0, 0.0, 0.0]
    basis[smallest] = 1.0
    return _cross(axis, basis)


def curve_point(model, node, t):
    values = node.values
    if node.name == "LINE":
        origin, direction = values["pvec"], _unit(values["direction"])
        return [origin[i] + t * direction[i] for i in range(3)]
    if node.name == "CIRCLE":
        centre, _axis, x, y = _frame(values, "centre", "normal")
        r = values["radius"]
        return [centre[i] + r * (math.cos(t) * x[i] + math.sin(t) * y[i])
                for i in range(3)]
    if node.name == "ELLIPSE":
        centre, _axis, x, y = _frame(values, "centre", "normal")
        a, b = values["major_radius"], values["minor_radius"]
        return [centre[i] + a * math.cos(t) * x[i] + b * math.sin(t) * y[i]
                for i in range(3)]
    if node.name == "B_CURVE":
        spec = getattr(node, _NURBS_CURVE_CACHE, None)
        if spec is None:
            spec = read_nurbs_curve(model, node)
            try:
                setattr(node, _NURBS_CURVE_CACHE, spec)
            except AttributeError:
                pass
        if spec["dim"] != 3:
            raise EvaluateError("a section curve that is not of the model")
        return extended_bspline_curve(spec, t)
    raise EvaluateError(f"no parametrisation for the curve {node.name}")


DERIVATIVE_STEP = 1.0e-6

DEGENERATE_DERIVATIVE = 1.0e-6

POLE_RETREAT = 1.0e-5


def _uv_domain(model, node):
    if node.name != "B_SURFACE":
        return None
    spec = read_nurbs_surface(model, node)
    return (
        (spec["u_knots"][spec["u_degree"]], spec["u_knots"][spec["n_u"]]),
        (spec["v_knots"][spec["v_degree"]], spec["v_knots"][spec["n_v"]]),
    )


def _partials(model, node, u, v, step=DERIVATIVE_STEP):
    if node.name == "B_SURFACE":
        spec = read_nurbs_surface(model, node)
        u_low, u_high = spec["u_knots"][spec["u_degree"]], spec["u_knots"][spec["n_u"]]
        v_low, v_high = spec["v_knots"][spec["v_degree"]], spec["v_knots"][spec["n_v"]]
        if spec.get("u_periodic") and u_high > u_low:
            u = u_low + (u - u_low) % (u_high - u_low)
        if spec.get("v_periodic") and v_high > v_low:
            v = v_low + (v - v_low) % (v_high - v_low)
        if u_low + step <= u <= u_high - step and v_low + step <= v <= v_high - step:
            _point, du, dv = bspline_surface_partials(spec, u, v)
            return du, dv
    if node.name == "SWEPT_SURF":
        section = model.get(node.values.get("section", 0))
        if section is not None and section.name == "B_CURVE":
            spec = getattr(section, _NURBS_CURVE_CACHE, None) or read_nurbs_curve(
                model, section)
            low, high = base_range(spec)
            width = high - low
            if spec["dim"] == 3 and width > 0.0:
                t = u
                if spec.get("periodic") and not low <= t <= high:
                    t = low + (t - low) % width
                t = min(max(t, low), high)
                _point, du = bspline_curve_derivative(spec, t)
                return du, _unit(node.values["sweep"])
    du = [
        (surface_point(model, node, u + step, v)[i]
         - surface_point(model, node, u - step, v)[i]) / (2.0 * step)
        for i in range(3)
    ]
    dv = [
        (surface_point(model, node, u, v + step)[i]
         - surface_point(model, node, u, v - step)[i]) / (2.0 * step)
        for i in range(3)
    ]
    return du, dv


def _normal(model, node, u, v):
    if node.name == "BLENDED_EDGE":
        radial = _blend_normal(model, node, u, v)
        if radial is not None:
            return radial
    return _normal_from_partials(model, node, u, v)


def _normal_from_partials(model, node, u, v):
    du, dv = _partials(model, node, u, v)
    long_u = math.sqrt(sum(c * c for c in du))
    long_v = math.sqrt(sum(c * c for c in dv))
    if min(long_u, long_v) <= DEGENERATE_DERIVATIVE * max(long_u, long_v):
        domain = _uv_domain(model, node)
        if domain is not None:
            axis = 1 if long_u <= long_v else 0
            low, high = domain[axis]
            if high > low:
                here = (u, v)[axis]
                toward = -1.0 if (here - low) > (high - here) else 1.0
                moved = here + toward * POLE_RETREAT * (high - low)
                moved = min(high, max(low, moved))
                du, dv = _partials(
                    model, node,
                    moved if axis == 0 else u,
                    moved if axis == 1 else v,
                )
    return _unit(_cross(du, dv))


PERIODS = {
    "CYLINDER": (2.0 * math.pi, None),
    "CONE": (2.0 * math.pi, None),
    "SPHERE": (2.0 * math.pi, None),
    "TORUS": (2.0 * math.pi, 2.0 * math.pi),
}


def surface_parameters(model, node, point):
    values = node.values
    name = node.name
    if name == "PLANE":
        origin, _axis, x, y = _frame(values, "pvec", "normal")
        offset = [point[i] - origin[i] for i in range(3)]
        found = (_dot(offset, x), _dot(offset, y))
    elif name in ("CYLINDER", "CONE"):
        origin, axis, x, y = _frame(values, "pvec", "axis")
        offset = [point[i] - origin[i] for i in range(3)]
        found = (math.atan2(_dot(offset, y), _dot(offset, x)) % (2.0 * math.pi),
                 _dot(offset, axis))
    elif name == "SPHERE":
        origin, axis, x, y = _frame(values, "centre", "axis")
        radius = values["radius"]
        if radius == 0.0:
            return None
        offset = [point[i] - origin[i] for i in range(3)]
        height = max(-1.0, min(1.0, _dot(offset, axis) / radius))
        found = (math.atan2(_dot(offset, y), _dot(offset, x)) % (2.0 * math.pi),
                 math.asin(height))
    elif name == "TORUS":
        origin, axis, x, y = _frame(values, "centre", "axis")
        offset = [point[i] - origin[i] for i in range(3)]
        across = math.hypot(_dot(offset, x), _dot(offset, y))
        found = (math.atan2(_dot(offset, y), _dot(offset, x)) % (2.0 * math.pi),
                 math.atan2(_dot(offset, axis),
                            across - values["major_radius"]) % (2.0 * math.pi))
    else:
        return None
    try:
        back = surface_point(model, node, found[0], found[1])
    except EvaluateError:
        return None
    if math.dist(back, point) > SURFACE_INVERSE:
        return None
    return found


SURFACE_INVERSE = 1e-9


def unwrap(values, period):
    if period is None or not values:
        return list(values)
    out = [values[0]]
    for value in values[1:]:
        turns = round((out[-1] - value) / period)
        out.append(value + turns * period)
    return out


def surface_point(model, node, u, v):
    values = node.values
    name = node.name
    if name == "PLANE":
        origin, _axis, x, y = _frame(values, "pvec", "normal")
        return [origin[i] + u * x[i] + v * y[i] for i in range(3)]
    if name == "CYLINDER":
        origin, axis, x, y = _frame(values, "pvec", "axis")
        r = values["radius"]
        return [origin[i] + r * (math.cos(u) * x[i] + math.sin(u) * y[i])
                + v * axis[i] for i in range(3)]
    if name == "CONE":
        origin, axis, x, y = _frame(values, "pvec", "axis")
        sin_a = values["sin_half_angle"]
        cos_a = values["cos_half_angle"]
        if cos_a == 0.0:
            raise EvaluateError("a cone of half-angle 90 degrees")
        radius = values["radius"] + v * (sin_a / cos_a)
        return [origin[i] + v * axis[i]
                + radius * (math.cos(u) * x[i] + math.sin(u) * y[i])
                for i in range(3)]
    if name == "SPHERE":
        origin, axis, x, y = _frame(values, "centre", "axis")
        r = values["radius"]
        return [origin[i] + r * (math.cos(v) * (math.cos(u) * x[i]
                                                + math.sin(u) * y[i])
                                 + math.sin(v) * axis[i]) for i in range(3)]
    if name == "TORUS":
        origin, axis, x, y = _frame(values, "centre", "axis")
        major = values["major_radius"]
        minor = values["minor_radius"]
        ring = major + minor * math.cos(v)
        return [origin[i] + ring * (math.cos(u) * x[i] + math.sin(u) * y[i])
                + minor * math.sin(v) * axis[i] for i in range(3)]
    if name == "B_SURFACE":
        return extended_bspline_surface(read_nurbs_surface(model, node), u, v)
    if name == "SWEPT_SURF":
        section = model.get(values.get("section", 0))
        if section is None:
            raise EvaluateError("a swept surface without a section curve")
        point = curve_point(model, section, u)
        sweep = _unit(values["sweep"])
        return [point[i] + v * sweep[i] for i in range(3)]
    if name == "OFFSET_SURF":
        basis = model.get(values.get("surface", 0))
        if basis is None:
            raise EvaluateError("an offset surface without a basis")
        distance = values["offset"]
        if _sense_of(values.get("sense")) == "-":
            distance = -distance
        point = surface_point(model, basis, u, v)
        normal = _normal(model, basis, u, v)
        return [point[i] + distance * normal[i] for i in range(3)]
    if name == "BLENDED_EDGE":
        return blend_point(model, node, u, v)
    raise EvaluateError(f"no parametrisation for {name}")


def _sense_of(value):
    return chr(value) if isinstance(value, int) else str(value)


def _composed(model, surface_node, curve, t):
    u, v = evaluate_bspline(
        curve["degree"], curve["poles"], curve["weights"], curve["knots"], t
    )[:2]
    return surface_point(model, surface_node, u, v)


CURVE_TOLERANCE = 1e-6

MAX_BISECTIONS = 8


def _bezier_span(model, surface_node, curve, t0, t1, start, end):
    third = (t1 - t0) / 3.0
    pa = _composed(model, surface_node, curve, t0 + third)
    pb = _composed(model, surface_node, curve, t0 + 2.0 * third)
    p1 = [(-5.0 * start[k] + 18.0 * pa[k] - 9.0 * pb[k] + 2.0 * end[k]) / 6.0
          for k in range(3)]
    p2 = [(2.0 * start[k] - 9.0 * pa[k] + 18.0 * pb[k] - 5.0 * end[k]) / 6.0
          for k in range(3)]
    poles = (start, p1, p2, end)
    worst = 0.0
    for i in range(5):
        f = (i + 0.5) / 5.0
        built = _bezier_point(poles, f)
        true = _composed(model, surface_node, curve, t0 + (t1 - t0) * f)
        worst = max(worst, math.dist(built, true))
    return p1, p2, worst


def _bezier_point(poles, f):
    g = 1.0 - f
    a, b, c, d = poles
    return [g * g * g * a[k] + 3.0 * g * g * f * b[k]
            + 3.0 * g * f * f * c[k] + f * f * f * d[k] for k in range(3)]


CLOSURE = 1.0e-9


def base_range(curve):
    return curve["knots"][curve["degree"]], curve["knots"][len(curve["poles"])]


def _homogeneous(curve):
    weights = curve["weights"]
    if weights is None:
        return [[*pole, 1.0] for pole in curve["poles"]]
    return [[c * w for c in pole] + [w]
            for pole, w in zip(curve["poles"], weights, strict=True)]


def _from_rows(curve, rows, knots):
    spatial = len(rows[0]) - 1
    out = dict(curve)
    if curve["weights"] is None:
        out["poles"] = [row[:spatial] for row in rows]
    else:
        out["poles"] = [[c / row[spatial] for c in row[:spatial]] for row in rows]
        out["weights"] = [row[spatial] for row in rows]
    distinct, mults = [], []
    for knot in knots:
        if distinct and knot == distinct[-1]:
            mults[-1] += 1
        else:
            distinct.append(knot)
            mults.append(1)
    out["knots"], out["distinct"], out["mults"] = list(knots), distinct, mults
    return out


def _inserted(rows, knots, degree, u):
    span = max(i for i in range(len(knots) - 1) if knots[i] <= u < knots[i + 1])
    out = []
    for i in range(len(rows) + 1):
        if i <= span - degree:
            out.append(rows[i])
        elif i <= span:
            a = (u - knots[i]) / (knots[i + degree] - knots[i])
            out.append([(1.0 - a) * p + a * q
                        for p, q in zip(rows[i - 1], rows[i], strict=True)])
        else:
            out.append(rows[i - 1])
    return out, [*knots[:span + 1], u, *knots[span + 1:]]


def clamped_curve(curve):
    degree = curve["degree"]
    first, last = base_range(curve)
    knots = list(curve["knots"])
    if (knots[:degree + 1] == [first] * (degree + 1)
            and knots[-degree - 1:] == [last] * (degree + 1)):
        return curve
    rows = _homogeneous(curve)
    for u in (first, last):
        while knots.count(u) < degree:
            rows, knots = _inserted(rows, knots, degree, u)
    start = knots.index(first) + knots.count(first) - 1 - degree
    end = knots.index(last) - 1
    inner = [k for k in knots if first < k < last]
    cut = [first] * (degree + 1) + inner + [last] * (degree + 1)
    rows = rows[start:end + 1]
    if len(rows) + degree + 1 != len(cut):
        raise EvaluateError("a periodic curve would not cut to its base range")
    return _from_rows(curve, rows, cut)


def _two_periods(model, surface_node, curve):
    curve = clamped_curve(curve)
    degree = curve["degree"]
    first, last = base_range(curve)
    period = last - first
    poles = curve["poles"]
    delta = [b - a for a, b in zip(poles[0], poles[-1], strict=True)]
    inner = [k for k in curve["knots"] if first < k < last]
    knots = ([first] * (degree + 1) + inner + [last] * degree
             + [k + period for k in inner] + [last + period] * (degree + 1))
    out = dict(curve)
    out["poles"] = [list(p) for p in poles] + [
        [c + d for c, d in zip(p, delta, strict=True)] for p in poles[1:]
    ]
    weights = curve["weights"]
    if weights is not None:
        ratio = weights[-1] / weights[0]
        out["weights"] = list(weights) + [w * ratio for w in weights[1:]]
    out = _from_rows(out, _homogeneous(out), knots)
    for f in (0.13, 0.5, 0.87):
        t = first + f * period
        here = _composed(model, surface_node, out, t)
        there = _composed(model, surface_node, out, t + period)
        if math.dist(here, there) > CLOSURE:
            raise EvaluateError("a curve closes on its surface only at its ends")
    return out


def unrolled_span(model, surface_node, curve, span):
    if span is None:
        return curve, span
    first, last = base_range(curve)
    period = last - first
    slack = 1.0e-12 * max(1.0, period)
    low, high = min(span), max(span)
    if first - slack <= low and high <= last + slack:
        return curve, span
    if high - low > period + slack:
        raise EvaluateError("a trim longer than its curve's period")
    start = _composed(model, surface_node, curve, first)
    end = _composed(model, surface_node, curve, last)
    if math.dist(start, end) > CLOSURE:
        raise EvaluateError("a trim past the end of a curve that is not closed")
    shift = math.floor((low - first) / period + slack) * period
    moved = (span[0] - shift, span[1] - shift)
    if max(moved) <= last + slack:
        return curve, moved
    return _two_periods(model, surface_node, curve), moved


def adaptive_cubic(model, surface_node, curve, tolerance=CURVE_TOLERANCE,
                   span=None):
    knots = curve["knots"]
    degree = curve["degree"]
    first, last = knots[degree], knots[len(curve["poles"])]
    if span is not None:
        low, high = min(span), max(span)
        slack = 1.0e-12 * max(1.0, last - first)
        if low < first - slack or high > last + slack:
            raise EvaluateError("a trim past the base range of its curve")
        first, last = max(first, low), min(last, high)
    if last <= first:
        raise EvaluateError("a curve whose parameter span does not increase")
    breaks = [k for k in curve["distinct"] if first < k < last]
    edges = [first] + breaks + [last]

    times = [first]
    poles = [_composed(model, surface_node, curve, first)]
    worst = 0.0
    for i in range(len(edges) - 1):
        pending = [(edges[i], edges[i + 1], 0)]
        while pending:
            t0, t1, depth = pending.pop()
            start = poles[-1]
            end = _composed(model, surface_node, curve, t1)
            p1, p2, error = _bezier_span(
                model, surface_node, curve, t0, t1, start, end
            )
            if error > tolerance and depth < MAX_BISECTIONS:
                middle = (t0 + t1) / 2.0
                pending.append((middle, t1, depth + 1))
                pending.append((t0, middle, depth + 1))
                continue
            worst = max(worst, error)
            poles.extend([p1, p2, end])
            times.append(t1)
    mults = [4] + [3] * (len(times) - 2) + [4]
    return poles, times, mults, worst


def _limit_kind(limit):
    kind = limit.values.get("type")
    return chr(kind) if isinstance(kind, int) else kind


def ring_point(model, node):
    start = model.get(node.values.get("start", 0))
    end = model.get(node.values.get("end", 0))
    if start is None or end is None:
        return None
    one = start.values.get("hvec") or []
    two = end.values.get("hvec") or []
    if len(one) < 3 or len(two) < 3 or list(one[:3]) != list(two[:3]):
        return None
    kind = _limit_kind(start)
    if kind != _limit_kind(end) or kind not in ("H", "T"):
        return None
    return list(one[:3]), kind


def chart_points(model, curve_node):
    points, error = [], 0.0
    index = curve_node.values.get("chart", 0)
    seen = set()
    while isinstance(index, int) and index and index not in seen:
        seen.add(index)
        node = model.get(index)
        if node is None or node.name != "CHART":
            break
        flat = node.values.get("hvec") or []
        flat = flat if isinstance(flat, list) else [flat]
        for at in range(0, len(flat) - 2, 3):
            points.append(list(flat[at:at + 3]))
        declared = node.values.get("chordal_error")
        if isinstance(declared, (int, float)) and 0 < declared < 1e6:
            error = max(error, declared)
        index = node.values.get("next", 0)
    ring = ring_point(model, curve_node)
    if ring is not None and len(points) >= 2:
        point, kind = ring
        if max(math.dist(points[0], point), math.dist(points[-1], point)) <= error:
            if kind == "H":
                points[0], points[-1] = list(point), list(point)
            else:
                points = [list(point), *points, list(point)]
    return points, error


def interpolating_cubic(points, alpha=0.5):
    width = len(points[0])
    kept = [points[0]]
    for point in points[1:]:
        if math.dist(point, kept[-1]) > 0.0:
            kept.append(point)
    if len(kept) < 2:
        raise EvaluateError("a chart of fewer than two distinct points")
    if len(kept) == 2:
        start, end = kept
        poles = [start] + [
            [start[k] + (end[k] - start[k]) * f for k in range(width)]
            for f in (1.0 / 3.0, 2.0 / 3.0)
        ] + [end]
        return poles, [0.0, 1.0], [4, 4]

    times = [0.0]
    for i in range(1, len(kept)):
        times.append(times[-1] + math.dist(kept[i], kept[i - 1]) ** alpha)

    tangents = []
    for i in range(len(kept)):
        if i == 0:
            span = times[1] - times[0]
            tangents.append([(kept[1][k] - kept[0][k]) / span for k in range(width)])
        elif i == len(kept) - 1:
            span = times[i] - times[i - 1]
            tangents.append(
                [(kept[i][k] - kept[i - 1][k]) / span for k in range(width)]
            )
        else:
            span = times[i + 1] - times[i - 1]
            tangents.append(
                [(kept[i + 1][k] - kept[i - 1][k]) / span for k in range(width)]
            )

    poles = [kept[0]]
    for i in range(len(kept) - 1):
        span = times[i + 1] - times[i]
        poles.append(
            [kept[i][k] + tangents[i][k] * span / 3.0 for k in range(width)]
        )
        poles.append(
            [kept[i + 1][k] - tangents[i + 1][k] * span / 3.0 for k in range(width)]
        )
        poles.append(kept[i + 1])
    mults = [4] + [3] * (len(kept) - 2) + [4]
    return poles, times, mults


def chart_numbers(model, node, points):
    numbers = _chart_numbers_read(model, node, points)
    if numbers is None:
        return None
    kept = getattr(node, _BLEND_SIDES_CACHE, None)
    if kept is not None and len(kept) == len(numbers):
        return list(kept)
    numbers = _blend_sides_from_points(model, node, points, numbers)
    _keep(node, _BLEND_SIDES_CACHE, tuple(numbers))
    return list(numbers)


_BLEND_SIDES_CACHE = "_cotes_sur_conge"


def _chart_numbers_read(model, node, points):
    data = model.get(node.values.get("intersection_data", 0))
    if data is None:
        return _numbers_from_points(model, node, points)
    numbers = data.values.get("values") or []
    numbers = list(numbers) if isinstance(numbers, list) else [numbers]
    start = model.get(node.values.get("start", 0))
    if (start is not None and _limit_kind(start) == "T"
            and ring_point(model, node) is None
            and len(numbers) == 4 * (len(points) + 1)):
        numbers = numbers[4:]
    return _fill_null_pairs(model, node, points, numbers)


_RECONSTRUCTED_CACHE = "_releve_inverse"

_NEWTON_INVERTIBLE = ("B_SURFACE", "OFFSET_SURF", "SWEPT_SURF")

_INVERSE_GRID = 12


def _numbers_from_points(model, node, points):
    kept = getattr(node, _RECONSTRUCTED_CACHE, None)
    if kept is not None and len(kept) == 4 * len(points):
        return list(kept)
    surfaces = [model.get(i) for i in (node.values.get("surface") or [])]
    if len(surfaces) != 2 or not all(surfaces) or not points:
        return None
    columns = []
    for surface in surfaces:
        periods = parameter_periods(model, surface)
        column, guess = [], None
        for point in points:
            found = _invert_point(model, surface, point, guess)
            if found is None:
                column.append((NULL_DOUBLE, NULL_DOUBLE))
                guess = None
                continue
            if guess is not None:
                found = tuple(
                    unwrap([guess[axis], found[axis]], period)[1]
                    for axis, period in enumerate(periods)
                )
            column.append(found)
            guess = found
        columns.append(column)
    numbers = []
    for one, two in zip(*columns, strict=True):
        numbers += [one[0], one[1], two[0], two[1]]
    try:
        setattr(node, _RECONSTRUCTED_CACHE, tuple(numbers))
    except AttributeError:
        pass
    return numbers


def _invert_point(model, surface, point, guess=None):
    if surface.name == "BLENDED_EDGE":
        return _blend_inverse(model, surface, point)
    if surface.name not in _NEWTON_INVERTIBLE:
        return surface_parameters(model, surface, point)
    seeds = [guess] if guess is not None else _grid_seeds(model, surface, point)
    best = None
    for seed in seeds:
        found = _newton_inverse(model, surface, point, seed)
        if found is not None and (best is None or found[1] < best[1]):
            best = found
    if best is None or best[1] > SURFACE_INVERSE:
        if guess is not None:
            return _invert_point(model, surface, point, None)
        return None
    return best[0]


def _grid_seeds(model, surface, point, keep=3):
    domain = _uv_domain(model, surface)
    basis = surface
    while domain is None and basis is not None and basis.name == "OFFSET_SURF":
        basis = model.get(basis.values.get("surface", 0))
        domain = _uv_domain(model, basis) if basis is not None else None
    if domain is None:
        return []
    (u_low, u_high), (v_low, v_high) = domain
    near = []
    for i in range(_INVERSE_GRID + 1):
        u = u_low + (u_high - u_low) * i / _INVERSE_GRID
        for j in range(_INVERSE_GRID + 1):
            v = v_low + (v_high - v_low) * j / _INVERSE_GRID
            try:
                here = surface_point(model, surface, u, v)
            except EvaluateError:
                continue
            near.append((math.dist(here, point), u, v))
    near.sort()
    return [(u, v) for _far, u, v in near[:keep]]


def _newton_inverse(model, surface, point, seed, turns=60):
    u, v = seed
    try:
        for _turn in range(turns):
            here = surface_point(model, surface, u, v)
            miss = [point[i] - here[i] for i in range(3)]
            du, dv = _partials(model, surface, u, v)
            a, b, c = _dot(du, du), _dot(du, dv), _dot(dv, dv)
            det = a * c - b * b
            if det == 0.0:
                return None
            g1, g2 = _dot(du, miss), _dot(dv, miss)
            step_u, step_v = (c * g1 - b * g2) / det, (a * g2 - b * g1) / det
            u, v = u + step_u, v + step_v
            if abs(step_u) + abs(step_v) < 1e-15:
                break
        here = surface_point(model, surface, u, v)
    except EvaluateError:
        return None
    return (u, v), math.dist(here, point)


def _fill_null_pairs(model, node, points, numbers):
    surfaces = [model.get(i) for i in (node.values.get("surface") or [])]
    if len(surfaces) != 2 or not all(surfaces):
        return numbers
    count = min(len(points), len(numbers) // 4)
    for side, surface in enumerate(surfaces):
        periods = None
        for k in range(count):
            at = 4 * k + 2 * side
            if NULL_DOUBLE not in (numbers[at], numbers[at + 1]):
                continue
            found = surface_parameters(model, surface, points[k])
            if found is None:
                continue
            if periods is None:
                periods = parameter_periods(model, surface)
            near = [4 * j + 2 * side for j in (k - 1, k + 1) if 0 <= j < count]
            near = [j for j in near
                    if NULL_DOUBLE not in (numbers[j], numbers[j + 1])]
            pair = list(found)
            for axis, period in enumerate(periods):
                if period and near:
                    pair[axis] = unwrap(
                        [numbers[near[0] + axis], pair[axis]], period)[1]
            numbers[at], numbers[at + 1] = pair
    return numbers


def chart_parameters(model, node):
    surfaces = [model.get(i) for i in (node.values.get("surface") or [])]
    if len(surfaces) != 2 or not all(surfaces):
        return None
    points, _declared = chart_points(model, node)
    numbers = chart_numbers(model, node, points)
    if numbers is None:
        return None
    if len(points) < 2 or len(numbers) < 4 * len(points):
        return None
    ring = ring_point(model, node)
    last = len(points) - 1

    def candidates(k):
        if ring is None or k not in (0, last):
            return [k]
        return [0, last] if ring[1] == "H" else [0]

    samples = []
    for side, surface in enumerate(surfaces):
        run = []
        for k, point in enumerate(points):
            found = None
            for q in candidates(k):
                pair = (numbers[4 * q + 2 * side], numbers[4 * q + 2 * side + 1])
                try:
                    back = surface_point(model, surface, pair[0], pair[1])
                except EvaluateError:
                    continue
                if math.dist(back, point) <= SURFACE_INVERSE:
                    found = pair
                    break
            if found is None:
                run = None
                break
            run.append(found)
        samples.append(run)
    if not any(run is not None for run in samples):
        return None
    return surfaces, samples


CHART_TOLERANCE = 1.0e-7

CHART_ROUNDS = 30
CHART_MAX_POINTS = 4096

_DENSE_CHART_CACHE = "_releve_dense"

CHART_REACH = 2.0

CHART_CHORD_REACH = 0.25

CHART_MAX_CHORD = 1.0


def _segment_distance(point, a, b):
    along = [b[k] - a[k] for k in range(3)]
    square = sum(c * c for c in along)
    t = 0.0
    if square > 0.0:
        t = sum((point[k] - a[k]) * along[k] for k in range(3)) / square
        t = min(1.0, max(0.0, t))
    return math.dist(point, [a[k] + t * along[k] for k in range(3)])


def parameter_periods(model, surface):
    periods = PERIODS.get(surface.name)
    if periods is not None or surface.name != "B_SURFACE":
        return periods or (None, None)
    try:
        spec = read_nurbs_surface(model, surface)
    except EvaluateError:
        return (None, None)
    out = []
    for axis, knots, degree, count in (("u", spec["u_knots"], spec["u_degree"], spec["n_u"]),
                                        ("v", spec["v_knots"], spec["v_degree"], spec["n_v"])):
        out.append(knots[count] - knots[degree] if spec.get(f"{axis}_periodic") else None)
    return tuple(out)


def _mid_parameters(surfaces, first, second, model=None):
    out = []
    for side, surface in enumerate(surfaces):
        periods = (parameter_periods(model, surface) if model is not None
                   else PERIODS.get(surface.name, (None, None)))
        for k in range(2):
            a, b = first[2 * side + k], second[2 * side + k]
            period = periods[k]
            if period is not None:
                b += round((a - b) / period) * period
            out.append((a + b) / 2.0)
    return out


def _intersection_point(model, surfaces, anchor, tangent, guess):
    found = _intersection_newton(model, surfaces, anchor, tangent, guess)
    if found is not None:
        return found
    return _intersection_on_chord(model, surfaces, anchor, guess)


def _intersection_newton(model, surfaces, anchor, tangent, guess):
    length = math.sqrt(sum(c * c for c in tangent))
    if length == 0.0:
        return None
    axis = [c / length for c in tangent]
    x = list(guess)

    def along(point):
        return sum((point[i] - anchor[i]) * axis[i] for i in range(3))

    best, stalled = None, 0
    for _turn in range(30):
        try:
            one = surface_point(model, surfaces[0], x[0], x[1])
            two = surface_point(model, surfaces[1], x[2], x[3])
        except EvaluateError:
            return None
        f = [one[i] - two[i] for i in range(3)] + [along(one)]
        residual = max(abs(v) for v in f)
        if residual < 1e-13:
            return one, tuple(x)
        if best is None or residual < best[0]:
            best, stalled = (residual, list(x)), 0
        else:
            stalled += 1
            if stalled >= 3 and best[0] < 1e-11:
                x = best[1]
                break
        jacobian = [[0.0] * 4 for _ in range(4)]
        for column in range(4):
            step = 1e-8 * (abs(x[column]) + 1.0)
            y = list(x)
            y[column] += step
            try:
                if column < 2:
                    moved = surface_point(model, surfaces[0], y[0], y[1])
                    g = [moved[i] - two[i] for i in range(3)] + [along(moved)]
                else:
                    moved = surface_point(model, surfaces[1], y[2], y[3])
                    g = [one[i] - moved[i] for i in range(3)] + [f[3]]
            except EvaluateError:
                return None
            for row in range(4):
                jacobian[row][column] = (g[row] - f[row]) / step
        delta = _solve_four(jacobian, [-v for v in f])
        if delta is None:
            return None
        x = [x[i] + delta[i] for i in range(4)]
    try:
        one = surface_point(model, surfaces[0], x[0], x[1])
        two = surface_point(model, surfaces[1], x[2], x[3])
    except EvaluateError:
        return None
    f = [one[i] - two[i] for i in range(3)] + [along(one)]
    if max(abs(v) for v in f) >= 1e-11:
        return None
    return one, tuple(x)


def _refine_chart(model, surfaces, dense, params, settled, reach, solve=None):
    for _round in range(CHART_ROUNDS):
        if len(dense) * 2 > CHART_MAX_POINTS:
            break
        try:
            poles, _times, _mults = interpolating_cubic(dense)
        except EvaluateError:
            break
        if len(poles) != 3 * (len(dense) - 1) + 1:
            break
        new_points, new_params, inserted = [dense[0]], [params[0]], False
        grew = [False] * (len(dense) - 1)
        for i in range(len(dense) - 1):
            if settled[i]:
                new_points.append(dense[i + 1])
                new_params.append(params[i + 1])
                continue
            b = poles[3 * i:3 * i + 4]
            middle = [(b[0][k] + 3 * b[1][k] + 3 * b[2][k] + b[3][k]) / 8.0
                      for k in range(3)]
            tangent = [b[3][k] + b[2][k] - b[1][k] - b[0][k] for k in range(3)]
            guess = _mid_parameters(surfaces, params[i], params[i + 1], model)
            if solve is None:
                found = _intersection_point(model, surfaces, middle, tangent, guess)
            else:
                found = solve(middle, tangent, guess)
            chord = math.dist(dense[i], dense[i + 1])
            if found is not None and chord <= CHART_MAX_CHORD and (
                _segment_distance(found[0], dense[i], dense[i + 1])
                <= min(reach, CHART_CHORD_REACH * chord) + CHART_TOLERANCE
            ):
                gap = math.dist(found[0], middle)
                if gap > CHART_TOLERANCE:
                    new_points.append(list(found[0]))
                    new_params.append(list(found[1]))
                    inserted = True
                    grew[i] = True
            new_points.append(dense[i + 1])
            new_params.append(params[i + 1])
        next_settled = []
        for i in range(len(dense) - 1):
            if grew[i]:
                next_settled += [False, False]
            else:
                next_settled.append(
                    not (i > 0 and grew[i - 1])
                    and not (i + 1 < len(grew) and grew[i + 1])
                )
        dense, params, settled = new_points, new_params, next_settled
        if not inserted:
            break
    return dense, params


def dense_chart(model, node):
    garde = getattr(node, _DENSE_CHART_CACHE, None)
    if garde is not None:
        return garde
    points, declared = chart_points(model, node)
    read = chart_parameters(model, node)
    result = (points, declared, read)
    if read is not None and len(points) >= 2 and all(r is not None for r in read[1]):
        surfaces, (one, two) = read
        dense = [list(p) for p in points]
        params = [list(one[k]) + list(two[k]) for k in range(len(points))]
        dense, params = _refine_chart(
            model, surfaces, dense, params, [False] * (len(dense) - 1),
            CHART_REACH * declared,
        )
        samples = [[(q[0], q[1]) for q in params], [(q[2], q[3]) for q in params]]
        result = (dense, declared, (surfaces, samples))
    try:
        setattr(node, _DENSE_CHART_CACHE, result)
    except AttributeError:
        pass
    return result


CLOSED_CHART_TOLERANCE = 1e-9


def closed_chart(points):
    if len(points) < 3:
        return False
    length = sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))
    if length <= 0:
        return False
    return math.dist(points[0], points[-1]) / length <= CLOSED_CHART_TOLERANCE


def clip_chart(points, ends):
    run = clip_run(points, ends)
    if run is None:
        return points
    return [ends[0]] + [points[i] for i in run] + [ends[1]]


VERTEX_ON_CHART = 1.0e-6

_CLIPPED_CHART_CACHE = "_releve_coupe"


def clipped_chart(model, node, ends, slack=(0.0, 0.0)):
    points, declared, read = dense_chart(model, node)
    run = clip_run(points, ends)
    if run is None:
        return points, declared, read
    clipped = [list(ends[0])] + [points[i] for i in run] + [list(ends[1])]
    if read is None or len(points) < 2 or any(r is None for r in read[1]):
        return clipped, declared, None
    key = (tuple(ends[0]), tuple(ends[1]), tuple(slack))
    cache = getattr(node, _CLIPPED_CHART_CACHE, None)
    if cache is None:
        cache = {}
        try:
            setattr(node, _CLIPPED_CHART_CACHE, cache)
        except AttributeError:
            pass
    if key in cache:
        return cache[key]
    surfaces, (one, two) = read
    params = [list(one[i]) + list(two[i]) for i in run]
    stops = []
    for end, inward in ((0, 1), (-1, -2)):
        vertex = clipped[end]
        if run:
            side = run[0] if end == 0 else run[-1]
            around = [i for i in (side - 1, side + 1) if 0 <= i < len(points)]
        else:
            around = list(range(len(points)))
        nearest = min(around, key=lambda i: math.dist(points[i], vertex))
        tangent = [clipped[inward][k] - vertex[k] for k in range(3)]
        found = _intersection_point(
            model, surfaces, vertex, tangent,
            list(one[nearest]) + list(two[nearest]),
        )
        allowed = max(VERTEX_ON_CHART, slack[0 if end == 0 else 1] or 0.0)
        gap = None if found is None else math.dist(found[0], vertex)
        if gap is None or gap > allowed:
            cache[key] = (clipped, declared, None)
            return cache[key]
        stops.append(list(found[1]))
    params = [stops[0], *params, stops[1]]
    segments = len(clipped) - 1
    settled = [not (i < 2 or i >= segments - 2) for i in range(segments)]
    dense, params = _refine_chart(
        model, surfaces, clipped, params, settled, CHART_REACH * declared
    )
    samples = [[(q[0], q[1]) for q in params], [(q[2], q[3]) for q in params]]
    cache[key] = (dense, declared, (surfaces, samples))
    return cache[key]


def _ring_position(points, target):
    best = None
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        along = [b[k] - a[k] for k in range(3)]
        square = sum(c * c for c in along)
        t = 0.0
        if square > 0.0:
            t = sum((target[k] - a[k]) * along[k] for k in range(3)) / square
            t = min(1.0, max(0.0, t))
        foot = [a[k] + t * along[k] for k in range(3)]
        gap = math.dist(foot, target)
        if best is None or gap < best[0]:
            best = (gap, i + t)
    return best[1]


def clip_run(points, ends):
    if ends is None:
        return None
    start, end = ends
    if start is None or end is None:
        return None
    if len(points) < 3:
        return []
    first = min(range(len(points)), key=lambda i: math.dist(points[i], start))
    last = min(range(len(points)), key=lambda i: math.dist(points[i], end))
    if closed_chart(points):
        ring = len(points) - 1
        first %= ring
        last %= ring
        if first == last and math.dist(start, end) > CLOSED_CHART_TOLERANCE * max(
            math.dist(points[i], points[i + 1]) for i in range(ring)
        ):
            def around(position):
                return (position - first + ring / 2.0) % ring - ring / 2.0

            if around(_ring_position(points, end)) >= around(
                _ring_position(points, start)
            ):
                return []
        if first < last:
            return list(range(first + 1, last))
        return list(range(first + 1, ring)) + list(range(0, last))
    if first == last:
        return []
    if first < last:
        return list(range(first + 1, last))
    return list(range(last + 1, first))[::-1]


def reversed_curve(spec):
    span = spec["distinct"][0] + spec["distinct"][-1]
    turned = dict(spec)
    turned["poles"] = spec["poles"][::-1]
    turned["distinct"] = [span - k for k in spec["distinct"][::-1]]
    turned["mults"] = spec["mults"][::-1]
    turned["knots"] = _expanded(turned["distinct"], turned["mults"])
    if spec["weights"] is not None:
        turned["weights"] = spec["weights"][::-1]
    return turned


BLEND_CONSISTENCY = 1e-9

_BLENDS_UNDER_WAY = set()


def _chart_base(model, curve_node):
    node = model.get(curve_node.values.get("chart", 0))
    if node is None or node.name != "CHART":
        return 0.0
    base = node.values.get("base_parameter")
    if isinstance(base, (int, float)) and abs(base) < 1e6:
        return float(base)
    return 0.0


def _blend_order(model, supports, spine):
    surfaces = [model.get(i) for i in (spine.values.get("surface") or [])]
    if len(surfaces) != 2 or not all(surfaces):
        return (0, 1), False

    def basis(node):
        seen = set()
        while node is not None and node.name == "OFFSET_SURF":
            if node.index in seen:
                break
            seen.add(node.index)
            node = model.get(node.values.get("surface", 0))
        return node

    for key in (lambda s: getattr(basis(s), "index", None),
                lambda s: getattr(basis(s), "name", None)):
        wanted = [key(s) for s in supports]
        given = [key(s) for s in surfaces]
        if None in wanted or wanted[0] == wanted[1]:
            continue
        if wanted == given:
            return (0, 1), True
        if wanted == given[::-1]:
            return (1, 0), True
    return (0, 1), False


ANALYTIC_SPINES = ("LINE", "CIRCLE", "ELLIPSE")

BLEND_SPINE_SAMPLES = 9

BLEND_SPINE_MARGIN = 0.02


def _foot_on(model, surface, point):
    values, name = surface.values, surface.name
    if name == "PLANE":
        origin, normal = values["pvec"], _unit(values["normal"])
        d = _dot([point[i] - origin[i] for i in range(3)], normal)
        foot = [point[i] - d * normal[i] for i in range(3)]
    elif name == "CYLINDER":
        origin, axis = values["pvec"], _unit(values["axis"])
        w = [point[i] - origin[i] for i in range(3)]
        along = _dot(w, axis)
        radial = [w[i] - along * axis[i] for i in range(3)]
        length = math.sqrt(_dot(radial, radial))
        if length == 0.0:
            raise EvaluateError("a ball centre on a cylinder's axis")
        foot = [origin[i] + along * axis[i] + values["radius"] * radial[i] / length
                for i in range(3)]
    elif name == "SPHERE":
        centre = values["centre"]
        w = [point[i] - centre[i] for i in range(3)]
        length = math.sqrt(_dot(w, w))
        if length == 0.0:
            raise EvaluateError("a ball centre at a sphere's centre")
        foot = [centre[i] + values["radius"] * w[i] / length for i in range(3)]
    elif name == "TORUS":
        centre, axis = values["centre"], _unit(values["axis"])
        w = [point[i] - centre[i] for i in range(3)]
        along = _dot(w, axis)
        radial = [w[i] - along * axis[i] for i in range(3)]
        length = math.sqrt(_dot(radial, radial))
        if length == 0.0:
            raise EvaluateError("a ball centre on a torus's axis")
        ring = [centre[i] + values["major_radius"] * radial[i] / length
                for i in range(3)]
        out = [point[i] - ring[i] for i in range(3)]
        gap = math.sqrt(_dot(out, out))
        if gap == 0.0:
            raise EvaluateError("a ball centre on a torus's ring")
        foot = [ring[i] + values["minor_radius"] * out[i] / gap for i in range(3)]
    elif name == "CONE":
        origin, axis = values["pvec"], _unit(values["axis"])
        w = [point[i] - origin[i] for i in range(3)]
        along = _dot(w, axis)
        radial = [w[i] - along * axis[i] for i in range(3)]
        rho = math.sqrt(_dot(radial, radial))
        if rho == 0.0 or values["cos_half_angle"] == 0.0:
            raise EvaluateError("a ball centre on a cone's axis")
        e = [radial[i] / rho for i in range(3)]
        slope = values["sin_half_angle"] / values["cos_half_angle"]
        norm = math.hypot(slope, 1.0)
        d0, d1 = slope / norm, 1.0 / norm
        s = (rho - values["radius"]) * d0 + along * d1
        foot = [origin[i] + s * d1 * axis[i] + (values["radius"] + s * d0) * e[i]
                for i in range(3)]
    else:
        raise EvaluateError(f"no foot on a {name}")
    return foot, surface_parameters(model, surface, foot)


def foot_point(model, surface, point):
    if surface.name == "BLENDED_EDGE":
        spine = model.get(surface.values.get("spine", 0))
        if spine is None:
            raise EvaluateError("a support blend without a spine")
        centre = _spine_centre(model, spine, point)
        radius = max(abs(r) for r in (surface.values.get("range") or [0.0]))
        w = [point[i] - centre[i] for i in range(3)]
        length = math.sqrt(_dot(w, w))
        if length == 0.0:
            raise EvaluateError("a ball centre on a support blend's spine")
        return [centre[i] + radius * w[i] / length for i in range(3)]
    foot, _uv = _foot_on(model, surface, point)
    return foot


def _spine_parameter(spine, point):
    values = spine.values
    if spine.name == "LINE":
        origin, direction = values["pvec"], _unit(values["direction"])
        return _dot([point[i] - origin[i] for i in range(3)], direction)
    centre, _axis, x, y = _frame(values, "centre", "normal")
    if spine.name == "CIRCLE":
        a = b = values["radius"]
    else:
        a, b = values["major_radius"], values["minor_radius"]
    w = [point[i] - centre[i] for i in range(3)]
    t = math.atan2(_dot(w, y) / b, _dot(w, x) / a)
    for _turn in range(20):
        c = [centre[i] + a * math.cos(t) * x[i] + b * math.sin(t) * y[i]
             for i in range(3)]
        d1 = [-a * math.sin(t) * x[i] + b * math.cos(t) * y[i] for i in range(3)]
        d2 = [-a * math.cos(t) * x[i] - b * math.sin(t) * y[i] for i in range(3)]
        diff = [point[i] - c[i] for i in range(3)]
        slope = _dot(diff, d2) - _dot(d1, d1)
        if slope == 0.0:
            break
        step = _dot(diff, d1) / slope
        t -= step
        if abs(step) < 1e-15:
            break
    return t


SPINE_TANGENCY = 1e-6

SPINE_CENTRE_RESOLUTION = 1e-13

SPINE_CENTRE_CLOSURE = 1e-15

SPINE_CENTRE_TURNS = 80

_SPINE_TANGENTS_CACHE = "_tangentes_epine"


def _lerp_parameters(surfaces, first, second, fraction, model=None):
    out = []
    for side, surface in enumerate(surfaces):
        periods = (parameter_periods(model, surface) if model is not None
                   else PERIODS.get(surface.name, (None, None)))
        for k in range(2):
            a, b = first[2 * side + k], second[2 * side + k]
            period = periods[k]
            if period is not None:
                b += round((a - b) / period) * period
            out.append(a + fraction * (b - a))
    return out


def _spine_tangent(model, surfaces, params, chord):
    tangent = _cross(_normal(model, surfaces[0], params[0], params[1]),
                     _normal(model, surfaces[1], params[2], params[3]))
    if math.sqrt(_dot(tangent, tangent)) < SPINE_TANGENCY:
        tangent = chord
    if _dot(tangent, chord) < 0.0:
        tangent = [-c for c in tangent]
    return _unit(tangent)


def _spine_tangents(model, spine, points, surfaces, params):
    garde = getattr(spine, _SPINE_TANGENTS_CACHE, None)
    if garde is not None:
        return garde
    last = len(points) - 1
    tangents = []
    for k in range(len(points)):
        chord = [points[min(k + 1, last)][i] - points[max(k - 1, 0)][i]
                 for i in range(3)]
        tangents.append(_spine_tangent(model, surfaces, params[k], chord))
    try:
        setattr(spine, _SPINE_TANGENTS_CACHE, tangents)
    except AttributeError:
        pass
    return tangents


def _spine_minimum(model, surfaces, point, ends, params, slopes):
    start, end = ends
    rising, falling = slopes
    if falling == 0.0:
        return list(end)
    chord = [end[i] - start[i] for i in range(3)]
    low, high, side = 0.0, 1.0, 0
    for _turn in range(SPINE_CENTRE_TURNS):
        fraction = (low * falling - high * rising) / (falling - rising)
        anchor = [start[i] + fraction * chord[i] for i in range(3)]
        found = _intersection_point(
            model, surfaces, anchor, chord,
            _lerp_parameters(surfaces, params[0], params[1], fraction, model),
        )
        if found is None:
            raise EvaluateError("a support blend whose spine will not resolve")
        tangent = _spine_tangent(model, surfaces, found[1], chord)
        slope = _dot([point[i] - found[0][i] for i in range(3)], tangent)
        if abs(slope) < SPINE_CENTRE_RESOLUTION or high - low < SPINE_CENTRE_CLOSURE:
            return found[0]
        if slope > 0.0:
            low, rising = fraction, slope
            if side == -1:
                falling /= 2.0
            side = -1
        else:
            high, falling = fraction, slope
            if side == 1:
                rising /= 2.0
            side = 1
    raise EvaluateError("a support blend whose spine will not resolve")


def _spine_centre(model, spine, point):
    if spine.name in ANALYTIC_SPINES:
        return curve_point(model, spine, _spine_parameter(spine, point))
    if spine.name != "INTERSECTION":
        raise EvaluateError("a support blend whose spine is of no known kind")
    points, _declared, read = dense_chart(model, spine)
    if read is None or len(points) < 2 or any(r is None for r in read[1]):
        raise EvaluateError("a support blend whose spine cannot be followed")
    surfaces, (one, two) = read
    params = [list(one[k]) + list(two[k]) for k in range(len(points))]
    tangents = _spine_tangents(model, spine, points, surfaces, params)
    slopes = [_dot([point[i] - points[k][i] for i in range(3)], tangents[k])
              for k in range(len(points))]
    best = None
    for k in range(len(points) - 1):
        if not slopes[k] > 0.0 >= slopes[k + 1]:
            continue
        centre = _spine_minimum(
            model, surfaces, point, (points[k], points[k + 1]),
            (params[k], params[k + 1]), (slopes[k], slopes[k + 1]),
        )
        distance = math.dist(centre, point)
        if best is None or distance < best[0]:
            best = (distance, centre)
    if math.dist(points[0], points[-1]) > CLOSURE:
        nearest_end = min(math.dist(points[0], point), math.dist(points[-1], point))
        if best is None or nearest_end < best[0]:
            raise EvaluateError(
                "a support blend whose nearest spine point is off its chart")
    if best is None:
        raise EvaluateError("a support blend whose spine will not resolve")
    return best[1]


def _faces_on(model, surface_node):
    index = getattr(model, "_faces_par_surface", None)
    if index is None:
        index = {}
        for face in model.of_type("FACE"):
            surface = model.surface_of(face)
            if surface is not None:
                index.setdefault(surface.index, []).append(face)
        try:
            model._faces_par_surface = index
        except AttributeError:
            pass
    return index.get(surface_node.index, [])


def _analytic_span(model, node, spine):
    params = []
    for face in _faces_on(model, node):
        for loop in model.loops_of(face):
            for halfedge in model.halfedges_of(loop):
                vertex = model.get(halfedge.values.get("vertex", 0))
                point = model.vertex_point(vertex) if vertex is not None else None
                if point is not None:
                    params.append(_spine_parameter(spine, point))
    if len(params) < 2:
        if spine.name == "LINE":
            raise EvaluateError(
                "a blend on an analytic spine with no vertex to bound it")
        return 0.0, 2.0 * math.pi
    if spine.name == "LINE":
        low, high = min(params), max(params)
    else:
        turn = 2.0 * math.pi
        angles = sorted(p % turn for p in params)
        gaps = [(angles[(k + 1) % len(angles)] - angles[k]) % turn
                for k in range(len(angles))]
        widest = max(range(len(gaps)), key=lambda k: gaps[k])
        low = angles[(widest + 1) % len(angles)]
        high = low + (turn - gaps[widest])
    width = high - low
    if width <= 0.0:
        raise EvaluateError("a blend on an analytic spine of no extent")
    return low - BLEND_SPINE_MARGIN * width, high + BLEND_SPINE_MARGIN * width


def _spine_chart(model, node, supports, spine):
    if spine.name == "INTERSECTION":
        centres, _declared = chart_points(model, spine)
        numbers = chart_numbers(model, spine, centres)
        if numbers is None:
            raise EvaluateError("a blend spine without intersection data")
        order, decided = _blend_order(model, supports, spine)
        return centres, numbers, order, decided, _chart_base(model, spine)
    if spine.name not in ANALYTIC_SPINES:
        raise EvaluateError("a blend whose spine is not an intersection")
    low, high = _analytic_span(model, node, spine)
    centres, numbers = [], []
    for k in range(BLEND_SPINE_SAMPLES):
        t = low + (high - low) * k / (BLEND_SPINE_SAMPLES - 1)
        centre = curve_point(model, spine, t)
        numbers_here = []
        for support in supports:
            _foot, uv = _foot_on(model, support, centre)
            if uv is None:
                raise EvaluateError("a blend foot that will not invert")
            numbers_here += [uv[0], uv[1]]
        centres.append(centre)
        numbers += numbers_here
    return centres, numbers, (0, 1), True, low


def analytic_blend_point(model, node, spine, u, v):
    supports = [model.get(i) for i in (node.values.get("surface") or [])]
    if len(supports) != 2 or not all(supports):
        raise EvaluateError("a blend without two supporting surfaces")
    centre = curve_point(model, spine, u)
    first = foot_point(model, supports[0], centre)
    second = foot_point(model, supports[1], centre)
    if _sense_of(spine.values.get("sense")) != "-":
        first, second = second, first
    radius = math.dist(first, centre)
    if radius <= 0.0:
        return centre
    step = DERIVATIVE_STEP
    before = curve_point(model, spine, u - step)
    after = curve_point(model, spine, u + step)
    tangent = [(after[i] - before[i]) / (2.0 * step) for i in range(3)]
    one, sideways, total = _arc_frame(centre, first, second, radius, tangent)
    angle = v * total
    return [centre[i] + radius * (math.cos(angle) * one[i]
                                  + math.sin(angle) * sideways[i])
            for i in range(3)]


_BLEND_FRAMES_CACHE = "_sections_conge"


def blend_frames(model, node):
    garde = getattr(node, _BLEND_FRAMES_CACHE, None)
    if garde is not None:
        return garde
    result = _blend_frames(model, node)
    try:
        setattr(node, _BLEND_FRAMES_CACHE, result)
    except AttributeError:
        pass
    return result


def _blend_frames(model, node):
    values = node.values
    supports = [model.get(i) for i in (values.get("surface") or [])]
    if len(supports) != 2 or not all(supports):
        raise EvaluateError("a blend without two supporting surfaces")
    spine = model.get(values.get("spine", 0))
    refusal = None
    try:
        read = _frames_from_chart(model, node)
    except EvaluateError as error:
        read, refusal = None, error
    if read is not None and max(g for *_r, g in read[0]) < BLEND_CONSISTENCY:
        return read
    feet = None
    if spine is not None:
        try:
            feet = _frames_from_feet(model, node, supports, spine)
        except EvaluateError as error:
            refusal = refusal or error
    if feet is None and read is None:
        raise refusal or EvaluateError("a blend whose supports will not evaluate")
    if feet is None:
        return read
    if read is not None and (max(g for *_r, g in read[0])
                             <= max(g for *_r, g in feet[0])):
        return read
    return feet


def _frames_from_feet(model, node, supports, spine):
    centres, _numbers, _order, _decided, base = _spine_chart(
        model, node, supports, spine)
    sections = []
    for centre in centres:
        first = foot_point(model, supports[0], centre)
        second = foot_point(model, supports[1], centre)
        radius = math.dist(first, centre)
        sections.append(
            (centre, first, second, radius, abs(radius - math.dist(second, centre)))
        )
    if len(sections) < 2:
        raise EvaluateError("a blend spine charted at fewer than two points")
    arcs = [0.0]
    for k in range(1, len(centres)):
        arcs.append(arcs[-1] + math.dist(centres[k], centres[k - 1]))
    return sections, arcs, base


def _frames_from_chart(model, node):
    values = node.values
    supports = [model.get(i) for i in (values.get("surface") or [])]
    if len(supports) != 2 or not all(supports):
        raise EvaluateError("a blend without two supporting surfaces")
    spine = model.get(values.get("spine", 0))
    if spine is None:
        raise EvaluateError("a blend whose spine is not an intersection")
    centres, numbers, order, decided, base = _spine_chart(model, node, supports, spine)
    if len(numbers) < 4 * len(centres):
        raise EvaluateError("a blend spine stating fewer than four numbers a point")

    def read(order):
        left, right = order
        sections = []
        for k, centre in enumerate(centres):
            quad = numbers[4 * k:4 * k + 4]
            if NULL_DOUBLE in quad:
                raise EvaluateError("a blend spine with a null parameter pair")
            first = surface_point(model, supports[0],
                                  quad[2 * left], quad[2 * left + 1])
            second = surface_point(model, supports[1],
                                   quad[2 * right], quad[2 * right + 1])
            radius = math.dist(first, centre)
            sections.append(
                (centre, first, second, radius,
                 abs(radius - math.dist(second, centre)))
            )
        return sections

    if decided:
        sections = read(order)
    else:
        def tried(order):
            try:
                return read(order)
            except EvaluateError:
                return None

        straight, crossed = tried((0, 1)), tried((1, 0))
        if straight is None and crossed is None:
            raise EvaluateError("a blend whose supports will not evaluate")
        if straight is None or crossed is None:
            sections = straight if crossed is None else crossed
        else:
            sections = (
                crossed
                if min(gap for *_r, gap in crossed)
                < min(gap for *_r, gap in straight)
                else straight
            )
    if len(sections) < 2:
        raise EvaluateError("a blend spine charted at fewer than two points")
    arcs = [0.0]
    for k in range(1, len(centres)):
        arcs.append(arcs[-1] + math.dist(centres[k], centres[k - 1]))
    return sections, arcs, base


def blend_tolerance(model, node):
    tolerance = BLEND_CONSISTENCY
    for support in (model.get(i) for i in (node.values.get("surface") or [])):
        if support is None or support.name != "BLENDED_EDGE":
            continue
        spine = model.get(support.values.get("spine", 0))
        if spine is None:
            continue
        _points, declared = chart_points(model, spine)
        tolerance = max(tolerance, declared)
    return tolerance


def blend_sections(model, node, stations=None):
    if stations is None:
        frames, _arcs, _base = blend_frames(model, node)
    else:
        frames = stations
    tolerance = blend_tolerance(model, node)
    sections = []
    for station in frames:
        centre, first, second, radius, gap = station[:5]
        if radius <= 0.0:
            continue
        if gap > tolerance:
            reach = math.dist(second, centre)
            if reach <= 0.0 or gap > radius:
                continue
            second = [
                centre[i] + radius * (second[i] - centre[i]) / reach
                for i in range(3)
            ]
        sections.append((centre, first, second, radius))
    if len(sections) < 2:
        raise EvaluateError("a blend with fewer than two usable sections")
    return sections


BLEND_STATION_TOLERANCE = 5.0e-6

BLEND_STATION_DEPTH = 4

BLEND_STATION_BRANCH = 0.5

BLEND_TOUCH_TOLERANCE = 1.0e-9


def _blend_parameters(model, node):
    supports = [model.get(i) for i in (node.values.get("surface") or [])]
    spine = model.get(node.values.get("spine", 0))
    if len(supports) != 2 or not all(supports) or spine is None:
        raise EvaluateError("a blend without two supports and a charted spine")
    _centres, numbers, order, _decided, _base = _spine_chart(
        model, node, supports, spine)
    frames, _arcs, _base = blend_frames(model, node)

    matched = False
    for essai in ((0, 1), (1, 0)):
        left, _right = essai
        quad = numbers[0:4]
        if len(quad) < 4 or NULL_DOUBLE in numbers[:4 * len(frames)]:
            break
        try:
            here = surface_point(model, supports[0], quad[2 * left], quad[2 * left + 1])
        except EvaluateError:
            continue
        if math.dist(here, frames[0][1]) < 1e-12:
            order, matched = essai, True
            break

    out = []
    if not matched:
        for centre, first, second, radius, gap in frames:
            one = surface_parameters(model, supports[0], first)
            two = surface_parameters(model, supports[1], second)
            if one is None or two is None:
                raise EvaluateError("a blend whose feet will not invert")
            out.append((centre, first, second, radius, gap, (one, two)))
        return supports, out
    left, right = order
    for k, (centre, first, second, radius, gap) in enumerate(frames):
        quad = numbers[4 * k:4 * k + 4]
        if len(quad) < 4:
            raise EvaluateError("a blend whose spine states too few parameters")
        out.append((centre, first, second, radius, gap,
                    ((quad[2 * left], quad[2 * left + 1]),
                     (quad[2 * right], quad[2 * right + 1]))))
    return supports, out


def _offset_point(model, surface, u, v, radius, sign):
    point = surface_point(model, surface, u, v)
    normal = _normal(model, surface, u, v)
    return [point[i] + sign * radius * normal[i] for i in range(3)]


def _blend_signs(model, supports, station):
    centre, _first, _second, _radius, _gap, ((u1, v1), (u2, v2)) = station
    signs = []
    for surface, (u, v) in zip(supports, ((u1, v1), (u2, v2)), strict=True):
        normal = _normal(model, surface, u, v)
        point = surface_point(model, surface, u, v)
        towards = sum((centre[i] - point[i]) * normal[i] for i in range(3))
        signs.append(1.0 if towards >= 0.0 else -1.0)
    return signs


def _solve_four(matrix, right):
    rows = [[*matrix[i], right[i]] for i in range(4)]
    for column in range(4):
        pivot = max(range(column, 4), key=lambda r: abs(rows[r][column]))
        if abs(rows[pivot][column]) < 1e-18:
            return None
        rows[column], rows[pivot] = rows[pivot], rows[column]
        for r in range(column + 1, 4):
            factor = rows[r][column] / rows[column][column]
            for c in range(column, 5):
                rows[r][c] -= factor * rows[column][c]
    out = [0.0] * 4
    for column in range(3, -1, -1):
        total = rows[column][4] - sum(rows[column][c] * out[c] for c in range(column + 1, 4))
        out[column] = total / rows[column][column]
    return out


def _solve_station(model, supports, signs, radius, x, plane):
    x = list(x)

    def residual(y):
        one = _offset_point(model, supports[0], y[0], y[1], radius, signs[0])
        two = _offset_point(model, supports[1], y[2], y[3], radius, signs[1])
        middle = [(one[i] + two[i]) / 2 for i in range(3)]
        return [one[i] - two[i] for i in range(3)] + [plane(model, middle, y)], one, two

    for _turn in range(40):
        try:
            f, _one, _two = residual(x)
        except EvaluateError:
            return None
        if max(abs(v) for v in f) < 1e-12:
            break
        jacobian = [[0.0] * 4 for _ in range(4)]
        for column in range(4):
            step = 1e-8 * (abs(x[column]) + 1.0)
            y = list(x)
            y[column] += step
            try:
                g, _o, _t = residual(y)
            except EvaluateError:
                return None
            for row in range(4):
                jacobian[row][column] = (g[row] - f[row]) / step
        delta = _solve_four(jacobian, [-v for v in f])
        if delta is None:
            return None
        size = max(abs(v) for v in f)
        factor = 1.0
        while True:
            trial = [x[i] + factor * delta[i] for i in range(4)]
            try:
                g, _one, _two = residual(trial)
            except EvaluateError:
                g = None
            if g is not None and max(abs(v) for v in g) < size:
                x = trial
                break
            factor /= 2.0
            if factor < 1e-6:
                x = [x[i] + delta[i] for i in range(4)]
                break

    try:
        f, one, two = residual(x)
    except EvaluateError:
        return None
    if max(abs(v) for v in f) >= 1e-9:
        return None
    centre = [(one[i] + two[i]) / 2 for i in range(3)]
    try:
        first = surface_point(model, supports[0], x[0], x[1])
        second = surface_point(model, supports[1], x[2], x[3])
    except EvaluateError:
        return None
    gap = abs(math.dist(first, centre) - math.dist(second, centre))
    return (centre, first, second, radius, gap, ((x[0], x[1]), (x[2], x[3])))


def _station_between(model, supports, signs, before, after, part=0.5):
    (_c0, _f0, _s0, r0, _g0, ((a1, b1), (a2, b2))) = before
    (_c1, _f1, _s1, r1, _g1, ((c1, d1), (c2, d2))) = after
    (c1, d1), (c2, d2) = (
        _unwrapped_towards(model, supports[0], (a1, b1), (c1, d1)),
        _unwrapped_towards(model, supports[1], (a2, b2), (c2, d2)),
    )
    radius = r0 + part * (r1 - r0)
    x = [a1 + part * (c1 - a1), b1 + part * (d1 - b1),
         a2 + part * (c2 - a2), b2 + part * (d2 - b2)]
    anchor = [before[0][i] + part * (after[0][i] - before[0][i]) for i in range(3)]
    chord = [after[0][i] - before[0][i] for i in range(3)]
    length = math.sqrt(sum(c * c for c in chord))
    if length == 0.0:
        return None
    chord = [c / length for c in chord]

    def plane(_model, centre, _y):
        return sum((centre[i] - anchor[i]) * chord[i] for i in range(3))

    return _solve_station(model, supports, signs, radius, x, plane)


def _unwrapped_towards(model, surface, near, pair):
    out = list(pair)
    for axis, period in enumerate(parameter_periods(model, surface)):
        if period:
            out[axis] += round((near[axis] - out[axis]) / period) * period
    return tuple(out)


_STATIONS_CACHE = "_stations_denses"


def blend_stations(model, node, tolerance=BLEND_STATION_TOLERANCE,
                   depth=BLEND_STATION_DEPTH):
    if tolerance != BLEND_STATION_TOLERANCE or depth != BLEND_STATION_DEPTH:
        return _blend_stations(model, node, tolerance, depth)
    kept = getattr(node, _STATIONS_CACHE, None)
    if kept is None:
        kept = _blend_stations(model, node, tolerance, depth)
        try:
            setattr(node, _STATIONS_CACHE, kept)
        except AttributeError:
            pass
    return list(kept)


def _blend_stations(model, node, tolerance, depth):
    supports, given = _blend_parameters(model, node)
    if len(given) < 2:
        return given
    signs = _blend_signs(model, supports, given[0])

    def near(found, before, after, part):
        aimed = [before[0][i] + part * (after[0][i] - before[0][i]) for i in range(3)]
        return (math.dist(found[0], aimed)
                <= BLEND_STATION_BRANCH * math.dist(before[0], after[0]))

    def between(before, after, left):
        found = _station_between(model, supports, signs, before, after)
        if found is None:
            if left <= 0:
                return []
            chain = [before]
            for part in (0.25, 0.75):
                station = _station_between(model, supports, signs, before, after,
                                           part=part)
                if station is not None and near(station, before, after, part):
                    chain.append(station)
            if len(chain) == 1:
                return []
            chain.append(after)
            out = []
            for k in range(len(chain) - 1):
                if k > 0:
                    out.append(chain[k])
                out.extend(between(chain[k], chain[k + 1], left - 1))
            return out
        middle = [(before[0][i] + after[0][i]) / 2 for i in range(3)]
        drift = math.dist(found[0], middle)
        if drift > BLEND_STATION_BRANCH * math.dist(before[0], after[0]):
            return []
        if left <= 0 or drift <= tolerance:
            return [found] if drift > tolerance else []
        return [*between(before, found, left - 1), found,
                *between(found, after, left - 1)]

    out = [given[0]]
    for k in range(len(given) - 1):
        out.extend(between(given[k], given[k + 1], depth))
        out.append(given[k + 1])
    if not _exact_supports(supports):
        return out
    return _refine_stations(model, supports, signs, out)


def _exact_supports(supports):
    return all(support.name != "BLENDED_EDGE" for support in supports)


def _refine_stations(model, supports, signs, stations):
    stations = list(stations)
    settled = [False] * (len(stations) - 1)
    for _round in range(CHART_ROUNDS):
        if len(stations) < 2 or len(stations) * 2 > CHART_MAX_POINTS:
            break
        times = [0.0]
        for k in range(1, len(stations)):
            step = math.dist(stations[k][0], stations[k - 1][0]) ** 0.5
            times.append(times[-1] + (step if step > 0.0 else 1e-9))
        curves = [_through([list(st[j]) for st in stations], times)[0] for j in (0, 1, 2)]
        out, grew = [stations[0]], [False] * (len(stations) - 1)
        for i in range(len(stations) - 1):
            before, after = stations[i], stations[i + 1]
            if not settled[i]:
                middles = []
                for poles in curves:
                    b = poles[3 * i:3 * i + 4]
                    middles.append([(b[0][k] + 3 * b[1][k] + 3 * b[2][k] + b[3][k]) / 8.0
                                    for k in range(3)])
                b = curves[0][3 * i:3 * i + 4]
                tangent = [b[3][k] + b[2][k] - b[1][k] - b[0][k] for k in range(3)]
                length = math.sqrt(sum(c * c for c in tangent))
                chord = math.dist(before[0], after[0])
                found = None
                if length > 0.0 and chord > 0.0:
                    axis = [c / length for c in tangent]
                    anchor = middles[0]

                    def plane(_model, centre, _y, anchor=anchor, axis=axis):
                        return sum((centre[k] - anchor[k]) * axis[k] for k in range(3))

                    guess = _mid_parameters(
                        supports,
                        [*before[5][0], *before[5][1]], [*after[5][0], *after[5][1]],
                        model)
                    found = _solve_station(
                        model, supports, signs, (before[3] + after[3]) / 2.0, guess, plane)
                if found is not None and (
                    math.dist(found[0], middles[0]) <= BLEND_STATION_BRANCH * chord
                ):
                    gap = max(math.dist(found[j], middles[j]) for j in (0, 1, 2))
                    if gap > CHART_TOLERANCE:
                        out.append(found)
                        grew[i] = True
            out.append(after)
        next_settled = []
        for i in range(len(stations) - 1):
            if grew[i]:
                next_settled += [False, False]
            else:
                next_settled.append(
                    not (i > 0 and grew[i - 1])
                    and not (i + 1 < len(grew) and grew[i + 1])
                )
        stations, settled = out, next_settled
        if not any(grew):
            break
    return stations


def _on_the_ball(station, side):
    centre, radius = station[0], station[3]
    touch = list(station[side])
    if radius <= 0.0:
        return None
    reach = math.dist(touch, centre)
    if abs(reach - radius) <= BLEND_TOUCH_TOLERANCE:
        return touch
    if reach <= 0.0 or abs(reach - radius) > radius:
        return None
    return [centre[i] + radius * (touch[i] - centre[i]) / reach for i in range(3)]


def blend_boundary_side(model, curve_node, blend_index):
    for index in (curve_node.values.get("surface") or []):
        found = model.get(index)
        if found is None or found.name != "BLEND_BOUND":
            continue
        if found.values.get("blend") != blend_index:
            continue
        return 1 if found.values.get("boundary") == 1 else 2
    return None


def blend_touches(model, node, side, chart, **kwargs):
    stations = blend_stations(model, node, **kwargs)
    touches = [t for t in (_on_the_ball(s, side) for s in stations) if t is not None]
    if len(touches) < 2 or len(chart) < 2:
        return chart
    touches = _touches_between(touches, chart)
    if len(touches) < 2:
        return chart
    if (math.dist(touches[0], chart[0]) + math.dist(touches[-1], chart[-1])
            > math.dist(touches[0], chart[-1]) + math.dist(touches[-1], chart[0])):
        touches.reverse()

    walked = [0.0]
    for k in range(1, len(chart)):
        walked.append(walked[-1] + math.dist(chart[k], chart[k - 1]))
    merged = [(walked[k], list(chart[k])) for k in range(len(chart))]
    for touch in touches:
        best, where = None, 0.0
        for k in range(len(chart) - 1):
            a, b = chart[k], chart[k + 1]
            span = [b[i] - a[i] for i in range(3)]
            square = sum(c * c for c in span) or 1.0
            part = sum((touch[i] - a[i]) * span[i] for i in range(3)) / square
            part = min(1.0, max(0.0, part))
            foot = [a[i] + part * span[i] for i in range(3)]
            far = math.dist(foot, touch)
            if best is None or far < best:
                best = far
                where = walked[k] + part * (walked[k + 1] - walked[k])
        if all(abs(where - u) > 1e-12 for u, _p in merged):
            merged.append((where, list(touch)))
    merged.sort(key=lambda pair: pair[0])
    return [point for _where, point in merged]


def _touches_between(touches, chart):
    def length(run):
        return sum(math.dist(run[k], run[k - 1]) for k in range(1, len(run)))

    one = min(range(len(touches)), key=lambda i: math.dist(touches[i], chart[0]))
    two = min(range(len(touches)), key=lambda i: math.dist(touches[i], chart[-1]))
    low, high = min(one, two), max(one, two)
    inside = touches[low:high + 1]
    if math.dist(touches[0], touches[-1]) > 1e-9:
        return inside
    around = touches[high:] + touches[1:low + 1]
    wanted = length(chart)
    if abs(length(around) - wanted) < abs(length(inside) - wanted):
        return around
    return inside


def refined_blend_boundary(model, blend, side, merged, ends, declared):
    clipped = clip_chart(merged, ends)
    if len(clipped) < 2:
        return clipped
    try:
        supports, given = _blend_parameters(model, blend)
        stations = blend_stations(model, blend)
    except EvaluateError:
        return clipped
    if len(given) < 2 or len(stations) < 2 or not _exact_supports(supports):
        return clipped
    signs = _blend_signs(model, supports, given[0])
    radii = sorted(station[3] for station in given)
    radius = radii[len(radii) // 2]
    k = side - 1

    def solve(anchor, tangent, guess):
        length = math.sqrt(sum(c * c for c in tangent))
        if length == 0.0:
            return None
        axis = [c / length for c in tangent]

        def plane(model_, _centre, y):
            touch = surface_point(model_, supports[k], y[2 * k], y[2 * k + 1])
            return sum((touch[i] - anchor[i]) * axis[i] for i in range(3))

        found = _solve_station(model, supports, signs, radius, guess, plane)
        if found is None:
            return None
        return list(found[side]), [*found[5][0], *found[5][1]]

    params = []
    for j, point in enumerate(clipped):
        before = clipped[max(j - 1, 0)]
        after = clipped[min(j + 1, len(clipped) - 1)]
        tangent = [after[i] - before[i] for i in range(3)]
        near = min(stations, key=lambda st: math.dist(st[side], point))
        found = solve(point, tangent, [*near[5][0], *near[5][1]])
        if found is None or math.dist(found[0], point) > VERTEX_ON_CHART:
            return clipped
        params.append(found[1])
    dense, _params = _refine_chart(
        model, supports, [list(p) for p in clipped], params,
        [False] * (len(clipped) - 1), CHART_REACH * declared, solve=solve,
    )
    return dense


_CLOSED_FORM_FEET = ("PLANE", "CYLINDER", "CONE", "SPHERE", "TORUS", "BLENDED_EDGE")

_EXACT_SECTIONS_KEPT = 20000

_EXACT_SPINE_CACHE = "_epine_exacte"
_EXACT_SECTIONS_CACHE = "_sections_exactes"
_SECTION_WARM_CACHE = "_amorce_section"
_FOOT_SEED_CACHE = "_amorce_pied"
_FILE_STATIONS_CACHE = "_stations_fichier"
_BLEND_ORDER_CACHE = "_ordre_appuis"
_NORMAL_SIGN_CACHE = "_signe_normale"


def _keep(node, name, value):
    try:
        setattr(node, name, value)
    except AttributeError:
        pass
    return value


def _exact_spine(model, spine):
    kept = getattr(spine, _EXACT_SPINE_CACHE, None)
    if kept is not None:
        return kept
    points, _declared, read = dense_chart(model, spine)
    if read is None or len(points) < 2 or any(r is None for r in read[1]):
        raise EvaluateError("a blend spine that cannot be followed exactly")
    surfaces, (one, two) = read
    params = [list(one[k]) + list(two[k]) for k in range(len(points))]
    arcs = [0.0]
    for k in range(1, len(points)):
        arcs.append(arcs[-1] + math.dist(points[k], points[k - 1]))
    return _keep(spine, _EXACT_SPINE_CACHE,
                 (points, surfaces, params, arcs, _chart_base(model, spine)))


def _stationary_foot(model, support, centre, seed, turns=40):
    u, v = seed
    step = 1e-7

    def field(u, v):
        point = surface_point(model, support, u, v)
        du, dv = _partials(model, support, u, v)
        w = [point[i] - centre[i] for i in range(3)]
        return _dot(w, du), _dot(w, dv)
    try:
        for _turn in range(turns):
            f1, f2 = field(u, v)
            if abs(f1) + abs(f2) < 1e-18:
                break
            a1, a2 = field(u + step, v)
            b1, b2 = field(u, v + step)
            j11, j12 = (a1 - f1) / step, (b1 - f1) / step
            j21, j22 = (a2 - f2) / step, (b2 - f2) / step
            det = j11 * j22 - j12 * j21
            if det == 0.0:
                return None
            du = (f1 * j22 - f2 * j12) / det
            dv = (j11 * f2 - j21 * f1) / det
            u, v = u - du, v - dv
            if abs(du) + abs(dv) < 1e-12:
                break
        point = surface_point(model, support, u, v)
    except EvaluateError:
        return None
    return (u, v), math.dist(point, centre)


def _blend_foot(model, support, centre, near, radius, given):
    if support.name in _CLOSED_FORM_FEET:
        return foot_point(model, support, centre)
    seeds = [given] if given is not None else []
    previous = getattr(support, _FOOT_SEED_CACHE, None)
    if previous is not None:
        seeds.append(previous)
    for seed in _grid_seeds(model, support, near) or []:
        found = _newton_inverse(model, support, near, seed)
        if found is not None and found[1] <= CHART_TOLERANCE:
            seeds.append(found[0])
            break
    best = None
    for seed in seeds:
        found = _stationary_foot(model, support, centre, seed)
        if found is None:
            continue
        miss = abs(found[1] - radius)
        if best is None or miss < best[0]:
            best = (miss, found[0])
        if miss <= BLEND_CONSISTENCY:
            break
    if best is None or best[0] > BLEND_CONSISTENCY:
        for seed in _grid_seeds(model, support, near) or []:
            found = _newton_inverse(model, support, centre, seed)
            if found is None:
                continue
            miss = abs(found[1] - radius)
            if best is None or miss < best[0]:
                best = (miss, found[0])
    if best is None:
        raise EvaluateError("a blend foot that will not resolve")
    _keep(support, _FOOT_SEED_CACHE, best[1])
    return surface_point(model, support, *best[1])


def _file_stations(model, node):
    kept = getattr(node, _FILE_STATIONS_CACHE, None)
    if kept is not None:
        return kept or None
    try:
        stations = _blend_parameters(model, node)[1]
    except EvaluateError:
        stations = ()
    return _keep(node, _FILE_STATIONS_CACHE, stations) or None


def _exact_section(model, node, u):
    sections = getattr(node, _EXACT_SECTIONS_CACHE, None)
    if sections is None:
        sections = _keep(node, _EXACT_SECTIONS_CACHE, {})
    found = sections.get(u)
    if found is None:
        found = _solve_exact_section(model, node, u)
        if len(sections) > _EXACT_SECTIONS_KEPT:
            sections.clear()
        sections[u] = found
    return found


def _spine_guess(model, spine, anchor):
    points, surfaces, params, _arcs, _base = _exact_spine(model, spine)
    best = None
    for k in range(1, len(points)):
        before, after = points[k - 1], points[k]
        chord = [after[i] - before[i] for i in range(3)]
        length = _dot(chord, chord)
        if not length:
            continue
        part = _dot([anchor[i] - before[i] for i in range(3)], chord) / length
        clamped = min(1.0, max(0.0, part))
        near = [before[i] + clamped * chord[i] for i in range(3)]
        key = math.dist(near, anchor)
        if best is None or key < best[0]:
            best = (key, k, part)
    _key, k, part = best
    return surfaces, _lerp_parameters(surfaces, params[k - 1], params[k], part, model)


def _solve_exact_section(model, node, u):
    spine = model.get(node.values.get("spine", 0))
    frames, arcs, base = blend_frames(model, node)
    distance = u - base
    at = 1
    while at < len(arcs) - 1 and arcs[at] < distance:
        at += 1
    step = arcs[at] - arcs[at - 1]
    part = (distance - arcs[at - 1]) / step if step else 0.0
    before, after = frames[at - 1][0], frames[at][0]
    chord = [after[i] - before[i] for i in range(3)]
    anchor = [before[i] + part * chord[i] for i in range(3)]
    surfaces, guess = _spine_guess(model, spine, anchor)
    found = None
    warm = getattr(node, _SECTION_WARM_CACHE, None)
    if warm is not None and abs(warm[0] - u) <= 0.25 * max(step, 1e-12):
        found = _intersection_point(model, surfaces, anchor, chord, warm[1])
    if found is None:
        found = _intersection_point(model, surfaces, anchor, chord, guess)
    if found is None:
        raise EvaluateError("a blend centre off its exact spine")
    _keep(node, _SECTION_WARM_CACHE, (u, found[1]))
    centre, x = found
    tangent = _spine_tangent(model, surfaces, x, chord)
    supports = [model.get(i) for i in node.values["surface"]]
    ranges = node.values.get("range") or [0.0, 0.0]
    radius = max(abs(r) for r in ranges)

    decided = getattr(node, _BLEND_ORDER_CACHE, None)
    if decided is None:
        decided = _keep(node, _BLEND_ORDER_CACHE, _blend_order(model, supports, spine))
    order, sure = decided
    for left, right in ([order] if sure else [(0, 1), (1, 0)]):
        try:
            first = surface_point(model, supports[0], x[2 * left], x[2 * left + 1])
            second = surface_point(model, supports[1], x[2 * right], x[2 * right + 1])
        except EvaluateError:
            continue
        if (abs(math.dist(first, centre) - radius) <= BLEND_CONSISTENCY
                and abs(math.dist(second, centre) - radius) <= BLEND_CONSISTENCY):
            return centre, first, second, tangent

    k = min(range(len(frames)), key=lambda j: math.dist(frames[j][0], centre))
    stations = _file_stations(model, node)
    touches = []
    for side, support in enumerate(supports):
        given = None
        if stations:
            j = min(range(len(stations)), key=lambda i: math.dist(stations[i][0], centre))
            given = tuple(stations[j][5][side])
        foot = _blend_foot(model, support, centre, frames[k][1 + side], radius, given)
        if radius <= 0.0 or abs(math.dist(foot, centre) - radius) <= BLEND_CONSISTENCY:
            touches.append(foot)
            continue
        if support.name == "BLENDED_EDGE":
            raise EvaluateError("a blend centre on a support blend")
        uv = _invert_point(model, support, foot)
        if uv is None:
            raise EvaluateError("a blend foot without parameters")
        normal = _normal(model, support, *uv)
        touches.append([centre[i] - ranges[side] * normal[i] for i in range(3)])
    return centre, touches[0], touches[1], tangent


def _section_frame(node, spine, centre, first, second, tangent):
    if max(abs(r) for r in node.values.get("range") or [0.0]) <= 0.0:
        return None, None, 1.0, 0.0
    radius = math.dist(first, centre)
    if abs(radius - math.dist(second, centre)) > BLEND_CONSISTENCY:
        raise EvaluateError("a blend section whose touches disagree on the radius")
    if _sense_of(spine.values.get("sense")) != "-":
        first, second = second, first
    one, sideways, total = _arc_frame(centre, first, second, radius, tangent)
    return one, sideways, total, radius


def _under_way_section(model, node, u):
    _BLENDS_UNDER_WAY.add(node.index)
    try:
        return _exact_section(model, node, u)
    finally:
        _BLENDS_UNDER_WAY.discard(node.index)


def _exact_blend_point(model, node, u, v):
    spine = model.get(node.values.get("spine", 0))
    try:
        centre, first, second, tangent = _under_way_section(model, node, u)
        one, sideways, total, radius = _section_frame(
            node, spine, centre, first, second, tangent)
    except EvaluateError:
        return None
    if one is None:
        return centre
    angle = v * total
    return [centre[i] + radius * (math.cos(angle) * one[i]
                                  + math.sin(angle) * sideways[i])
            for i in range(3)]


def _beyond_spine_ends(model, spine, point):
    points, surfaces, params, _arcs, _base = _exact_spine(model, spine)
    best = None
    ends = ((points[-2], points[-1], params[-1]), (points[1], points[0], params[0]))
    for inner, end, end_params in ends:
        chord = [end[i] - inner[i] for i in range(3)]
        axis = _unit(chord)

        def at(t, end=end, axis=axis, chord=chord, end_params=end_params):
            anchor = [end[i] + t * axis[i] for i in range(3)]
            found = _intersection_point(model, surfaces, anchor, chord, end_params)
            if found is None:
                return None
            tangent = _spine_tangent(model, surfaces, found[1], chord)
            return found[0], _dot([point[i] - found[0][i] for i in range(3)], tangent)
        reach = _dot([point[i] - end[i] for i in range(3)], axis)
        if reach <= 0.0:
            continue
        t0, t1 = 0.0, reach
        f0, f1 = at(t0), at(t1)
        if f0 is None or f1 is None:
            continue
        for _turn in range(60):
            if f1[1] == f0[1] or abs(f1[1]) < 1e-15:
                break
            t0, t1 = t1, t1 - f1[1] * (t1 - t0) / (f1[1] - f0[1])
            f0, f1 = f1, at(t1)
            if f1 is None:
                break
        if f1 is not None and (best is None or abs(f1[1]) < best[0]):
            best = (abs(f1[1]), f1[0])
    if best is None or best[0] > 1e-12:
        raise EvaluateError("a point beyond both ends of a blend spine")
    return best[1]


def _u_by_secant(model, node, spine, point):
    frames, arcs, base = blend_frames(model, node)
    points = [frame[0] for frame in frames]
    radius = max(abs(r) for r in node.values.get("range") or [0.0])
    best = None
    for k in range(1, len(points)):
        before, after = points[k - 1], points[k]
        chord = [after[i] - before[i] for i in range(3)]
        length = _dot(chord, chord)
        if not length:
            continue
        part = _dot([point[i] - before[i] for i in range(3)], chord) / length
        outside = max(0.0, -part, part - 1.0)
        foot = [before[i] + part * chord[i] for i in range(3)]
        key = (outside > 0.0 and 0 < k < len(points) - 1,
               abs(math.dist(foot, point) - radius) + outside)
        if best is None or key < best[0]:
            best = (key, k, part)
    if best is None:
        return None
    _key, k, part = best

    def g(u):
        centre, _first, _second, tangent = _under_way_section(model, node, u)
        return _dot([point[i] - centre[i] for i in range(3)], _unit(tangent))
    u0 = base + arcs[k - 1] + part * (arcs[k] - arcs[k - 1])
    try:
        g0 = g(u0)
        u1 = u0 + g0
        g1 = g(u1)
        for _turn in range(30):
            if abs(g1) < 1e-15:
                return u1
            if g1 == g0:
                return None
            u0, u1, g0 = u1, u1 - g1 * (u1 - u0) / (g1 - g0), g1
            g1 = g(u1)
    except EvaluateError:
        return None
    return None


def _exact_blend_inverse(model, node, point):
    spine = model.get(node.values.get("spine", 0))
    u = _u_by_secant(model, node, spine, point)
    if u is None:
        frames, arcs, base = blend_frames(model, node)
        points = [frame[0] for frame in frames]
        try:
            centre = _spine_centre(model, spine, point)
        except EvaluateError:
            centre = _beyond_spine_ends(model, spine, point)
        best = None
        for k in range(1, len(points)):
            before, after = points[k - 1], points[k]
            chord = [after[i] - before[i] for i in range(3)]
            length = _dot(chord, chord)
            part = (_dot([centre[i] - before[i] for i in range(3)], chord) / length
                    if length else 0.0)
            outside = max(0.0, -part, part - 1.0)
            if best is None or outside < best[0]:
                best = (outside, k, part)
        _outside, k, part = best
        u = base + arcs[k - 1] + part * (arcs[k] - arcs[k - 1])
    centre, first, second, tangent = _under_way_section(model, node, u)
    one, sideways, total, _radius = _section_frame(
        node, spine, centre, first, second, tangent)
    if one is None:
        return u, 0.0
    w = [point[i] - centre[i] for i in range(3)]
    angle = math.atan2(_dot(w, sideways), _dot(w, one))
    angle = min((angle + j * 2.0 * math.pi for j in (-1, 0, 1)),
                key=lambda a: abs(a / total - 0.5))
    return u, angle / total


def _on_intersection_spine(model, node):
    if node is None or node.name != "BLENDED_EDGE":
        return False
    spine = model.get(node.values.get("spine", 0))
    return spine is not None and spine.name == "INTERSECTION"


def _blend_inverse(model, node, point):
    if not _on_intersection_spine(model, node):
        return surface_parameters(model, node, point)
    try:
        uv = _exact_blend_inverse(model, node, point)
    except EvaluateError:
        return None
    back = _exact_blend_point(model, node, *uv)
    if back is None or math.dist(back, point) > SURFACE_INVERSE:
        return None
    return uv


def _blend_normal(model, node, u, v):
    if not _on_intersection_spine(model, node):
        return None
    if max(abs(r) for r in node.values.get("range") or [0.0]) <= 0.0:
        return None
    try:
        centre = _under_way_section(model, node, u)[0]
    except EvaluateError:
        return None
    point = _exact_blend_point(model, node, u, v)
    if point is None:
        return None
    radial = _unit([point[i] - centre[i] for i in range(3)])
    sign = getattr(node, _NORMAL_SIGN_CACHE, None)
    if sign is None:
        by_differences = _normal_from_partials(model, node, u, v)
        sign = _keep(node, _NORMAL_SIGN_CACHE,
                     1.0 if _dot(by_differences, radial) >= 0.0 else -1.0)
    return [sign * c for c in radial]


def _blend_sides_from_points(model, node, points, numbers):
    surfaces = [model.get(i) for i in (node.values.get("surface") or [])]
    sides = [s for s, surface in enumerate(surfaces)
             if _on_intersection_spine(model, surface)]
    if not sides:
        return numbers
    out = list(numbers)
    for side in sides:
        for k, point in enumerate(points):
            if 4 * k + 2 * side + 1 >= len(out):
                break
            uv = _blend_inverse(model, surfaces[side], point)
            if uv is None:
                out[4 * k + 2 * side] = out[4 * k + 2 * side + 1] = NULL_DOUBLE
            else:
                out[4 * k + 2 * side], out[4 * k + 2 * side + 1] = uv
    return out


def _intersection_on_chord(model, surfaces, anchor, guess):
    if guess is None:
        return None
    params = []
    for side, surface in enumerate(surfaces):
        if surface.name == "BLENDED_EDGE":
            return None
        if surface.name in _NEWTON_INVERTIBLE:
            found = _newton_inverse(model, surface, anchor,
                                    (guess[2 * side], guess[2 * side + 1]), turns=20)
            if found is None or found[1] > SURFACE_INVERSE:
                return None
            uv = found[0]
        else:
            uv = surface_parameters(model, surface, anchor)
            if uv is None or math.dist(surface_point(model, surface, *uv),
                                       anchor) > SURFACE_INVERSE:
                return None
        params += list(uv)
    return list(anchor), tuple(params)


def blend_point(model, node, u, v):
    if node.index in _BLENDS_UNDER_WAY:
        raise EvaluateError("a blend supporting itself")
    spine = model.get(node.values.get("spine", 0))
    if spine is not None and spine.name in ANALYTIC_SPINES:
        _BLENDS_UNDER_WAY.add(node.index)
        try:
            return analytic_blend_point(model, node, spine, u, v)
        finally:
            _BLENDS_UNDER_WAY.discard(node.index)
    if spine is not None and spine.name == "INTERSECTION":
        exact = _exact_blend_point(model, node, u, v)
        if exact is not None:
            return exact
    _BLENDS_UNDER_WAY.add(node.index)
    try:
        frames, arcs, base = blend_frames(model, node)
    finally:
        _BLENDS_UNDER_WAY.discard(node.index)
    distance = u - base

    at = 1
    while at < len(arcs) - 1 and arcs[at] < distance:
        at += 1
    step = arcs[at] - arcs[at - 1]
    part = (distance - arcs[at - 1]) / step if step else 0.0
    before, after = frames[at - 1], frames[at]

    def between(first, second):
        return [first[i] + part * (second[i] - first[i]) for i in range(3)]

    centre = between(before[0], after[0])
    radius = before[3] + part * (after[3] - before[3])
    if radius <= 0.0:
        return centre
    first, second = between(before[1], after[1]), between(before[2], after[2])
    if spine is not None and _sense_of(spine.values.get("sense")) != "-":
        first, second = second, first
    one, sideways, total = _arc_frame(
        centre, first, second, radius,
        [after[0][i] - before[0][i] for i in range(3)],
    )
    angle = v * total
    return [
        centre[i] + radius * (math.cos(angle) * one[i]
                              + math.sin(angle) * sideways[i])
        for i in range(3)
    ]


def _arc_frame(centre, first, second, radius, normal):
    one = [(first[i] - centre[i]) / radius for i in range(3)]
    two = [(second[i] - centre[i]) / radius for i in range(3)]
    axis = _cross(one, two)
    if math.sqrt(sum(c * c for c in axis)) < 1e-12:
        axis = normal
    axis = _unit(axis)
    sideways = _cross(axis, one)
    total = math.atan2(_dot(_cross(one, two), axis), _dot(one, two))
    if total <= 0.0:
        total += 2.0 * math.pi
    return one, sideways, total


def _arc_spans(total):
    return max(1, math.ceil(total / (math.pi / 2.0) - 1e-9))


def _arc_poles(centre, first, second, radius, normal, spans):
    one, sideways, total = _arc_frame(centre, first, second, radius, normal)
    step = total / spans
    weight = math.cos(step / 2.0)
    reach = radius / weight

    def at(angle):
        return [
            centre[i] + radius * (math.cos(angle) * one[i]
                                  + math.sin(angle) * sideways[i])
            for i in range(3)
        ]

    poles = [first]
    weights = [1.0]
    for k in range(spans):
        middle = at(step * (k + 0.5))
        poles.append(
            [centre[i] + reach * (middle[i] - centre[i]) / radius
             for i in range(3)]
        )
        weights.append(weight)
        poles.append(second if k == spans - 1 else at(step * (k + 1)))
        weights.append(1.0)
    knots = [k / spans for k in range(spans + 1)]
    mults = [3] + [2] * (spans - 1) + [3]
    return poles, weights, knots, mults


def blend_surface(model, node, stations=None):
    sections = blend_sections(model, node, stations)
    spine = [sections[min(1, len(sections) - 1)][0][i] - sections[0][0][i]
             for i in range(3)]
    spans = max(
        _arc_spans(_arc_frame(centre, first, second, radius, spine)[2])
        for centre, first, second, radius in sections
    )
    rows, weights = [], []
    v_knots = v_mults = None
    for centre, first, second, radius in sections:
        poles, weight, knots, mults = _arc_poles(
            centre, first, second, radius, spine, spans
        )
        v_knots, v_mults = knots, mults
        rows.append(poles)
        weights.append(weight)
    width = len(rows[0])

    times = [0.0]
    for k in range(1, len(sections)):
        step = math.dist(sections[k][0], sections[k - 1][0]) ** 0.5
        times.append(times[-1] + (step if step > 0.0 else 1e-9))

    columns = [[row[i] for row in rows] for i in range(width)]
    column_weights = [[w[i] for w in weights] for i in range(width)]
    built, built_weights = [], []
    for column, column_weight in zip(columns, column_weights, strict=True):
        poles, knots, mults = _through(column, times)
        built.append(poles)
        built_weights.append(_through_scalars(column_weight, times))
    u_knots, u_mults = knots, mults
    net = [[built[i][k] for i in range(width)] for k in range(len(built[0]))]
    net_weights = [
        [built_weights[i][k] for i in range(width)]
        for k in range(len(built[0]))
    ]
    return net, net_weights, u_knots, u_mults, v_knots, v_mults


def _through(points, times):
    width = len(points[0])
    tangents = _tangents_at(points, times)
    poles = [points[0]]
    for i in range(len(points) - 1):
        span = times[i + 1] - times[i]
        poles.append(
            [points[i][k] + tangents[i][k] * span / 3.0 for k in range(width)]
        )
        poles.append(
            [points[i + 1][k] - tangents[i + 1][k] * span / 3.0 for k in range(width)]
        )
        poles.append(points[i + 1])
    return poles, times, [4] + [3] * (len(points) - 2) + [4]


def _through_scalars(values, times):
    points = [[v, 0.0, 0.0] for v in values]
    poles, _knots, _mults = _through(points, times)
    return [p[0] for p in poles]


def _tangents_at(points, times):
    width = len(points[0])
    out = []
    for i in range(len(points)):
        if i == 0:
            span = times[1] - times[0]
            out.append([(points[1][k] - points[0][k]) / span for k in range(width)])
        elif i == len(points) - 1:
            span = times[i] - times[i - 1]
            out.append(
                [(points[i][k] - points[i - 1][k]) / span for k in range(width)]
            )
        else:
            span = times[i + 1] - times[i - 1]
            out.append(
                [(points[i + 1][k] - points[i - 1][k]) / span for k in range(width)]
            )
    return out

