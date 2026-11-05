# tests/test_santa_windows.py
# Santa for Windows: checks that run on any OS (the Mac included), so Windows
# behaviour is covered without a Windows machine. They cover export byte parity with
# the Mac (golden files written by the Mac export code), Hiring Right folders and
# preset, Windows path names, the watch folder's "still being copied" guard,
# NVIDIA GPU detection and CPU fallback, and the installer's set-up steps.
# No ML libraries needed (faster_whisper is faked where a model is loaded).

import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src'))

from whisper_key.santa import cuda, export, hiring_right, watch  # noqa: E402
from whisper_key.santa.settings import SettingsStore  # noqa: E402

FIXTURES = REPO / 'tests' / 'fixtures' / 'santa_export'


# ── Export parity with the Mac ───────────────────────────────────────────────
class ExportParityTests(unittest.TestCase):
    """expected.json/.srt were written by the Mac export code (commit 5a4999f)
    from result.json. Whatever OS runs this must produce the same bytes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.result = json.loads((FIXTURES / 'result.json').read_text(encoding='utf-8'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_and_srt_are_byte_identical_to_mac(self):
        json_path, srt_path = export.write_transcript(self.tmp.name, '2026-11-30_KOL_G1.mp4', self.result)
        self.assertEqual(Path(json_path).read_bytes(), (FIXTURES / 'expected.json').read_bytes())
        self.assertEqual(Path(srt_path).read_bytes(), (FIXTURES / 'expected.srt').read_bytes())

    def test_no_windows_line_endings_or_bom(self):
        paths = export.write_transcript(self.tmp.name, 'clip.mov', self.result)
        for path in paths:
            data = Path(path).read_bytes()
            self.assertNotIn(b'\r\n', data)
            self.assertFalse(data.startswith(b'\xef\xbb\xbf'))
            data.decode('utf-8')                       # valid UTF-8, non-ASCII kept as is

    def test_hiring_right_contract_fields(self):
        # SANTA_INTEGRATION.md: {source, model, engine, language, audio_seconds, segments:[{start,end,text,speaker?}]}
        json_path, _ = export.write_transcript(self.tmp.name, 'G1.m4a', self.result)
        payload = json.loads(Path(json_path).read_text(encoding='utf-8'))
        for key in ('source', 'model', 'engine', 'language', 'audio_seconds', 'segments'):
            self.assertIn(key, payload)
        self.assertEqual(payload['source'], 'G1.m4a')
        for seg in payload['segments']:
            self.assertTrue({'start', 'end', 'text'} <= set(seg) <= {'start', 'end', 'text', 'speaker'})
            self.assertIsInstance(seg['start'], float)

    def test_two_hour_timestamps(self):
        _, srt_path = export.write_transcript(self.tmp.name, 'long.mp4', self.result)
        self.assertIn('01:59:59,000 --> 02:00:01,130', Path(srt_path).read_text(encoding='utf-8'))


# ── File names and paths ─────────────────────────────────────────────────────
class WindowsPathTests(unittest.TestCase):
    def test_stems_of_typical_interview_files(self):
        cases = {
            '2026-11-30_KOL_G1.mp4': '2026-11-30_KOL_G1',
            '2026-11-30 KOL G1.MOV': '2026-11-30 KOL G1',
            'Interview (1).m4a': 'Interview (1)',
            'Sprinters.Kolkata.G2.wav': 'Sprinters.Kolkata.G2',
            'রাহুল.mp4': 'রাহুল',
        }
        for name, stem in cases.items():
            self.assertEqual(export.export_stem(name), stem)

    def test_backslashes_never_reach_the_file_name(self):
        # On a Mac a Windows path is one long name; either way no separator survives.
        stem = export.export_stem('C:\\Users\\asha\\Videos\\G3.mp4')
        self.assertNotIn('\\', stem)
        self.assertNotIn('/', stem)
        self.assertTrue(stem.endswith('G3'))

    def test_folder_with_spaces_and_tilde(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'HOME': home, 'USERPROFILE': home}):
            folder = export.resolve_dir('~/Documents/Hiring Right/03_Transcripts')
            self.assertEqual(folder, os.path.join(home, 'Documents', 'Hiring Right', '03_Transcripts'))
            paths = export.write_transcript(folder, 'G1.mp4', {'segments': [{'start': 0, 'end': 1, 'text': 'hi'}]})
            self.assertTrue(all(os.path.exists(p) for p in paths))

    def test_already_exported_compares_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = os.path.join(tmp, 'G1.mp4')
            Path(video).write_bytes(b'x')
            out = os.path.join(tmp, 'out')
            self.assertFalse(export.already_exported(out, video))
            export.write_transcript(out, 'G1.mp4', {'segments': [{'start': 0, 'end': 1, 'text': 'hi'}]})
            self.assertTrue(export.already_exported(out, video))
            later = time.time() + 60                        # video replaced by a newer copy
            os.utime(video, (later, later))
            self.assertFalse(export.already_exported(out, video))


# ── Hiring Right folders + preset ────────────────────────────────────────────
class HiringRightTests(unittest.TestCase):
    def test_documents_dir_uses_windows_known_folder(self):
        onedrive = 'C:\\Users\\asha\\OneDrive - Kout\\Documents'
        with mock.patch.object(hiring_right, '_windows_documents_dir', return_value=onedrive):
            self.assertEqual(hiring_right.documents_dir('win32'), onedrive)

    def test_documents_dir_falls_back_to_home(self):
        with mock.patch.object(hiring_right, '_windows_documents_dir', return_value=None):
            self.assertEqual(hiring_right.documents_dir('win32'),
                             os.path.join(os.path.expanduser('~'), 'Documents'))
        self.assertEqual(hiring_right.documents_dir('darwin'), os.path.join(os.path.expanduser('~'), 'Documents'))

    def test_folder_names_match_the_mac(self):
        dirs = hiring_right.hiring_right_dirs('/docs')
        self.assertEqual(dirs['videos'], os.path.join('/docs', 'Hiring Right', '02_Batch_Videos'))
        self.assertEqual(dirs['transcripts'], os.path.join('/docs', 'Hiring Right', '03_Transcripts'))

    def test_model_follows_memory(self):
        self.assertEqual(hiring_right.recommended_model(16), 'large-v3-turbo')
        self.assertEqual(hiring_right.recommended_model(8), 'large-v3-turbo')
        self.assertEqual(hiring_right.recommended_model(7.7), 'small')   # "8 GB" laptops report ~7.7
        self.assertEqual(hiring_right.recommended_model(0), 'large-v3-turbo')   # unknown

    def test_apply_creates_folders_and_saves_valid_settings(self):
        with tempfile.TemporaryDirectory() as docs, tempfile.TemporaryDirectory() as home:
            store = SettingsStore(home)
            done = hiring_right.apply(store, documents=docs, ram_gb=16)
            self.assertTrue(os.path.isdir(done['dirs']['videos']))
            self.assertTrue(os.path.isdir(done['dirs']['transcripts']))
            saved = SettingsStore(home).get()                # survives a restart
            self.assertTrue(saved['watch_enabled'] and saved['export_enabled'])
            self.assertEqual(saved['watch_dir'], done['dirs']['videos'])
            self.assertEqual(saved['export_dir'], done['dirs']['transcripts'])
            self.assertEqual((saved['language'], saved['watch_language']), ('en', 'en'))
            self.assertEqual((saved['max_duration_min'], saved['max_upload_mb']), (120, 2000))
            self.assertEqual(saved['model'], 'large-v3-turbo')
            # every preset value passed validation unchanged
            for key, value in hiring_right.preset_settings(done['dirs'], 16).items():
                self.assertEqual(saved[key], value, key)

    def test_setup_step_prints_folders(self):
        from whisper_key.santa import setup_tasks
        with tempfile.TemporaryDirectory() as docs, tempfile.TemporaryDirectory() as home, \
                mock.patch.object(hiring_right, 'documents_dir', return_value=docs), \
                mock.patch('builtins.print') as fake_print:
            self.assertEqual(setup_tasks.setup_hiring_right(SettingsStore(home)), 0)
            printed = ' '.join(str(c.args[0]) for c in fake_print.call_args_list)
            self.assertIn('02_Batch_Videos', printed)


# ── Watch folder on Windows ──────────────────────────────────────────────────
class _FakeJob:
    def __init__(self, n):
        self.id, self.state = f'job{n}', 'queued'


class _FakeService:
    def __init__(self, home, folder):
        self.settings = SettingsStore(home)
        self.settings.update({'watch_enabled': True, 'watch_dir': folder,
                              'export_enabled': True, 'export_dir': os.path.join(folder, 'out')})
        self.submitted = []

    def submit_file(self, path, name, language, source, delete_after):
        self.submitted.append(name)
        return _FakeJob(len(self.submitted))

    def get_job(self, job_id):
        return None


class WatchFolderWindowsTests(unittest.TestCase):
    def test_lock_check_only_on_windows(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            path = fh.name
        try:
            self.assertFalse(watch.still_being_written(path, platform='darwin'))
            self.assertFalse(watch.still_being_written(path, platform='win32'))   # nobody holds it
            with mock.patch('builtins.open', side_effect=PermissionError(13, 'sharing violation')):
                self.assertTrue(watch.still_being_written(path, platform='win32'))
                self.assertFalse(watch.still_being_written(path, platform='darwin'))
        finally:
            os.remove(path)

    def test_read_only_file_counts_as_finished(self):
        with mock.patch('os.access', return_value=False), \
                mock.patch('builtins.open', side_effect=PermissionError):
            self.assertFalse(watch.still_being_written('x.mp4', platform='win32'))

    def test_video_still_copying_is_not_taken(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as home:
            video = os.path.join(folder, '2026-11-30_KOL_G1.mp4')
            Path(video).write_bytes(b'\0' * 1024)              # Explorer pre-sizes the file
            old = time.time() - 60
            os.utime(video, (old, old))
            svc = _FakeService(home, folder)
            watcher = watch.WatchFolder(svc, settle_seconds=0)
            with mock.patch.object(watch, 'still_being_written', return_value=True):
                watcher.scan_once()                            # first sight
                self.assertEqual(watcher.scan_once(), [])      # settled, but still locked
                self.assertEqual(svc.submitted, [])
            queued = watcher.scan_once()                       # copy finished (settle time 0 here)
            self.assertEqual([j.id for j in queued], ['job1'])
            self.assertEqual(watcher.scan_once(), [])          # taken once only
            self.assertEqual(svc.submitted, ['2026-11-30_KOL_G1.mp4'])


# ── NVIDIA GPU ───────────────────────────────────────────────────────────────
class CudaTests(unittest.TestCase):
    def setUp(self):
        cuda._cached = None

    def tearDown(self):
        cuda._cached = None

    def test_missing_libraries(self):
        present = {'cublas64_12.dll', 'cublasLt64_12.dll', 'cudnn64_9.dll'}

        def loader(name):
            if name not in present:
                raise OSError(name)

        self.assertEqual(cuda.missing_libraries('win32', loader), ['cudnn_ops64_9.dll', 'cudnn_cnn64_9.dll'])
        present.update(cuda.REQUIRED_LIBS['win32'])
        self.assertEqual(cuda.missing_libraries('win32', loader), [])
        self.assertTrue(cuda.missing_libraries('darwin', loader))

    def test_finds_gpu_pack_folders(self):
        with tempfile.TemporaryDirectory() as site:
            for pkg in ('cublas', 'cudnn'):
                os.makedirs(os.path.join(site, 'nvidia', pkg, 'bin'))
            os.makedirs(os.path.join(site, 'nvidia', 'cudnn', 'include'))
            dirs = cuda.nvidia_wheel_dirs([site, '', '/nonexistent'])
            self.assertEqual(sorted(os.path.relpath(d, site) for d in dirs),
                             [os.path.join('nvidia', 'cublas', 'bin'), os.path.join('nvidia', 'cudnn', 'bin')])
            with mock.patch.dict(os.environ, {'PATH': 'C:\\Windows'}):
                cuda.register_library_dirs([site], platform='win32')
                cuda.register_library_dirs([site], platform='win32')      # idempotent
                parts = os.environ['PATH'].split(os.pathsep)
                self.assertEqual(sum(p.endswith(os.path.join('cudnn', 'bin')) for p in parts), 1)

    def test_status_decisions(self):
        with mock.patch.object(sys, 'platform', 'win32'), \
                mock.patch.object(cuda, 'register_library_dirs'), \
                mock.patch.object(cuda, 'gpu_count', return_value=0):
            self.assertFalse(cuda.cuda_status(refresh=True)['usable'])
        with mock.patch.object(sys, 'platform', 'win32'), \
                mock.patch.object(cuda, 'register_library_dirs'), \
                mock.patch.object(cuda, 'gpu_count', return_value=1), \
                mock.patch.object(cuda, 'missing_libraries', return_value=['cudnn_ops64_9.dll']):
            status = cuda.cuda_status(refresh=True)
            self.assertFalse(status['usable'])
            self.assertIn('GPU pack', status['reason'])
        with mock.patch.object(sys, 'platform', 'win32'), \
                mock.patch.object(cuda, 'register_library_dirs'), \
                mock.patch.object(cuda, 'gpu_count', return_value=1), \
                mock.patch.object(cuda, 'missing_libraries', return_value=[]):
            self.assertTrue(cuda.cuda_status(refresh=True)['usable'])
        with mock.patch.object(sys, 'platform', 'darwin'):
            self.assertFalse(cuda.cuda_status(refresh=True)['usable'])

    def test_compute_types(self):
        self.assertEqual(cuda.gpu_compute_type('int8'), 'int8_float16')
        self.assertEqual(cuda.gpu_compute_type('int8_float32'), 'int8_float16')
        self.assertEqual(cuda.gpu_compute_type('float32'), 'float16')


class _FakeWhisperModel:
    """Stands in for faster_whisper.WhisperModel; records where it was loaded."""
    loads = []
    fail_on_cuda = False

    def __init__(self, path, device='cpu', compute_type='int8', **kw):
        _FakeWhisperModel.loads.append((device, compute_type))
        if device == 'cuda' and _FakeWhisperModel.fail_on_cuda:
            raise RuntimeError('CUDA failed with error out of memory')
        self.device = device

    def transcribe(self, *a, **k):
        return iter([]), None


class ModelDeviceTests(unittest.TestCase):
    def setUp(self):
        _FakeWhisperModel.loads, _FakeWhisperModel.fail_on_cuda = [], False
        fake = types.ModuleType('faster_whisper')
        fake.WhisperModel = _FakeWhisperModel
        self.modules = mock.patch.dict(sys.modules, {'faster_whisper': fake})
        self.modules.start()

    def tearDown(self):
        self.modules.stop()

    def _manager(self, allow_gpu=True):
        from whisper_key.santa.models import ModelManager
        return ModelManager('int8', 0, allow_gpu=allow_gpu)

    def test_gpu_used_when_usable(self):
        mm = self._manager()
        with mock.patch.object(cuda, 'cuda_status', return_value={'usable': True}):
            model, device, compute = mm._load_on_best_device('/m', 4)
        self.assertEqual((device, compute), ('cuda', 'int8_float16'))

    def test_gpu_failure_falls_back_to_cpu(self):
        _FakeWhisperModel.fail_on_cuda = True
        mm = self._manager()
        with mock.patch.object(cuda, 'cuda_status', return_value={'usable': True}):
            model, device, compute = mm._load_on_best_device('/m', 4)
        self.assertEqual((device, compute), ('cpu', 'int8'))
        self.assertEqual([d for d, _ in _FakeWhisperModel.loads], ['cuda', 'cpu'])

    def test_cpu_only_setting_never_touches_gpu(self):
        mm = self._manager(allow_gpu=False)
        with mock.patch.object(cuda, 'cuda_status', side_effect=AssertionError('not asked')):
            _, device, _ = mm._load_on_best_device('/m', 4)
        self.assertEqual(device, 'cpu')
        self.assertEqual(mm.engine_label(), 'faster-whisper · CPU')
        mm.device = 'cuda'
        self.assertEqual(mm.engine_label(), 'faster-whisper · NVIDIA GPU')

    def test_engine_setting_change_reloads(self):
        mm = self._manager()
        mm._model, mm._model_key = object(), 'small'
        with mock.patch('threading.Thread') as thread:
            mm.ensure_loading('small', 'int8', 0, allow_gpu=True)      # same config: nothing to do
            thread.assert_not_called()
            mm.ensure_loading('small', 'int8', 0, allow_gpu=False)     # CPU only: reload
            thread.assert_called_once()


class KeepAwakeTests(unittest.TestCase):
    def test_windows_sets_and_clears_the_flag(self):
        from whisper_key.santa import keep_awake as ka
        calls = []
        fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(
            kernel32=types.SimpleNamespace(SetThreadExecutionState=calls.append)))
        with mock.patch.dict(sys.modules, {'ctypes': fake_ctypes}):
            with ka.keep_awake('win32'):
                self.assertEqual(calls, [ka.ES_CONTINUOUS | ka.ES_SYSTEM_REQUIRED])
        self.assertEqual(calls[-1], ka.ES_CONTINUOUS)

    def test_noop_elsewhere(self):
        from whisper_key.santa.keep_awake import keep_awake
        with keep_awake('darwin'):
            pass


class TrayTests(unittest.TestCase):
    def test_menu_and_quit(self):
        from whisper_key.santa import tray
        made = {}

        class FakeItem:
            def __init__(self, text, action, default=False, visible=True):
                self.text, self.action, self.visible = text, action, visible

        class FakeIcon:
            def __init__(self, name, image, title, menu):
                made.update(icon=self, menu=menu)
                self.stopped = False

            def run(self):
                pass

            def stop(self):
                self.stopped = True

        fake = types.SimpleNamespace(MenuItem=FakeItem, Icon=FakeIcon,
                                     Menu=type('Menu', (), {'SEPARATOR': None,
                                                           '__init__': lambda self, *items: setattr(self, 'items', items)}))
        quits = []
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(sys.modules, {'pystray': fake}), \
                mock.patch.object(tray, '_icon_image', return_value=None):
            store = SettingsStore(home)
            self.assertTrue(tray.start_tray('http://127.0.0.1:8765/', store, lambda: quits.append(1)))
            items = [i for i in made['menu'].items if i is not None]
            self.assertEqual([i.text for i in items], ['Open Santa', 'Videos folder (watched)',
                                                      'Transcripts folder', 'Quit Santa'])
            self.assertFalse(items[2].visible(items[2]))          # no export folder set yet
            store.update({'export_dir': home})
            self.assertTrue(items[2].visible(items[2]))
            items[-1].action(made['icon'], items[-1])
            self.assertEqual(quits, [1])
            self.assertTrue(made['icon'].stopped)

    def test_missing_pystray_is_not_fatal(self):
        from whisper_key.santa import tray
        with mock.patch.dict(sys.modules, {'pystray': None}), tempfile.TemporaryDirectory() as home:
            self.assertFalse(tray.start_tray('http://x/', SettingsStore(home), lambda: None))


class MemoryTests(unittest.TestCase):
    def test_total_ram_known_here(self):
        from whisper_key.santa.models import total_ram_gb
        self.assertGreater(total_ram_gb(), 0.5)


# ── Installer set-up steps ───────────────────────────────────────────────────
class SetupTaskTests(unittest.TestCase):
    def test_gpu_step_skips_without_nvidia(self):
        from whisper_key.santa import setup_tasks
        with mock.patch.object(setup_tasks, 'has_nvidia_gpu', return_value=False), \
                mock.patch('subprocess.call', side_effect=AssertionError('no pip')), \
                mock.patch('builtins.print'):
            self.assertEqual(setup_tasks.install_gpu_pack(), 0)

    def test_stop_when_not_running_is_fine(self):
        from whisper_key.santa import setup_tasks
        with mock.patch('builtins.print'):
            self.assertEqual(setup_tasks.stop_running_santa(port=1), 0)

    def test_cli_accepts_setup_steps(self):
        from whisper_key.santa import cli
        with mock.patch('whisper_key.santa.setup_tasks.run', return_value=0) as run:
            self.assertEqual(cli.main(['--setup', 'model', '--model', 'small']), 0)
            run.assert_called_once_with('model', 'small')


if __name__ == '__main__':
    unittest.main()
