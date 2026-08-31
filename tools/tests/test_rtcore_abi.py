"""Regression checks for the generated fixed PDS-header ABI constants."""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "tools/rtcore_abi.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("rtcore_abi", TOOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RtCoreAbiGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = load_tool()
        source_text = cls.tool.SOURCE.read_text(encoding="utf-8")
        cls.constants = cls.tool.parse_constants(source_text, ("VT_RT_",))
        cls.function = cls.tool.parse_payload_base_function(source_text)

    def test_field_major_hit_record_layout(self) -> None:
        self.assertEqual(self.constants["VT_RT_ABI_VERSION"], 11)
        self.assertEqual(self.constants["VT_RT_ABI_CANDIDATE_HIT_RECORD_BASE_BYTES"], 76)
        self.assertEqual(self.constants["VT_RT_ABI_CANDIDATE_HIT_RECORD_SIZE_BYTES"], 36)
        self.assertEqual(self.constants["VT_RT_ABI_COMMITTED_HIT_RECORD_BASE_BYTES"], 112)
        self.assertEqual(self.constants["VT_RT_ABI_COMMITTED_HIT_RECORD_SIZE_BYTES"], 36)
        self.assertEqual(self.constants["VT_RT_ABI_HIT_ATTRIB_BASE_BYTES"], 148)
        self.assertEqual(self.constants["VT_RT_ABI_FIXED_HEADER_SIZE_BYTES"], 148)
        self.assertEqual(self.constants["VT_RT_HIT_RECORD_WORD_COUNT"], 9)
        self.assertEqual(self.constants["VT_RT_ABI_PDS_FIELD_MAJOR_FIXED_HEADER_WORD_COUNT"], 37)
        self.assertNotIn("VT_RT_ABI_PDS_LANE_MAJOR_HIT_RECORD_BODY_WORD_COUNT", self.constants)
        self.assertNotIn("VT_RT_ABI_PDS_CANDIDATE_HIT_KIND_BASE_WORD_COUNT", self.constants)
        self.assertNotIn("VT_RT_ABI_HIT_RECORD_ADDR_TAG", self.constants)

    def test_compact_trace_meta_replaces_individual_trace_slots(self) -> None:
        self.assertEqual(self.constants["VT_RT_SLOT_TRACE_META0"], 3)
        self.assertEqual(self.constants["VT_RT_SLOT_TRACE_META1"], 4)
        self.assertEqual(self.constants["VT_RT_SLOT_WORD_COUNT"], 18)
        self.assertNotIn("VT_RT_SLOT_SBT_INDEX", self.constants)
        self.assertNotIn("VT_RT_SLOT_RESERVED_19", self.constants)

        constants = {
            name: value
            for name, value in self.constants.items()
            if name not in self.tool.DYNAMIC_DEFAULTS
        }
        rendered = self.tool.render_scala(constants, self.function)
        self.assertIn("final val SlotTraceMeta0: Int = 3", rendered)
        self.assertIn("final val SlotTraceMeta1: Int = 4", rendered)
        self.assertNotIn("SlotSbtIndex", rendered)

    def test_scala_generation_exposes_field_major_hit_record_layout(self) -> None:
        constants = {
            name: value
            for name, value in self.constants.items()
            if name not in self.tool.DYNAMIC_DEFAULTS
        }
        rendered = self.tool.render_scala(constants, self.function)
        self.assertIn("final val AbiCandidateHitRecordBaseBytes: Int = 76", rendered)
        self.assertIn("final val AbiCommittedHitRecordBaseBytes: Int = 112", rendered)
        self.assertIn("final val AbiFixedHeaderSizeBytes: Int = 148", rendered)
        self.assertIn("final val AbiPdsFieldMajorFixedHeaderWordCount: Int = 37", rendered)
        self.assertIn("final val HitRecordMeta0: Int = 0", rendered)
        self.assertIn("final val HitRecordMeta1: Int = 1", rendered)
        self.assertIn("final val HitRecordWordCount: Int = 9", rendered)
        self.assertNotIn("HitRecordShaderRecordPtr", rendered)
        self.assertNotIn("HitRecordPrimitiveAddr", rendered)
        self.assertNotIn("HitRecordInstanceAddr", rendered)

    def test_scala_generation_exposes_vtas_layout(self) -> None:
        constants = self.tool.parse_constants(
            self.tool.VTAS_SOURCE.read_text(encoding="utf-8"),
            ("VENTUS_AS_", "VENTUS_BVH_", "VENTUS_BOX4_", "VENTUS_TRIANGLE_",
             "VENTUS_AABB_", "VENTUS_INSTANCE_", "VENTUS_NODE_REF_"),
        )
        rendered = self.tool.render_scala_vtas(constants)
        self.assertIn("object RtVtasLayout", rendered)
        self.assertIn("final val InvalidNodeRef: Long = 0xffffffffL", rendered)
        self.assertIn("final val NodeBox4: Long = 1", rendered)
        self.assertIn("final val Box4ChildRef: Long = 0", rendered)

    def test_checked_in_generated_outputs_are_fresh(self) -> None:
        result = subprocess.run(
            [sys.executable, str(TOOL_PATH), "--check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
