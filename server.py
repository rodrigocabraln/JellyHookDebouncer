#!/usr/bin/env python3
"""
Jellyfin → Home Assistant wrapper.

Receives Jellyfin webhook POSTs, debounces false pauses (caused by seek),
and forwards simplified play/pause/media_end events to a Home Assistant webhook.

Config: reads .env from the same directory (or use real env vars).
"""

import json
import logging
import os
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ── Load .env ───────────────────────────────────────────────────────────────

def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key:
                os.environ.setdefault(key, value)

_load_dotenv(Path(__file__).resolve().parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────

HA_WEBHOOK_URL: str = os.environ.get("HA_WEBHOOK_URL", "")
PORT: int = int(os.environ.get("PORT", "8099"))
PAUSE_DEBOUNCE_SECS: float = float(os.environ.get("PAUSE_DEBOUNCE_SECS", "5"))
CREDITS_THRESHOLD_PCT: float = float(os.environ.get("CREDITS_THRESHOLD_PCT", "95"))
ALLOWED_DEVICES_RAW: str = os.environ.get("ALLOWED_DEVICES", "")
ALLOWED_DEVICES: set[str] = (
    {d.strip().lower() for d in ALLOWED_DEVICES_RAW.split(",") if d.strip()}
    if ALLOWED_DEVICES_RAW else set()
)
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jellyfin-ha")

# ── Session state (per device) ────────────────────────────────────────────────

@dataclass
class Session:
    device_id: str
    device_name: str = ""
    client_name: str = ""
    media_name: str = ""
    media_type: str = ""
    item_id: str = ""
    run_time_ticks: int = 0

    state: str = "idle"                  # idle | playing | paused
    last_position_ticks: int = 0
    media_end_emitted: bool = False
    _pause_timer: threading.Timer | None = field(default=None, repr=False)
    _debouncing: bool = False            # True while waiting for debounce

    def cancel_pause_timer(self) -> None:
        if self._pause_timer is not None:
            self._pause_timer.cancel()
            self._pause_timer = None
        self._debouncing = False


sessions: dict[str, Session] = {}
_lock = threading.Lock()

# ── Notify Home Assistant ─────────────────────────────────────────────────────

def _notify_ha(payload: dict) -> None:
    if not HA_WEBHOOK_URL:
        log.warning("HA_WEBHOOK_URL not set — event dropped: %s", payload)
        return

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        HA_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            log.info("→ HA %s  (status %s)", payload["event"], resp.status)
    except Exception as e:
        log.error("→ HA failed: %s", e)


def emit(event: str, session: Session) -> None:
    position_pct = 0.0
    if session.run_time_ticks > 0:
        position_pct = round(
            session.last_position_ticks / session.run_time_ticks * 100, 1
        )
    payload = {
        "event": event,
        "device": session.device_name,
        "client": session.client_name,
        "media": session.media_name,
        "media_type": session.media_type,
        "position_pct": position_pct,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    log.info(
        "EMIT %-10s  device=%-15s  media=%s  pos=%.1f%%",
        event, session.device_name, session.media_name, position_pct,
    )
    threading.Thread(target=_notify_ha, args=(payload,), daemon=True).start()

# ── Core logic ────────────────────────────────────────────────────────────────

def process_event(body: dict) -> None:
    notification_type = body.get("NotificationType", "")
    device_id = body.get("DeviceId", "")
    device_name = body.get("DeviceName", "")

    if not device_id:
        return

    # Filter by device name if configured
    if ALLOWED_DEVICES and device_name.strip().lower() not in ALLOWED_DEVICES:
        log.debug("SKIP device=%s (not in ALLOWED_DEVICES)", device_name)
        return

    is_paused: bool = body.get("IsPaused", False)
    position_ticks: int = body.get("PlaybackPositionTicks", 0)
    run_time_ticks: int = body.get("RunTimeTicks", 0)
    item_id: str = body.get("ItemId", "")

    with _lock:
        s = sessions.get(device_id)
        if s is None:
            s = Session(device_id=device_id)
            sessions[device_id] = s

        # Update metadata
        s.device_name = body.get("DeviceName", s.device_name)
        s.client_name = body.get("ClientName", s.client_name)
        s.media_name = body.get("Name", s.media_name)
        s.media_type = body.get("ItemType", s.media_type)
        s.run_time_ticks = run_time_ticks or s.run_time_ticks

        # New media item → reset
        if item_id and item_id != s.item_id:
            s.cancel_pause_timer()
            s.item_id = item_id
            s.media_end_emitted = False

        log.debug(
            "IN   %-18s  device=%-15s  paused=%s  pos=%s  state=%s  debouncing=%s",
            notification_type, s.device_name, is_paused, position_ticks,
            s.state, s._debouncing,
        )

        # ── PlaybackStart ────────────────────────────────────────────────
        # Just forward the raw Jellyfin event
        if notification_type == "PlaybackStart":
            s.cancel_pause_timer()
            s.last_position_ticks = position_ticks
            s.state = "playing"
            s.media_end_emitted = False
            emit("PlaybackStart", s)
            return

        # ── PlaybackStop ─────────────────────────────────────────────────
        # Just forward the raw Jellyfin event
        if notification_type == "PlaybackStop":
            s.cancel_pause_timer()
            s.last_position_ticks = position_ticks
            s.state = "idle"
            emit("PlaybackStop", s)
            return

        # ── PlaybackProgress ─────────────────────────────────────────────
        if notification_type != "PlaybackProgress":
            return

        prev_position = s.last_position_ticks
        s.last_position_ticks = position_ticks

        # -- Reset media_end if user seeks backward below threshold --
        if (
            s.media_end_emitted
            and s.run_time_ticks > 0
            and position_ticks / s.run_time_ticks * 100 < CREDITS_THRESHOLD_PCT
        ):
            s.media_end_emitted = False
            log.debug("  media_end reset — user seeked back below credits threshold")

        # -- Check media_end (credits) --
        if (
            not s.media_end_emitted
            and s.run_time_ticks > 0
            and position_ticks / s.run_time_ticks * 100 >= CREDITS_THRESHOLD_PCT
        ):
            s.cancel_pause_timer()
            s.media_end_emitted = True
            s.state = "idle"
            emit("media_end", s)
            return

        # -- IsPaused=false → playing --
        if not is_paused:
            if s._debouncing:
                # Seek detected: cancel pending pause, stay in "playing"
                log.debug("  seek detected — cancelling pause debounce")
                s.cancel_pause_timer()
                # Don't emit play — we never really paused
                return

            if s.state in ("paused", "idle"):
                # Don't emit play if we already emitted media_end
                # (user is still in credits or past the threshold)
                if s.media_end_emitted:
                    log.debug("  skip play — media already ended (in credits)")
                    return
                s.state = "playing"
                emit("play", s)
            return

        # -- IsPaused=true → maybe pause --
        if is_paused:
            if s.state == "playing" and not s._debouncing:
                # Start debounce
                s._debouncing = True

                # Capture the device_id for the timer callback
                did = device_id

                def _confirm_pause(did=did):
                    with _lock:
                        ss = sessions.get(did)
                        if ss and ss._debouncing:
                            ss._debouncing = False
                            ss._pause_timer = None
                            ss.state = "paused"
                            emit("pause", ss)

                t = threading.Timer(PAUSE_DEBOUNCE_SECS, _confirm_pause)
                t.daemon = True
                s._pause_timer = t
                t.start()
                log.debug("  pause debounce started (%ss)", PAUSE_DEBOUNCE_SECS)

# ── HTTP Server ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Silence default access logs; we do our own logging
        pass

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"ok": True})
            return
        self._send(404, {"ok": False})

    def do_POST(self) -> None:
        if self.path != "/jellyfin":
            self._send(404, {"ok": False})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b""

        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            self._send(400, {"ok": False, "error": "bad json"})
            return

        process_event(body)
        self._send(200, {"ok": True})


def main() -> None:
    if not HA_WEBHOOK_URL:
        log.warning("HA_WEBHOOK_URL not set — events will be logged but not forwarded")

    log.info("Jellyfin → HA wrapper")
    log.info("  Listen:           0.0.0.0:%s", PORT)
    log.info("  HA webhook:       %s", HA_WEBHOOK_URL or "(not set)")
    log.info("  Pause debounce:   %ss", PAUSE_DEBOUNCE_SECS)
    log.info("  Credits at:       %s%%", CREDITS_THRESHOLD_PCT)
    if ALLOWED_DEVICES:
        log.info("  Allowed devices:  %s", ", ".join(sorted(ALLOWED_DEVICES)))
    else:
        log.info("  Allowed devices:  (all)")

    httpd = HTTPServer(("0.0.0.0", PORT), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
