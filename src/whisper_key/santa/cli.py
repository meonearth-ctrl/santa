# santa/cli.py
# `santa` command: start the local server (127.0.0.1 only), preload the model in
# the background, and open the UI in the default browser. If Santa is already
# running on the port, just open the browser instead of starting a second copy.

import argparse
import ipaddress
import json
import logging
import logging.handlers
import os
import sys
import urllib.request
import webbrowser

from . import __version__, APP_NAME
from .server import DEFAULT_HOST, DEFAULT_PORT, make_server
from .service import TranscriptionService
from .settings import SettingsStore, santa_home


def _setup_logging(home: str, verbose: bool) -> str:
    log_dir = os.path.join(home, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, 'santa.log')
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2,
                                                   encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter('%(message)s'))
    root.addHandler(console)
    # Third-party libraries can be chatty (and faster_whisper logs nothing sensitive
    # at WARNING); keep them quiet.
    for noisy in ('faster_whisper', 'huggingface_hub', 'urllib3', 'httpx'):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


def _is_loopback(host: str) -> bool:
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _already_running(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f'http://{host}:{port}/api/status', timeout=1.5) as resp:
            return json.loads(resp.read().decode('utf-8')).get('app') == APP_NAME
    except Exception:
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='santa', description='Santa — local multilingual dictation')
    parser.add_argument('--host', default=DEFAULT_HOST)
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--no-browser', action='store_true', help="don't open the browser")
    parser.add_argument('--model', help='use this model (saved to settings)')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--version', action='store_true')
    args = parser.parse_args(argv)

    if args.version:
        print(f'{APP_NAME} {__version__}')
        return 0
    if not _is_loopback(args.host):
        print('The Santa window only listens on this computer (127.0.0.1). For your phone, '
              'switch on Settings › iPhone access (HTTPS + pairing) instead.')
        return 2

    url = f'http://{"127.0.0.1" if args.host != "localhost" else "localhost"}:{args.port}/'
    if _already_running(args.host, args.port):
        print(f'{APP_NAME} is already running — opening {url}')
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    home = santa_home()
    log_path = _setup_logging(home, args.verbose)
    settings = SettingsStore(home)
    if args.model:
        settings.update({'model': args.model})
    from .accel_cpp import MetalAccelerator
    service = TranscriptionService(settings=settings,
                                   accelerator=MetalAccelerator(os.path.join(home, 'logs')))
    try:
        httpd = make_server(args.host, args.port, service)
    except OSError as exc:
        print(f'Could not start on port {args.port}: {exc}. Is another app using it? '
              'Try: santa --port 8766')
        return 1

    logging.getLogger(__name__).info('%s %s listening on %s (log: %s)', APP_NAME, __version__, url, log_path)
    if settings.get()['preload_model']:
        service.preload()
    from .watch import WatchFolder
    service.watcher = WatchFolder(service)      # idle unless enabled in Settings
    service.watcher.start()
    from .phone_access import PhoneAccess
    service.phone = PhoneAccess(service, home)  # iPhone access: off unless enabled in Settings
    service.phone.apply_settings(settings.get())
    if service.phone.running:
        print(f'    iPhone access is on: {service.phone.url()}')
    print(f'\n🎅  {APP_NAME} is running at {url}\n    Close this window or use "Quit Santa" in the app to stop.\n')
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        service.phone.stop()
        httpd.server_close()
    print(f'{APP_NAME} stopped.')
    # Exit without running native destructors: onnxruntime/ctranslate2 worker
    # threads can abort ("recursive_mutex lock failed") while Python tears down,
    # which macOS would report as a crash. Everything is already saved.
    logging.shutdown()
    sys.stdout.flush()
    os._exit(0)


if __name__ == '__main__':
    sys.exit(main())
