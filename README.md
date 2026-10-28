# obs-drum-viz

A live e-drum kit overlay for OBS. Each pad lights up when you hit it, brighter the harder you hit. A strip along the bottom shows your APM (hits per minute), how many times you've hit each piece this session, and your all-time hit count. Every hit is logged, so session and all-time stats survive restarts, and a history page shows them.

![preview](docs/preview.png)

Built for the Alesis Nitro Max. Any kit that sends General MIDI drum notes over USB should work.

## How it works

```
e-drum kit --USB MIDI--> ALSA sequencer --aseqdump--> server.py --SSE--> index.html (OBS Browser Source)
```

- `server.py` is a small bridge with no dependencies (Python stdlib only). It finds the kit's MIDI port, reads hits through `aseqdump`, and streams them to the page as Server-Sent Events. It shares the port, so it runs alongside Hydrogen or a DAW. It waits for the kit if the kit isn't plugged in, and reconnects if the kit is unplugged.
- `server.py` also logs every hit to SQLite (see [Stats and history](#stats-and-history)).
- `index.html` draws a top-down drum kit as SVG on a transparent background. `stats.html` is the history page. Both load the note map from `kit.js`.

Linux only, because it uses the ALSA sequencer. You need `aseqdump` and `stdbuf`, which come with `alsa-utils` and `coreutils`.

## Setup

```sh
git clone https://github.com/Antimatter543/obs-drum-viz ~/Projects/obs-drum-viz
cd ~/Projects/obs-drum-viz
python3 server.py            # serves http://127.0.0.1:7870
```

To run it in the background and start it at login:

```sh
cp drum-viz.service ~/.config/systemd/user/
systemctl --user enable --now drum-viz
```

The unit file assumes the repo lives at `~/Projects/obs-drum-viz`. If yours is somewhere else, edit the `ExecStart` line.

### OBS

Add a **Browser Source** with these settings:
- URL: `http://127.0.0.1:7870/`
- Width / height: `800` × `500`
- Leave the custom CSS empty. The background is already transparent.

## Options

URL query parameters:

| Param | Effect |
|---|---|
| `?demo=1` | Plays a looping beat, so you can preview without the kit |
| `?labels=0` | Hides the piece labels |
| `?status=0` | Hides the "waiting for drum kit…" line |
| `?fade=400` | Sets the fade-out time in ms (default 280) |
| `?stats=0` | Hides the stats strip, so the kit fills the whole source |
| `?window=10` | Sets the APM averaging window in seconds (default 5) |
| `?alltime=0` | Hides the all-time hit count in the stats strip |

Sessions are tracked by `server.py`, so refreshing the browser source keeps the current session's counts and clock. The peak APM shown in the strip is the overlay's own, and it resets on refresh.

Environment variables for `server.py`:

| Var | Default | Meaning |
|---|---|---|
| `DRUM_VIZ_PORT` | `7870` | HTTP port |
| `DRUM_PORT_MATCH` | `nitro\|alesis` | Regex matched against ALSA MIDI client and port names |
| `DRUM_VIZ_DB` | `~/.local/share/drum-viz/drums.db` | Where the hit log lives |
| `DRUM_SESSION_GAP` | `600` | Idle seconds that end a session |
| `DRUM_VIZ_FAKE` | unset | Testing only: read `aseqdump`-style lines from this FIFO instead of the kit. Each time a writer opens it counts as the kit connecting |

## Stats and history

Every hit is written to a SQLite database at `~/.local/share/drum-viz/drums.db` (WAL mode). A writer thread commits once a second, so the MIDI loop never waits on the disk, and stopping the service flushes the last second first. Tables:

- `hits`: one row per hit (`ts`, `session_id`, `note`, `vel`). This is the raw log, so you can work out any other stat from it later.
- `sessions`: `id`, `start`, `end`, `hits`, `peak_apm`.
- `session_notes`: hit counts per session per note, kept so the stats queries stay fast.

**Sessions.** A new session starts on the first hit after the kit connects or reconnects (which includes the server starting), or on the first hit after more than 10 minutes of silence (`DRUM_SESSION_GAP`). A session ends at its last hit, so idle time doesn't count as time played. Peak APM is the most hits in any 60-second window of the session.

**Endpoints:**

| Path | What |
|---|---|
| `/` | The overlay |
| `/stats` | History page: all-time totals per piece, hits per day for the last 8 weeks, and the last 100 sessions |
| `/api/stats` | JSON: `current` (the live session, or `null`), `alltime` (`hits`, per-note `notes`, `sessions`, `seconds` played), `sessions` (last 100, newest first, each with `id`, `start`, `end`, `hits`, `duration`, `peak_apm`, `notes`), and `days` (hits and seconds per local date) |
| `/events` | Server-Sent Events: `status`, `hit` (with `session` and `n`, the hit's number within the session), `session` (sent when a new session starts), `cc` |

Times are Unix seconds. Per-note counts use MIDI note numbers as keys.

To look at the raw data:

```sh
sqlite3 ~/.local/share/drum-viz/drums.db 'SELECT id, datetime(start, "unixepoch", "localtime"), hits, peak_apm FROM sessions'
```

## Note map

These are the Nitro Max defaults, which mostly match the GM drum map. To change them, edit `NOTES` in `kit.js`. Notes that aren't in the map are logged to the browser console. You can read those from OBS's browser-source dev tools, or open the page in a normal browser.

| Piece | Notes |
|---|---|
| Kick | 35, 36 |
| Snare (head / rim) | 38 / 37, 40 |
| Tom 1 / 2 / Floor | 48 / 45 / 43 (rims 50 / 47 / 58) |
| Hi-hat closed / half-open / open | 42 / 23 / 46 (edges 22 / 26) |
| Hi-hat pedal / foot splash | 44 / 21 |
| Crash | 49, 55, 57, 52 |
| Ride (bow / edge / bell) | 51 / 59 / 53 |

## License

MIT
