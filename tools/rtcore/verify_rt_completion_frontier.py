#!/usr/bin/env python3
"""Verify the machine-code completion frontier after RT_TRAVERSE_V5.

Background: ADR-0024 permits completion-independent instructions to execute
after a traversal is issued, but every dynamic path must read the traverse
destination VGPR before it can access RT-published state or issue another RT
operation.  This final-ELF check runs llvm-objdump, finds RT_TRAVERSE_V5 by its
numeric match/mask, reconstructs the local CFG, and audits the prefix on every
reachable path.

Usage:
  python3 tools/rtcore/verify_rt_completion_frontier.py kernel.riscv [...]
  python3 tools/rtcore/verify_rt_completion_frontier.py --mode audit old.riscv

Strict mode requires the compiler-internal frontier marker to lower to
``vor.vi vout, vd, 0``.  The allocator may choose a different output register;
the ordering property comes from reading the traverse destination ``vd``.
Audit mode accepts the first independently decoded read of ``vd`` and is
intended only for inspecting pre-frontier baseline binaries; it is not a
release gate.  Keep the numeric RT encoding constants synchronized with
spike/riscv/encoding.h and keep the prefix whitelist deliberately small.
Register-extension ELFs are rejected until their physical-register state is
modeled; this checker is not a proof of arbitrary SIMT reconvergence behavior.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import shutil
import struct
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Sequence


RT_TRAVERSE_V5_MATCH = 0xE200200A
RT_TRAVERSE_V5_MASK = 0xFE00707F

_INSTRUCTION_RE = re.compile(
    r"^\s*([0-9a-fA-F]+):\s+"
    r"((?:[0-9a-fA-F]{2}(?:\s+|$)){2,8})"
    r"\s*([^\s]+)(?:\s+(.*?))?\s*$"
)
_TARGET_RE = re.compile(r"0x([0-9a-fA-F]+)")
_VECTOR_REGISTER_RE = re.compile(r"(?<![A-Za-z0-9_])v([0-9]|[12][0-9]|3[01])(?![A-Za-z0-9_])")


@dataclasses.dataclass(frozen=True)
class Instruction:
    address: int
    word: int
    size: int
    mnemonic: str
    operands: str
    source_line: str


@dataclasses.dataclass(frozen=True)
class Frontier:
    traverse_address: int
    destination: int
    frontier_address: int
    frontier_mnemonic: str
    prefix_length: int


class VerificationError(RuntimeError):
    pass


def parse_disassembly(text: str) -> list[Instruction]:
    instructions: list[Instruction] = []
    for line in text.splitlines():
        match = _INSTRUCTION_RE.match(line)
        if not match:
            if re.match(r"^\s*[0-9a-fA-F]+:", line):
                raise VerificationError(f"unparsed instruction: {line.strip()}")
            continue
        raw = bytes(int(byte, 16) for byte in match.group(2).split())
        if len(raw) not in (2, 4, 6, 8):
            continue
        instructions.append(
            Instruction(
                address=int(match.group(1), 16),
                word=int.from_bytes(raw[:4], "little"),
                size=len(raw),
                mnemonic=match.group(3).lower(),
                operands=(match.group(4) or "").strip(),
                source_line=line.rstrip(),
            )
        )
    if not instructions:
        raise VerificationError("llvm-objdump output contains no instructions")
    if len({inst.address for inst in instructions}) != len(instructions):
        raise VerificationError("overlapping disassembly addresses")
    return instructions


def is_rt_traverse_v5(inst: Instruction) -> bool:
    return inst.size == 4 and (inst.word & RT_TRAVERSE_V5_MASK) == RT_TRAVERSE_V5_MATCH


def destination_register(inst: Instruction) -> int:
    return (inst.word >> 7) & 0x1F


def _operand_vector_registers(inst: Instruction) -> tuple[list[int], list[int]]:
    operands = [part.strip() for part in inst.operands.split(",") if part.strip()]
    all_registers = [int(match.group(1)) for match in _VECTOR_REGISTER_RE.finditer(inst.operands)]
    destination_registers: list[int] = []
    vector_source_first = inst.mnemonic.startswith(("vb", "vs"))
    if operands and not vector_source_first:
        destination_registers = [
            int(match.group(1))
            for match in _VECTOR_REGISTER_RE.finditer(operands[0])
        ]
    return all_registers, destination_registers


def reads_vector_register(inst: Instruction, register: int) -> bool:
    all_registers, destination_registers = _operand_vector_registers(inst)
    if register in all_registers[len(destination_registers):]:
        return True

    # Ventus vector arithmetic uses the standard vector opcode.  vs2 is always
    # bits 24:20; bits 19:15 are a vector source only for vv forms.  This also
    # recognizes result consumers that an older llvm-objdump prints unknown.
    if inst.size == 4 and (inst.word & 0x7F) == 0x57:
        if ((inst.word >> 20) & 0x1F) == register:
            return True
        funct3 = (inst.word >> 12) & 0x7
        if funct3 in (0, 1, 2) and ((inst.word >> 15) & 0x1F) == register:
            return True
    return False


def is_strict_frontier(inst: Instruction, traverse_destination: int) -> bool:
    if inst.mnemonic != "vor.vi":
        return False
    operands = [part.strip() for part in inst.operands.split(",")]
    if len(operands) != 3:
        return False
    return (
        inst.size == 4
        and (inst.word & 0xFE0FF07F) == 0x2A003057
        and ((inst.word >> 20) & 0x1F) == traverse_destination
        and _VECTOR_REGISTER_RE.fullmatch(operands[0]) is not None
        and operands[1] == f"v{traverse_destination}"
        and operands[2] in ("0", "0x0")
    )


_SCALAR_PREFIX_MNEMONICS = {
    "add", "addi", "addiw", "and", "andi", "auipc", "li", "lui", "mv",
    "or", "ori", "sext.w", "sll", "slli", "srl", "srli", "sub", "xor",
    "xori", "zext.w",
}
_CONDITIONAL_BRANCHES = {
    "beq", "bge", "bgeu", "blt", "bltu", "bne",
    "vbeq", "vbge", "vbgeu", "vblt", "vbltu", "vbne",
}


def _is_prefix_whitelisted(inst: Instruction) -> bool:
    mnemonic = inst.mnemonic
    if mnemonic in {"join", "j", "nop", "vmv.v.x"}:
        return True
    if mnemonic in _CONDITIONAL_BRANCHES:
        return True
    if mnemonic in _SCALAR_PREFIX_MNEMONICS:
        return not _VECTOR_REGISTER_RE.search(inst.operands)
    if mnemonic in {"csrr", "csrr.v"}:
        return True
    return False


def _branch_target(inst: Instruction) -> int | None:
    match = _TARGET_RE.search(inst.operands)
    if not match:
        raise VerificationError(f"unresolved branch target at 0x{inst.address:x}")
    return int(match.group(1), 16)


def _successors(
    inst: Instruction,
    next_address: int | None,
) -> tuple[int, ...]:
    if inst.mnemonic == "j":
        target = _branch_target(inst)
        return (target,) if target is not None else ()
    if inst.mnemonic in _CONDITIONAL_BRANCHES:
        target = _branch_target(inst)
        return tuple(address for address in (target, next_address) if address is not None)
    if inst.mnemonic in {"ret", "jr", "jalr", "ecall", "ebreak", "unimp"}:
        return ()
    return (next_address,) if next_address is not None else ()


def verify_disassembly(
    text: str,
    *,
    strict: bool,
    max_prefix: int = 64,
) -> list[Frontier]:
    instructions = parse_disassembly(text)
    by_address = {inst.address: inst for inst in instructions}
    next_by_address = {
        inst.address: inst.address + inst.size
        for inst in instructions
    }
    traversals = [inst for inst in instructions if is_rt_traverse_v5(inst)]
    if not traversals:
        raise VerificationError("no numeric RT_TRAVERSE_V5 instruction found")
    # Register-extension state is not yet modeled. Never interpret its low
    # five register bits as a proof about a different physical VGPR. This
    # conservative limitation is a release-gate gap, not an audit bypass.
    if any(inst.mnemonic in {"regext", "regexti"} for inst in instructions):
        raise VerificationError("register-extension ELF is not yet supported")

    verified: list[Frontier] = []
    for traversal in traversals:
        destination = destination_register(traversal)
        first = next_by_address[traversal.address]
        if first is None:
            raise VerificationError(
                f"0x{traversal.address:x}: traversal has no successor instruction"
            )
        work = deque([(first, 0, frozenset())])
        path_frontiers: list[Frontier] = []
        visits = 0
        while work:
            visits += 1
            if visits > 10000:
                raise VerificationError("CFG path budget exceeded before proof completed")
            address, prefix_length, path = work.popleft()
            if address in path:
                raise VerificationError(
                    f"0x{traversal.address:x}: CFG cycle reaches 0x{address:x} before a frontier"
                )
            if prefix_length > max_prefix:
                raise VerificationError(
                    f"0x{traversal.address:x}: no frontier within {max_prefix} instructions"
                )
            inst = by_address.get(address)
            if inst is None:
                raise VerificationError(
                    f"0x{traversal.address:x}: CFG leaves disassembly at 0x{address:x} before a frontier"
                )
            if is_rt_traverse_v5(inst) or inst.mnemonic.startswith("vt.rt."):
                raise VerificationError(
                    f"0x{traversal.address:x}: RT operation at 0x{address:x} precedes the frontier"
                )
            if reads_vector_register(inst, destination):
                if strict and not is_strict_frontier(inst, destination):
                    raise VerificationError(
                        f"0x{traversal.address:x}: first v{destination} read at 0x{address:x} "
                        f"is not 'vor.vi vout, v{destination}, 0'"
                    )
                path_frontiers.append(
                    Frontier(
                        traverse_address=traversal.address,
                        destination=destination,
                        frontier_address=address,
                        frontier_mnemonic=inst.mnemonic,
                        prefix_length=prefix_length,
                    )
                )
                continue
            _, written = _operand_vector_registers(inst)
            if destination in written:
                raise VerificationError(
                    f"0x{traversal.address:x}: v{destination} overwritten before frontier"
                )
            if not _is_prefix_whitelisted(inst):
                raise VerificationError(
                    f"0x{traversal.address:x}: non-whitelisted instruction before frontier: "
                    f"{inst.source_line.strip()}"
                )
            successors = _successors(inst, next_by_address[address])
            if not successors:
                raise VerificationError(
                    f"0x{traversal.address:x}: path terminates at 0x{address:x} before a frontier"
                )
            next_path = path | {address}
            for successor in successors:
                work.append((successor, prefix_length + 1, next_path))
        if not path_frontiers:
            raise VerificationError(f"0x{traversal.address:x}: no reachable frontier")
        verified.extend(path_frontiers)
    return verified


def _run_objdump(objdump: str, elf: Path) -> str:
    command = [objdump, "-d", "--mattr=+v,+zfinx", str(elf)]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise VerificationError(f"{' '.join(command)} failed: {detail}")
    return result.stdout


def verify_elf_header(elf: Path) -> None:
    with elf.open("rb") as stream:
        header = stream.read(52)
    if len(header) != 52 or header[:7] != b"\x7fELF\x01\x01\x01":
        raise VerificationError("expected a little-endian ELF32 final image")
    elf_type, machine = struct.unpack_from("<HH", header, 16)
    if elf_type not in (2, 3) or machine != 243:
        raise VerificationError("expected linked RISC-V ET_EXEC/ET_DYN, not an object file")


def _find_objdump(explicit: str | None) -> str:
    if explicit:
        return explicit
    environment = os.environ.get("VENTUS_OBJDUMP")
    if environment:
        return environment
    found = shutil.which("llvm-objdump")
    if found:
        return found
    raise VerificationError("llvm-objdump not found; pass --objdump or set VENTUS_OBJDUMP")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", nargs="+", type=Path, help="final Ventus ELF(s) to verify")
    parser.add_argument("--objdump", help="path to the Ventus llvm-objdump")
    parser.add_argument(
        "--mode",
        choices=("strict", "audit"),
        default="strict",
        help="strict is the release gate; audit accepts legacy natural consumers",
    )
    parser.add_argument("--max-prefix", type=int, default=64)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        objdump = _find_objdump(args.objdump)
        for elf in args.elf:
            if not elf.is_file():
                raise VerificationError(f"ELF does not exist: {elf}")
            verify_elf_header(elf)
            frontiers = verify_disassembly(
                _run_objdump(objdump, elf),
                strict=args.mode == "strict",
                max_prefix=args.max_prefix,
            )
            for frontier in frontiers:
                print(
                    f"{'PASS' if args.mode == 'strict' else 'AUDIT'} {elf}: "
                    f"traverse=0x{frontier.traverse_address:x} "
                    f"vd=v{frontier.destination} frontier=0x{frontier.frontier_address:x} "
                    f"({frontier.frontier_mnemonic}) prefix={frontier.prefix_length}"
                )
    except (VerificationError, OSError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
