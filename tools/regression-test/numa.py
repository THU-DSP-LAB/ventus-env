import os
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass


NUMACTL_AUTO = "auto"
NUMACTL_OFF = "off"
NUMACTL_REQUIRE = "require"
NUMACTL_CHOICES = (NUMACTL_AUTO, NUMACTL_OFF, NUMACTL_REQUIRE)


class NumaBindingError(RuntimeError):
    pass


@dataclass(frozen=True)
class CpuRecord:
    cpu: int
    socket: int
    node: int


@dataclass(frozen=True)
class NumaBinding:
    node: int
    cpus: tuple[int, ...]

    def command_prefix(self) -> list[str]:
        return ["numactl", "-m", str(self.node), "-C", ",".join(str(cpu) for cpu in self.cpus), "--"]


class NumaAllocator:
    """Round-robin NUMA binding selector.

    Bindings are shared by design: multiple heavy testcases may run on the same
    NUMA node when the worker budget allows that concurrency.
    """

    def __init__(self, bindings: list[NumaBinding]):
        self._bindings = sorted(bindings, key=_binding_sort_key)
        self._next_index = 0

    @property
    def enabled(self) -> bool:
        return bool(self._bindings)

    @property
    def capacity(self) -> int:
        return len(self._bindings)

    def allocate(self) -> NumaBinding:
        if not self._bindings:
            raise NumaBindingError("NUMA CPU binding unavailable")
        binding = self._bindings[self._next_index]
        self._next_index = (self._next_index + 1) % len(self._bindings)
        return binding


def create_allocator(policy: str, min_cpus_per_node: int) -> tuple[NumaAllocator, str | None]:
    if policy not in NUMACTL_CHOICES:
        raise NumaBindingError(f"invalid numactl policy: {policy}")
    if policy == NUMACTL_OFF or min_cpus_per_node <= 0:
        return NumaAllocator([]), None

    try:
        bindings = discover_bindings(min_cpus_per_node)
    except NumaBindingError as exc:
        if policy == NUMACTL_REQUIRE:
            raise
        return NumaAllocator([]), str(exc)

    if not bindings:
        message = f"no NUMA node has at least {min_cpus_per_node} usable online CPUs"
        if policy == NUMACTL_REQUIRE:
            raise NumaBindingError(message)
        return NumaAllocator([]), message

    return NumaAllocator(bindings), None


def discover_bindings(min_cpus_per_node: int) -> list[NumaBinding]:
    if shutil.which("numactl") is None:
        raise NumaBindingError("numactl executable not found")
    records = _read_lscpu_records()
    if not records:
        raise NumaBindingError("lscpu did not report any usable online CPUs")
    return _build_bindings(records, min_cpus_per_node)


def wrap_command(cmd: list[str], binding: NumaBinding | None) -> list[str]:
    if binding is None:
        return list(cmd)
    return binding.command_prefix() + list(cmd)


def parse_lscpu(output: str, allowed_cpus: set[int] | None = None) -> list[CpuRecord]:
    records: list[CpuRecord] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        record = _parse_lscpu_line(stripped, allowed_cpus)
        if record is not None:
            records.append(record)
    return records


def _read_lscpu_records() -> list[CpuRecord]:
    try:
        allowed_cpus = set(os.sched_getaffinity(0))
    except AttributeError:
        allowed_cpus = None
    try:
        result = subprocess.run(
            ["lscpu", "--parse=CPU,CORE,SOCKET,NODE,ONLINE"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise NumaBindingError("lscpu executable not found") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else f"exit code {exc.returncode}"
        raise NumaBindingError(f"lscpu topology query failed: {stderr}") from exc
    return parse_lscpu(result.stdout, allowed_cpus)


def _parse_lscpu_line(line: str, allowed_cpus: set[int] | None) -> CpuRecord | None:
    fields = [field.strip() for field in line.split(",")]
    if len(fields) != 5:
        raise NumaBindingError(f"unexpected lscpu row: {line}")
    cpu = _parse_int_field(fields[0], "CPU", line)
    if allowed_cpus is not None and cpu not in allowed_cpus:
        return None
    if fields[4] and fields[4].upper() != "Y":
        return None
    socket = _parse_int_field(fields[2], "SOCKET", line)
    node = socket if fields[3] in {"", "-"} else _parse_int_field(fields[3], "NODE", line)
    return CpuRecord(cpu=cpu, socket=socket, node=node)


def _parse_int_field(value: str, field_name: str, line: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise NumaBindingError(f"invalid {field_name} value in lscpu row: {line}") from exc


def _build_bindings(records: list[CpuRecord], min_cpus_per_node: int) -> list[NumaBinding]:
    cpus_by_node: dict[int, list[int]] = defaultdict(list)
    for record in sorted(records, key=lambda item: item.cpu):
        cpus_by_node[record.node].append(record.cpu)

    return [
        NumaBinding(node=node, cpus=tuple(sorted(cpus)))
        for node, cpus in sorted(cpus_by_node.items())
        if len(cpus) >= min_cpus_per_node
    ]


def _binding_sort_key(binding: NumaBinding) -> tuple[int, int]:
    return binding.node, binding.cpus[0]
