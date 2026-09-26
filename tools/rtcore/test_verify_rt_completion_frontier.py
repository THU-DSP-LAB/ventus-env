#!/usr/bin/env python3
"""Focused regression tests for verify_rt_completion_frontier.py."""

from __future__ import annotations

import importlib.util
import sys
import struct
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("verify_rt_completion_frontier.py")
SPEC = importlib.util.spec_from_file_location("verify_rt_completion_frontier", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECKER
SPEC.loader.exec_module(CHECKER)


def instruction(address: int, word: int, assembly: str) -> str:
    encoded = " ".join(f"{byte:02x}" for byte in word.to_bytes(4, "little"))
    return f"{address:08x}: {encoded}  \t{assembly}"


def traverse(destination: int) -> int:
    return (CHECKER.RT_TRAVERSE_V5_MATCH & ~(0x1F << 7)) | destination << 7


class CompletionFrontierTest(unittest.TestCase):
    def test_strict_frontier_follows_control_prefix(self) -> None:
        text = "\n".join(
            (
                instruction(0x100, traverse(5), "<unknown>"),
                instruction(0x104, 0x0000006F, "j 0x10c"),
                instruction(0x108, 0x00000013, "addi a0, a0, 1"),
                instruction(0x10C, 0x2A5032D7, "vor.vi v5, v5, 0"),
            )
        )
        result = CHECKER.verify_disassembly(text, strict=True)
        self.assertEqual(result[0].frontier_address, 0x10C)
        self.assertEqual(result[0].prefix_length, 1)

    def test_strict_frontier_allows_allocator_copy_destination(self) -> None:
        frontier = (0x2A5032D7 & ~(0x1F << 7)) | 3 << 7
        text = "\n".join(
            (
                instruction(0x100, traverse(5), "vt.rt.traverse v5, v0, v2"),
                instruction(0x104, frontier, "vor.vi v3, v5, 0"),
            )
        )
        result = CHECKER.verify_disassembly(text, strict=True)
        self.assertEqual(result[0].frontier_address, 0x104)

    def test_audit_accepts_legacy_natural_consumer(self) -> None:
        text = "\n".join(
            (
                instruction(0x100, traverse(8), "vt.rt.traverse v8, v3, v0"),
                instruction(0x104, 0x0000205B, "join zero, zero, 0"),
                instruction(0x108, 0x268FB457, "vand.vi v8, v8, 255"),
            )
        )
        result = CHECKER.verify_disassembly(text, strict=False)
        self.assertEqual(result[0].frontier_mnemonic, "vand.vi")
        with self.assertRaises(CHECKER.VerificationError):
            CHECKER.verify_disassembly(text, strict=True)

    def test_rejects_memory_access_before_frontier(self) -> None:
        text = "\n".join(
            (
                instruction(0x100, traverse(5), "vt.rt.traverse v5, v0, v2"),
                instruction(0x104, 0x00012087, "lw ra, 0(sp)"),
                instruction(0x108, 0x2A5032D7, "vor.vi v5, v5, 0"),
            )
        )
        with self.assertRaisesRegex(CHECKER.VerificationError, "non-whitelisted"):
            CHECKER.verify_disassembly(text, strict=True)

    def test_vector_branch_is_a_real_destination_read(self) -> None:
        text = "\n".join(
            (
                instruction(0x100, traverse(5), "vt.rt.traverse v5, v0, v2"),
                instruction(0x104, 0x0000005B, "vbne v5, v1, 0x10c"),
                instruction(0x108, 0x2A5032D7, "vor.vi v5, v5, 0"),
                instruction(0x10C, 0x2A5032D7, "vor.vi v5, v5, 0"),
            )
        )
        with self.assertRaisesRegex(CHECKER.VerificationError, "first v5 read"):
            CHECKER.verify_disassembly(text, strict=True)

    def check_rejected_prefix(self, middle: list[str], message: str) -> None:
        text = "\n".join([
            instruction(0x100, traverse(5), "<unknown>"), *middle,
            instruction(0x110, 0x2A5032D7, "vor.vi v5, v5, 0"),
        ])
        with self.assertRaisesRegex(CHECKER.VerificationError, message):
            CHECKER.verify_disassembly(text, strict=True)

    def test_rejects_stack_publication(self) -> None:
        self.check_rejected_prefix(
            [instruction(0x104, 0x00A12023, "sw a0, 0(sp)")], "non-whitelisted")

    def test_rejects_clobbered_result(self) -> None:
        self.check_rejected_prefix(
            [instruction(0x104, 0x5E0542D7, "vmv.v.x v5, a0")], "overwritten")

    def test_rejects_unresolved_branch(self) -> None:
        self.check_rejected_prefix(
            [instruction(0x104, 0x00000063, "beq a0, a1, unknown")], "unresolved")

    def test_checks_both_branch_paths(self) -> None:
        self.check_rejected_prefix([
            instruction(0x104, 0x00000063, "beq a0, a1, 0x110"),
            instruction(0x108, 0x00052503, "lw a0, 0(a0)"),
        ], "non-whitelisted")

    def test_rejects_cycle(self) -> None:
        self.check_rejected_prefix(
            [instruction(0x104, 0x0000006F, "j 0x104")], "CFG cycle")

    def test_rejects_disassembly_gap(self) -> None:
        self.check_rejected_prefix([], "leaves disassembly")

    def test_rejects_second_traversal(self) -> None:
        self.check_rejected_prefix(
            [instruction(0x104, traverse(6), "<unknown>")], "RT operation")

    def test_rejects_unmodeled_register_extension(self) -> None:
        self.check_rejected_prefix(
            [instruction(0x104, 0x0000000B, "regext 1")], "register-extension")

    def test_marker_must_match_machine_encoding(self) -> None:
        text = "\n".join([
            instruction(0x100, traverse(5), "<unknown>"),
            instruction(0x104, 0x2852B2D7, "vor.vi v5, v5, 0"),
        ])
        with self.assertRaisesRegex(CHECKER.VerificationError, "first v5 read"):
            CHECKER.verify_disassembly(text, strict=True)

    def test_no_traverse_fails_closed(self) -> None:
        with self.assertRaisesRegex(CHECKER.VerificationError, "no numeric"):
            CHECKER.verify_disassembly(instruction(0x100, 0x13, "nop"), strict=True)

    def test_unparsed_instruction_fails_closed(self) -> None:
        with self.assertRaisesRegex(CHECKER.VerificationError, "unparsed"):
            CHECKER.verify_disassembly("100: e200228a unknown", strict=True)

    def test_relocatable_object_is_not_final_elf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            elf = Path(directory) / "input.o"
            header = bytearray(52)
            header[:7] = b"\x7fELF\x01\x01\x01"
            struct.pack_into("<HH", header, 16, 1, 243)
            elf.write_bytes(header)
            with self.assertRaisesRegex(CHECKER.VerificationError, "not an object"):
                CHECKER.verify_elf_header(elf)

    def test_masked_marker_is_not_a_full_frontier(self) -> None:
        text = "\n".join([
            instruction(0x100, traverse(5), "<unknown>"),
            instruction(0x104, 0x285032D7, "vor.vi v5, v5, 0, v0.t"),
        ])
        with self.assertRaisesRegex(CHECKER.VerificationError, "first v5 read"):
            CHECKER.verify_disassembly(text, strict=True)


if __name__ == "__main__":
    unittest.main()
