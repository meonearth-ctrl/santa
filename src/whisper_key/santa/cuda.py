# santa/cuda.py
# NVIDIA GPU support for faster-whisper (Windows and Linux; never macOS).
# CTranslate2 can see a GPU through the driver alone, but it also needs cuBLAS
# and cuDNN at the first transcription. If those are missing it can kill the
# whole process, so the GPU is used only after the libraries are confirmed to
# load. Santa's optional "NVIDIA GPU pack" installs them as pip wheels
# (nvidia-cublas-cu12, nvidia-cudnn-cu12); on Windows their DLL folders have to
# be added to the search path by hand.

import ctypes
import glob
import logging
import os
import sys

logger = logging.getLogger(__name__)

# Library files CTranslate2 4.x (CUDA 12 + cuDNN 9) opens on first use.
REQUIRED_LIBS = {
    'win32': ['cublas64_12.dll', 'cublasLt64_12.dll', 'cudnn64_9.dll',
              'cudnn_ops64_9.dll', 'cudnn_cnn64_9.dll'],
    'linux': ['libcublas.so.12', 'libcublasLt.so.12', 'libcudnn.so.9',
              'libcudnn_ops.so.9', 'libcudnn_cnn.so.9'],
}

_cached = None


# ── Library discovery ────────────────────────────────────────────────────────
def nvidia_wheel_dirs(search_paths=None) -> list:
    """DLL/.so folders of pip-installed nvidia-* packages (site-packages/nvidia/*/bin|lib)."""
    found = []
    for base in (search_paths if search_paths is not None else sys.path):
        if not base or not os.path.isdir(os.path.join(base, 'nvidia')):
            continue
        for sub in ('bin', 'lib'):
            found.extend(sorted(glob.glob(os.path.join(base, 'nvidia', '*', sub))))
    return found


def register_library_dirs(search_paths=None, platform: str = None) -> list:
    """Make the GPU-pack DLLs findable (Windows). Safe to call more than once."""
    platform = platform or sys.platform
    dirs = nvidia_wheel_dirs(search_paths)
    if platform != 'win32':
        return dirs
    path_parts = os.environ.get('PATH', '').split(os.pathsep)
    for folder in dirs:
        try:
            os.add_dll_directory(folder)
        except (AttributeError, OSError):
            pass
        if folder not in path_parts:
            # CTranslate2 loads cuDNN sub-libraries with plain LoadLibrary, which
            # ignores add_dll_directory and only searches PATH.
            os.environ['PATH'] = folder + os.pathsep + os.environ.get('PATH', '')
    return dirs


def missing_libraries(platform: str = None, loader=None) -> list:
    """Names of required GPU libraries that cannot be loaded (empty = all good)."""
    platform = platform or sys.platform
    names = REQUIRED_LIBS.get(platform)
    if names is None:
        return ['(no NVIDIA support on this platform)']
    loader = loader or ctypes.CDLL
    missing = []
    for name in names:
        try:
            loader(name)
        except OSError:
            missing.append(name)
    return missing


def gpu_count() -> int:
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


# ── Decision ─────────────────────────────────────────────────────────────────
def cuda_status(refresh: bool = False) -> dict:
    """{'usable': bool, 'gpus': n, 'reason': str}. Cached after the first call."""
    global _cached
    if _cached is not None and not refresh:
        return dict(_cached)
    if sys.platform == 'darwin':
        _cached = {'usable': False, 'gpus': 0, 'reason': 'macOS has no NVIDIA GPU support.'}
        return dict(_cached)
    register_library_dirs()
    gpus = gpu_count()
    if gpus == 0:
        _cached = {'usable': False, 'gpus': 0, 'reason': 'No NVIDIA GPU found.'}
    else:
        missing = missing_libraries()
        if missing:
            _cached = {'usable': False, 'gpus': gpus,
                       'reason': 'NVIDIA GPU found, but the GPU pack is not installed '
                                 f'(missing {", ".join(missing[:2])}).'}
        else:
            _cached = {'usable': True, 'gpus': gpus, 'reason': 'NVIDIA GPU ready.'}
    logger.info('cuda: %s', _cached['reason'])
    return dict(_cached)


def gpu_compute_type(cpu_compute_type: str) -> str:
    """Map the CPU compute type from Settings to the GPU equivalent."""
    return 'int8_float16' if cpu_compute_type.startswith('int8') else 'float16'
