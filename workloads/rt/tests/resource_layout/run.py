#!/usr/bin/env python3
"""Build a standard Vulkan resource/geometry probe; preserve per-backend logs.

Run: python3 workloads/rt/tests/resource_layout/run.py [--backend lavapipe|spike|both]
Uses canonical Mesa and existing build/install libraries; never installs anything.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[4]
SOURCE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['lavapipe', 'spike', 'rtl', 'both'], default='both')
    parser.add_argument('--rtl-variant', default='basic-sm1-curve', help='existing isolated RTL build suffix')
    parser.add_argument('--variant', action='append', choices=['single', 'rebind', 'aabb', 'aabb-rebind', 'aabb-onegeometry'])
    args = parser.parse_args()
    output = ROOT / 'artifacts/rtcore-binding' / time.strftime('%Y%m%dT%H%M%S')
    output.mkdir(parents=True, exist_ok=False)
    binary = output / 'resource_layout'
    with (output / 'build.log').open('w') as log:
        subprocess.run(['c++', '-std=c++17', '-O0', '-g', str(SOURCE / 'main.cpp'), '-lvulkan', '-o', str(binary)], check=True, stdout=log, stderr=log)
        for name in ['raygen.rgen', 'miss.rmiss', 'closesthit.rchit', 'intersection.rint']:
            subprocess.run(['glslangValidator', '-V', '--target-env', 'vulkan1.2', str(SOURCE / name), '-o', str(output / (name + '.spv'))], check=True, stdout=log, stderr=log)
    results = {}
    provenance = {}
    for backend in (['lavapipe', 'spike'] if args.backend == 'both' else [args.backend]):
        env = os.environ.copy()
        if backend == 'lavapipe':
            icd = Path('/usr/share/vulkan/icd.d/lvp_icd.json')
        else:
            icd = ROOT / 'mesa/build-ventus/src/ventus/vulkan/ventus_devenv_icd.x86_64.json'
            elf = output / 'elf'
            elf.mkdir()
            env.update({
                'VENTUS_VK_DRIVER_BACKEND': 'spike', 'VENTUS_VK_DRIVER_BRIDGE': '1',
                'VENTUS_VK_EMIT_ELF': '1', 'VENTUS_SPIKE_LOG': '0',
                'VENTUS_VK_DRIVER_LIB': str(ROOT / 'driver/build/driver/spike_device/libspike_driver.so'),
                'VENTUS_VK_SHADER_ARTIFACT_DIR': str(elf),
                'VENTUS_VK_LLC': str(ROOT / 'llvm/build/bin/llc'),
                'VENTUS_VK_LD_LLD': str(ROOT / 'llvm/build/bin/ld.lld'),
                'VENTUS_VK_LLVM_NM': str(ROOT / 'llvm/build/bin/llvm-nm'),
                'VENTUS_VK_CRT0': str(ROOT / 'install/lib/crt0.o'),
                'VENTUS_VK_LLVM_TOOL_LIB_DIR': str(ROOT / 'llvm/build/lib'),
                'MESA_SHADER_CACHE_DISABLE': 'true',
                'LD_LIBRARY_PATH': ':'.join(str(ROOT / p) for p in ['spike/build', 'driver/build/driver/spike_device', 'llvm/build/lib']) + ':' + env.get('LD_LIBRARY_PATH', ''),
            })
        if backend == 'rtl':
            runtime = ROOT / ('install-rtcore-rtl-' + args.rtl_variant) / 'lib'
            driver = ROOT / ('driver/build-rtcore-rtl-' + args.rtl_variant) / 'driver/rtlsim_device/librtlsim_driver.so'
            for path in [runtime / 'crt0.o', runtime / 'libVentusRTL-withcache.so', driver]:
                if not path.is_file():
                    raise FileNotFoundError(path)
            env.update({
                'VENTUS_VK_DRIVER_BACKEND': 'rtlsim', 'VENTUS_BACKEND': 'rtl',
                'VENTUS_VK_DRIVER_LIB': str(driver), 'VENTUS_PMU_MODE': 'basic',
                'VENTUS_VK_CRT0': str(runtime / 'crt0.o'),
                'LD_LIBRARY_PATH': ':'.join([str(runtime), str(driver.parent), str(ROOT / 'llvm/build/lib'), os.environ.get('LD_LIBRARY_PATH', '')]),
            })
        env['VK_DRIVER_FILES'] = env['VK_ICD_FILENAMES'] = str(icd)
        tracked = [icd, ROOT / 'mesa/build-ventus/src/ventus/vulkan/libvulkan_ventus.so']
        if backend != 'lavapipe':
            tracked += [Path(env[k]) for k in ['VENTUS_VK_DRIVER_LIB', 'VENTUS_VK_LLC', 'VENTUS_VK_CRT0']]
        if backend == 'rtl':
            tracked.append(runtime / 'libVentusRTL-withcache.so')
        provenance[backend] = {str(p): hashlib.file_digest(p.open('rb'), 'sha256').hexdigest() for p in tracked}
        for variant in (args.variant or ['single', 'rebind', 'aabb', 'aabb-rebind']):
            key = backend + '-' + variant
            with (output / (key + '.log')).open('w') as log:
                result = subprocess.run([str(binary), str(output), variant], env=env, cwd=output, stdout=log, stderr=log, timeout=180)
            results[key] = result.returncode
            print(f'{key}: {"PASS" if result.returncode == 0 else "FAIL"}; {output / (key + ".log")}', flush=True)
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.glob('*.spv')}
    (output / 'results.json').write_text(json.dumps({'exit_codes': results, 'spirv_sha256': hashes, 'binaries_sha256': provenance}, indent=2) + '\n')
    return int(any(results.values()))


if __name__ == '__main__':
    raise SystemExit(main())
