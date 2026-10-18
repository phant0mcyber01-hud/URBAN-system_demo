#!/usr/bin/env python3
"""A machine's screen in a browser: noVNC in front of QEMU's VNC display, with nothing but Python's library.

    python3 screen.py --listen 6080 --vnc 127.0.0.1:5900 --novnc /work/os/demo/novnc --title "centre"

Serves the page (/) and noVNC's own files (/core, /vendor) and carries WebSocket frames to and from the VNC
socket (/websockify), which is all websockify does for noVNC. Used by os/demo.mjs; not part of URBAN OS.
"""
import argparse
import base64
import hashlib
import http.server
import os
import socket
import socketserver
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>URBAN OS - %(title)s</title>
<style>html,body{margin:0;height:100%%;background:#0d0f12;color:#9aa;font:13px system-ui}#s{height:100%%}#m{position:fixed;top:6px;left:8px;background:#0d0f12cc;padding:2px 8px;border-radius:4px}</style>
</head><body><div id="s"></div><div id="m">%(title)s: connecting...</div>
<script type="module">
import RFB from './core/rfb.js';
const m = document.getElementById('m');
const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/websockify';
const rfb = new RFB(document.getElementById('s'), url, { wsProtocols: ['binary'] });
rfb.scaleViewport = true; rfb.resizeSession = false; rfb.viewOnly = false;
rfb.addEventListener('connect', () => { m.textContent = '%(title)s'; setTimeout(() => m.remove(), 2000); });
rfb.addEventListener('disconnect', () => { m.textContent = '%(title)s: disconnected - reload'; document.body.append(m); });
</script></body></html>"""


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "urban-screen/1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/websockify"):
            return self.websocket()
        if self.path in ("/", "/index.html", "/vnc.html") or self.path.startswith("/vnc.html?"):
            body = (PAGE % {"title": self.server.title}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/core/") or self.path.startswith("/vendor/"):
            return super().do_GET()  # noVNC's modules, from its directory
        self.send_error(404)

    def websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "upgrade" not in self.headers.get("Connection", "").lower():
            self.send_error(400, "not a websocket")
            return
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        if "binary" in self.headers.get("Sec-WebSocket-Protocol", ""):
            self.send_header("Sec-WebSocket-Protocol", "binary")
        self.end_headers()
        self.wfile.flush()
        vnc = socket.create_connection(self.server.vnc, timeout=10)
        vnc.settimeout(None)
        ws = self.connection
        ws.settimeout(None)
        done = threading.Event()

        def to_vnc():  # browser frames (masked) -> VNC bytes
            try:
                while not done.is_set():
                    head = self.rfile.read(2)
                    if len(head) < 2:
                        break
                    opcode, length = head[0] & 0x0F, head[1] & 0x7F
                    masked = head[1] & 0x80
                    if length == 126:
                        length = struct.unpack(">H", self.rfile.read(2))[0]
                    elif length == 127:
                        length = struct.unpack(">Q", self.rfile.read(8))[0]
                    mask = self.rfile.read(4) if masked else b""
                    data = self.rfile.read(length)
                    if opcode == 8:  # close
                        break
                    if opcode == 9:  # ping -> pong
                        ws.sendall(bytes([0x8A, len(data)]) + data)
                        continue
                    if opcode not in (1, 2):
                        continue
                    if mask:
                        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                    vnc.sendall(data)
            except (OSError, ValueError):
                pass
            finally:
                done.set()
                vnc.close()

        def to_browser():  # VNC bytes -> one binary frame each
            try:
                while not done.is_set():
                    data = vnc.recv(65536)
                    if not data:
                        break
                    n = len(data)
                    if n < 126:
                        head = bytes([0x82, n])
                    elif n < 65536:
                        head = bytes([0x82, 126]) + struct.pack(">H", n)
                    else:
                        head = bytes([0x82, 127]) + struct.pack(">Q", n)
                    ws.sendall(head + data)
            except OSError:
                pass
            finally:
                done.set()
                try:
                    ws.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        t = threading.Thread(target=to_browser, daemon=True)
        t.start()
        to_vnc()
        t.join(timeout=5)
        self.close_connection = True


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=6080)
    ap.add_argument("--vnc", default="127.0.0.1:5900")
    ap.add_argument("--novnc", required=True, help="noVNC's directory (core/ and vendor/ inside)")
    ap.add_argument("--title", default="URBAN OS")
    args = ap.parse_args()
    host, _, port = args.vnc.rpartition(":")
    os.chdir(args.novnc)
    srv = Server(("0.0.0.0", args.listen), Handler)
    srv.vnc, srv.title = (host or "127.0.0.1", int(port)), args.title
    srv.serve_forever()


if __name__ == "__main__":
    main()
