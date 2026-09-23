# Reachy Mini Home Assistant integration — Clove fork

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

> **Fork** of the official [pollen-robotics/reachy_mini_homeassistant](https://github.com/pollen-robotics/reachy_mini_homeassistant) (Pollen Robotics, Apache-2.0). Upstream is mirrored on `main`; **this fork lives on the default branch, `feature/play-audio`**, which is what HACS installs.
>
> **What the fork adds** — one thing, live-tested end-to-end:
>
> - **`reachy_mini.play_audio`** — play any audio through the robot's speaker from Home Assistant: a `/media/` path, an `http(s)` URL, or a media-source id (e.g. `media-source://tts/…` from `tts.speak` or a shell-rendered sanotts clip). HA decodes and re-encodes to 16 kHz WAV on the host, uploads it via the daemon's own REST routes, and plays it — awake-gated, per-robot serialized, optional volume override. No app slot, no extra protocol, no daemon changes. Usage examples below; full feasibility study + build notes in [`fork-notes/`](fork-notes/).
>
> Everything else in this README is upstream's and still accurate.

Adds a [Reachy Mini](https://github.com/pollen-robotics/reachy_mini) to Home
Assistant with **zero YAML**. Drops a "Reachy Mini" device into your HA
instance, polls the robot's daemon every 30 s, and exposes:

- read-only **sensors** for active app, app transport, and voice
  direction (microphone-array DoA),
- **binary sensors** for awake state, remote-session activity, and
  speech detection,
- a **camera** with MJPEG live view and JPEG stills, consuming the
  robot's WebRTC stream server-side (works alongside apps and
  mobile/desktop remote sessions; no SDK changes),
- writable **selects** for motor mode (enabled / disabled /
  gravity compensation), emotion, and dance,
- writable **number sliders** for speaker and microphone volume,
- one-shot action **buttons** for wake up, go to sleep, stop/restart
  the running app, play a test sound, restart the daemon, play
  emotion, and play dance,
- **`reachy_mini.play_audio`** — plays a file, an `http(s)` URL or a TTS
  clip (e.g. sanotts speech) on the robot's speaker, with no extra
  protocol and no app slot needed.

Auto-discovery uses the `_reachy-mini._tcp.local.` mDNS advertisement
the daemon ships out of the box. Polling fans out to several of the
daemon's existing REST routes (`/api/daemon/status`,
`/api/daemon/robot-app-lock-status`, `/api/state/doa`,
`/api/volume/current`, `/api/volume/microphone/current`); writes POST
to `/api/motors/set_mode/{mode}`, `/api/move/play/{wake_up,goto_sleep}`,
`/api/apps/{stop,restart}-current-app`, `/api/volume/set`, etc. No
extra protocols, no broker, no auth.

## Install

### Via HACS (recommended)

> Until this repo is added to the default HACS registry, install it as a
> [Custom Repository](https://hacs.xyz/docs/faq/custom_repositories/).

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add `https://github.com/pollen-robotics/reachy_mini_homeassistant`
   with category **Integration**, then click **Add**.
3. Find **Reachy Mini** in the HACS list and click **Download**.
4. Restart Home Assistant. The first restart takes a little longer:
   HA installs the camera's WebRTC dependency (`aiortc`), fetched as a
   sha256-pinned wheel from this repo (see `wheels/README.md` for why),
   so the host needs
   internet access. A failed download shows up in the log as
   *"Setup failed for custom integration 'reachy_mini':
   Requirements ... not found"* — restart to retry.

### Manual install (no HACS)

1. Copy the `custom_components/reachy_mini/` folder into your HA config
   directory: `<config>/custom_components/reachy_mini/`.
2. Restart Home Assistant.

## Use

1. Make sure your Reachy Mini daemon is running on the **same LAN**.
   For *auto-discovery* you need a daemon that advertises
   `manufacturer=Pollen Robotics` in its mDNS TXT record — that's the
   [`feat/homeassistant` branch](https://github.com/pollen-robotics/reachy_mini/tree/feat/homeassistant)
   or any release that includes it. For *manual setup* any current
   daemon will do (the integration probes `/api/daemon/status`).
2. Within ~30 s of HA starting, you'll see a **"Discovered: Reachy Mini"**
   card under **Settings → Devices & Services**. Click **Add**.
3. Confirm — your Reachy Mini appears as a device with all entities
   grouped underneath, identified by its stable `unit_id`.

No DNS / no IPs to type. If `.local` resolution is broken on your
network, you can still **Manual configuration** with the daemon's IP.

## What you get

The integration creates one **device** per Reachy Mini, identified by
its stable `unit_id` (a hash of the audio device serial). Underneath:

### Sensors (read-only)

| Entity | Unit | Notes |
|---|---|---|
| Active app | — | Local Python app or WebRTC peer name |
| Active app transport | — | `local` or `webrtc` |
| Voice direction | rad | 0 = left, π/2 = front, π = right |

### Binary sensors

| Entity | Device class | Notes |
|---|---|---|
| Awake | `power` | True when motors are enabled or in gravity comp |
| Remote session | `connectivity` | True when a remote client (mobile/desktop app over WebRTC) holds the robot app slot |
| Speech detected | `sound` | Mic-array VAD signal (LAN-side speech-activity VAD) |

### Camera

| Entity | Notes |
|---|---|
| Camera | MJPEG live view (~10 fps) + stills. HA consumes the robot's WebRTC camera producer (`ws://<robot>:8443`) through one shared session; available only while the robot is awake. |

### Select (writable dropdown)

| Entity | Options | Notes |
|---|---|---|
| Motor mode | `enabled` / `disabled` / `gravity_compensation` | Picking an option POSTs to `/api/motors/set_mode/{mode}`. Use `disabled` as a soft E-stop (motors release torque). |
| Emotion | One entry per move in the emotions library | Stores the chosen emotion name; does not play it. Use the *Play emotion* button (or the `reachy_mini.play_recorded_move` service) to trigger playback. |
| Dance | One entry per move in the dances library | Stores the chosen dance name; does not play it. Use the *Play dance* button (or the `reachy_mini.play_recorded_move` service) to trigger playback. |

### Numbers (controllable sliders)

Drag the slider in the HA UI to change the value on the robot. The
daemon may clamp/round to its supported range; the slider snaps to
the value actually applied.

| Entity | Unit | Notes |
|---|---|---|
| Speaker volume | % | 0-100. The daemon plays a short confirmation sound after each change — existing SDK behaviour. |
| Microphone volume | % | 0-100. |

### Buttons (one-shot actions)

| Entity | Action | Notes |
|---|---|---|
| Wake up | `POST /api/move/play/wake_up` | Goes through the wake-up move; the "Awake" binary sensor follows. |
| Go to sleep | `POST /api/move/play/goto_sleep` | Plays the sleep move and releases torque. |
| Stop current app | `POST /api/apps/stop-current-app` | Kills the currently running managed app (no-op when free). |
| Restart current app | `POST /api/apps/restart-current-app` | Stop + start the current app — handy if it got stuck. |
| Play test sound | `POST /api/volume/test-sound` | Plays `impatient1.wav` — quick speaker check. |
| Restart daemon | `POST /api/daemon/restart` | Soft restart of the daemon process. Use sparingly. |
| Play emotion | `POST /api/move/play/recorded-move-dataset/{dataset}/{move}` | Plays the emotion currently selected in the *Emotion* select entity. |
| Play dance | `POST /api/move/play/recorded-move-dataset/{dataset}/{move}` | Plays the dance currently selected in the *Dance* select entity. |

### Recorded moves (emotions and dances)

The integration picks up the daemon's two preloaded move libraries
(`pollen-robotics/reachy-mini-emotions-library` and
`pollen-robotics/reachy-mini-dances-library`) at setup. For each
populated library you get:

- a select entity (`Emotion` / `Dance`) listing every move in the
  library — picking does not play; it just stores the choice;
- a button entity (`Play emotion` / `Play dance`) that plays whatever
  is selected.

For automations and blueprints there's also a global service action,
`reachy_mini.play_recorded_move`, which takes a target device, a
`dataset` (HF repo path) and a `move` (string). The dataset can be one
of the bundled libraries or any custom HF dataset the daemon has
cached — the daemon validates unknown datasets.

### Audio playback (files, URLs, TTS)

The `reachy_mini.play_audio` service action plays audio on the robot's
speaker through the daemon's own REST surface (`/api/media/sounds/upload`
→ `/api/media/play_sound`). It needs no dashboard open, no WebRTC
session and no app slot, and it works alongside local apps and
phone/desktop remote sessions.

| Field | Required | Notes |
|---|---|---|
| `media` | yes | A `/media/...` path, an `http(s)` URL (e.g. a TTS cache file), or a `media-source://` id — a media picker in the UI, but plain strings work from automations |
| `transport` | no | `auto` (default) and `rest` play over REST. `webrtc` is reserved for a later phase and currently raises |
| `volume` | no | 0-100. Sets the speaker volume through `/api/volume/set` just before playing; omit to keep the current volume |
| `keepalive` / `wait` | no | Reserved for the WebRTC transport; accepted and ignored on REST |

Under the hood the clip is fetched (if it isn't a local file), decoded
with **PyAV** — in-process, never a shelled-out `ffmpeg`, which HA OS
doesn't guarantee — re-encoded to 16 kHz mono WAV, uploaded to the
robot's temp sound directory and played. Any container the daemon can
decode works (`.wav`, `.mp3`, `.ogg`/`.opus`, `.flac`, `.m4a`, `.aac`);
the upload is capped at the daemon's 25 MiB limit, and clips go out as
~32 kB/s of PCM, so even a 13-minute clip fits.

**The robot must be awake.** Its daemon stops the entire media backend
while asleep, so every media route answers 503 — the service fails fast
with *"wake the robot first"* instead of letting the call time out.

Direct calls:

```yaml
# A file from HA's /media folder
- action: reachy_mini.play_audio
  target:
    device_id: 0f1e2d3c4b5a69788796a5b4c3d2e1f0   # your Reachy Mini
  data:
    media: /media/ding.wav
    volume: 60

# Anything Home Assistant can produce TTS for, via HA's TTS media source
- action: reachy_mini.play_audio
  target:
    device_id: 0f1e2d3c4b5a69788796a5b4c3d2e1f0
  data:
    media: "media-source://tts/tts.sanotts?message={{ 'Dinner is ready' | urlencode }}"
```

A reusable script, so any automation can make the robot talk:

```yaml
script:
  reachy_announce:
    fields:
      message:
        required: true
    sequence:
      - action: reachy_mini.play_audio
        target:
          device_id: 0f1e2d3c4b5a69788796a5b4c3d2e1f0
        data:
          media: "media-source://tts/tts.sanotts?message={{ message | urlencode }}"
          volume: 60

automation:
  - alias: Announce the front door on Reachy
    triggers:
      - trigger: state
        entity_id: binary_sensor.front_door
        to: "on"
    actions:
      - action: script.reachy_announce
        data:
          message: "Someone's at the front door."
```

#### sanotts on the LAN

A `sanotts` (or any OpenAI-compatible TTS) server on your LAN
(`http://<sanotts-host>:8880/v1`) speaks OpenAI's
`/v1/audio/speech` shape (model `sanotts`, voice `heart-nano`). If it
isn't wired into HA as a TTS entity, render the speech to a file first
and play that file — the audio never leaves the LAN, and the API key
stays out of the automation (read it from an `input_text` helper, or
swap in whatever secret mechanism you use):

```yaml
# configuration.yaml
shell_command:
  sanotts_render: >-
    curl -sS -X POST "http://<sanotts-host>:8880/v1/audio/speech"
    -H "Authorization: Bearer {{ states('input_text.sanotts_tts_key') }}"
    -H "Content-Type: application/json"
    -d "{\"model\":\"sanotts\",\"voice\":\"heart-nano\",\"input\":\"{{ message }}\",\"response_format\":\"wav\"}"
    -o /media/reachy-tts.wav

script:
  reachy_say_sanotts:
    fields:
      message:
        required: true
    sequence:
      - action: shell_command.sanotts_render
        data:
          message: "{{ message }}"
      - action: reachy_mini.play_audio
        target:
          device_id: 0f1e2d3c4b5a69788796a5b4c3d2e1f0
        data:
          media: /media/reachy-tts.wav
          volume: 60
# note: replace the example device_id with your own (Settings →
# Devices & Services → Reachy Mini → copy Device ID)

automation:
  - alias: Reachy says good morning
    triggers:
      - trigger: time
        at: "07:30:00"
    actions:
      - action: script.reachy_say_sanotts
        data:
          message: "Morning, Captain. Parcels are waiting."
```

> `shell_command` renders its command as a Jinja template with the
> service data available as variables, which is what `{{ message }}`
> relies on. Overwriting `reachy-tts.wav` is fine — the robot's own
> upload copy is content-addressed, so replays don't pile up there.

A `media_player.reachy_mini` entity — so `tts.speak` can target the
robot directly, with no script in between — and the low-latency WebRTC
transport are planned for a later phase.

### Move catalog reference

A snapshot of the moves the SDK ships in its two bundled libraries. The
left column is what you see in the `Emotion` / `Dance` dropdowns; the
right column is the SDK move name you pass to the
`reachy_mini.play_recorded_move` service action. Future SDK releases
may add or rename moves — anything the daemon returns but isn't listed
here will appear in the dropdown using its raw SDK name.

> Emoji-and-label mapping adapted from
> [reachy-mini-desktop-app](https://github.com/pollen-robotics/reachy-mini-desktop-app)
> (Apache 2.0 © Pollen Robotics).

<details>
<summary><strong>Emotions</strong> (81 moves)</summary>

| Display label | SDK move name |
|---|---|
| 😨 Fear | `fear1` |
| 😩 Exhausted | `exhausted1` |
| 🥰 Loving | `loving1` |
| 🪩 Dance 3 | `dance3` |
| 😑 Boredom 2 | `boredom2` |
| 😌 Relief 1 | `relief1` |
| 😟 Anxiety | `anxiety1` |
| 🤢 Disgusted | `disgusted1` |
| 👋 Welcoming 1 | `welcoming1` |
| ⏳ Impatient 1 | `impatient1` |
| 😭 Sad 1 | `sad1` |
| 🤝 Helpful 2 | `helpful2` |
| 😞 Resigned | `resigned1` |
| 🤩 Amazed | `amazed1` |
| 💭 Thoughtful 2 | `thoughtful2` |
| 😵‍💫 Lost | `lost1` |
| 😲 Surprised 1 | `surprised1` |
| 🧘 Serenity | `serenity1` |
| 😒 Displeased 1 | `displeased1` |
| 🤷 Incomprehensible | `incomprehensible2` |
| 😤 Irritated 2 | `irritated2` |
| 🥹 Yes sad | `yes_sad1` |
| 🕺 Dance 2 | `dance2` |
| 💡 Understanding 1 | `understanding1` |
| 🙄 Contempt | `contempt1` |
| ❓ Inquiring 1 | `inquiring1` |
| 😡 Rage | `rage1` |
| 🦉 Attentive 2 | `attentive2` |
| 👎 No | `no1` |
| 🫣 Oops 1 | `oops1` |
| 💪 Proud 3 | `proud3` |
| 🚫 Reprimand 3 | `reprimand3` |
| 😡 Reprimand 2 | `reprimand2` |
| 😱 Scared | `scared1` |
| 🙅‍♂️ No excited | `no_excited1` |
| 🫴 Come | `come1` |
| 🏆 Proud 2 | `proud2` |
| ✨ Success 1 | `success1` |
| 🥳 Enthusiastic 2 | `enthusiastic2` |
| 😂 Laughing 1 | `laughing1` |
| 😵 Dying | `dying1` |
| 🌟 Success 2 | `success2` |
| 🎊 Enthusiastic 1 | `enthusiastic1` |
| 🧐 Curious | `curious1` |
| 🤣 Laughing 2 | `laughing2` |
| 😴 Tired | `tired1` |
| 😤 Reprimand 1 | `reprimand1` |
| 😎 Proud 1 | `proud1` |
| 🙏 Grateful | `grateful1` |
| 😫 Frustrated | `frustrated1` |
| ☮️ Calming | `calming1` |
| 👂 Attentive 1 | `attentive1` |
| 🤬 Furious | `furious1` |
| 😅 Oops 2 | `oops2` |
| 😠 Irritated 1 | `irritated1` |
| 👍 Yes | `yes1` |
| 😕 Confused | `confused1` |
| 🤝 Understanding 2 | `understanding2` |
| 💃 Dance 1 | `dance1` |
| 😳 Shy | `shy1` |
| 🔍 Inquiring 2 | `inquiring2` |
| 🤨 Uncertain | `uncertain1` |
| 🤔 Thoughtful 1 | `thoughtful1` |
| 😯 Surprised 2 | `surprised2` |
| 😑 Displeased 2 | `displeased2` |
| 🙄 Impatient 2 | `impatient2` |
| 🤗 Welcoming 2 | `welcoming2` |
| 😐 Indifferent | `indifferent1` |
| 😢 Sad 2 | `sad2` |
| 🙋 Helpful 1 | `helpful1` |
| 🥺 Lonely | `lonely1` |
| 😊 Cheerful | `cheerful1` |
| 🤨 Inquiring 3 | `inquiring3` |
| 😔 Downcast | `downcast1` |
| 💤 Sleep | `sleep1` |
| 🥱 Boredom 1 | `boredom1` |
| 😬 Uncomfortable | `uncomfortable1` |
| 👉 Go away | `go_away1` |
| ⚡ Electric | `electric1` |
| 😮‍💨 Relief 2 | `relief2` |
| 😥 No sad | `no_sad1` |
</details>

<details>
<summary><strong>Dances</strong> (34 moves)</summary>

| Display label | SDK move name |
|---|---|
| 🫨 Stumble and recover | `stumble_and_recover` |
| 🎭 Chin lead | `chin_lead` |
| 🔃 Head tilt roll | `head_tilt_roll` |
| 🕴️ Jackson square | `jackson_square` |
| 🎐 Pendulum swing | `pendulum_swing` |
| 👁️ Side glance flick | `side_glance_flick` |
| 🤖 Grid snap | `grid_snap` |
| 😌 Simple nod | `simple_nod` |
| 🌊 Side to side sway | `side_to_side_sway` |
| 🥁 Polyrhythm combo | `polyrhythm_combo` |
| 🌀 Interwoven spirals | `interwoven_spirals` |
| 😏 Uh huh tilt | `uh_huh_tilt` |
| 🐓 Chicken peck | `chicken_peck` |
| 🙌 Yeah nod | `yeah_nod` |
| 🤘 Headbanger combo | `headbanger_combo` |
| 🙈 Side peekaboo | `side_peekaboo` |
| 💫 Dizzy spin | `dizzy_spin` |
| ⚡ Neck recoil | `neck_recoil` |
| 🪩 Groovy sway and roll | `groovy_sway_and_roll` |
| 📐 Sharp side tilt | `sharp_side_tilt` |
| 💍 Beyonce single ladies | `beyonce-single-ladies` |
| 👹 Demon hunters | `demon-hunters-1` |
| 🌴 Eagles hotel california | `eagles-hotel-california` |
| 🎤 Eminem lose yourself | `eminem-lose-yourself` |
| ✨ Feel the magic in the air | `feel-the-magic-in-the-air` |
| 🎆 Katy perry fireworks | `katy-perry-fireworks` |
| 🍅 Las ketchup | `las-ketchup` |
| 🧟 Michael jackson thriller | `michael-jackson-thriller` |
| 🖤 Paint it black | `paint-it-black` |
| 😀 Pharrell williams happy | `pharrell-williams-happy` |
| 👑 Queen we will rock you | `queen-we-will-rock-you` |
| 🎀 Spice girls | `spice-girls` |
| 🎻 The fratellis whistle for the choir | `the-fratellis-whistle-for-the-choir` |
| ⚔️ The white stripes seven nation army | `the-white-stripes-seven-nation-army` |
</details>

## Blueprints

Ready-made automation blueprints ship in the
`blueprints/automation/reachy_mini/` folder of this repo. Each pre-fills
its entity dropdowns with the names this integration creates — you
typically only have to pick the action targets. Click an import badge,
then *Import* and *Create automation* in HA.

| Blueprint | What it does | Showcases | Import |
|---|---|---|---|
| **Notify when robot wakes up** | Fires the chosen actions on every sleep → awake transition. | `binary_sensor.reachy_mini_awake` | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpollen-robotics%2Freachy_mini_homeassistant%2Fmain%2Fblueprints%2Fautomation%2Freachy_mini%2Fnotify_on_wake.yaml) |
| **Dim lights when someone speaks to it** | Lights fade down when the mic array detects sustained speech — for a "conversation mode" atmosphere. | `binary_sensor.reachy_mini_speech_detected` | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpollen-robotics%2Freachy_mini_homeassistant%2Fmain%2Fblueprints%2Fautomation%2Freachy_mini%2Fdim_lights_on_speech.yaml) |
| **Activate a scene when an app launches** | When `sensor.reachy_mini_active_app` becomes the chosen app, run a scene. | `sensor.reachy_mini_active_app` | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpollen-robotics%2Freachy_mini_homeassistant%2Fmain%2Fblueprints%2Fautomation%2Freachy_mini%2Fscene_on_app_launch.yaml) |
| **Run an action with the speaker's direction** | Exposes `doa_deg` (0 = left, 90 = front, 180 = right) to the chosen actions on speech detect — for steering a smart light, panning a camera, etc. | `sensor.reachy_mini_voice_direction` + speech sensor | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpollen-robotics%2Freachy_mini_homeassistant%2Fmain%2Fblueprints%2Fautomation%2Freachy_mini%2Frun_with_doa.yaml) |
| **Wake up on arrival or presence** | When a person, motion sensor, door sensor (etc.) changes to a chosen state, press the wake-up button — only if the robot isn't already awake. | `button.reachy_mini_wake_up` + awake binary sensor | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpollen-robotics%2Freachy_mini_homeassistant%2Fmain%2Fblueprints%2Fautomation%2Freachy_mini%2Fwake_on_presence.yaml) |
| **Day / night speaker volume** | Two scheduled times → set `number.reachy_mini_speaker_volume` to different day and night levels automatically. | `number.reachy_mini_speaker_volume` slider | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpollen-robotics%2Freachy_mini_homeassistant%2Fmain%2Fblueprints%2Fautomation%2Freachy_mini%2Fnight_volume.yaml) |
| **Greet on arrival** | Plays a chosen emotion when a person/motion/door entity flips to a target state. Optional "skip if asleep" safety condition. | `reachy_mini.play_recorded_move` service action | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpollen-robotics%2Freachy_mini_homeassistant%2Fmain%2Fblueprints%2Fautomation%2Freachy_mini%2Fgreet_on_arrival.yaml) |
| **React with an emotion on any trigger** | Generic "state-change → play emotion" wiring. Pair a doorbell button with `surprised1`, a smoke alarm with `scared1`, etc. | `reachy_mini.play_recorded_move` | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpollen-robotics%2Freachy_mini_homeassistant%2Fmain%2Fblueprints%2Fautomation%2Freachy_mini%2Femotion_on_event.yaml) |
| **Daily dance party** | At a scheduled time (with weekday filter), the robot plays a chosen dance. Works with the bundled dances or community music dances. | `reachy_mini.play_recorded_move` | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpollen-robotics%2Freachy_mini_homeassistant%2Fmain%2Fblueprints%2Fautomation%2Freachy_mini%2Fdaily_dance.yaml) |
| **Random emotion on trigger** | Picks a move at random from a user-curated comma-separated list each time the trigger fires. Showcases Jinja templating. | `reachy_mini.play_recorded_move` | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpollen-robotics%2Freachy_mini_homeassistant%2Fmain%2Fblueprints%2Fautomation%2Freachy_mini%2Frandom_emotion_on_trigger.yaml) |
| **Voice assistant feedback** | When an `assist_satellite.*` entity (or any entity) transitions to a "listening" state, the robot plays `attentive1`. | `reachy_mini.play_recorded_move` | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpollen-robotics%2Freachy_mini_homeassistant%2Fmain%2Fblueprints%2Fautomation%2Freachy_mini%2Fvoice_assistant_feedback.yaml) |
| **Photo notification on trigger** | Doorbell, motion, door contact — any trigger takes a snapshot through the robot's camera and sends it as a picture notification to your phone. | Camera entity + `camera.snapshot` | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpollen-robotics%2Freachy_mini_homeassistant%2Fmain%2Fblueprints%2Fautomation%2Freachy_mini%2Fphoto_notification_on_trigger.yaml) |
| **Snapshot when spoken to** | Sustained speech at the mic array grabs a time-stamped photo of whoever is talking; optional follow-up actions get the file path. | Camera + speech sensor together | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpollen-robotics%2Freachy_mini_homeassistant%2Fmain%2Fblueprints%2Fautomation%2Freachy_mini%2Fsnapshot_on_speech.yaml) |

## Troubleshooting

**HA doesn't show the "Discovered" card.**

- Your daemon is running but on an older version that doesn't advertise
  the `manufacturer=Pollen Robotics` TXT record. Upgrade to the SDK build
  that includes the `feat/homeassistant` work.
- mDNS isn't traversing your network. Confirm with `avahi-browse -ar`
  (Linux) or `dns-sd -B _reachy-mini._tcp` (macOS).
- HA's zeroconf component is disabled. Re-enable via Settings →
  Integrations.

**"Cannot connect" when adding manually.**

`curl http://<host>:8000/api/daemon/status` — if that returns a JSON
blob with `"type": "daemon_status"` and a `version` field, the
integration should also work. If not, check the daemon logs.

**Entities show `unknown` after add.**

The daemon backend hasn't finished starting yet (motor configuration
takes ~5–10 s on first boot). Wait one update interval (30 s).

**The camera is `unavailable` / the live view ended.**

The camera exists only while the robot is awake — the daemon stops its
WebRTC producer during sleep. Wake the robot and the camera returns on
the next poll (≤30 s). An MJPEG live view also ends when the robot goes
to sleep mid-stream; just reopen it after waking.

**`reachy_mini.play_audio` says "wake the robot first".**

A sleeping Reachy Mini has its whole media backend stopped, so every
`/api/media/*` route answers 503 — there is nothing to play into. Wake
the robot (`button.<robot>_wake_up`) and retry, or guard the automation
with a condition on `binary_sensor.<robot>_awake`. If the robot *is*
awake and it still refuses, the daemon may still be starting up (motor
configuration takes ~5-10 s on first boot); retry once the next poll
picks up `daemon_state: running`.

**`play_audio` uploads but nothing is audible.**

Check the robot's speaker volume (`number.<robot>_speaker_volume`) —
`play_audio` leaves it untouched unless you pass `volume`. Also note
the daemon plays one sound at a time: a new `play_sound` replaces any
previous `playbin`, so a second utterance cuts the first one off
(`play_audio` serialises calls per robot, but a `play_recorded_move` or
an app playing audio sits outside that lock).

**Some entities are `unavailable` but others work.**

By design — the integration fans out across several SDK endpoints in
parallel each tick, and one failing endpoint only takes down the
entities backed by it (e.g. `/api/state/doa` returning 404 because
audio is disabled leaves the speech / voice-direction entities
unavailable but everything else keeps polling normally).

## Design notes

The integration is a thin client over the daemon's existing REST
surface — no extra protocols, no schema parsing beyond the documented
fields each endpoint returns. The fan-out coordinator (~80 lines of
Python) calls five GET endpoints in parallel each poll and does the
HA-shaping (`awake`, `active_app_transport`, `remote_session_active`,
`active_app`) locally on top of the raw values. Writes POST to the
same routes the dashboard / SDK clients have always used.

That means upgrading the daemon to a newer release rarely requires
upgrading the integration too — only renames or removals of the
specific routes the integration calls would break it.

`play_audio` follows the same rule: it posts to the daemon's existing
media routes and adds no new requirement to `manifest.json`. The only
processing it does on the HA side is re-encoding the clip to WAV —
PyAV (`av`), which the camera's aiortc dependency already brings in — in
a worker thread, never a shelled-out `ffmpeg`, because HA OS guarantees
no such binary. The daemon's own GStreamer stack does the rest, and its
upload route re-validates the payload before anything is played.

## License

Apache 2.0 — same as the upstream Reachy Mini SDK.
