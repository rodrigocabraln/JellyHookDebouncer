# JellyHookDebouncer 🍿🤖

A smart webhook wrapper for Jellyfin that filters noise and provides reliable playback events for home automation.

## The Problem
Jellyfin's native webhook system presents two major challenges for automation (like controlling lights):
1. **Event Flooding**: The `PlaybackProgress` event sends constant updates, which can overwhelm automation triggers.
2. **Ambiguous Events**: There is no clear differentiation between events like `Play`, `Pause`, `Resume`, and `Seek`.
3. **The Seeking Trigger**: When seeking through a video, Jellyfin briefly reports a "Pause" state. This causes lights to turn on/off incorrectly during a simple scrub.

## The Solution
**JellyHookDebouncer** sits between Jellyfin and your automation platform (Home Assistant). It intelligently processes the raw stream of events to emit only what matters:
- **Clean Events**: Emits clear `play`, `pause`, and `media_end` signals.
- **Seek Filtering**: Intelligently ignores the false "pause" events created during seeking.
- **Debounced Pause**: Only confirms a pause if the media stays paused for a configurable duration (e.g., 2 seconds).
- **Smart Credits Detection**: Uses chapter data from the media file (e.g., "End Credits") for precise detection, with a percentage-based fallback.

---

## 🚀 Quick Start (Docker)

1. **Clone the repository**:
   ```bash
   git clone https://github.com/rodrigocabraln/JellyHookDebouncer.git
   cd JellyHookDebouncer
   ```

2. **Setup environment**:
   ```bash
   cp .env.example .env
   # Edit .env and set your HA_WEBHOOK_URL
   ```

3. **Build and launch**:
   ```bash
   docker compose up -d --build
   ```

4. **Check logs**:
   ```bash
   docker compose logs -f jellyhook-debouncer
   ```

---

## ⚙️ Configuration

### 1. Jellyfin Webhook Setup
In your Jellyfin Dashboard, go to **Plugins → Webhooks**:
- **URL**: `http://<YOUR_SERVER_IP>:<PORT>/jellyfin`
- **Notification Type**: Check `PlaybackStart`, `PlaybackProgress`, and `PlaybackStop`.
- **Note**: The `<PORT>` is defined in your `.env` (default is `8099`). Use your server's local IP address.

### 2. Environment Variables (.env)
| Variable | Default | Description |
|----------|---------|-------------|
| `HA_WEBHOOK_URL` | (required) | The destination webhook in Home Assistant |
| `PORT` | 8099 | Port this wrapper listens on |
| `PAUSE_DEBOUNCE_SECS` | 2 | Seconds to wait before confirming a real pause |
| `CREDITS_THRESHOLD_PCT` | 95 | Fallback % of progress to trigger `media_end` |
| `JELLYFIN_URL` | (optional) | Jellyfin server URL for chapter-based credits detection |
| `JELLYFIN_API_KEY` | (optional) | Jellyfin API key (see below) |
| `ALLOWED_DEVICES` | (all) | Comma-separated list of devices to process |

### 3. Jellyfin API Key (for chapter-based credits detection)

To enable precise credits detection using chapter data, you need a Jellyfin API key:

1. Open your Jellyfin Dashboard
2. Go to **Administration → API Keys**
3. Click **+** to create a new key
4. Give it a name (e.g., "JellyHookDebouncer")
5. Copy the generated key and set it as `JELLYFIN_API_KEY` in your `.env`
6. Set `JELLYFIN_URL` to your Jellyfin server address (e.g., `http://10.1.1.X:8096`)

> **💡 How it works**: When playback starts, the wrapper queries Jellyfin for the media's chapter list. If a chapter named "End Credits" (or similar) is found, its exact start position is used to trigger `media_end`. If no credits chapter exists, the fallback `CREDITS_THRESHOLD_PCT` percentage is used instead.

> **⚠️ Networking note**: The event flow requires two connections:
> `Jellyfin → :8099 → JellyHookDebouncer → :8123 → Home Assistant`
>
> Make sure your firewall allows Jellyfin to reach port `8099` and JellyHookDebouncer to reach Home Assistant on port `8123`. If running in Docker, evaluate your environment — firewalls like `ufw` or `firewalld` may require additional rules for both inbound and outbound traffic on the container.

---

## 📡 Events Emitted
The wrapper sends a simplified JSON to your Home Assistant webhook:

| Event | Logic |
|-------|-------|
| `play` | Emitted when playback starts or resumes from a real pause. |
| `pause` | Emitted only after `PAUSE_DEBOUNCE_SECS` of sustained pause. |
| `media_end` | Emitted when entering the credits chapter (or at `CREDITS_THRESHOLD_PCT` fallback). |
| `PlaybackStop` | Direct pass-through of the Jellyfin stop event. |

### Payload Example
```json
{
  "event": "pause",
  "device": "Living Room TV",
  "client": "Kodi",
  "media": "Inception",
  "media_type": "Movie",
  "position_pct": 42.3,
  "timestamp": "2026-02-05T21:15:00+00:00"
}
```

---

## 🏠 Home Assistant Configuration Example

Below is a minimal automation that dims the lights when you play a movie and restores them on pause or when the credits start. It uses a single webhook trigger that receives all events from JellyHookDebouncer.

### 1. Create the webhook

Set your `HA_WEBHOOK_URL` in `.env` to match the webhook ID you'll use in the automation:
```
HA_WEBHOOK_URL=http://<HA_IP>:8123/api/webhook/jellyfin_event
```

### 2. Automation (automations.yaml)

```yaml
alias: "Jellyfin Playback Lights"
description: "Dim lights on play, restore on pause/end"
mode: single
max_exceeded: silent

trigger:
  - platform: webhook
    webhook_id: jellyfin_event
    allowed_methods:
      - POST
    local_only: true

condition:
  # Only react to the Living Room TV (optional)
  - condition: template
    value_template: "{{ trigger.json.device == 'Living Room TV' }}"

action:
  - choose:
      # ── Play ──────────────────────────────────────
      - conditions:
          - condition: template
            value_template: "{{ trigger.json.event == 'play' }}"
        sequence:
          - service: light.turn_on
            target:
              entity_id: light.living_room
            data:
              brightness_pct: 10
              transition: 3

      # ── Pause ─────────────────────────────────────
      - conditions:
          - condition: template
            value_template: "{{ trigger.json.event == 'pause' }}"
        sequence:
          - service: light.turn_on
            target:
              entity_id: light.living_room
            data:
              brightness_pct: 80
              transition: 2

      # ── Media End (credits) ───────────────────────
      - conditions:
          - condition: template
            value_template: "{{ trigger.json.event == 'media_end' }}"
        sequence:
          - service: light.turn_on
            target:
              entity_id: light.living_room
            data:
              brightness_pct: 100
              transition: 5

      # ── Playback Stop ─────────────────────────────
      - conditions:
          - condition: template
            value_template: "{{ trigger.json.event == 'PlaybackStop' }}"
        sequence:
          - service: light.turn_on
            target:
              entity_id: light.living_room
            data:
              brightness_pct: 100
              transition: 2
```

### Available template variables

All fields from the payload are accessible via `trigger.json`:

| Template | Example value |
|----------|---------------|
| `trigger.json.event` | `play`, `pause`, `media_end`, `PlaybackStop` |
| `trigger.json.device` | `Living Room TV` |
| `trigger.json.client` | `Kodi` |
| `trigger.json.media` | `Inception` |
| `trigger.json.media_type` | `Movie`, `Episode` |
| `trigger.json.position_pct` | `42.3` |
| `trigger.json.timestamp` | `2026-02-05T21:15:00+00:00` |

> **Tip**: You can filter by `media_type` to apply different behaviors for movies vs episodes, or use `position_pct` to trigger actions at specific points in playback.

---

## 🛠 How It Works
Each device is tracked independently. When a "Pause" arrives, a timer starts. If a "Play" arrives before the timer ends, the pause is discarded as a **Seek Artifact**. If the timer expires, the `pause` event is finally sent to Home Assistant.

### Credits Detection Priority
1. **Chapter-based** (precise): If `JELLYFIN_URL` and `JELLYFIN_API_KEY` are configured, the wrapper fetches the media's chapters on playback start. If it finds a chapter matching "End Credits" / "Credits" / "Créditos", it uses that chapter's start position as the trigger point.
2. **Percentage-based** (fallback): If no credits chapter is found (or the Jellyfin API is not configured), the wrapper falls back to triggering `media_end` at `CREDITS_THRESHOLD_PCT`% of the total runtime.

---
License: MIT
