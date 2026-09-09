#!/usr/bin/env python3
"""Generate and exhaustively verify the RTCore FP32 reciprocal coefficients.

Background:
  RTCore uses a seven-stage, 128-interval quadratic reciprocal pipeline.  The
  structure follows the public THU-DSP-LAB/sfu interpolation description, but
  this tool independently derives the coefficients with a minimax linear
  program and a discrete search against the implemented fixed-point datapath.

Flow:
  1. Solve one quadratic minimax problem for each normalized mantissa interval.
  2. Quantize and locally optimize c0/c1/c2 for the hardware fixed-point format.
  3. Exhaustively compare all 2^23 normalized mantissas and exponent edges.
  4. Generate the Scala and C++ constant tables, or check existing tables.

Usage:
  python3 tools/rtcore-rcp-coeffs.py --write
  python3 tools/rtcore-rcp-coeffs.py --check

Maintenance:
  NumPy and SciPy are generation-time dependencies only.  The normal build has
  no Python dependency.  Keep the fixed-point equations here synchronized with
  RtFp32Reciprocal.scala and ventus_rt.h; --check verifies generated text and
  the complete mantissa accuracy envelope.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

import numpy as np
from scipy.optimize import linprog


INTERVALS = 128
MANTISSA_COUNT = 1 << 23
GRID_POINTS = 4097


def _hardware_poly(coefficients: tuple[int, int, int], xl: np.ndarray) -> np.ndarray:
    c0, c1, c2 = coefficients
    xl2 = (xl * xl) >> 17
    # Python and NumPy use an arithmetic right shift for negative signed
    # integers, matching the SInt datapath's floor-toward-negative-infinity.
    return c0 + ((-c1 * xl) >> 13) + ((c2 * xl2) >> 15)


def _normal_reference(interval: int, xl: np.ndarray) -> np.ndarray:
    mantissa = (np.int64(interval) << 16) | xl
    input_bits = (np.uint32(0x3F800000) | mantissa.astype(np.uint32))
    return (np.float32(1.0) / input_bits.view(np.float32)).view(np.uint32).astype(np.int64)


def _score(coefficients: tuple[int, int, int], xl: np.ndarray,
           reference: np.ndarray) -> tuple[int, int, int]:
    poly = _hardware_poly(coefficients, xl)
    result = np.int64(0x3F000000) | ((poly >> 2) & 0x7FFFFF)
    error = np.abs(result - reference)
    return int(error.max()), int(np.count_nonzero(error > 1)), int(error.sum())


def _continuous_coefficients(interval: int) -> tuple[int, int, int]:
    t = np.linspace(0.0, 1.0, GRID_POINTS)
    function = 2.0 / (1.0 + (interval + t) / INTERVALS)
    basis = np.column_stack((np.ones_like(t), t, t * t))
    ones = np.ones_like(t)
    constraints = np.vstack((
        np.column_stack((basis, -ones)),
        np.column_stack((-basis, -ones)),
    ))
    solved = linprog(
        [0.0, 0.0, 0.0, 1.0],
        A_ub=constraints,
        b_ub=np.concatenate((function, -function)),
        bounds=[(None, None), (None, None), (None, None), (0.0, None)],
        method="highs",
    )
    if not solved.success:
        raise RuntimeError(f"interval {interval}: minimax solve failed: {solved.message}")

    a, b, c = solved.x[:3]
    c0 = int(np.rint(a * (1 << 25)))
    c1 = int(np.rint(-b * INTERVALS * (1 << 15)))
    c2 = int(np.rint(c * INTERVALS * INTERVALS * (1 << 11)))
    return (
        min((1 << 26) - 1, max(0, c0)),
        min((1 << 16) - 1, max(0, c1)),
        min((1 << 12) - 1, max(0, c2)),
    )


def generate_coefficients() -> list[tuple[int, int, int]]:
    xl = np.arange(1 << 16, dtype=np.int64)
    coefficients: list[tuple[int, int, int]] = []
    limits = ((1 << 26) - 1, (1 << 16) - 1, (1 << 12) - 1)

    for interval in range(INTERVALS):
        current = list(_continuous_coefficients(interval))
        reference = _normal_reference(interval, xl)
        best = _score(tuple(current), xl, reference)

        for iteration in range(8):
            changed = False
            radius = 64 if iteration == 0 else 16
            for axis, high in enumerate(limits):
                base = current[axis]
                local_score = best
                local_value = base
                for value in range(max(0, base - radius), min(high, base + radius) + 1):
                    candidate = current.copy()
                    candidate[axis] = value
                    candidate_score = _score(tuple(candidate), xl, reference)
                    if candidate_score < local_score:
                        local_score = candidate_score
                        local_value = value
                if local_value != base:
                    current[axis] = local_value
                    best = local_score
                    changed = True
            if not changed:
                break

        if best[0] > 1:
            raise RuntimeError(
                f"interval {interval}: quantized polynomial exceeds 1 ULP: {best}"
            )
        coefficients.append(tuple(current))

    return coefficients


def _coefficient_arrays(coefficients: Iterable[tuple[int, int, int]]) -> tuple[np.ndarray, ...]:
    rows = list(coefficients)
    return tuple(np.array([row[column] for row in rows], dtype=np.int64)
                 for column in range(3))


def _poly_for_mantissa(coefficients: list[tuple[int, int, int]],
                       mantissa: np.ndarray) -> np.ndarray:
    c0, c1, c2 = _coefficient_arrays(coefficients)
    index = mantissa >> 16
    xl = mantissa & 0xFFFF
    xl2 = (xl * xl) >> 17
    return c0[index] + ((-c1[index] * xl) >> 13) + ((c2[index] * xl2) >> 15)


def _round_to_nearest_even_shift(value: np.ndarray, shift: int) -> np.ndarray:
    base = value >> shift
    remainder = value & ((1 << shift) - 1)
    half = 1 << (shift - 1)
    increment = (remainder > half) | ((remainder == half) & ((base & 1) != 0))
    return base + increment


def _compose(coefficients: list[tuple[int, int, int]], mantissa: np.ndarray,
             unbiased_exponent: int) -> np.ndarray:
    poly = _poly_for_mantissa(coefficients, mantissa)
    biased_exponent = 126 - unbiased_exponent
    if biased_exponent >= 255:
        return np.full_like(mantissa, 0x7F7FFFFF)
    if biased_exponent > 0:
        return (np.int64(biased_exponent) << 23) | ((poly >> 2) & 0x7FFFFF)
    fraction = _round_to_nearest_even_shift(poly, 3 - biased_exponent)
    return np.minimum(fraction, np.int64(0x00800000))


def verify_accuracy(coefficients: list[tuple[int, int, int]]) -> dict[str, tuple[int, int, int]]:
    results: dict[str, tuple[int, int, int]] = {}
    chunk = 1 << 20

    for raw_exponent, unbiased_exponent, name in (
        (127, 0, "normalized"),
        (253, 126, "subnormal-output-e126"),
        (254, 127, "subnormal-output-e127"),
    ):
        exact = 0
        one_ulp = 0
        maximum = 0
        for low in range(0, MANTISSA_COUNT, chunk):
            mantissa = np.arange(low, min(low + chunk, MANTISSA_COUNT), dtype=np.int64)
            actual = _compose(coefficients, mantissa, unbiased_exponent)
            input_bits = (np.int64(raw_exponent) << 23) | mantissa
            reference = (np.float32(1.0) / input_bits.astype(np.uint32).view(np.float32))
            reference_bits = reference.view(np.uint32).astype(np.int64)
            error = np.abs(actual - reference_bits)
            maximum = max(maximum, int(error.max()))
            exact += int(np.count_nonzero(error == 0))
            one_ulp += int(np.count_nonzero(error == 1))
        if maximum > 1:
            raise RuntimeError(f"{name}: maximum error is {maximum} ULP")
        results[name] = (maximum, exact, one_ulp)

    exact = 0
    one_ulp = 0
    maximum = 0
    for low in range(1, MANTISSA_COUNT, chunk):
        fraction = np.arange(low, min(low + chunk, MANTISSA_COUNT), dtype=np.int64)
        top_bit = np.floor(np.log2(fraction)).astype(np.int64)
        shift = 23 - top_bit
        exponent = top_bit - 149
        mantissa = ((fraction << shift) & 0xFFFFFF) & 0x7FFFFF
        actual = np.empty_like(fraction)
        for value in np.unique(exponent):
            selected = exponent == value
            actual[selected] = _compose(coefficients, mantissa[selected], int(value))
        with np.errstate(over="ignore", divide="ignore"):
            reference = (np.float32(1.0) / fraction.astype(np.uint32).view(np.float32))
        reference_bits = reference.view(np.uint32).astype(np.int64)
        reference_bits = np.where(reference_bits == 0x7F800000, 0x7F7FFFFF,
                                  reference_bits)
        error = np.abs(actual - reference_bits)
        maximum = max(maximum, int(error.max()))
        exact += int(np.count_nonzero(error == 0))
        one_ulp += int(np.count_nonzero(error == 1))
    if maximum > 1:
        raise RuntimeError(f"subnormal-input: maximum error is {maximum} ULP")
    results["subnormal-input"] = (maximum, exact, one_ulp)
    return results


def render_scala(coefficients: list[tuple[int, int, int]]) -> str:
    rows = "\n".join(
        f'    (BigInt("{c0:07x}", 16), BigInt("{c1:04x}", 16), BigInt("{c2:03x}", 16)){"," if index + 1 < len(coefficients) else ""}'
        for index, (c0, c1, c2) in enumerate(coefficients)
    )
    return f"""/*
 * Copyright (c) 2026 International Innovation Center of Tsinghua University, Shanghai
 * Ventus is licensed under Mulan PSL v2.
 * Generated by tools/rtcore-rcp-coeffs.py; do not edit by hand.
 */
package rtcore

private[rtcore] object RtRcpCoefficients {{
  val values: Seq[(BigInt, BigInt, BigInt)] = Seq(
{rows}
  )
}}
"""


def render_cpp(coefficients: list[tuple[int, int, int]]) -> str:
    rows = "\n".join(
        f"    {{0x{c0:07x}u, 0x{c1:04x}u, 0x{c2:03x}u}}{',' if index + 1 < len(coefficients) else ''}"
        for index, (c0, c1, c2) in enumerate(coefficients)
    )
    return f"""/* Generated by tools/rtcore-rcp-coeffs.py; do not edit by hand. */
#ifndef RISCV_VENTUS_RT_RCP_COEFFS_H
#define RISCV_VENTUS_RT_RCP_COEFFS_H

#include <cstdint>

namespace ventus_rt {{

struct VtasV2RcpCoefficient {{
  uint32_t c0;
  uint16_t c1;
  uint16_t c2;
}};

constexpr VtasV2RcpCoefficient vtas_v2_rcp_coefficients[128] = {{
{rows}
}};

}} // namespace ventus_rt

#endif
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true")
    action.add_argument("--check", action="store_true")
    args = parser.parse_args()

    coefficients = generate_coefficients()
    accuracy = verify_accuracy(coefficients)
    env_root = Path(__file__).resolve().parents[1]
    outputs = {
        env_root / "gpgpu/ventus/src/rtcore/RtRcpCoefficients.scala":
            render_scala(coefficients),
        env_root / "spike/riscv/ventus_rt_rcp_coeffs.h":
            render_cpp(coefficients),
    }

    if args.write:
        for path, content in outputs.items():
            path.write_text(content, encoding="utf-8")
    else:
        stale = [str(path) for path, content in outputs.items()
                 if not path.exists() or path.read_text(encoding="utf-8") != content]
        if stale:
            raise SystemExit("stale generated RCP table(s): " + ", ".join(stale))

    for name, (maximum, exact, one_ulp) in accuracy.items():
        print(f"{name}: max_ulp={maximum} exact={exact} one_ulp={one_ulp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
