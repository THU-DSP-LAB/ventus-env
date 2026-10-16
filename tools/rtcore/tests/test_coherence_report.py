"""Fast parser checks for the all-depth RT coherence report."""
import tempfile
import unittest
from pathlib import Path

from tools.rtcore.coherence_report import parse_log, summarize, write_report


class CoherenceReportTest(unittest.TestCase):
    def test_joins_depth_geometry_and_node_paths(self):
        log = """
[RTCORE coherence-meta] warp= 0 epoch=         7 issue=  3 lane=0 depth=  0 active_level=  0 mask=0x3
[RTCORE coherence-meta] warp= 0 epoch=         7 issue=  3 lane=1 depth=  0 active_level=  0 mask=0x3
[RTCORE coherence-ray] warp= 0 epoch=         7 lane= 0 depth=  0 active_level=  0 source=box o=[00000000,00000000,00000000] d=[3f800000,00000000,00000000] tmin=00000000 tmax=41200000
[RTCORE coherence-ray] warp= 0 epoch=         7 lane= 1 depth=  0 active_level=  0 source=instance o=[00000000,3f800000,00000000] d=[3f800000,00000000,00000000] tmin=00000000 tmax=41200000
[RTCORE node] warp=0 epoch=         7 issue=  3 lane=0 kind=1 ref=0x10 object=0
[RTCORE node] warp=0 epoch=         7 issue=  3 lane=1 kind=1 ref=0x10 object=0
[RTCORE coherence-line] warp= 0 epoch=         7 lane= 0 addr=0x1000 is64=0 object=0
[RTCORE coherence-line] warp= 0 epoch=         7 lane= 1 addr=0x1000 is64=0 object=0
[RTCORE coherence-meta] warp= 0 epoch=         8 issue=  4 lane=0 depth=  0 active_level=  0 mask=0x1
[RTCORE coherence-ray] warp= 0 epoch=         8 lane= 0 depth=  0 active_level=  0 source=box o=[3f800000,00000000,00000000] d=[00000000,3f800000,00000000] tmin=00000000 tmax=41200000
[RTCORE node] warp=0 epoch=         8 issue=  4 lane=0 kind=1 ref=0x20 object=0
[RTCORE coherence-line] warp= 0 epoch=         8 lane= 0 addr=0x2000 is64=0 object=0
""".strip()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "render.log"
            path.write_text(log)
            parsed = parse_log(path)
            summary = summarize(parsed)
            self.assertEqual(summary["input"]["meta_rays"], 3)
            self.assertEqual(summary["by_depth"]["0"]["rays"], 2)
            self.assertEqual(summary["by_depth"]["1"]["rays"], 1)
            self.assertAlmostEqual(summary["by_depth"]["0"]["direction_resultant"], 1.0)
            self.assertAlmostEqual(summary["by_depth"]["0"]["line_jaccard_mean"], 1.0)
            output = Path(directory) / "report"
            write_report(summary, output, path)
            self.assertTrue((output / "报告.md").is_file())
            self.assertTrue((output / "data.json").is_file())


if __name__ == "__main__":
    unittest.main()
