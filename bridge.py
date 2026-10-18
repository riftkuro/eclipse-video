import json
import os
import socket
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PySide6.QtCore import QObject, Signal, QTimer

PORT = 49185


class Request:
    def __init__(self, data):
        self.data = data
        self.done = threading.Event()
        self.result = None

    def finish(self, result):
        if not self.done.is_set():
            self.result = result
            self.done.set()


class Bridge(QObject):
    received = Signal(object)
    activated = Signal()

    def __init__(self, parent=None, port=PORT, version=''):
        super().__init__(parent)
        self.token = secrets.token_urlsafe(32)
        self.version = version
        self.server = None
        self.error = ''
        self.port = port
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data):
                body = json.dumps(data, allow_nan=False).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def local(self):
                return not self.headers.get('Origin') and self.client_address[0] == '127.0.0.1'

            def do_GET(self):
                if not self.local():
                    return self.reply(403, {'error': 'local app only'})
                if self.path != '/hello':
                    return self.reply(404, {'error': 'unknown route'})
                self.reply(200, {'app': 'Eclipse Video', 'version': bridge.version, 'protocol': 1, 'token': bridge.token, 'now': time.perf_counter()})

            def do_POST(self):
                if not self.local() or not secrets.compare_digest(self.headers.get('X-Eclipse-Token', ''), bridge.token):
                    return self.reply(403, {'error': 'connection required'})
                if self.path != '/command':
                    return self.reply(404, {'error': 'unknown route'})
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    return self.reply(415, {'error': 'json required'})
                try:
                    size = int(self.headers.get('Content-Length', 0))
                    if not 0 < size <= 65536:
                        return self.reply(413, {'error': 'request too large'})
                    self.connection.settimeout(3)
                    data = json.loads(self.rfile.read(size), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                    if not isinstance(data, dict):
                        raise ValueError('object required')
                except (ValueError, OSError):
                    return self.reply(400, {'error': 'invalid request'})
                if data.get('action') == 'activate':
                    bridge.activated.emit()
                    return self.reply(200, {'activated': True})
                request = Request(data)
                bridge.received.emit(request)
                if request.done.wait(4):
                    self.reply(200, dict(request.result, now=time.perf_counter()))
                else:
                    request.finish({'error': 'player busy'})
                    self.reply(503, request.result)

        self.handler = Handler
        self.retry = QTimer(self)
        self.retry.setInterval(2000)
        self.retry.timeout.connect(self.listen)
        self.listen()

    def listen(self):
        if self.server:
            return
        try:
            self.server = LocalServer(('127.0.0.1', self.port), self.handler)
        except OSError as error:
            self.error = f'Cannot connect on port {self.port}. Close another copy of Eclipse Video or the app using this port. Retrying automatically. ({error.errno})'
            self.retry.start()
            return
        self.error = ''
        self.retry.stop()
        threading.Thread(target=self.server.serve_forever, daemon=True, name='eclipse-video-local').start()

    def close(self):
        self.retry.stop()
        if self.server:
            server, self.server = self.server, None
            threading.Thread(target=lambda: (server.shutdown(), server.server_close()), daemon=True).start()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def activate_existing(port):
    import urllib.request
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f'http://127.0.0.1:{port}'
        with opener.open(url+'/hello', timeout=1) as response:
            hello = json.load(response)
        if hello.get('app') != 'Eclipse Video' or hello.get('protocol') != 1:
            return False
        request = urllib.request.Request(url+'/command', data=b'{"action":"activate"}', headers={
            'Content-Type': 'application/json', 'X-Eclipse-Token': hello['token']})
        with opener.open(request, timeout=1) as response:
            return json.load(response).get('activated') is True
    except (OSError, ValueError, KeyError):
        return False
