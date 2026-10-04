import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PySide6.QtCore import QObject, Signal
from companion_access import AccessGate, AccessError

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

    def __init__(self, parent=None, port=PORT, version=''):
        super().__init__(parent)
        self.token = secrets.token_urlsafe(32)
        self.version = version
        self.server = None
        self.access = AccessGate()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data):
                body = json.dumps(data, allow_nan=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def local(self):
                return not self.headers.get('Origin') and self.client_address[0] == '127.0.0.1'

            def do_GET(self):
                if not self.local():
                    return self.reply(403, {'error': 'local app only'})
                if self.path != '/hello':
                    return self.reply(404, {'error': 'unknown route'})
                self.reply(200, {'app': 'Eclipse Video', 'version': bridge.version, 'protocol': 1, 'authorization': 1, 'token': bridge.token, 'now': time.perf_counter()})

            def do_POST(self):
                if not self.local() or not secrets.compare_digest(self.headers.get('X-Eclipse-Token', ''), bridge.token):
                    return self.reply(403, {'error': 'connection required'})
                if self.path not in {'/authorize', '/command'}:
                    return self.reply(404, {'error': 'unknown route'})
                if self.path != '/authorize' and not bridge.access.allowed(self.headers.get('X-Eclipse-Access', '')):
                    return self.reply(403, {'error': 'Update and activate the official Eclipse plugin, then reconnect.'})
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
                except (ValueError, TimeoutError):
                    return self.reply(400, {'error': 'invalid request'})
                if self.path == '/authorize':
                    try:
                        return self.reply(200, bridge.access.authorize(data))
                    except AccessError as error:
                        return self.reply(403, {'error': str(error)})
                request = Request(data)
                bridge.received.emit(request)
                if request.done.wait(4):
                    self.reply(200, dict(request.result, now=time.perf_counter()))
                else:
                    request.finish({'error': 'player busy'})
                    self.reply(503, request.result)

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True, name='eclipse-video-local').start()

    def close(self):
        self.access.clear()
        if self.server:
            server, self.server = self.server, None
            threading.Thread(target=lambda: (server.shutdown(), server.server_close()), daemon=True).start()
