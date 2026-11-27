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

    def test_curve_refines_one_small_region_without_relaxing_error(self):
        spec={"degree":1,"poles":[(0,0),(1,0)],"knots":[0,0,1,1],"distinct":[0,1]}
        def span(model,surface,curve,a,b,p,q):
            return p,q,1.0 if a==0 and b>2**-12 else 0.0
        with patch.object(e,"_bezier_span",side_effect=span), patch.object(e,"_composed",side_effect=lambda m,s,c,t:[t,0,0]):
            _,times,_,error=e.adaptive_cubic(None,None,spec)
            self.assertEqual((times[1],error),(2**-12,0))
            with patch.object(e,"MAX_FITTED_SEGMENTS",8),self.assertRaisesRegex(e.EvaluateError,"segment limit"):
                e.adaptive_cubic(None,None,spec)


if __name__=="__main__":
    unittest.main()
