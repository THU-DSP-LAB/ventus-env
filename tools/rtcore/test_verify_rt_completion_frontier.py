#!/usr/bin/env python3
"""Machine-byte regressions for the final-ELF completion frontier gate."""
from __future__ import annotations

import importlib.util
import struct
import sys
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    'verify_rt_completion_frontier', Path(__file__).with_name('verify_rt_completion_frontier.py'))
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECKER
SPEC.loader.exec_module(CHECKER)


def instruction(address: int, word: int, assembly: str = '<unknown>') -> str:
    encoded = ' '.join(f'{byte:02x}' for byte in word.to_bytes(4, 'little'))
    return f'{address:08x}: {encoded}  \t{assembly}'


def traverse(destination: int = 5) -> int:
    return CHECKER.RT_TRAVERSE_V5_MATCH | (destination & 31) << 7


def marker(source: int = 5, destination: int = 5) -> int:
    return 0x2A003057 | (source & 31) << 20 | (destination & 31) << 7


def regext(rd: int = 0, rs1: int = 0, rs2: int = 0) -> int:
    return ((rd | rs1 << 3 | rs2 << 6) << 20) | 0x200B


def regexti(rd: int = 0, rs2: int = 0, immediate: int = 0) -> int:
    return ((rd | rs2 << 3 | immediate << 6) << 20) | 0x300B


def jump(offset: int) -> int:
    offset &= 0x1FFFFF
    return 0x6F | ((offset >> 20) << 31) | (((offset >> 1) & 1023) << 21) | \
        (((offset >> 11) & 1) << 20) | (((offset >> 12) & 255) << 12)


def branch(offset: int) -> int:
    offset &= 0x1FFF
    return 0x00050063 | ((offset >> 12) << 31) | (((offset >> 5) & 63) << 25) | \
        (((offset >> 1) & 15) << 8) | (((offset >> 11) & 1) << 7)


def disassembly(words: list[int]) -> str:
    return '\n'.join(instruction(0x100 + i * 4, word) for i, word in enumerate(words))


class CompletionFrontierTest(unittest.TestCase):
    def verify(self, words: list[int], strict: bool = True):
        return CHECKER.verify_disassembly(disassembly(words), strict=strict)

    def reject(self, words: list[int], message: str):
        with self.assertRaisesRegex(CHECKER.VerificationError, message):
            self.verify(words)

    def test_numeric_marker_even_when_objdump_cannot_decode(self):
        self.assertEqual(self.verify([traverse(), marker()])[0].frontier_address, 0x104)

    def test_allocator_can_copy_to_another_destination(self):
        self.assertEqual(self.verify([traverse(), marker(destination=3)])[0].destination, 5)

    def test_high_destination_and_source_regext(self):
        result = self.verify([regext(rd=3), traverse(), regext(rs2=3), marker()])
        self.assertEqual(result[0].destination, 101)

    def test_high_destination_and_source_regexti(self):
        result = self.verify([regext(rd=7), traverse(31), regexti(rd=2, rs2=7), marker(31)])
        self.assertEqual(result[0].destination, 255)

    def test_extended_zero_marker_is_not_nonzero_immediate(self):
        self.reject([traverse(), regexti(immediate=1), marker()], 'first v5 read')

    def test_regext_rs1_does_not_extend_vi_immediate(self):
        self.verify([traverse(), regext(rs1=7), marker()])

    def test_same_low_bits_are_not_the_same_physical_source(self):
        self.reject([regext(rd=1), traverse(), marker()], 'non-whitelisted')

    def test_extension_applies_to_only_one_instruction(self):
        self.verify([regext(rd=1), 0x13, traverse(), marker()])

    def test_regexti_rs2_layout_differs_from_regext(self):
        self.reject([regext(rd=1), traverse(), regext(rs1=1), marker()], 'non-whitelisted')

    def test_unused_regext_elsewhere_does_not_reject_elf(self):
        self.verify([traverse(), marker(), regext(rd=1), 0x5E0542D7])

    def test_orphan_regext_rejected(self):
        self.reject([traverse(), marker(), regext()], 'orphan')

    def test_consecutive_regext_rejected(self):
        self.reject([traverse(), regext(), regexti(), marker()], 'consecutive')

    def test_noncanonical_regext_rejected(self):
        self.reject([traverse(), regext() | 0x80, marker()], 'noncanonical')

    def test_jump_cannot_bypass_extension_before_traverse(self):
        self.reject([jump(8), regext(rd=1), traverse(), regext(rs2=1), marker()], 'bypasses')

    def test_jump_cannot_bypass_extension_before_marker(self):
        self.reject([traverse(), jump(8), regext(rs2=1), marker()], 'bypasses')

    def test_symbol_cannot_enter_second_word_of_pair(self):
        text = disassembly([regext(rd=1), traverse(), regext(rs2=1), marker()])
        text = text.replace('00000104:', '00000104 <entry>:\n00000104:')
        with self.assertRaisesRegex(CHECKER.VerificationError, 'symbol entry'):
            CHECKER.verify_disassembly(text, strict=True)

    def test_scalar_jump_prefix(self):
        result = self.verify([traverse(), jump(8), 0x13, marker()])
        self.assertEqual(result[0].frontier_address, 0x10C)

    def test_conditional_branch_checks_both_paths(self):
        self.reject([traverse(), branch(8), 0x00052503, marker()], 'non-whitelisted')

    def test_both_branch_paths_prove_frontier(self):
        result = self.verify([traverse(), branch(8), marker(), marker()])
        self.assertEqual(len(result), 2)

    def test_numeric_branch_target_ignores_misleading_text(self):
        text = '\n'.join([instruction(0x100, traverse()),
                          instruction(0x104, branch(8), 'beq a0, zero, 0x108'),
                          instruction(0x108, marker()),
                          instruction(0x10C, 0x00A12023, 'nop')])
        with self.assertRaisesRegex(CHECKER.VerificationError, 'non-whitelisted'):
            CHECKER.verify_disassembly(text, strict=True)

    def test_cycle_before_frontier(self):
        self.reject([traverse(), jump(0), marker()], 'CFG cycle')

    def test_exit_from_available_code(self):
        self.reject([traverse()], 'leaves disassembly')

    def test_indirect_jump_and_call_rejected(self):
        for transfer in [0x8067, 0x80E7, jump(4) | 0x80]:
            with self.subTest(transfer=transfer):
                self.reject([traverse(), transfer, marker()], 'non-whitelisted')

    def test_memory_and_publication_before_frontier(self):
        for memory in [0x00052503, 0x00A12023, 0x00052087, 0x000520A7, 0x0005202F]:
            with self.subTest(memory=memory):
                self.reject([traverse(), memory, marker()], 'non-whitelisted')

    def test_clobber_of_high_destination(self):
        self.reject([regext(rd=1), traverse(), regext(rd=1), 0x5E0542D7,
                     regext(rs2=1), marker()], 'overwritten')

    def test_write_to_different_high_bank_is_independent(self):
        self.verify([traverse(), regext(rd=1), 0x5E0542D7, marker()])

    def test_csr_write_cannot_change_simt_mask(self):
        self.reject([traverse(), 0x80051073, marker()], 'non-whitelisted')

    def test_read_only_csr_and_scalar_prefix_allowed(self):
        self.verify([traverse(), 0x800022F3, 0x00128293, marker()])

    def test_extended_scalar_zero_fields_are_not_zero_registers(self):
        self.reject([traverse(), regext(rd=1), jump(4), marker()], 'non-whitelisted')
        self.reject([traverse(), regext(rs1=1), 0x800022F3, marker()], 'non-whitelisted')

    def test_reserved_scalar_shift_is_not_whitelisted(self):
        self.reject([traverse(), 0x40001013, marker()], 'non-whitelisted')

    def test_join_cannot_hide_deferred_else_path(self):
        self.reject([traverse(), 0x205B, marker()], 'SIMT control')

    def test_vector_branch_and_setrpc_before_frontier_rejected(self):
        for inst in [0x0050905B, 0x305B]:
            self.reject([traverse(), inst, marker()], 'SIMT control')

    def test_masked_marker_rejected(self):
        self.reject([traverse(), marker() & ~(1 << 25)], 'first v5 read')

    def test_nonzero_marker_rejected(self):
        self.reject([traverse(), marker() | 1 << 15], 'first v5 read')

    def test_second_rt_operation_rejected(self):
        for inst in [traverse(6), 0xE400200A]:
            self.reject([traverse(), inst, marker()], 'RT operation')

    def test_audit_accepts_natural_consumer_but_strict_rejects(self):
        natural = 0x26003057 | 5 << 20 | 31 << 15 | 5 << 7
        self.verify([traverse(), natural], strict=False)
        self.reject([traverse(), natural], 'first v5 read')

    def test_prefix_length_is_bounded(self):
        with self.assertRaisesRegex(CHECKER.VerificationError, 'no frontier within'):
            CHECKER.verify_disassembly(disassembly([traverse(), 0x13, marker()]),
                                       strict=True, max_prefix=0)

    def test_no_traverse_rejected(self):
        self.reject([0x13], 'no numeric')

    def test_unparsed_instruction_rejected(self):
        with self.assertRaisesRegex(CHECKER.VerificationError, 'unparsed'):
            CHECKER.verify_disassembly('100: e200228a unknown', strict=True)

    def test_duplicate_address_rejected(self):
        text = disassembly([traverse(), marker()]) + '\n' + instruction(0x104, marker())
        with self.assertRaisesRegex(CHECKER.VerificationError, 'overlapping'):
            CHECKER.verify_disassembly(text, strict=True)

    def test_relocatable_object_is_not_final_elf(self):
        with tempfile.TemporaryDirectory() as directory:
            elf = Path(directory) / 'input.o'
            header = bytearray(52)
            header[:7] = b'\x7fELF\x01\x01\x01'
            struct.pack_into('<HH', header, 16, 1, 243)
            elf.write_bytes(header)
            with self.assertRaisesRegex(CHECKER.VerificationError, 'not an object'):
                CHECKER.verify_elf_header(elf)


if __name__ == '__main__':
    unittest.main()
