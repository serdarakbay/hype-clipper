"""Tiny standard-library Server-Sent Events server for the OBS browser overlay."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import threading
from urllib.parse import urlsplit


HOST = "localhost"
PORT = 8765
HTML_FILE = Path(__file__).with_name("overlay.html")
_lock = threading.Lock()
_clients = set()
_server = None
_last_status = None


def publish(event_dict):
    """Broadcast an event dictionary to all open /events streams."""
    global _last_status
    if event_dict.get("type") == "status":
        _last_status = dict(event_dict)
    payload = "data: " + json.dumps(event_dict, ensure_ascii=False) + "\n\n"
    with _lock:
        clients = tuple(_clients)
        for client in clients:
            if client.full():
                try:
                    client.get_nowait()
                except queue.Empty:
                    pass
            try:
                client.put_nowait(payload)
            except queue.Full:
                pass


class OverlayHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            try:
                body = HTML_FILE.read_bytes()
            except OSError:
                self.send_error(500, "overlay.html could not be read")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            client = queue.Queue(maxsize=100)
            with _lock:
                _clients.add(client)
                if _last_status is not None:
                    client.put_nowait("data: " + json.dumps(_last_status) + "\n\n")
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        payload = client.get(timeout=15)
                    except queue.Empty:
                        payload = ": keep-alive\n\n"
                    self.wfile.write(payload.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with _lock:
                    _clients.discard(client)
            return
        self.send_error(404)

    def log_message(self, _format, *_args):
        pass


def start_server():
    """Start the overlay server once, in a daemon thread."""
    global _server
    if _server is not None:
        return _server
    _server = ThreadingHTTPServer((HOST, PORT), OverlayHandler)
    _server.daemon_threads = True
    thread = threading.Thread(target=_server.serve_forever, name="obs-overlay", daemon=True)
    thread.start()
    return _server
