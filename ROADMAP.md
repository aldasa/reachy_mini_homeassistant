# Roadmap — Reachy Mini HA fork (play_audio)

**Status: Phase 1 shipped and live-tested.** 2026-09-22 — installed via HACS on
a real Home Assistant; `reachy_mini.play_audio` rendered TTS from a LAN sanotts
server through the robot's speaker ("100% loud & clear", volume 80–100).

- Design & build notes: `fork-notes/DESIGN.md`, `fork-notes/BUILD_NOTES.md`
- Default branch: `feature/play-audio` (`main` mirrors upstream for rebases)
- Measured pipeline (2026-09-22): sanotts synthesis ≈ **10× realtime**
  (15.8 s of speech in 1.65 s, no warmup penalty); `play_audio` transport
  (fetch → WAV re-encode → upload → play) ≈ **0.8 s**; end-to-end text →
  audible ≈ **~2.5 s** for a paragraph, ~1 s for a short line.
- Transient note: the sanotts server 401'd a whole batch once at 19:45 and
  recovered without intervention — worth remembering if voice notes go silent.

## Phase 2 — open items (tracked as GitHub issues)

1. **`media_player` entity** — makes `tts.speak` target the robot natively
   (no URL-fetch hop), enables `stop` via `/api/media/stop_sound`, and media
   browsing of uploaded sounds via `/api/media/sounds`. DESIGN §5.2 (stretch).
2. **WebRTC uplink transport** — low-latency streaming and true
   `keepalive`/`wait` support. The robot's offer already carries a `sendrecv`
   audio m-line today; the fork answers it `recvonly` and discards mic audio.
   Flipping that is ~20 lines, but needs real-hardware SDP/pad debugging
   (DESIGN §5.4, risks §6.1–§6.4).
3. **Barge-in** — wire `clear_incoming_audio` for interruptions.
4. **Upstream PR** to `pollen-robotics/reachy_mini_homeassistant` — repo has
   been quiet; the PR doubles as documentation for other Reachy owners.

## Backlog ideas

- Doorbell / presence automations that speak through the robot.
- "Wake-word → local LLM → robot voice" loop (pipeline is already
  response-grade: the LLM, not the voice stack, is the latency).
- Test suite: the 32 `play_audio` tests are green in a local venv; a CI
  workflow (GitHub Actions, HA plugin pytest) would keep them honest.
