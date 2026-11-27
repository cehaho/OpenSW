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
