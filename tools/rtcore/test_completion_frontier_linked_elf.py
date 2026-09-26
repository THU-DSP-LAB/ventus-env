#!/usr/bin/env python3
"""Exercise the strict CLI on linked machine-byte fixtures, not mocked dumps.

Usage: python3 tools/rtcore/test_completion_frontier_linked_elf.py \
    --llvm-bin llvm/build/bin
No fixture is executed. Encodings follow Spike encoding.h / processor::ext_set.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--llvm-bin', type=Path, required=True)
    args = parser.parse_args()
    llvm = args.llvm_bin.resolve()
    entry = Path(__file__).resolve().parents[1] / 'rtcore.py'
    # Independent literal bytes: v255 via REGEXT; the marker copies to v69
    # via REGEXTI, whose vs2-high field differs from REGEXT's.
    high = [0x0070200B, 0xE2002F8A, 0x03A0300B, 0x2BF032D7]
    cases = {
        'physical_v255': (high, None),
        'low_v5': ([0xE200228A, 0x2A5032D7], None),
        'wrong_high_bank': (high[:2] + [0x0320300B, high[3]], 'non-whitelisted'),
        'early_store': (high[:2] + [0x00012023] + high[2:], 'non-whitelisted'),
        'join_before_read': (high[:2] + [0x0000205B] + high[2:], 'SIMT control'),
        'branch_bypass': ([0xE200228A, 0x00000463, 0x2A5032D7,
                           0x00012023], 'non-whitelisted'),
        'skip_regext': ([0x0080006F] + high, 'bypasses register-extension'),
    }
    with tempfile.TemporaryDirectory(prefix='ventus-frontier-elf-') as temporary:
        root = Path(temporary)
        for name, (words, error) in cases.items():
            assembly, obj, elf = (root / (name + suffix) for suffix in ('.s', '.o', '.elf'))
            assembly.write_text('.text\n.globl _start\n_start:\n' +
                                ''.join(f'.word 0x{word:08x}\n' for word in words) +
                                'ret\n')
            subprocess.run([str(llvm / 'llvm-mc'), '-triple=riscv32', '-filetype=obj',
                            str(assembly), '-o', str(obj)], check=True)
            subprocess.run([str(llvm / 'ld.lld'), '-m', 'elf32lriscv', str(obj),
                            '-o', str(elf)], check=True)
            result = subprocess.run([sys.executable, str(entry), '--mode', 'strict',
                                     '--objdump', str(llvm / 'llvm-objdump'), str(elf)],
                                    capture_output=True, text=True)
            output = result.stdout + result.stderr
            if error is None:
                assert result.returncode == 0 and 'PASS ' in output, output
                if name == 'physical_v255':
                    assert 'vd=v255 ' in output, output
            else:
                assert result.returncode != 0 and error in output, output
            print(f'PASS linked ELF {name}: ' + ('accepted' if error is None else 'rejected'))


if __name__ == '__main__':
    main()
