#!/usr/bin/env python3
"""Drum MIDI -> OBS browser-source bridge.

Reads note/CC events from the e-drum kit's ALSA sequencer port (via aseqdump,
so it coexists with Hydrogen) and pushes them to index.html over Server-Sent
Events. Every hit is also logged to SQLite so session and all-time stats
survive restarts. Stdlib only.

    python3 server.py            # serves http://127.0.0.1:7870
    DRUM_PORT_MATCH=nitro DRUM_VIZ_PORT=7870 python3 server.py
"""
import collections
import json
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
HTTP_PORT = int(os.environ.get("DRUM_VIZ_PORT", "7870"))
PORT_MATCH = re.compile(os.environ.get("DRUM_PORT_MATCH", "nitro|alesis"), re.I)
DB_PATH = Path(os.environ.get("DRUM_VIZ_DB") or Path.home() / ".local/share/drum-viz/drums.db").expanduser()
SESSION_GAP = float(os.environ.get("DRUM_SESSION_GAP", "600"))  # idle seconds that end a session
FAKE = os.environ.get("DRUM_VIZ_FAKE")  # testing: read aseqdump-style lines from this FIFO instead of the kit
APM_WINDOW = 60  # seconds; a session's peak APM = most hits inside any 60 s window

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


# ---- Persistent log ----
# Raw hits are the source of truth; sessions/session_notes are running
# aggregates kept in the same transactions, so they never disagree with hits.
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY, start REAL NOT NULL, "end" REAL NOT NULL,  -- end = last hit
    hits INTEGER NOT NULL DEFAULT 0, peak_apm INTEGER);
CREATE TABLE IF NOT EXISTS hits (ts REAL NOT NULL, session_id INTEGER NOT NULL,
                                 note INTEGER NOT NULL, vel INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS hits_session ON hits (session_id, ts);
CREATE TABLE IF NOT EXISTS session_notes (session_id INTEGER NOT NULL, note INTEGER NOT NULL,
                                          hits INTEGER NOT NULL, PRIMARY KEY (session_id, note)) WITHOUT ROWID;
"""
PEAK_SQL = f"""SELECT max(c) FROM (SELECT count(*) OVER (ORDER BY ts RANGE BETWEEN {APM_WINDOW} PRECEDING
               AND CURRENT ROW) AS c FROM hits WHERE session_id = ?)"""

writes: queue.Queue = queue.Queue()  # unbounded: put() never blocks the MIDI loop
state_lock = threading.Lock()
cur: dict | None = None     # live session: id, start, last, hits, notes, peak, window
fresh = True                # next hit starts a new session (kit just (re)connected)
next_id = 1
alltime = {"hits": 0, "notes": {}, "sessions": 0, "seconds": 0.0}  # excludes cur's duration


def db_connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def open_db() -> None:
    """Create/repair the DB and load all-time totals into memory."""
    global next_id
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = db_connect()
    db.executescript(SCHEMA)
    with db:
        # Close whatever session was live when we last stopped: recompute it from
        # its raw hits (drops it if none made it to disk), and fill any missing peaks.
        last = db.execute("SELECT max(id) FROM sessions").fetchone()[0]
        if last is not None:
            n, end = db.execute("SELECT count(*), max(ts) FROM hits WHERE session_id = ?", (last,)).fetchone()
            db.execute("DELETE FROM session_notes WHERE session_id = ?", (last,))
            if n:
                db.execute('UPDATE sessions SET "end" = ?, hits = ? WHERE id = ?', (end, n, last))
                db.execute("""INSERT INTO session_notes SELECT session_id, note, count(*) FROM hits
                              WHERE session_id = ? GROUP BY note""", (last,))
                db.execute("UPDATE sessions SET peak_apm = NULL WHERE id = ?", (last,))
            else:
                db.execute("DELETE FROM sessions WHERE id = ?", (last,))
        for (sid,) in db.execute("SELECT id FROM sessions WHERE peak_apm IS NULL").fetchall():
            db.execute("UPDATE sessions SET peak_apm = (" + PEAK_SQL + ") WHERE id = ?", (sid, sid))
    hits, count, secs, top = db.execute(
        'SELECT coalesce(sum(hits), 0), count(*), coalesce(sum("end" - start), 0), max(id) FROM sessions').fetchone()
    alltime.update(hits=hits, sessions=count, seconds=secs,
                   notes=dict(db.execute("SELECT note, sum(hits) FROM session_notes GROUP BY note")))
    next_id = (top or 0) + 1
    db.close()
    print(f"[drum-viz] log {DB_PATH}: {count} sessions, {hits} hits", flush=True)


def flush(db: sqlite3.Connection, batch: list) -> None:
    hits, touched, notes = [], {}, collections.Counter()
    with db:
        for item in batch:
            if item[0] == "session":
                _, sid, start = item
                db.execute('INSERT INTO sessions (id, start, "end", hits) VALUES (?, ?, ?, 0)', (sid, start, start))
            else:
                _, ts, sid, note, vel, peak = item
                hits.append((ts, sid, note, vel))
                t = touched.setdefault(sid, [ts, 0, peak])
                t[0], t[1], t[2] = ts, t[1] + 1, peak
                notes[sid, note] += 1
        db.executemany("INSERT INTO hits VALUES (?, ?, ?, ?)", hits)
        db.executemany('UPDATE sessions SET "end" = ?, hits = hits + ?, peak_apm = ? WHERE id = ?',
                       [(end, n, peak, sid) for sid, (end, n, peak) in touched.items()])
        db.executemany("""INSERT INTO session_notes VALUES (?, ?, ?)
                          ON CONFLICT (session_id, note) DO UPDATE SET hits = hits + excluded.hits""",
                       [(sid, note, n) for (sid, note), n in notes.items()])


def writer_loop() -> None:
    """Drain the write queue into SQLite, one transaction per ~1 s batch."""
    db = db_connect()
    while True:
        batch, stop = [writes.get()], False
        deadline = time.monotonic() + 1
        while batch[-1] is not None and (left := deadline - time.monotonic()) > 0:
            try:
                batch.append(writes.get(timeout=left))
            except queue.Empty:
                break
        if batch[-1] is None:  # shutdown sentinel
            batch.pop()
            stop = True
        if batch:
            try:
                flush(db, batch)
            except sqlite3.Error as e:
                print(f"[drum-viz] db write failed, dropped {len(batch)} events: {e}", flush=True)
        if stop:
            db.close()
            return


def record_hit(note: int, vel: int) -> list[dict]:
    """Update the live session + totals, queue the DB write; returns events to broadcast."""
    global cur, fresh, next_id
    ts, events = time.time(), []
    with state_lock:
        if fresh or cur is None or ts - cur["last"] > SESSION_GAP:
            if cur:
                alltime["seconds"] += cur["last"] - cur["start"]
            cur = {"id": next_id, "start": ts, "last": ts, "hits": 0, "notes": {}, "peak": 0,
                   "window": collections.deque()}
            next_id += 1
            fresh = False
            alltime["sessions"] += 1
            writes.put(("session", cur["id"], ts))
            events.append({"type": "session", "id": cur["id"], "start": ts})
        cur["last"] = ts
        cur["hits"] += 1
        cur["notes"][note] = cur["notes"].get(note, 0) + 1
        w = cur["window"]
        w.append(ts)
        while ts - w[0] > APM_WINDOW:
            w.popleft()
        cur["peak"] = max(cur["peak"], len(w))
        alltime["hits"] += 1
        alltime["notes"][note] = alltime["notes"].get(note, 0) + 1
        writes.put(("hit", ts, cur["id"], note, vel, cur["peak"]))
        events.append({"type": "hit", "note": note, "vel": vel, "session": cur["id"], "n": cur["hits"]})
    return events


def session_row(s: dict) -> dict:
    return {"id": s["id"], "start": s["start"], "end": s["last"], "hits": s["hits"],
            "duration": s["last"] - s["start"], "peak_apm": s["peak"], "notes": dict(s["notes"])}


def stats() -> dict:
    now = time.time()
    with state_lock:
        live = session_row(cur) if cur else None
        active = live is not None and not fresh and now - live["end"] <= SESSION_GAP
        total = {"hits": alltime["hits"], "notes": dict(alltime["notes"]), "sessions": alltime["sessions"],
                 "seconds": alltime["seconds"] + (live["duration"] if live else 0)}
    db = sqlite3.connect(DB_PATH, timeout=10)
    try:
        rows = db.execute('SELECT id, start, "end", hits, peak_apm FROM sessions ORDER BY id DESC LIMIT 100').fetchall()
        sessions = [{"id": i, "start": s, "end": e, "hits": h, "duration": e - s, "peak_apm": p, "notes": {}}
                    for i, s, e, h, p in rows]
        by_id = {s["id"]: s for s in sessions}
        if sessions:
            for sid, note, n in db.execute("SELECT session_id, note, hits FROM session_notes WHERE session_id >= ?",
                                           (sessions[-1]["id"],)):
                if sid in by_id:
                    by_id[sid]["notes"][note] = n
        days = [{"date": d, "hits": h, "seconds": s} for d, h, s in db.execute(
            """SELECT date(start, 'unixepoch', 'localtime') AS d, sum(hits), sum("end" - start) FROM sessions
               WHERE start >= ? GROUP BY d ORDER BY d""", (now - 70 * 86400,))]
    finally:
        db.close()
    if live:  # the DB lags the live session by up to a batch; prefer the in-memory copy
        if live["id"] in by_id:
            sessions[sessions.index(by_id[live["id"]])] = live
        else:
            sessions = [live] + sessions[:99]
    return {"now": now, "session_gap": SESSION_GAP, "current": live if active else None,
            "alltime": total, "sessions": sessions, "days": days}


# ---- MIDI input ----
def set_status(connected: bool, device: str | None) -> None:
    global fresh
    if connected:
        fresh = True  # any (re)connect starts a new session on the next hit
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


def read_events(lines) -> None:
    for line in lines:
        if m := NOTE_ON.search(line):
            ch, note, vel = map(int, m.groups())
            if vel > 0:  # velocity-0 note-on is a note-off
                for ev in record_hit(note, vel):
                    broadcast(ev | {"ch": ch} if ev["type"] == "hit" else ev)
        elif m := CC.search(line):
            ch, cc, val = map(int, m.groups())
            broadcast({"type": "cc", "ch": ch, "cc": cc, "value": val})


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
        read_events(proc.stdout)
        proc.wait()
        set_status(False, None)
        time.sleep(1)


def fake_loop() -> None:
    # Each time a writer opens the FIFO counts as the kit connecting; EOF = unplugged.
    while True:
        with open(FAKE) as f:
            set_status(True, f"fake ({FAKE})")
            read_events(f)
        set_status(False, None)
        time.sleep(1)


STATIC = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
          "/stats": ("stats.html", "text/html"), "/stats.html": ("stats.html", "text/html"),
          "/kit.js": ("kit.js", "application/javascript")}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in STATIC:
            name, ctype = STATIC[path]
            self.send_body((HERE / name).read_bytes(), ctype)
        elif path == "/api/stats":
            try:
                self.send_body(json.dumps(stats()).encode(), "application/json")
            except sqlite3.Error as e:
                print(f"[drum-viz] stats query failed: {e}", flush=True)
                self.send_error(500)
        elif path == "/events":
            self.stream_events()
        else:
            self.send_error(404)

    def send_body(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    open_db()
    writer = threading.Thread(target=writer_loop, daemon=True)  # joined below so the last batch lands
    writer.start()
    threading.Thread(target=fake_loop if FAKE else midi_loop, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # systemd stop: flush the last batch first
    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    server.daemon_threads = True
    print(f"[drum-viz] http://127.0.0.1:{HTTP_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        writes.put(None)
        writer.join(timeout=5)
