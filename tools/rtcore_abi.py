#!/usr/bin/env python3
"""Generate RT ABI constants from Mesa's canonical RT ABI headers.

Background: Mesa writes both the fixed RT Local header and the VTAS binary layout;
            Spike and RTL must not hand-copy either set of constants.
Flow: parse Mesa's source-of-truth headers and emit checked-in C++ and Scala
      constants for Spike and the RTL, plus C++ VTAS constants for Spike.
Usage: python3 tools/rtcore_abi.py [--check]
Maintenance: edit the Mesa ABI header(s) only, then run this script and commit
             every generated output.  CI can use --check to reject stale
             generated files.  Pipeline-dependent payload and continuation
             sizes are deliberately not emitted as constants.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "mesa/src/ventus/compiler/vt_rt_abi.h"
VTAS_SOURCE = ROOT / "mesa/src/ventus/common/ventus_rt_bvh.h"
SPIKE_OUTPUT = ROOT / "spike/riscv/ventus_rt_abi_generated.h"
SPIKE_VTAS_OUTPUT = ROOT / "spike/riscv/ventus_vtas_abi_generated.h"
SCALA_OUTPUT = ROOT / "gpgpu/ventus/src/rtcore/RtAbiLayout.scala"
SCALA_VTAS_OUTPUT = ROOT / "gpgpu/ventus/src/rtcore/RtVtasLayout.scala"
DYNAMIC_DEFAULTS = {
    "VT_RT_PDS_HIT_ATTRIB_SIZE_BYTES",
    "VT_RT_PDS_DEFAULT_PAYLOAD_SIZE_BYTES",
}
PAYLOAD_BASE_FUNCTION = "VT_RT_PDS_PAYLOAD_BASE_BYTES"


def source_without_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def parse_constants(text: str, prefixes: tuple[str, ...]) -> dict[str, int]:
    text = source_without_comments(text)
    text = re.sub(r"\\\n\s*", " ", text)
    raw: dict[str, str] = {}
    order: list[str] = []

    for name, expression in re.findall(
        r"^\s*#define[ \t]+([A-Z][A-Z0-9_]+)[ \t]+([^\r\n]+)$", text, re.MULTILINE
    ):
        if not name.startswith(prefixes) or name.endswith("_H"):
            continue
        raw[name] = expression.strip()
        order.append(name)

    for body in re.findall(r"enum\s+\w+\s*\{(.*?)\};", text, re.DOTALL):
        current = -1
        for item in body.split(","):
            item = item.strip()
            if not item:
                continue
            match = re.fullmatch(r"([A-Z][A-Z0-9_]+)(?:\s*=\s*(.+))?", item)
            if not match:
                continue
            name, expression = match.groups()
            if not name.startswith(prefixes):
                continue
            if expression is None:
                current += 1
            else:
                raw[name] = expression.strip()
                current = resolve_constant(name, raw, {})
            raw[name] = str(current)
            order.append(name)

    resolved: dict[str, int] = {}
    for name in order:
        resolve_constant(name, raw, resolved)
    return {name: resolved[name] for name in order}


def parse_payload_base_function(text: str) -> tuple[str, str]:
    text = source_without_comments(text)
    text = re.sub(r"\\\n\s*", " ", text)
    pattern = (
        rf"^\s*#define[ \t]+{PAYLOAD_BASE_FUNCTION}"
        r"\(([A-Za-z_][A-Za-z0-9_]*)\)[ \t]+([^\r\n]+)$"
    )
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise ValueError(f"missing function-like ABI macro {PAYLOAD_BASE_FUNCTION}")
    return match.group(1), match.group(2).strip()


def resolve_constant(name: str, raw: dict[str, str], resolved: dict[str, int]) -> int:
    if name in resolved:
        return resolved[name]
    if name not in raw:
        raise ValueError(f"unknown ABI constant {name}")

    expression = raw[name]

    def replace_identifier(match: re.Match[str]) -> str:
        identifier = match.group(0)
        return str(resolve_constant(identifier, raw, resolved)) if identifier in raw else identifier

    expression = re.sub(r"\b[A-Z][A-Z0-9_]+\b", replace_identifier, expression)
    expression = re.sub(r"\b(0[xX][0-9a-fA-F]+|[0-9]+)[uUlL]+\b", r"\1", expression)
    if not re.fullmatch(r"[0-9xXa-fA-F\s+\-*/%<>&|()~]+", expression):
        raise ValueError(f"unsafe ABI expression for {name}: {expression!r}")
    try:
        value = eval(expression, {"__builtins__": {}}, {})
    except (ArithmeticError, SyntaxError) as error:
        raise ValueError(f"cannot evaluate ABI expression for {name}: {expression!r}") from error
    if not isinstance(value, int):
        raise ValueError(f"non-integer ABI expression for {name}")
    resolved[name] = value
    return value


def cxx_name(name: str) -> str:
    return name.removeprefix("VT_RT_").lower()


def scala_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.removeprefix("VT_RT_").lower().split("_"))


def snake_name(name: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()


def lower_camel_name(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


def generated_banner(language: str) -> list[str]:
    return [
        f"// Generated by tools/rtcore_abi.py from {SOURCE.relative_to(ROOT)}.",
        "// Do not edit: update the Mesa ABI header and regenerate.",
        f"// {language}",
    ]


def vtas_cxx_name(name: str) -> str:
    if name == "VENTUS_AS_INVALID_NODE":
        return "invalid_node_ref"
    if name.startswith("VENTUS_BVH_NODE_"):
        return "node_" + name.removeprefix("VENTUS_BVH_NODE_").lower()
    return name.removeprefix("VENTUS_").lower()


def vtas_scala_name(name: str) -> str:
    if name == "VENTUS_AS_INVALID_NODE":
        return "InvalidNodeRef"
    if name.startswith("VENTUS_BVH_NODE_"):
        return "Node" + "".join(
            part.capitalize() for part in name.removeprefix("VENTUS_BVH_NODE_").lower().split("_")
        )
    return "".join(part.capitalize() for part in name.removeprefix("VENTUS_").lower().split("_"))


def render_spike(constants: dict[str, int], function: tuple[str, str]) -> str:
    argument, expression = function
    cxx_expression = re.sub(r"\bVT_RT_[A-Z0-9_]+\b", lambda m: cxx_name(m.group(0)), expression)
    cxx_argument = snake_name(argument)
    lines = [
        "#ifndef RISCV_VENTUS_RT_ABI_GENERATED_H",
        "#define RISCV_VENTUS_RT_ABI_GENERATED_H",
        *generated_banner("Fixed RT Local header constants for namespace ventus_rt."),
        "",
    ]
    for name, value in constants.items():
        lines.append(f"constexpr reg_t {cxx_name(name)} = {value};")
    lines.extend([
        "",
        f"constexpr reg_t {cxx_name(PAYLOAD_BASE_FUNCTION)}(reg_t {cxx_argument})",
        "{",
        f"  return {cxx_expression};",
        "}",
    ])
    lines.extend(["", "#endif", ""])
    return "\n".join(lines)


def render_scala(constants: dict[str, int], function: tuple[str, str]) -> str:
    argument, expression = function
    scala_expression = re.sub(r"\bVT_RT_[A-Z0-9_]+\b", lambda m: scala_name(m.group(0)), expression)
    scala_argument = lower_camel_name(argument)
    scala_expression = re.sub(rf"\b{re.escape(argument)}\b", scala_argument, scala_expression)
    lines = [
        *generated_banner("Fixed RT Local header constants for package rtcore."),
        "package rtcore",
        "",
        "object RtAbiLayout {",
    ]
    for name, value in constants.items():
        lines.append(f"  final val {scala_name(name)}: Int = {value}")
    lines.extend([
        "",
        f"  final def {lower_camel_name(PAYLOAD_BASE_FUNCTION.removeprefix('VT_RT_ABI_').lower())}({scala_argument}: Int): Int =",
        f"    {scala_expression}",
    ])
    lines.extend(["}", ""])
    return "\n".join(lines)


def render_spike_vtas(constants: dict[str, int]) -> str:
    lines = [
        "#ifndef RISCV_VENTUS_VTAS_ABI_GENERATED_H",
        "#define RISCV_VENTUS_VTAS_ABI_GENERATED_H",
        "// Generated by tools/rtcore_abi.py from mesa/src/ventus/common/ventus_rt_bvh.h.",
        "// Do not edit: update the Mesa VTAS ABI header and regenerate.",
        "// Fixed VTAS binary-layout constants for namespace ventus_rt.",
        "",
    ]
    for name, value in constants.items():
        rendered = f"0x{value & 0xffffffff:x}u" if value < 0 or value > 0x7fffffff else str(value)
        lines.append(f"constexpr uint32_t {vtas_cxx_name(name)} = {rendered};")
    lines.extend(["", "#endif", ""])
    return "\n".join(lines)


def render_scala_vtas(constants: dict[str, int]) -> str:
    lines = [
        "// Generated by tools/rtcore_abi.py from mesa/src/ventus/common/ventus_rt_bvh.h.",
        "// Do not edit: update the Mesa VTAS ABI header and regenerate.",
        "// Fixed VTAS binary-layout constants for package rtcore.",
        "package rtcore",
        "",
        "object RtVtasLayout {",
    ]
    for name, value in constants.items():
        rendered = f"0x{value & 0xffffffff:x}L" if value < 0 or value > 0x7fffffff else str(value)
        lines.append(f"  final val {vtas_scala_name(name)}: Long = {rendered}")
    lines.extend(["}", ""])
    return "\n".join(lines)


def update_or_check(path: pathlib.Path, content: str, check: bool) -> bool:
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == content:
        return True
    if check:
        print(f"stale generated RT ABI file: {path.relative_to(ROOT)}")
        return False
    path.write_text(content, encoding="utf-8")
    print(f"generated {path.relative_to(ROOT)}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if generated files are stale")
    args = parser.parse_args()

    try:
        source_text = SOURCE.read_text(encoding="utf-8")
        constants = parse_constants(source_text, ("VT_RT_",))
        function = parse_payload_base_function(source_text)
        vtas_constants = parse_constants(
            VTAS_SOURCE.read_text(encoding="utf-8"),
            ("VENTUS_AS_", "VENTUS_BVH_", "VENTUS_BOX4_", "VENTUS_TRIANGLE_",
             "VENTUS_AABB_", "VENTUS_INSTANCE_", "VENTUS_NODE_REF_"),
        )
    except (OSError, ValueError) as error:
        print(f"RT ABI generation failed: {error}", file=sys.stderr)
        return 1

    constants = {name: value for name, value in constants.items() if name not in DYNAMIC_DEFAULTS}
    spike_ok = update_or_check(SPIKE_OUTPUT, render_spike(constants, function), args.check)
    spike_vtas_ok = update_or_check(
        SPIKE_VTAS_OUTPUT, render_spike_vtas(vtas_constants), args.check
    )
    scala_ok = update_or_check(SCALA_OUTPUT, render_scala(constants, function), args.check)
    scala_vtas_ok = update_or_check(
        SCALA_VTAS_OUTPUT, render_scala_vtas(vtas_constants), args.check
    )
    return 0 if spike_ok and spike_vtas_ok and scala_ok and scala_vtas_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
