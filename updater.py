import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, Signal

REPO = 'riftkuro/eclipse-video'
LATEST = f'https://api.github.com/repos/{REPO}/releases/latest'
HEADERS = {'User-Agent': 'Eclipse-Video', 'Accept': 'application/vnd.github+json'}
MAC = sys.platform == 'darwin'
WINDOWS = sys.platform == 'win32'


def numbers(tag):
    return tuple(int(part) for part in re.findall(r'\d+', tag or '')[:3]) or (0,)


def bundle():
    if not MAC or not getattr(sys, 'frozen', False):
        return None
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == '.app':
            return parent
    return None


def suits(name):
    name = name.lower()
    if WINDOWS:
        return name.endswith('.exe')
    if MAC:
        if not name.endswith('.zip') or 'mac' not in name:
            return False
        arm = platform.machine() == 'arm64'
        return ('arm64' in name) == arm
    return False


class Updater(QObject):
    found = Signal(dict)
    current = Signal()
    progress = Signal(int)
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, version, folder, parent=None):
        super().__init__(parent)
        self.version = version
        self.folder = Path(folder)
        self.release = None
        self.installer = None
        self.busy = False
        self.tidy()

    def tidy(self):
        for old in self.folder.glob('*'):
            if old.is_dir() or old.suffix == '.part' or numbers(old.stem) <= numbers(self.version):
                try:
                    shutil.rmtree(old) if old.is_dir() else old.unlink()
                except OSError:
                    pass

    def check(self):
        self.run(self.fetch)

    def download(self):
        if self.release:
            self.run(self.pull)

    def run(self, work):
        if self.busy:
            return
        self.busy = True

        def guarded():
            try:
                outcome = work()
            except Exception as error:
                message = str(error) or error.__class__.__name__
                outcome = lambda: self.failed.emit(message)
            self.busy = False
            outcome()

        threading.Thread(target=guarded, daemon=True, name='eclipse-video-updates').start()

    def fetch(self):
        request = urllib.request.Request(LATEST, headers=HEADERS)
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
        tag = data.get('tag_name', '')
        asset = next((item for item in data.get('assets', []) if suits(item.get('name', ''))), None)
        if asset and numbers(tag) > numbers(self.version) and (WINDOWS or bundle()):
            self.release = {'version': tag.lstrip('vV'), 'name': asset['name'], 'url': asset['browser_download_url'], 'size': asset.get('size', 0)}
            return lambda: self.found.emit(self.release)
        return self.current.emit

    def pull(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / self.release['name']
        partial = target.with_suffix('.part')
        request = urllib.request.Request(self.release['url'], headers={'User-Agent': HEADERS['User-Agent']})
        with urllib.request.urlopen(request, timeout=30) as response, open(partial, 'wb') as file:
            total = int(response.headers.get('Content-Length') or self.release['size'] or 0)
            done = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                file.write(chunk)
                done += len(chunk)
                if total:
                    self.progress.emit(min(99, done * 100 // total))
        partial.replace(target)
        self.installer = target
        self.progress.emit(100)
        return lambda: self.ready.emit(target)

    def install(self, relaunch):
        if not self.installer or not self.installer.is_file():
            return False
        if WINDOWS:
            command = f'start "" /wait "{self.installer}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS'
            if relaunch:
                launcher = '' if getattr(sys, 'frozen', False) else f' "{Path(sys.argv[0]).resolve()}"'
                command += f' & start "" "{sys.executable}"{launcher}'
            flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
            subprocess.Popen(f'cmd /d /s /c "{command}"', creationflags=flags, close_fds=True)
            return True
        app = bundle()
        if not app:
            return False
        return self.swap(app, relaunch)

    # the running bundle is replaced once this process has exited, then reopened
    def swap(self, app, relaunch):
        stage = self.folder / 'stage'
        script = f'''
while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.2; done
rm -rf {shlex.quote(str(stage))}
mkdir -p {shlex.quote(str(stage))}
ditto -x -k {shlex.quote(str(self.installer))} {shlex.quote(str(stage))} || exit 1
NEW=$(find {shlex.quote(str(stage))} -maxdepth 2 -name "*.app" -print -quit)
[ -d "$NEW" ] || exit 1
rm -rf {shlex.quote(str(app))}
mv "$NEW" {shlex.quote(str(app))} || ditto "$NEW" {shlex.quote(str(app))} || exit 1
xattr -dr com.apple.quarantine {shlex.quote(str(app))} 2>/dev/null
rm -rf {shlex.quote(str(stage))} {shlex.quote(str(self.installer))}
{'open -n ' + shlex.quote(str(app)) if relaunch else 'true'}
'''
        subprocess.Popen(['/bin/sh', '-c', script], start_new_session=True, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
