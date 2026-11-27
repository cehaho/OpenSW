import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from sldprt import evaluate as e


class SurfaceCurveTest(unittest.TestCase):
    def test_curve_uses_its_two_dimensional_basis_on_the_recorded_surface(self):
        nodes = {1:SimpleNamespace(name="PLANE", values={"pvec":[0,0,1],"normal":[0,0,1],"x_axis":[1,0,0]}),
                 2:SimpleNamespace(name="B_CURVE")}
        curve = SimpleNamespace(name="SP_CURVE", values={"b_curve":2,"surface":1})
        spec = {"dim":2,"degree":1,"poles":[[0,0],[1,2]],"weights":None,"knots":[0,0,1,1]}
        with patch.object(e,"read_nurbs_curve",return_value=spec), patch.object(e,"surface_point",side_effect=lambda m,s,u,v:[u,v,1]) as surface:
            self.assertEqual(e.curve_point(nodes,curve,.5),[.5,1.,1])
            self.assertIs(surface.call_args.args[1],nodes[1])

    def test_intersection_stays_on_both_cylinders_in_both_directions(self):
        def cylinder(radius, axis):
            return SimpleNamespace(name="CYLINDER", values={
                "pvec": [0, 0, 0], "axis": axis, "x_axis": [1, 0, 0], "radius": radius,
            })

        carriers = {1: cylinder(0.002, [0, 0, 1]), 2: cylinder(0.004, [0, 1, 0])}
        intersection = SimpleNamespace(values={"surface": [1, 2]})
        stations = []
        for angle in (2.8, 3.0, 3.3, 3.6):
            x = 0.002 * math.cos(angle)
            stations.append([x, 0.002 * math.sin(angle), math.sqrt(0.004**2 - x*x)])
        for sequence in (stations, stations[::-1]):
            poles, times, _, _ = e.cylinder_intersection(carriers, intersection, sequence)
            self.assertLess(math.dist(poles[0], sequence[0]), 1e-14)
            self.assertLess(math.dist(poles[-1], sequence[-1]), 1e-14)
            self.assertTrue(all(b > a for a, b in zip(times, times[1:])))
            for i in range(0, len(poles)-1, 3):
                for j in range(21):
                    x, y, z = e._bezier_point(poles[i:i+4], j / 20)
                    self.assertLess(abs(math.hypot(x, y) - 0.002), 1e-9)
                    self.assertLess(abs(math.hypot(x, z) - 0.004), 1e-9)

        x, y, z = stations[-1]
        for wrong_station in ([x, y, -z], [x, y, z+1e-5]):
            self.assertIsNone(e.cylinder_intersection(
                carriers, intersection, [*stations[:-1], wrong_station]
            ))

    def test_projected_chart_meets_both_carriers_between_stations(self):
        def surface(name, **kwargs):
            return SimpleNamespace(name=name, values=kwargs)
        common = {"axis":[0,0,1], "x_axis":[1,0,0], "pvec":[0,0,0]}
        plane = surface("PLANE", normal=[0,0,1], x_axis=[1,0,0], pvec=[0,0,0])
        for carrier, radius in [
            (surface("CYLINDER", **common, radius=.004), .004),
            (surface("CONE", **common, radius=.004, sin_half_angle=.6, cos_half_angle=.8), .004),
            (surface("SPHERE", centre=[0,0,0], axis=[0,0,1], x_axis=[1,0,0], radius=.004), .004),
            (surface("TORUS", centre=[0,0,0], axis=[0,0,1], x_axis=[1,0,0], major_radius=.005, minor_radius=.001), .006),
        ]:
            with self.subTest(carrier=carrier.name):
                points = [[radius*math.cos(t),radius*math.sin(t),0] for t in (.2,.4,.6,.8)]
                poles, _, _, _ = e.projected_intersection({}, [carrier,plane], points, None)
                for j in range(0,len(poles)-1,3):
                    for k in range(17):
                        x,y,z = e._bezier_point(poles[j:j+4], k/16)
                        self.assertLess(abs(z), 5e-9)
                        self.assertLess(abs(math.hypot(x,y)-radius), 5e-9)

    def test_revolved_bspline_and_offset_carriers_use_the_same_projector(self):
        def node(name, **values):
            return SimpleNamespace(name=name, values=values)
        plane = node("PLANE", pvec=[0,0,0], normal=[0,0,1], x_axis=[1,0,0])
        spline = node("B_SURFACE")
        spline._releve_nurbs = {"u_knots":[0,0,1,1], "v_knots":[0,0,1,1],
            "u_degree":1,"v_degree":1,"n_u":2,"n_v":2,"weights":None,
            "net":[[[0,0,-.5],[0,0,.5]],[[1,0,-.5],[1,0,.5]]]}
        offset = node("OFFSET_SURF", surface=1, offset=.01)
        revolved = node("SPUN_SURF", profile=2, base=[0,0,0], axis=[0,0,1])
        model = {1:spline, 2:node("LINE",pvec=[.02,0,0],direction=[0,0,1])}
        for carrier, uv in [(spline,[(t,.5) for t in (.2,.4,.6)]),
                            (offset,[(t,.5) for t in (.2,.4,.6)]),
                            (revolved,[(0,t) for t in (.2,.4,.6)])]:
            with self.subTest(carrier=carrier.name):
                points = [e.surface_point(model, carrier, *pair) for pair in uv]
                samples = [uv, [e.surface_parameters(model, plane, point) for point in points]]
                poles, _, _, error = e.projected_intersection(model,[carrier,plane],points,samples)
                self.assertLess(error,5e-9)
                for j in range(0,len(poles)-1,3):
                    for k in range(17):
                        x,y,z = e._bezier_point(poles[j:j+4], k/16)
                        self.assertLess(abs(z),5e-9)
                        deviation = math.hypot(x,y)-.02 if carrier is revolved else y-points[0][1]
                        self.assertLess(abs(deviation),5e-9)

    def test_revolution_grid_uses_the_active_profile_knot_range(self):
        surface = SimpleNamespace(name="SPUN_SURF", values={"profile": 1})
        profile = SimpleNamespace(name="B_CURVE")
        curve = {"degree": 2, "knots": [-2, -1, 3, 4, 5, 9, 10], "poles": [0] * 4}
        with patch.object(e, "read_nurbs_curve", return_value=curve):
            self.assertEqual(e._uv_domain({1: profile}, surface), ((3, 5), (0, 2 * math.pi)))
        self.assertEqual(e.PERIODS[surface.name], (None, 2 * math.pi))

    def test_curve_refines_one_small_region_without_relaxing_error(self):
        spec={"degree":1,"poles":[(0,0),(1,0)],"knots":[0,0,1,1],"distinct":[0,1]}
        def span(point_at,a,b,p,q):
            return p,q,1.0 if a==0 and b>2**-12 else 0.0
        with patch.object(e,"_bezier_span",side_effect=span), patch.object(e,"_composed",side_effect=lambda m,s,c,t:[t,0,0]):
            _,times,_,error=e.adaptive_cubic(None,None,spec)
            self.assertEqual((times[1],error),(2**-12,0))
            with patch.object(e,"MAX_FITTED_SEGMENTS",8),self.assertRaisesRegex(e.EvaluateError,"segment limit"):
                e.adaptive_cubic(None,None,spec)


if __name__=="__main__":
    unittest.main()
