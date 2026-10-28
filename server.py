#!/usr/bin/env python3
"""Drum MIDI -> OBS browser-source bridge.

Reads note/CC events from the e-drum kit's ALSA sequencer port (via aseqdump,
so it coexists with Hydrogen) and pushes them to index.html over Server-Sent
Events. Stdlib only.

    python3 server.py            # serves http://127.0.0.1:7870
    DRUM_PORT_MATCH=nitro DRUM_VIZ_PORT=7870 python3 server.py
"""
import json
import os
import queue
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
HTTP_PORT = int(os.environ.get("DRUM_VIZ_PORT", "7870"))
PORT_MATCH = re.compile(os.environ.get("DRUM_PORT_MATCH", "nitro|alesis"), re.I)

NOTE_ON = re.compile(r"Note on\s+(\d+), note (\d+), velocity (\d+)")
CC = re.compile(r"Control change\s+(\d+), controller (\d+), value (\d+)")

clients: set[queue.Queue] = set()
clients_lock = threading.Lock()
status = {"type": "status", "connected": None, "device": None}  # None = not checked yet


def broadcast(event: dict) -> None:
    data = json.dumps(event)
    with clients_lock:
        for q in list(clients):
            try:
                q.put_nowait(data)
            except queue.Full:
                pass  # stalled client; drop rather than block MIDI


def set_status(connected: bool, device: str | None) -> None:
    if status["connected"] != connected or status["device"] != device:
        status.update(connected=connected, device=device)
        print(f"[drum-viz] {'connected: ' + device if connected else 'waiting for drum kit'}", flush=True)
        broadcast(status)


def find_port() -> tuple[str, str] | None:
    try:
        out = subprocess.run(["aseqdump", "-l"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines()[1:]:
        m = re.match(r"\s*(\d+:\d+)\s+(.*?)\s{2,}(.*)$", line)
        if m and PORT_MATCH.search(m.group(2) + " " + m.group(3)):
            return m.group(1), m.group(2).strip()
    return None


def watch_port(proc: subprocess.Popen, port: str) -> None:
    while proc.poll() is None:
        time.sleep(2)
        found = find_port()
        if not found or found[0] != port:
            proc.terminate()
            return


def midi_loop() -> None:
    while True:
        found = find_port()
        if not found:
            set_status(False, None)
            time.sleep(2)
            continue
        port, name = found
        # stdbuf: aseqdump block-buffers stdout when piped, which would add lag
        proc = subprocess.Popen(["stdbuf", "-oL", "aseqdump", "-p", port],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        set_status(True, name)
        # aseqdump keeps running when the kit is unplugged, so a replug would
        # leave it on a dead subscription. Kill it once the port is gone.
        threading.Thread(target=watch_port, args=(proc, port), daemon=True).start()
        for line in proc.stdout:
            if m := NOTE_ON.search(line):
                ch, note, vel = map(int, m.groups())
                if vel > 0:  # velocity-0 note-on is a note-off
                    broadcast({"type": "hit", "ch": ch, "note": note, "vel": vel})
            elif m := CC.search(line):
                ch, cc, val = map(int, m.groups())
                broadcast({"type": "cc", "ch": ch, "cc": cc, "value": val})
        proc.wait()
        set_status(False, None)
        time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/events":
            self.stream_events()
        else:
            self.send_error(404)

    def stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q: queue.Queue = queue.Queue(maxsize=256)
        with clients_lock:
            clients.add(q)
        try:
            self.wfile.write(f"data: {json.dumps(status)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    msg = f"data: {q.get(timeout=15)}\n\n"
                except queue.Empty:
                    msg = ": keepalive\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with clients_lock:
                clients.discard(q)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=midi_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    server.daemon_threads = True
    print(f"[drum-viz] http://127.0.0.1:{HTTP_PORT}", flush=True)
    server.serve_forever()
