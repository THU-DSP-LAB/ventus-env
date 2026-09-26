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
REGEXT/REGEXTI are decoded numerically as one-instruction prefixes. The
matched producer must establish the frontier before SIMT branch/JOIN; these
are rejected rather than approximated as ordinary CFG fall-through.
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


@dataclasses.dataclass(frozen=True)
class Instruction:
    address: int
    word: int
    size: int
    mnemonic: str
    operands: str
    source_line: str
    extension: int = 0
    extension_immediate: bool = False
    prefix_address: int | None = None


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


def is_extension(inst: Instruction) -> bool:
    return inst.size == 4 and (inst.word & 0x707F) in (0x200B, 0x300B)


def _register(inst: Instruction, shift: int, role: int) -> int:
    high = (inst.extension >> (role * 3)) & 7
    if inst.extension_immediate:
        high = ((inst.extension >> 3) & 7) if role == 2 else (
            (inst.extension & 7) if role == 0 else 0)
    return ((inst.word >> shift) & 31) | (high << 5)


def destination_register(inst: Instruction) -> int:
    return _register(inst, 7, 0)


def _signed(value: int, bits: int) -> int:
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _direct_target(inst: Instruction) -> int | None:
    w = inst.word
    opcode, funct3 = w & 127, (w >> 12) & 7
    if opcode in (0x63, 0x5B) and funct3 in (0, 1, 4, 5, 6, 7):
        immediate = ((w >> 31) << 12) | (((w >> 7) & 1) << 11)
        immediate |= (((w >> 25) & 63) << 5) | (((w >> 8) & 15) << 1)
        return inst.address + _signed(immediate, 13)
    if opcode == 0x6F:
        immediate = ((w >> 31) << 20) | (((w >> 12) & 255) << 12)
        immediate |= (((w >> 20) & 1) << 11) | (((w >> 21) & 1023) << 1)
        return inst.address + _signed(immediate, 21)
    return None


def _bind_extensions(instructions: list[Instruction], text: str) -> list[Instruction]:
    # REGEXT/REGEXTI affect exactly the next instruction (Spike ext_set and
    # execute.cc ext_clear; LLVM VentusRegextInsertion). Treat the pair as an
    # indivisible compiler carrier, and reject direct or symbol entry into its
    # second word. Arbitrary indirect mid-instruction entry is outside the
    # matched-toolchain ELF contract, as with any compiler-generated code.
    by_address = {inst.address: inst for inst in instructions}
    for inst in instructions:
        if inst.size != 4:
            raise VerificationError("only the uncompressed RV32 Ventus profile is supported")
        if not is_extension(inst):
            continue
        if inst.word & 0x000F8F80:
            raise VerificationError(f"0x{inst.address:x}: noncanonical register-extension")
        following = by_address.get(inst.address + 4)
        if following is None or is_extension(following):
            raise VerificationError(f"0x{inst.address:x}: orphan or consecutive register-extension")
        by_address[following.address] = dataclasses.replace(
            following, extension=inst.word >> 20,
            extension_immediate=((inst.word >> 12) & 7) == 3,
            prefix_address=inst.address)
    protected = {inst.address for inst in by_address.values() if inst.prefix_address is not None}
    for inst in instructions:
        if _direct_target(inst) in protected:
            raise VerificationError(f"0x{inst.address:x}: control flow bypasses register-extension")
    symbols = {int(m.group(1), 16) for m in re.finditer(
        r"^\s*([0-9a-fA-F]+)\s+<[^>]+>:\s*$", text, re.MULTILINE)}
    if symbols & protected:
        raise VerificationError("symbol entry bypasses register-extension")
    return [by_address[inst.address] for inst in instructions]


def _is_broadcast(inst: Instruction) -> bool:
    # VMV.V.X or VMV.V.I: vs2 is a fixed zero field, not a source register.
    return (inst.word & 0xFFF0707F) in (0x5E004057, 0x5E003057)


def _vector_reads(inst: Instruction) -> set[int]:
    w = inst.word
    opcode, funct3 = w & 127, (w >> 12) & 7
    if opcode == 0x57:
        if _is_broadcast(inst) or funct3 == 7:
            return set()
        reads = {_register(inst, 20, 2)}
        if funct3 in (0, 1, 2):
            reads.add(_register(inst, 15, 1))
        return reads
    if opcode == 0x5B and funct3 in (0, 1, 4, 5, 6, 7):
        return {_register(inst, 20, 2), _register(inst, 15, 1)}
    return set()


def reads_vector_register(inst: Instruction, register: int) -> bool:
    return register in _vector_reads(inst)


def is_strict_frontier(inst: Instruction, traverse_destination: int) -> bool:
    # Numeric identity, independent of objdump's incomplete custom decoder.
    # vm=1, immediate low5=0, and REGEXTI high6=0 are all required. REGEXT
    # high rs1 bits do not extend immediates, whereas REGEXTI does.
    return (
        (inst.word & 0xFE0FF07F) == 0x2A003057
        and _register(inst, 20, 2) == traverse_destination
        and (not inst.extension_immediate or (inst.extension >> 6) == 0)
    )


def _is_prefix_whitelisted(inst: Instruction) -> bool:
    w = inst.word
    opcode, funct3, funct7 = w & 127, (w >> 12) & 7, w >> 25
    if is_extension(inst):
        return True
    if opcode in (0x17, 0x37):  # scalar AUIPC, LUI
        return True
    if opcode == 0x13:
        return (funct7 == 0 if funct3 == 1 else
                funct7 in (0, 0x20) if funct3 == 5 else True)
    if opcode == 0x33:
        return funct7 in (0, 1) or (funct7 == 0x20 and funct3 in (0, 5))
    if opcode == 0x63:
        return funct3 in (0, 1, 4, 5, 6, 7)
    if opcode == 0x6F:  # direct jump, never a call
        return destination_register(inst) == 0
    if opcode == 0x73:  # read-only scalar CSRRS, no CSR write or mask change
        return funct3 == 2 and _register(inst, 15, 1) == 0
    return _is_broadcast(inst)


def _successors(inst: Instruction) -> tuple[int, ...]:
    target = _direct_target(inst)
    if (inst.word & 127) == 0x6F:
        assert target is not None
        return (target,)
    if (inst.word & 127) == 0x63:
        assert target is not None
        return (target, inst.address + inst.size)
    return (inst.address + inst.size,)


def verify_disassembly(
    text: str,
    *,
    strict: bool,
    max_prefix: int = 64,
) -> list[Frontier]:
    instructions = _bind_extensions(parse_disassembly(text), text)
    by_address = {inst.address: inst for inst in instructions}
    traversals = [inst for inst in instructions if is_rt_traverse_v5(inst)]
    if not traversals:
        raise VerificationError("no numeric RT_TRAVERSE_V5 instruction found")

    verified: list[Frontier] = []
    for traversal in traversals:
        destination = destination_register(traversal)
        work = deque([(traversal.address + 4, 0, frozenset())])
        visits = 0
        while work:
            visits += 1
            if visits > 10000:
                raise VerificationError("CFG path budget exceeded before proof completed")
            address, prefix_length, path = work.popleft()
            if address in path:
                raise VerificationError(
                    f"0x{traversal.address:x}: CFG cycle reaches 0x{address:x} before a frontier")
            if prefix_length > max_prefix:
                raise VerificationError(
                    f"0x{traversal.address:x}: no frontier within {max_prefix} instructions")
            inst = by_address.get(address)
            if inst is None:
                raise VerificationError(
                    f"0x{traversal.address:x}: CFG leaves disassembly at 0x{address:x} before a frontier")
            opcode = inst.word & 127
            if opcode == 0x0A:
                raise VerificationError(
                    f"0x{traversal.address:x}: RT operation at 0x{address:x} precedes the frontier")
            if opcode == 0x5B:
                # JOIN may execute a deferred else arm; SETRPC and vector
                # branches mutate the SIMT stack/mask. The producer places the
                # frontier before leaving the issuing block. Never approximate
                # reconvergence by fall-through and silently omit an else arm.
                raise VerificationError(
                    f"0x{traversal.address:x}: SIMT control before frontier at 0x{address:x}")
            is_marker = is_strict_frontier(inst, destination)
            reads = reads_vector_register(inst, destination)
            if is_marker or (reads and not strict):
                verified.append(Frontier(
                    traversal.address, destination, address,
                    "vor.vi" if is_marker else inst.mnemonic, prefix_length))
                continue
            if reads:
                raise VerificationError(
                    f"0x{traversal.address:x}: first v{destination} read at 0x{address:x} "
                    "is not an unmasked zero-immediate completion frontier")
            if opcode == 0x57 and destination_register(inst) == destination:
                raise VerificationError(
                    f"0x{traversal.address:x}: v{destination} overwritten before frontier")
            if not _is_prefix_whitelisted(inst):
                raise VerificationError(
                    f"0x{traversal.address:x}: non-whitelisted instruction before frontier: "
                    f"{inst.source_line.strip()}")
            for successor in _successors(inst):
                work.append((successor, prefix_length + 1, path | {address}))
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
