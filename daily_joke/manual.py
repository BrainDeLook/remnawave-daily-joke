import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading


class ManualControl:
    def __init__(self, dry_run, provider="local", model=""):
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.dry_run = dry_run
        self.provider = provider
        self.model = model
        self.ready = False
        self.requested = False
        self.busy = False
        self.message = "Загрузка модели. Дождитесь готовности."

    def request(self):
        with self.lock:
            if not self.ready or self.busy or self.requested:
                return False
            self.requested = True
            self.message = "Ручной запуск ожидает выполнения."
            return True

    def take(self, scheduled):
        with self.lock:
            if not self.ready or self.busy or not (self.requested or scheduled):
                return None
            source = "manual" if self.requested else "schedule"
            self.requested = False
            self.busy = True
            self.message = "Генерация анекдота… Это может занять несколько минут."
            return source

    def finish(self, message):
        with self.lock:
            self.busy = False
            self.message = message

    def snapshot(self):
        with self.lock:
            return {"dry_run": self.dry_run, "provider": self.provider, "model": self.model,
                    "disabled": not self.ready or self.busy or self.requested,
                    "message": self.message, "token": self.token}


class ManualServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, control):
        self.control = control
        self.page = Path(__file__).with_name("manual.html").read_bytes()
        super().__init__(address, ManualHandler)


class ManualHandler(BaseHTTPRequestHandler):
    def respond(self, code, body, content_type="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        # Home Assistant authenticates ingress users; accept only its proxy.
        if self.client_address[0] != "172.30.32.2":
            self.respond(403, b'{}')
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        if self.path == "/":
            self.respond(200, self.server.page, "text/html; charset=utf-8")
        elif self.path == "/status":
            self.respond(200, json.dumps(self.server.control.snapshot()).encode())
        else:
            self.respond(404, b'{}')

    def do_POST(self):
        if not self.allowed():
            return
        if self.path != "/run":
            self.respond(404, b'{}')
            return
        if self.headers.get("X-Run-Token") != self.server.control.token:
            self.respond(403, b'{}')
            return
        accepted = self.server.control.request()
        self.respond(202 if accepted else 409, b'{}')

    def log_message(self, *args):
        pass
