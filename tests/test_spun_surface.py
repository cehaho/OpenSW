import math
import unittest

from sldprt.evaluate import surface_point
from sldprt.step import Writer, write_surface, _same_sense
from sldprt.xt.binary import Node


class SpunSurfaceTests(unittest.TestCase):
    def setUp(self):
        line = Node(0, "LINE", 1, {"pvec": [3, 2, 5], "direction": [0, 0, 1]})
        trim = Node(0, "TRIMMED_CURVE", 2, {"basis_curve": 1, "parm_1": -0.25, "parm_2": 0.5})
        self.surface = Node(0, "SPUN_SURF", 3, {"base": [1, 2, 3], "axis": [0, 0, 1], "profile": 2})
        self.model = {n.index: n for n in (line, trim, self.surface)}

    def test_profile_and_signed_angle(self):
        for angle, y in [(math.pi / 2, 4), (-math.pi / 2, 0)]:
            actual = surface_point(self.model, self.surface, 0.25, angle)
            for got, expected in zip(actual, [1, y, 5.25]):
                self.assertAlmostEqual(got, expected)

    def test_step_keeps_trimmed_generator_and_surface_orientation(self):
        writer = Writer()
        index = write_surface(writer, self.surface, self.model)
        self.assertIn("SURFACE_OF_REVOLUTION", writer._lines[index-1])
        trim = next(line for line in writer._lines if line.startswith("TRIMMED_CURVE"))
        self.assertIn("-0.25", trim)
        self.assertIn("0.5", trim)
        self.assertEqual(_same_sense(Node(0, "FACE", 4), self.surface), ".F.")
