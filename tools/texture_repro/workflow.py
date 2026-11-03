"""Bounded build/replay workflow; no fetching, publishing or branch switching."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ('llvm', 'spike', 'driver', 'mesa')
IMAGE_SHA = '88b724540dc17b23161c638ebcbb71e9acaf73ff098b43764f9edf17cd5bebb0'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def git(*args, cwd=ROOT):
    return subprocess.check_output(['git', '-c', 'submodule.recurse=false', *args],
                                   cwd=cwd, text=True,
                                   env=dict(os.environ, GIT_NO_LAZY_FETCH='1')).strip()


def source_identity():
    result = {'env': git('rev-parse', 'HEAD'), 'submodules': {}}
    for name in SOURCES:
        pin = git('ls-tree', 'HEAD', name).split()[2]
        path = ROOT / name
        if not (path / '.git').exists():
            raise RuntimeError(f'{name} is not initialized; see docs/texture-reproduction.md')
        actual = git('rev-parse', 'HEAD', cwd=path)
        if pin != actual:
            raise RuntimeError(f'{name}: expected pinned {pin}, got {actual}')
        result['submodules'][name] = actual
    child = ROOT / 'driver/dependencies/membox'
    if not (child / '.git').exists():
        raise RuntimeError('initialize driver/dependencies/membox before building')
    expected = git('ls-tree', 'HEAD', 'dependencies/membox', cwd=ROOT / 'driver').split()[2]
    actual = git('rev-parse', 'HEAD', cwd=child)
    if expected != actual:
        raise RuntimeError('driver membox differs from its pinned commit')
    result['submodules']['driver/dependencies/membox'] = actual
    return result


def environment():
    # Do not inherit a different checkout's compiler paths or experimental knobs.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('VENTUS_', 'VK_', 'MESA_', 'RT_', 'LLVM_'))
           and k not in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'CPATH', 'CPLUS_INCLUDE_PATH',
                         'LIBRARY_PATH', 'CMAKE_PREFIX_PATH', 'PKG_CONFIG_PATH',
                         'PYTHONPATH', 'CC', 'CXX', 'CFLAGS', 'CXXFLAGS', 'LDFLAGS')}
    env.update(PATH='/usr/bin:/bin', CCACHE_DISABLE='1', PYTHONNOUSERSITE='1')
    return env


def run(command, *, cwd=ROOT, env=None, log=None, timeout=7200):
    command = [str(x) for x in command]
    print('+ ' + ' '.join(command), flush=True)
    if log:
        with Path(log).open('w') as stream:
            return subprocess.run(command, cwd=cwd, env=env, stdout=stream,
                                  stderr=subprocess.STDOUT, timeout=timeout).returncode
    subprocess.run(command, cwd=cwd, env=env, check=True, timeout=timeout)
    return 0


def build(args):
    identity = source_identity()
    env = environment()
    jobs = str(args.jobs)
    prefix = ROOT / 'install-texture'
    log_root = ROOT / 'artifacts/texture-build'
    log_root.mkdir(parents=True, exist_ok=True)
    logs = Path(tempfile.mkdtemp(prefix='run-', dir=log_root))
    commands = []

    def execute(command, cwd=ROOT):
        log = logs / f'{len(commands):02d}.log'
        record = {'command': list(map(str, command)), 'cwd': str(cwd), 'log': str(log)}
        commands.append(record)
        (logs / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
        record['returncode'] = run(command, cwd=cwd, env=env, log=log)
        (logs / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
        if record['returncode']:
            raise RuntimeError(f'build failed; inspect {log}')

    components = args.component or ['llvm', 'runtime', 'spike', 'driver', 'mesa']
    for component in components:
        if component == 'llvm':
            execute(['cmake', '-G', 'Ninja', '-S', ROOT / 'llvm/llvm', '-B', ROOT / 'llvm/build',
                     '-DCMAKE_BUILD_TYPE=Release', '-DLLVM_ENABLE_PROJECTS=clang;lld',
                     '-DLLVM_TARGETS_TO_BUILD=AMDGPU;X86;RISCV', '-DLLVM_TARGET_ARCH=riscv32',
                     '-DBUILD_SHARED_LIBS=ON', '-DLLVM_BUILD_LLVM_DYLIB=ON',
                     '-DLLVM_ENABLE_ASSERTIONS=OFF', '-DLLVM_PARALLEL_LINK_JOBS=2',
                     '-DLLVM_CCACHE_BUILD=OFF'])
            execute(['ninja', '-C', ROOT / 'llvm/build', '-j', jobs,
                     'clang', 'llc', 'ld.lld', 'llvm-nm', 'llvm-objdump'])
        elif component == 'runtime':
            target = ROOT / 'llvm/build-rt-libclc/lib/crt0.o'
            target.parent.mkdir(parents=True, exist_ok=True)
            # The RT path needs only this startup assembly, not the OpenCL library.
            execute([ROOT / 'llvm/build/bin/clang', '-target', 'riscv32',
                     '-mcpu=ventus-gpgpu', '-ffunction-sections', '-fdata-sections',
                     '-c', ROOT / 'llvm/libclc/riscv32/lib/crt0.S', '-o', target])
        elif component == 'spike':
            directory = ROOT / 'spike/build'
            directory.mkdir(parents=True, exist_ok=True)
            execute(['../configure', f'--prefix={prefix}', '--enable-commitlog'], cwd=directory)
            execute(['make', '-j', jobs], cwd=directory)
            execute(['make', 'install'], cwd=directory)
        elif component == 'driver':
            execute(['cmake', '-G', 'Ninja', '-S', ROOT / 'driver', '-B', ROOT / 'driver/build',
                     '-DCMAKE_BUILD_TYPE=Release', f'-DCMAKE_INSTALL_PREFIX={prefix}',
                     f'-DVENTUS_INSTALL_PREFIX={prefix}', f'-DSPIKE_SRC_DIR={ROOT / "spike"}',
                     *[f'-DDRIVER_ENABLE_{x}=OFF' for x in
                       ('AUTOSELECT', 'RTLSIM', 'CYCLESIM', 'GVM', 'PTX')]])
            execute(['ninja', '-C', ROOT / 'driver/build', '-j', jobs, 'spike_driver'])
        elif component == 'mesa':
            meson = ROOT / 'build/texture-tools/bin/meson'
            if not meson.is_file():
                raise RuntimeError('prepare build/texture-tools as documented (Meson 1.11.1)')
            llvm_config = Path(args.host_llvm_config).resolve()
            version = subprocess.check_output([llvm_config, '--version'], text=True).strip()
            if not version.startswith('20.'):
                raise RuntimeError(f'Mesa baseline requires host LLVM 20, got {version}')
            native = ROOT / 'build/texture-mesa-native.ini'
            native.write_text("[binaries]\nc = '/usr/bin/cc'\ncpp = '/usr/bin/c++'\n"
                              f"llvm-config = '{llvm_config}'\n")
            directory = ROOT / 'mesa/build-ventus'
            command = [meson, 'setup']
            if (directory / 'meson-private/coredata.dat').exists():
                command.append('--reconfigure')
            execute([*command, directory, ROOT / 'mesa', '--native-file', native,
                     '--wrap-mode=nofallback', '-Dbuildtype=debugoptimized',
                     '-Dvulkan-drivers=ventus', '-Dgallium-drivers=', '-Dllvm=enabled',
                     '-Dplatforms=x11', '-Dglx=disabled', '-Degl=disabled',
                     '-Dgles1=disabled', '-Dgles2=disabled', '-Dopengl=false',
                     '-Dbuild-tests=false', '-Dvulkan-layers=', '-Dtools=',
                     '-Dventus_rt_resident_warps=8'])
            execute(['ninja', '-C', directory, '-j', jobs])
    (logs / 'sources.json').write_text(json.dumps(identity, indent=2) + '\n')


def validate_numeric(item, returncode, manifest, log_text):
    """Require the intended negative signal; any generic failure is insufficient."""
    if returncode != item.get('expected_returncode', 0):
        raise RuntimeError(f'{item["name"]}: unexpected exit {returncode}')
    if manifest.get('timeout'):
        raise RuntimeError(f'{item["name"]}: timeout is not a valid negative result')
    expected_failure = item.get('expected_returncode', 0) != 0
    if manifest.get('status') != ('FAIL' if expected_failure else 'PASS'):
        raise RuntimeError(f'{item["name"]}: unexpected manifest status')
    summary = manifest.get('summary', {})
    if expected_failure:
        if '--quad-without-opt-in' in item['args']:
            if 'requires the private complete same-warp quad experiment' not in log_text:
                raise RuntimeError('missing explicit private-quad compiler rejection')
        elif int(summary.get('quad_gradient_failed', '0')) <= 0:
            raise RuntimeError('negative control did not detect the wrong quad gradient')
    for key, value in item.get('expected_summary', {}).items():
        if summary.get(key) != value:
            raise RuntimeError(f'{item["name"]}: {key}: {summary.get(key)} != {value}')
    if not expected_failure:
        maximum = float(summary.get('max_abs_error', '0'))
        tolerance = float(summary.get('tolerance', '0'))
        if not 0 <= maximum <= tolerance:
            raise RuntimeError(f'{item["name"]}: color error outside tolerance')


def verify(args):
    identity = source_identity()
    env = environment()
    if args.out_dir:
        output = Path(args.out_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            raise RuntimeError('verification output must be empty')
    else:
        directory = ROOT / 'artifacts/texture-repro'
        directory.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix='run-', dir=directory))
    matrix = json.loads(Path(__file__).with_name('matrix.json').read_text())
    cases = matrix['quad'] if args.suite == 'quad' else matrix['features'] + matrix['quad']
    if args.suite == 'quick':
        cases = matrix['quick']
    report = {'status': 'RUNNING', 'sources': identity, 'suite': args.suite,
              'started_at': datetime.now(timezone.utc).isoformat(), 'commands': []}

    def save():
        (output / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')

    save()
    try:
        modes = [False, True] if args.suite == 'full' else [False]
        for sanitize in modes:
            name = 'model-sanitize' if sanitize else 'model'
            command = [sys.executable, ROOT / 'tools/texture_model.py', 'test']
            if sanitize:
                command.append('--sanitize')
            log = output / f'{name}.log'
            rc = run(command, env=env, log=log, timeout=args.timeout)
            report['commands'].append({'name': name, 'returncode': rc})
            if rc or 'PASS texture model: 21 suites, 42658996 checks' not in log.read_text():
                raise RuntimeError(f'{name}: model acceptance changed or failed')
            save()
        for item in cases:
            destination = output / item['name']
            command = [sys.executable, ROOT / 'tools/texture_model.py', *item['args'],
                       '--timeout', str(args.timeout), '--out-dir', destination]
            log = output / f'{item["name"]}.log'
            rc = run(command, env=env, log=log, timeout=args.timeout + 120)
            manifest_path = destination / 'manifest.json'
            if not manifest_path.is_file():
                raise RuntimeError(f'{item["name"]}: missing manifest; inspect {log}')
            manifest = json.loads(manifest_path.read_text())
            detail = (destination / 'run.log').read_text(errors='replace')
            validate_numeric(item, rc, manifest, detail)
            report['commands'].append({'name': item['name'], 'command': list(map(str, command)),
                                       'returncode': rc, 'expected_returncode': item.get('expected_returncode', 0),
                                       'manifest': str(manifest_path), 'summary': manifest['summary']})
            if not args.keep_numeric:
                for path in destination.glob('numeric.csv*'):
                    path.unlink()
            save()
        if args.with_image:
            image_dir = output / 'any-hit'
            rc = run(['bash', ROOT / 'tools/rtcore/verify_textured_any_hit_image.sh'],
                     env=dict(env, OUT_DIR=str(image_dir)), log=output / 'any-hit.log',
                     timeout=args.timeout)
            ppm = image_dir / '160x96/raytracingtextures_spike.ppm'
            if rc or not ppm.is_file() or digest(ppm) != IMAGE_SHA:
                raise RuntimeError('textured any-hit image gate failed')
            report['image_sha256'] = digest(ppm)
        report['status'] = 'PASS'
    except (RuntimeError, subprocess.SubprocessError, OSError) as error:
        report['status'] = 'FAIL'
        report['error'] = str(error)
        raise
    finally:
        save()
        print(f'Report: {output / "verification.json"}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('check', help='verify initialized sources match parent gitlinks')
    b = sub.add_parser('build', help='build only the local Spike texture dependencies')
    b.add_argument('--jobs', type=int, default=8)
    b.add_argument('--component', action='append', choices=('llvm', 'runtime', 'spike', 'driver', 'mesa'))
    b.add_argument('--host-llvm-config', default='/usr/bin/llvm-config-20')
    v = sub.add_parser('verify', help='run model and GPU gates with frozen expectations')
    v.add_argument('--suite', choices=('quick', 'quad', 'full'), default='quick')
    v.add_argument('--out-dir', type=Path)
    v.add_argument('--timeout', type=int, default=900)
    v.add_argument('--keep-numeric', action='store_true')
    v.add_argument('--with-image', action='store_true')
    args = parser.parse_args()
    if getattr(args, 'jobs', 1) < 1 or getattr(args, 'timeout', 1) < 1:
        parser.error('jobs and timeout must be positive')
    try:
        if args.action == 'check':
            print(json.dumps(source_identity(), indent=2))
        elif args.action == 'build':
            build(args)
        else:
            verify(args)
    except (RuntimeError, subprocess.SubprocessError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0
