#!/usr/bin/env python3
# santa-app/windows/build_windows.py
# Builds "Santa for Windows" (x64) on a Mac or Linux machine, without Windows:
#   1. downloads a standalone Python 3.12 for Windows (python-build-standalone)
#   2. cross-installs Santa's Windows wheels into it (uv --python-platform)
#   3. adds the Microsoft C++ runtime DLLs next to python.exe (no admin install)
#   4. packs Santa-Setup-<version>.exe with NSIS (makensis, run in Docker)
#      and Santa-for-Windows-<version>.zip with "Install Santa.bat" as a fallback
#   5. prints the one-page interviewer guide to PDF and copies the test plan
# Needs: uv, Docker (for makensis; or makensis on PATH), Pillow (for the icon).
# Usage: python santa-app/windows/build_windows.py [--out DIR] [--skip-installer]

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DEFAULT_OUT = Path.home() / 'Documents' / 'Hiring Right' / '09_Santa_Install' / 'Windows'

PYTHON_VERSION = '3.12'
PLATFORM = 'x86_64-pc-windows-msvc'
# Microsoft Visual C++ runtime (msvcp140*.dll) as a wheel; CTranslate2 and
# onnxruntime need it and a fresh Windows PC may not have it installed.
MSVC_RUNTIME_WHEEL = 'msvc_runtime-14.51.36231-cp312-cp312-win_amd64.whl'
NSIS_IMAGE = 'santa-nsis:1'


def log(msg: str) -> None:
    print(f'== {msg}', flush=True)


def run(cmd: list, **kw) -> None:
    print('   $', ' '.join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def santa_version() -> str:
    init = (REPO / 'src' / 'whisper_key' / 'santa' / '__init__.py').read_text(encoding='utf-8')
    for line in init.splitlines():
        if line.startswith('__version__'):
            return line.split('=')[1].strip().strip('"\'')
    raise SystemExit('santa __version__ not found')


def fetch(url: str, cache: Path) -> Path:
    """Download once into the build cache."""
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / url.rsplit('/', 1)[1].replace('%2B', '+')
    if not dest.exists():
        log(f'Downloading {dest.name}')
        tmp = dest.with_suffix(dest.suffix + '.part')
        # The python-build-standalone mirror refuses urllib's default User-Agent.
        request = urllib.request.Request(url, headers={'User-Agent': 'santa-windows-build/1'})
        with urllib.request.urlopen(request, timeout=120) as resp, open(tmp, 'wb') as out:
            shutil.copyfileobj(resp, out)
        tmp.replace(dest)
    return dest


# ── 1. Standalone Python ─────────────────────────────────────────────────────
def python_url() -> str:
    """URL of the newest CPython 3.12 Windows x64 build that uv knows about."""
    listing = subprocess.run(['uv', 'python', 'list', '--all-platforms', '--output-format', 'json'],
                             check=True, capture_output=True, text=True).stdout
    for entry in json.loads(listing):
        if (entry.get('os') == 'windows' and entry.get('arch') == 'x86_64'
                and entry.get('implementation') == 'cpython' and entry.get('variant') == 'default'
                and entry['version'].startswith(PYTHON_VERSION + '.') and entry.get('url')):
            return entry['url']
    raise SystemExit('No Windows CPython build found via `uv python list --all-platforms`.')


def unpack_python(archive: Path, payload: Path) -> Path:
    log('Unpacking Python')
    with tarfile.open(archive) as tar:
        tar.extractall(payload, filter='data')          # archive root is python/
    py = payload / 'python'
    if not (py / 'python.exe').exists():
        raise SystemExit('python.exe missing from the Python archive')
    return py


# ── 2. Santa + wheels ────────────────────────────────────────────────────────
def install_packages(py: Path, cache: Path) -> None:
    site = py / 'Lib' / 'site-packages'
    common = ['--target', site, '--python-platform', PLATFORM, '--python-version', PYTHON_VERSION,
              '--cache-dir', cache / 'uv']
    log('Installing Santa\'s Windows wheels')
    run(['uv', 'pip', 'install', *common, '--only-binary', ':all:',
         '-r', HERE / 'requirements-windows.txt'])
    log('Installing Santa itself')
    run(['uv', 'pip', 'install', *common, '--no-deps', '--reinstall-package', 'whisper-local', REPO])
    # Console-script wrappers made for this Mac, and duplicate DLL copies, are not needed.
    for extra in ('bin', 'Scripts'):
        shutil.rmtree(site / extra, ignore_errors=True)
    for cache_dir in site.rglob('__pycache__'):
        shutil.rmtree(cache_dir, ignore_errors=True)


def add_msvc_runtime(py: Path, cache: Path) -> None:
    """Put msvcp140*.dll / vcruntime140*.dll next to python.exe (app-local copy)."""
    log('Adding the Microsoft C++ runtime DLLs')
    wheel = fetch_pypi_file('msvc-runtime', MSVC_RUNTIME_WHEEL, cache)
    added = []
    with zipfile.ZipFile(wheel) as zf:
        for name in zf.namelist():
            if name.lower().endswith('.dll') and '.data/data/' in name:
                target = py / Path(name).name
                if not target.exists():           # keep Python's own vcruntime140.dll
                    target.write_bytes(zf.read(name))
                    added.append(target.name)
    if 'msvcp140.dll' not in added and not (py / 'msvcp140.dll').exists():
        raise SystemExit('msvcp140.dll not found in the msvc-runtime wheel')
    print('   added:', ', '.join(sorted(added)))


def fetch_pypi_file(project: str, filename: str, cache: Path) -> Path:
    meta = json.load(urllib.request.urlopen(f'https://pypi.org/pypi/{project}/json', timeout=60))
    for release in meta['releases'].values():
        for f in release:
            if f['filename'] == filename:
                path = fetch(f['url'], cache)
                if hashlib.sha256(path.read_bytes()).hexdigest() != f['digests']['sha256']:
                    path.unlink()
                    raise SystemExit(f'{filename}: checksum mismatch')
                return path
    raise SystemExit(f'{filename} not found on PyPI')


# ── 3. Icon, launch files ────────────────────────────────────────────────────
def make_icon(payload: Path) -> None:
    from PIL import Image
    src = REPO / 'santa-app' / 'icon-1024.png'
    Image.open(src).save(payload / 'santa.ico', sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                                       (64, 64), (128, 128), (256, 256)])


def write_payload_extras(payload: Path, version: str) -> None:
    (payload / 'VERSION.txt').write_text(f'Santa for Windows {version}\n', encoding='utf-8')
    shutil.copy(REPO / 'LICENSE', payload / 'LICENSE.txt')


# ── 4. Installer + zip ───────────────────────────────────────────────────────
def build_nsis(work: Path, out: Path, version: str) -> Path:
    exe_name = f'Santa-Setup-{version}.exe'
    defines = [f'-DVERSION={version}', '-DPAYLOAD=payload', f'-DOUTFILE={exe_name}']
    shutil.copy(HERE / 'installer.nsi', work / 'installer.nsi')
    log('Packing the installer with NSIS (takes a few minutes: LZMA over ~350 MB)')
    makensis = shutil.which('makensis') or next(
        (p for p in (r'C:\Program Files (x86)\NSIS\makensis.exe', r'C:\Program Files\NSIS\makensis.exe')
         if Path(p).exists()), None)
    if makensis:
        run([makensis, '-V2', *defines, 'installer.nsi'], cwd=work)
    else:
        ensure_nsis_image()
        run(['docker', 'run', '--rm', '-v', f'{work}:/w', '-w', '/w', NSIS_IMAGE,
             'makensis', '-V2', *defines, 'installer.nsi'])
    built = work / exe_name
    shutil.copy(built, out / exe_name)
    return out / exe_name


def ensure_nsis_image() -> None:
    have = subprocess.run(['docker', 'image', 'inspect', NSIS_IMAGE], capture_output=True)
    if have.returncode == 0:
        return
    log('Building a small Docker image with NSIS (once)')
    dockerfile = 'FROM debian:bookworm-slim\nRUN apt-get update && apt-get install -y --no-install-recommends nsis && rm -rf /var/lib/apt/lists/*\n'
    run(['docker', 'build', '-t', NSIS_IMAGE, '-'], input=dockerfile.encode())


def build_zip(work: Path, out: Path, version: str) -> Path:
    """Fallback: a zip with Install Santa.bat (same files, no installer engine)."""
    zip_path = out / f'Santa-for-Windows-{version}.zip'
    log(f'Writing {zip_path.name}')
    tmp = zip_path.with_suffix('.zip.part')
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name in ('Install Santa.bat', 'Uninstall Santa.bat', 'READ ME FIRST.txt'):
            zf.write(HERE / name, f'Santa/{name}')
        for path in sorted((work / 'payload').rglob('*')):
            if path.is_file():
                zf.write(path, 'Santa/app/' + path.relative_to(work / 'payload').as_posix())
    tmp.replace(zip_path)
    return zip_path


def write_guide(out: Path) -> None:
    """Interviewer guide as PDF (Chrome/Edge headless) and HTML, next to the installer."""
    html = HERE / 'Interviewer Guide.html'
    shutil.copy(html, out / 'Santa for Windows - Interviewer Guide.html')
    browsers = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                shutil.which('google-chrome') or '', shutil.which('chromium') or '',
                r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe']
    browser = next((b for b in browsers if b and Path(b).exists()), None)
    if browser is None:
        print('   (no Chrome/Edge found: guide written as HTML only)')
        return
    pdf = out / 'Santa for Windows - Interviewer Guide.pdf'
    run([browser, '--headless', '--disable-gpu', '--no-pdf-header-footer',
         f'--print-to-pdf={pdf}', html.as_uri()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(description='Build Santa for Windows')
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--work', type=Path, default=HERE / 'build')
    parser.add_argument('--skip-installer', action='store_true', help='zip only (no NSIS)')
    parser.add_argument('--skip-zip', action='store_true')
    args = parser.parse_args()

    version = santa_version()
    work, cache = args.work.resolve(), (args.work / 'cache').resolve()
    payload = work / 'payload'
    shutil.rmtree(payload, ignore_errors=True)
    payload.mkdir(parents=True)
    args.out.mkdir(parents=True, exist_ok=True)

    py = unpack_python(fetch(python_url(), cache), payload)
    install_packages(py, cache)
    add_msvc_runtime(py, cache)
    make_icon(payload)
    write_payload_extras(payload, version)
    size_mb = sum(p.stat().st_size for p in payload.rglob('*') if p.is_file()) / 1e6
    log(f'Payload ready: {size_mb:.0f} MB')

    built = []
    if not args.skip_installer:
        built.append(build_nsis(work, args.out, version))
    if not args.skip_zip:
        built.append(build_zip(work, args.out, version))
    log('Writing the interviewer guide')
    write_guide(args.out)
    shutil.copy(REPO / 'docs' / 'santa' / 'WINDOWS.md', args.out / 'Windows build and test plan.md')
    for path in built:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f'   {path}  {path.stat().st_size / 1e6:.0f} MB  sha256 {digest[:16]}…')
    return 0


if __name__ == '__main__':
    sys.exit(main())
