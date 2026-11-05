# santa/setup_tasks.py
# One-off set-up steps the installers run after copying Santa (and that support
# staff can re-run by hand): apply the Hiring Right preset, download the speech
# model so the first interview doesn't wait for it, and, on a PC with an NVIDIA
# card, install the GPU pack (cuBLAS + cuDNN wheels); stop a running Santa before
# an upgrade or uninstall. Each step prints plain
# progress lines for the installer's console window and never raises.

import os
import shutil
import subprocess
import sys

from . import hiring_right
from .models import CATALOG, cached_model_path
from .settings import SettingsStore

# CUDA 12 / cuDNN 9 runtime wheels matching CTranslate2 4.x.
GPU_PACK = ['nvidia-cublas-cu12>=12.4,<13', 'nvidia-cudnn-cu12>=9.1,<10']


def setup_hiring_right(store: SettingsStore = None) -> int:
    """Create the Hiring Right folders and switch on watch + export."""
    store = store or SettingsStore()
    done = hiring_right.apply(store)
    s = done['settings']
    print('Hiring Right set-up saved:')
    print(f"  Videos to transcribe:  {done['dirs']['videos']}")
    print(f"  Transcripts saved to:  {done['dirs']['transcripts']}")
    print(f"  Model: {s['model']} · language: {s['watch_language']} · up to {s['max_duration_min']} min per file")
    return 0


def download_model(key: str = None, store: SettingsStore = None) -> int:
    """Download the configured model into the Hugging Face cache (once)."""
    store = store or SettingsStore()
    key = key or store.get()['model']
    info = CATALOG.get(key)
    if info is None:
        print(f'Unknown model: {key}')
        return 2
    speakers_ok = download_speaker_models(store)
    if cached_model_path(key):
        print(f'{info.label} is already downloaded.')
        return 0 if speakers_ok else 1
    print(f'Downloading the {info.label} speech model (~{info.download_mb} MB, once). '
          'This can take a few minutes…')
    try:
        from huggingface_hub import snapshot_download
        from .models import _MODEL_FILES
        snapshot_download(info.repo, allow_patterns=_MODEL_FILES)
    except Exception as exc:
        print(f'Could not download the model now ({type(exc).__name__}). '
              'Santa will try again the first time it starts.')
        return 1
    print(f'{info.label} downloaded.')
    return 0 if speakers_ok else 1


def download_speaker_models(store: SettingsStore) -> bool:
    """Speaker-label models (~40 MB), so the first interview also works offline."""
    if not store.get()['diarize_files']:
        return True
    from .speakers import SpeakerEngine
    engine = SpeakerEngine(store.home)
    if not engine.available() or engine.models_present():
        return True
    print('Downloading the speaker-label models (small, once)…')
    try:
        engine.ensure_models()
    except Exception as exc:
        print(f'Speaker models not downloaded now ({type(exc).__name__}); Santa retries when needed.')
        return False
    return True


def has_nvidia_gpu() -> bool:
    """Driver tool present, or CTranslate2 sees a CUDA device."""
    if sys.platform == 'darwin':
        return False
    if shutil.which('nvidia-smi'):
        return True
    from .cuda import gpu_count
    return gpu_count() > 0


def install_gpu_pack(force: bool = False) -> int:
    """pip-install the cuBLAS/cuDNN wheels into Santa's own Python (~1.2 GB)."""
    if not force and not has_nvidia_gpu():
        print('No NVIDIA graphics card found: Santa will use the processor (CPU).')
        return 0
    from . import cuda
    if cuda.cuda_status(refresh=True)['usable']:
        print('NVIDIA GPU pack already installed.')
        return 0
    print('NVIDIA graphics card found. Downloading the GPU pack (~1.2 GB, once)…')
    cmd = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
           '--no-warn-script-location', *GPU_PACK]
    if subprocess.call(cmd) != 0:
        print('GPU pack could not be installed. Santa will use the CPU (slower, but works).')
        return 1
    status = cuda.cuda_status(refresh=True)
    print('GPU pack installed: ' + status['reason'])
    return 0 if status['usable'] else 1


def stop_running_santa(port: int = 8765) -> int:
    """Ask a running Santa to quit (installer upgrade / uninstall)."""
    import urllib.request
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/shutdown', data=b'{}', method='POST',
                                     headers={'X-Santa': '1', 'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(request, timeout=3).close()
        print('Santa was running and has been stopped.')
    except Exception:
        print('Santa was not running.')
    return 0


def run(task: str, model: str = None) -> int:
    """Dispatch for `santa --setup <task>`."""
    os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
    if task == 'hiring-right':
        return setup_hiring_right()
    if task == 'model':
        return download_model(model)
    if task == 'gpu':
        return install_gpu_pack()
    if task == 'stop':
        return stop_running_santa()
    print(f'Unknown set-up step: {task}')
    return 2
