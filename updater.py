import json
import re
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, Signal

REPO = 'riftkuro/eclipse-video'
LATEST = f'https://api.github.com/repos/{REPO}/releases/latest'
HEADERS = {'User-Agent': 'Eclipse-Video', 'Accept': 'application/vnd.github+json'}


def numbers(tag):
    return tuple(int(part) for part in re.findall(r'\d+', tag or '')[:3]) or (0,)


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
            if old.suffix == '.part' or numbers(old.stem) <= numbers(self.version):
                try:
                    old.unlink()
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
        asset = next((item for item in data.get('assets', []) if item.get('name', '').lower().endswith('.exe')), None)
        if asset and numbers(tag) > numbers(self.version):
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
        command = f'start "" /wait "{self.installer}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS'
        if relaunch:
            launcher = Path(sys.argv[0]).resolve()
            command += f' & start "" "{sys.executable}" "{launcher}"'
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        subprocess.Popen(['cmd', '/c', command], creationflags=flags, close_fds=True)
        return True
