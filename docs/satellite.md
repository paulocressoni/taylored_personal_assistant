# The voice satellite — a guided tour

This is the long-form companion to [`firmware/README.md`](../firmware/README.md). The README is the
reference you keep open while working: pin tables, versions, commands, log lines. This document is
the explanation — what the board is, how the toolchain fits together, why the firmware is shaped
the way it is, and what every failure we hit along the way actually meant.

Read it in order the first time. After that, use it as a lookup: the troubleshooting catalogue in
§8 has one row per symptom we have already paid for.

The protocol itself — frames, close codes, latency budget — lives in [`voice.md`](voice.md). This
document does not repeat it; it explains the device that speaks it.

---

## 1. What the satellite is

Three processors cooperate, and each owns exactly one thing:

```text
┌──────────────────────────┐   I2S (audio)    ┌───────────────────────────┐
│  XIAO ESP32-S3           │◄────────────────►│  reSpeaker XVF3800        │
│  Wi-Fi + WebSocket       │   I2C (control)  │  mics, AEC, beamforming,  │
│  the "dumb endpoint"     │◄────────────────►│  amplifier, LED ring      │
└───────────┬──────────────┘                  └───────────────────────────┘
            │ Wi-Fi: WS /ws/voice
            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Your server (Docker)                                                   │
│  VAD → Groq STT → LangGraph → OpenAI TTS → streamed PCM back            │
└─────────────────────────────────────────────────────────────────────────┘
```

The split is deliberate, and it is the most useful thing to understand about this project:

- **The XVF3800 does the DSP.** Echo cancellation (so the microphones do not hear the speaker),
  beamforming (so the device listens in one direction), the LED ring and the mute button. It is a
  separate chip with its own firmware, and the ESP32 does not program it — the ESP32 *configures*
  it over I2C and then merely moves bytes.
- **The ESP32 moves bytes.** Capture audio, chop it into 20 ms frames, put them on a socket; take
  the answer's audio off the socket and push it back out. No speech detection, no wake word, no
  audio processing in the firmware.
- **The server decides everything interesting.** When a turn starts and ends, what language it is
  in, what the answer says, and how it sounds.

The payoff is that changing behaviour — a new wake phrase, a different voice, a different model —
is a server change that needs no reflash. The firmware is the dumbest possible endpoint on
purpose; see the *why this shape* note under D1.3 in
[`implementation_plan.md`](implementation_plan.md).

### The wake word lives on the server

Through the 19.7-hour soak the satellite streamed continuously, so anything said in the room
became a turn — transcripts like *"and then they'll just kiss you"* and *"in the night"* came from
ambient conversation, and any noise could also cut an answer short by flushing the playout.

A server-side gate now sits in front of that, and **this firmware did not change to get it**:
it still sends only `session_id`, `device_id` and `output_sample_rate`, which the server resolves
to `wake_word: "auto"`. The practical differences on the device:

- room audio is scored and discarded, so it no longer opens a turn, flushes playback, or moves the
  LED out of SPEAKING;
- saying **"Hey Jarvis"** is what starts a turn, and saying it over an answer is what interrupts
  one;
- a bare "Hey Jarvis" lights LISTENING and waits for the command — but only for
  `VOICE_WAKE_WORD_WINDOW_SECONDS` (6 s by default);
- the `ready` frame now carries `wake_word: true`, so a device could show whether it is gated
  without any firmware change to *send* anything.

The phrase is a server setting (`VOICE_WAKE_WORD_PHRASE`), which is the point of putting it
there: changing it, or disabling the gate entirely, is a config change and a restart, not a
reflash. Phrase and model details, and the honest state of the measurements, are in
[`docs/voice.md`](voice.md#wake-word-server-side-openwakeword).

---

## 2. Hardware orientation

### The two boards

| Board | What it is | What runs on it |
|---|---|---|
| Seeed **XIAO ESP32-S3** | a stamp-sized ESP32-S3 module: 8 MB flash, 8 MB **octal PSRAM**, 240 MHz | our firmware |
| Seeed **reSpeaker XVF3800** carrier | 4-microphone array, XMOS XVF3800 DSP, speaker amplifier, 12-LED ring | vendor firmware, v1.0.9 |

The XIAO plugs into the carrier. The carrier exposes **two USB-C ports**, which trips up everybody
once:

| Port | Position | What it does |
|---|---|---|
| **XIAO** port | on the XIAO module itself | flashes the ESP32 and is the **serial console** (`/dev/ttyACM0`) |
| **XMOS** port | on the carrier, next to the 3.5 mm jack | powers the whole board; used for **DFU** (flashing the XVF3800) |

Power the board from **one** source at a time. The XIAO port powers only the ESP32; the XMOS port
powers the carrier and its amplifier. A phone charger works — the board draws well under 1 A, so
any 5 V/2 A supply is comfortable.

The unit documented here is `xiao-68ee8f511704`: the `device_id` derives from the ESP32's MAC
address, and the XVF3800's DFU serial is `114993702262400687`.

### The two buses

| Bus | Pins | Carries |
|---|---|---|
| I2C | SDA GPIO5, SCL GPIO6, address `0x2C` | control: version, LED ring, amplifier, mute button |
| I2S | BCLK GPIO8, WS GPIO7, DIN GPIO43, DOUT GPIO44 (**MCLK unused**) | audio: 16 kHz, 32-bit stereo |

Both are worth understanding conceptually:

- **I2C** is a slow two-wire control bus: one clock line, one data line, devices distinguished by
  address. It is how the ESP32 says "set the ring green" or "tell me your firmware version".
- **I2S** is a fast audio bus: a bit clock, a word-select line marking left/right, and one data
  line per direction. It runs continuously and carries a **hard real-time deadline** — a frame must
  be shifted out every 20 ms or there is an audible gap. That deadline is why the firmware's task
  structure looks the way it does (§5).

### Why the ESP32 is the I2S master

The XVF3800 runs the **slave** image (`intdev-lr16-sqr-i2c`: I2S mode, 16 kHz, square array, I2C
control), so the ESP32 generates the clocks. Nothing is resampled anywhere: the bus runs at 16 kHz
and the server contract is 16 kHz.

The one non-obvious detail: the bus carries **32-bit stereo**, but the wire format is **16-bit
mono**. The firmware reads the **left** channel and shifts right by 16 bits. In firmware 1.0.9 both
output channels carry ASR beam 0 and are identical, so the choice is about keeping the downstream
contract honest while halving the data.

---

## 3. Toolchain from zero

This section costs a beginner the most time, so it is written as cause and effect rather than a
list of commands.

### What ESP-IDF actually is

ESP-IDF is Espressif's SDK: a build system (CMake + Ninja), the chip-specific compiler
(`xtensa-esp32s3-elf-gcc`), a Python environment holding the flashing and monitoring tools, and a
large library tree for Wi-Fi, I2S, I2C and the rest.

Its installer writes to **four** places, and knowing which is which explains most errors:

| Location | What lives there |
|---|---|
| `~/esp/esp-idf` | the SDK source, a git clone pinned to a tag |
| `~/.espressif` | the downloaded toolchains and the Python virtualenvs |
| `~/.espressif/python_env/idf5.4_py3.12_env` | a venv whose *name encodes the Python version* |
| `<project>/build` | compile output, plus `compile_commands.json` for editors |

### Step 1 — install the SDK

```bash
git clone -b v5.4.4 --recursive https://github.com/espressif/esp-idf.git ~/esp/esp-idf
cd ~/esp/esp-idf
./install.sh esp32s3
```

`--recursive` is not optional: the SDK pulls in dozens of submodules (mbedTLS, lwIP, the toolchain
wrappers). Without it the build fails much later with missing headers that look like your own
mistake. `install.sh esp32s3` downloads the toolchain for that one chip — the full set is several
GB, one target is a few hundred MB.

The version is pinned on purpose. `v5.4.4` is what this project is tested against; a newer major
release moves APIs and silently invalidates every instruction here.

### Step 2 — the Python trap on this machine

Skipping ahead produces this, and it is worth understanding rather than working around:

```text
ERROR: ESP-IDF Python virtual environment
"/home/minipc/.espressif/python_env/idf5.4_py3.14_env/bin/python" not found.
Please run the install script to set it up before proceeding.
```

Read the path: `idf5.4_py3.14_env`. The installer built its virtualenv with **Python 3.12**, the
`python3` at the time. Later, Ubuntu's `python3` became **3.14**, and `export.sh` derives the venv
directory name from the *current* `python3`. It therefore looks for a directory named after 3.14,
which was never created. The install is not broken; the lookup key changed.

The fix is a `python3` shim that reports **3.12**, placed **first on `PATH`** — one symlink, and it
survives upgrades of the system Python. The symlink alone is not enough: `install.sh` resolves
`python3` through `PATH`, so if the shim's directory is not on `PATH` the installer still finds
`/usr/bin/python3` and reproduces the failure above.

Run exactly this, in order:

```bash
# 1. drop the broken venv, so it cannot be reused by name
rm -rf ~/.espressif/python_env/idf5.4_py3.14_env

# 2. create the shim (any 3.12 interpreter works; this one came from uv)
mkdir -p ~/.local/share/esp-idf-python
ln -sf "$(uv python find 3.12)" ~/.local/share/esp-idf-python/python3

# 3. put it FIRST on PATH — this is the step that is easy to miss
export PATH="$HOME/.local/share/esp-idf-python:$PATH"

# 4. prove the shell now resolves 3.12 (must print Python 3.12.x)
which python3 && python3 --version

# 5. install: the venv it creates is now named idf5.4_py3.12_env
cd ~/esp/esp-idf && ./install.sh esp32s3
```

What the last command must print, near the end:

```text
Creating a new Python environment in /home/<you>/.espressif/python_env/idf5.4_py3.12_env
```

`3.12` in that directory name **is** the success criterion — it is exactly the key `export.sh`
computes. `export PATH=...` only affects the current shell, which is all `install.sh` needs;
`firmware/env.sh` (Step 4) prepends the same directory itself, so later terminals need nothing.

The general lesson: *when a tool builds a path out of an interpreter version, that version is part
of the contract.* Never "fix" this class of problem with `sudo` or by editing the venv by hand.

### Step 3 — CMake and Ninja without root

ESP-IDF needs `cmake` and `ninja`, and installing them system-wide wants `sudo`. `install.sh` does
**not** install them on Linux: both are marked `"install": "on_request"` in ESP-IDF's own
`tools/tools.json` (only Windows gets them automatically), so a fresh `install.sh esp32s3` finishes
"successfully" and the build then fails with `cmake: command not found`.

Install them into a self-contained directory instead and prepend it to `PATH`:

```bash
uv venv ~/.local/share/idf-build-tools
uv pip install --python ~/.local/share/idf-build-tools/bin/python cmake ninja
~/.local/share/idf-build-tools/bin/cmake --version | head -1   # must print a version
```

Both packages ship their binaries in `.../bin`, so one `PATH` entry covers both. Nothing outside
your home directory changes, and deleting the directory removes the toolchain. `firmware/env.sh`
fails with an explicit message if this directory is missing, rather than letting the first `cmake`
call inside the build produce a confusing error.

### Step 4 — the `esp-idf` shell function

`export.sh` sets `PATH` and `IDF_PATH`, and it can only do that for **the shell that runs it**. Run
it as `./export.sh` and it happens in a throwaway child process: `idf.py` then "does not exist" in
your terminal, which looks like a broken install.

Two ways to make it stick:

```bash
# option A — once per terminal, from the repo root
source firmware/env.sh

# option B — once per machine: write the absolute path of THIS clone into ~/.bashrc
# (run from the repo root; the path is resolved for you, so it is correct on any machine)
echo "esp-idf() { . \"$PWD/firmware/env.sh\"; }" >> ~/.bashrc
exec bash
```

Then a new terminal is simply:

```bash
esp-idf
```

**Why a function and not a script.** A shell function runs in *your* shell, so the environment it
sets survives the call. A script (`bash env.sh`) runs in a child process, and every export dies
with it. That is the entire difference, and it is why `source` — or a function — is mandatory.

The committed [`firmware/env.sh`](../firmware/env.sh) does the work: it validates the paths,
prepends them once (safe to call repeatedly), sources `export.sh`, and prints where each piece came
from, so a wrong toolchain is obvious immediately instead of surfacing as a confusing compiler
error.

### Step 5 — verify before you build

```bash
esp-idf
```

Healthy output names four things:

```text
idf.py      : /home/<you>/esp/esp-idf/tools/idf.py
xtensa gcc  : /home/<you>/.espressif/tools/xtensa-esp32s3-elf/.../bin/xtensa-esp32s3-elf-gcc
python      : Python 3.12.x
IDF version : v5.4.4
```

If it says `Python 3.14`, the shim is not first on `PATH` and the next `idf.py` command will fail
with the venv error from Step 2.

### Editors

VSCode needs no extension. What it needs is `build/compile_commands.json`, written by the first
build: the complete list of every include path and define. `.vscode/c_cpp_properties.json` points
IntelliSense at it, which is why **a build must run once** before `#include` lines resolve. The
ESP-IDF extension is optional and mostly duplicates what `idf.py` already does here.

---

## 4. `idf.py` day to day

`idf.py` is a thin dispatcher: every subcommand is a CMake target plus a Python wrapper. The ones
that matter:

| Command | What it does |
|---|---|
| `idf.py build` | configure if needed, then compile and link. Writes `build/satellite_xvf3800_xiao.bin` |
| `idf.py flash` | write bootloader, partition table and app over the serial port |
| `idf.py app-flash` | write **only** the app — faster, and enough when you have not changed partitions |
| `idf.py monitor` | attach to the serial console, decode addresses, decode panics |
| `idf.py -p /dev/ttyACM0 flash monitor` | the everyday command: flash, then watch |
| `idf.py menuconfig` | the configuration UI; writes `sdkconfig` |
| `idf.py fullclean` | delete `build/` — the fix for "why is it still building the old file" |
| `idf.py erase-flash` | wipe the whole chip, including NVS. Use when stale settings are suspected |
| `idf.py size` | how much flash and RAM each component costs |

Two habits pay for themselves immediately:

- **`app-flash` between rebuilds.** `flash` rewrites the bootloader and partition table too, which
  is rarely what you want and takes longer.
- **`fullclean` when a change seems ignored.** CMake dependency tracking is good but not psychic;
  a renamed header or a changed `sdkconfig` can leave stale objects.

### Monitor keys

| Key | Action |
|---|---|
| `Ctrl-]` | leave the monitor |
| `Ctrl-T` `Ctrl-H` | print the help of all the shortcuts |
| `Ctrl-T` `Ctrl-R` | reset the chip |
| `Ctrl-T` `Ctrl-F` | re-flash from inside the monitor |

### Capturing a log you can keep

**For a soak, do not do this by hand.** `make soak-up` attaches the monitor itself, records it as a
managed collector, and refuses to start if the board is missing, unreadable or already held — see
[`soak.md`](soak.md#the-satellite-stream). Attaching also **resets the board**, which is deliberate at
the start of a soak: the boot banner it prints is where the report gets the firmware version.

The recipe below is for watching the device yourself, and for a capture outside a soak.

`idf.py monitor` shows one screenful and forgets the rest. For anything longer than a minute, run
it inside `screen` and tee it to a file:

```bash
cd firmware/satellite-xvf3800-xiao
esp-idf
screen -L -Logfile ~/soak.log -S soak idf.py -p /dev/ttyACM0 monitor
```

- `-S soak` names the session; `Ctrl-a`, then `d` **detaches** it, and it keeps running with no
  terminal open.
- `-L -Logfile` is the point: the file survives a closed terminal, and the boot-time baseline we
  compare against can be 24 hours old.
- `screen -r soak` reattaches. `screen -ls` lists sessions. `screen -S soak -X quit` stops one.

The device is not affected by any of this: closing the monitor closes the serial port, and the
satellite keeps running and reconnecting on its own. You only lose visibility.

### `Path '/dev/ttyACM0' is not readable`

The port is there, but your user may not open it:

```text
Error: Invalid value for '-p' / '--port': Path '/dev/ttyACM0' is not readable.
```

Check the two halves — the owning group, and whether you are in it:

```bash
ls -l /dev/ttyACM0     # crw-rw---- 1 root dialout 166, 0 ...
id -nG                 # dialout must appear in this list
```

Serial devices belong to the `dialout` group on Debian/Ubuntu, and a fresh account is usually not
a member. Add yourself once, then get a shell that carries the new group:

```bash
sudo usermod -aG dialout "$USER"
newgrp dialout         # this terminal only; the next login session has it automatically
id -nG                 # must now list dialout
```

`newgrp` opens a subshell, so re-load the toolchain inside it (`source firmware/env.sh`) before
running `idf.py` again. No reboot and no udev rule are needed, and nothing is loosened for other
users.

**A newly opened terminal does not help.** Groups are attached to a process when it starts and can
never be added later, so every terminal spawned by a long-running parent — an IDE's integrated
terminal, a terminal multiplexer, a remote session — inherits the old, group-less credentials no
matter how new it is. Only `newgrp dialout` in that terminal, a full restart of the editor (not a
window reload), or a fresh login gets you the group. Verify with the two checks above rather than
assuming, and note that `screen -L` **appends** to its logfile, so a capture that failed once will
keep its error output at the top of the soak file — start a fresh filename.

### `Writing to serial is timing out`

This warning comes from `idf_monitor` on the **host**, not from the firmware:

```text
--- WARNING: Writing to serial is timing out. Please make sure that your application supports
an interactive console and that you have picked the correct console for serial communication.
```

Two halves explain it. The firmware **never reads its console** — nothing initialises a UART or
USB-Serial-JTAG reader — so the chip's 64-byte input FIFO fills once and then rejects everything.
Meanwhile the monitor forwards your keystrokes to the port; each write blocks until it times out,
and the warning prints once every three failures.

So: harmless, and a sign that something is feeding the monitor's stdin (a held key, mouse reports,
a terminal answering an escape query). It floods the log if you leave a terminal attached and
active. Detach with `Ctrl-a`, `d` and it stops. When analysing a capture, filter it out:

```bash
grep -v "Writing to serial is timing out" ~/soak.log > ~/soak.clean
```

---

## 5. How the firmware works

### The task model

FreeRTOS gives us independent loops with their own stack, priority and core. The ESP32-S3 has two
cores, and the split is chosen so that network work can never starve the audio clock:

| Task | Core | Priority | Stack | Job |
|---|---|---|---|---|
| `playout` | 1 | 11 (highest) | 4096 | pull samples from the playout ring, write them to I2S every 20 ms |
| `capture` | 1 | 10 | 8192 | read I2S every 20 ms, hand the frame to the uplink ring |
| `uplink` | 1 | 9 | 4096 | drain the uplink ring and `send` it on the socket |
| `satellite` (supervisor) | 0 | 5 | 6144 | own the socket lifecycle: connect, retry, keepalive, stats, LED policy |
| main (`app_main`) | 0 | 1 | 3584 default | wiring only: start everything, then poll the mute button and drive the heartbeat |
| websocket client (library) | — | — | 6144 | receive, reassemble and dispatch server frames |

Priority order is not arbitrary. `playout` is highest because it has the hard deadline: if it misses
a frame the speaker clicks. `capture` comes next for the same reason on the input side. `uplink`
sits below both so a slow or blocked send can never delay audio. The supervisor runs on the other
core at a low priority because it does bookkeeping and must not compete with timing-critical work.

`uxTaskGetStackHighWaterMark` reports **bytes** in ESP-IDF (not words, as on some other ports), and
the firmware logs it once a second as `capture errors=0 (stack free 5976 bytes)`. That number is the
*margin that was left at the worst moment*, so it only ever falls. A steady value over 19 hours is
the leak check: it means the deepest call path never got deeper.

### The path, in both directions

```text
MIC  → XVF3800 (AEC + beamforming) → I2S 32-bit stereo
     → capture_task: take left channel, >>16, 320 samples
     → uplink ring (12 frames = 240 ms)
     → uplink_task: send 640-byte binary frame
     → Wi-Fi → WS /ws/voice

WS /ws/voice → binary PCM frames → ws client task
     → satellite_on_ws_binary → playout ring (30 s, in PSRAM)
     → playout_task: pull 320 samples, pad with silence if empty
     → I2S 32-bit stereo → XVF3800 (DSP + amplifier) → SPK
```

### Why the uplink has its own task

This is the most instructive change in the whole build, because the bug it fixed was invisible in
the code and obvious in the audio.

Originally `capture_task` called `ws_send_audio()` directly. The library holds its internal lock
across an entire receive-and-dispatch cycle, so during playback — when the server is streaming a
large answer — a send could wait a long time. The log filled with

```text
W (…) ws: Could not lock ws-client within 20 timeout
```

and frames were dropped (`dropped=15`), which corrupted the *transcript* of the next turn, because
the server heard a gappy utterance: `heard: is`, `heard: And then I'll just...`.

The fix is a classic producer/consumer decoupling: `capture_task` appends to a small ring and never
blocks on the network; `uplink_task` does the sending and absorbs the latency. Costs 240 ms of
buffering and one task; removes an entire class of timing coupling. **Any time a hard-deadline
producer talks to a soft-deadline consumer, insert a buffer and a separate task.**

### The playout ring and why it grew

The answer's audio arrives faster than it plays. Measured on this server: about **5× real time**.
So while the ring is filling, playback is also draining it, and an answer of *D* seconds needs a
ring of roughly `0.8 × D`.

Two bugs came from getting this wrong:

1. **10 s ring.** A 16.7-second answer lost four seconds of speech. Because the writer keeps trying
   while the reader drains, the drops were interleaved rather than appended — the audio had words
   spliced out of the middle, which sounded like several words crammed together.
2. **No prebuffer.** Playback started on the first bytes to arrive, caught up with the tail of the
   server's first sentence, and went silent until the next one. Five audible holes per reply.

The current design is a 30-second ring — 480 000 samples, 960 KB, in **PSRAM** — plus a 350 ms
**prebuffer**: playback refuses to start until that much has accumulated, and re-arms after running
dry. This is the same idea as a video player's buffer, and it is the standard cure for a bursty
source.

`playout ran dry after 160 of 320 samples` at the end of an answer is the tail half-frame, not a
fault. A shortfall in the *middle* of an answer is audible and means the ring emptied.

### Mute

The mute button belongs to the XVF3800, which reports it as a bit in a GPI read. Two details:

- **Polling, not interrupts.** `app_main` polls every 20 ms and only accepts a level after it has
  held for 3 polls (60 ms), because a mechanical button bounces for a few milliseconds. The first
  version polled once every 5 seconds and simply missed presses.
- **The latch lives in `satellite.c`, not `app_main.c`,** because it is the audio path that has to
  honour it: while muted, `satellite_queue_mic` substitutes silence rather than skipping frames, so
  the stream's timing does not change.

### LED policy

The ring is driven over I2C by the XVF3800, never by an ESP32 GPIO. Every state is a **steady**
colour — see the table in the README. The original design used `LED_EFFECT_BREATH`, which at
brightness 48 is effectively invisible across a room; that cost an hour of debugging twice.

The rule that came out of it: **an indicator you cannot see from where you stand is not an
indicator.** Prefer a steady colour at full brightness over a tasteful animation.

| State | Colour | When |
|---|---|---|
| `LED_STATE_OFF` | none | boot, before the ring is configured |
| `LED_STATE_IDLE` | blue `0x0000FF` | not in a session, or the socket dropped |
| `LED_STATE_LISTENING` | green `0x00FF00` | session ready, and again when the server reports speech |
| `LED_STATE_THINKING` | orange `0xFF8000` | a transcript arrived, the answer is being produced |
| `LED_STATE_SPEAKING` | cyan `0x00FFFF` | answer audio is playing |
| `LED_STATE_MUTED` | red `0xFF0000` | the microphone is muted |

Two things to keep in mind when reading a log: **blue is not a wake-up**, it is the absence of a
session — a soak counted 490 IDLE transitions in 47 hours, one per reconnect; and the mute state is
per-call-site, so a handler that forgets `s_muted` paints green over a muted device.

## Measuring a soak

A single session proves the contract; a 47-hour one proves the product. `make soak-up` starts all
three collectors — the two container logs and the board's serial console — so there is nothing to
start by hand; the capture procedure, the device-clock anchor (the serial log has no wall clock) and
the metric dictionary are in [`soak.md`](soak.md), and `backend/scripts/soak_report.py` turns the three
logs into one comparable report.

### Reconnecting

`satellite.c` owns the socket, so one place decides the retry policy:

- A drop is reported from the event handler by setting a flag; the **supervisor** acts on it,
  because it is the only task allowed to join the client task. Calling `ws_stop()` from inside the
  client's own callback is a self-join deadlock.
- Backoff doubles from 1 s to 30 s.
- When the Wi-Fi radio returns, the delay is **reset** and a connection is attempted immediately.
  Acting on the radio's *transition* rather than its *level* is what makes this correct: acting on
  the level would reset the backoff continuously and defeat it on a flapping link.
- A keepalive goes out if nothing has been sent for 20 s, because the server drops a silent socket
  at 60 s.

---

## 6. The XVF3800 side

### Control framing

The chip groups commands by **resource id**. A write is:

```text
[resid, cmd, payload_len, payload…]
```

and a read is the same frame with bit 7 set in the command byte, expecting
`[status, data…]` back. A non-zero status means the device rejected the command — which is why
every LED call in `leds.c` goes through an `expect()` helper that logs failures instead of
discarding them. Silently ignoring an I2C error is how the green "ready" LED stayed dark for an
entire debugging session.

The resources and commands this firmware uses:

| Resource | Id | Command | Id | Purpose |
|---|---|---|---|---|
| `APP` | 48 | `VERSION` | 0 | firmware version |
| `APP` | 48 | `BUILD` | 1 | build string (`intdev-lr16-sqr-i2c`) |
| `GPO` | 20 | `GPO_WRITE` | 1 | amplifier enable (GPO 31, **active-low**) |
| `IO_CONFIG` | 36 | `LED_EFFECT` | 12 | 0 = off, 3 = single colour |
| `IO_CONFIG` | 36 | `LED_BRIGHTNESS` | 13 | 0–255 |
| `IO_CONFIG` | 36 | `LED_COLOR` | 16 | 24-bit RGB |
| `IO_CONFIG` | 36 | `MUTE_FUNCTION` | 8 | 0 = button becomes a pure event |
| `IO_CONFIG` | 36 | `GPI_VALUES` | 6 | bit 0 = mute button held |

### Three quirks worth remembering

- **`MUTE_FUNCTION_ENABLE=0` is not persisted.** The chip's own mute toggle comes back after every
  boot, so the firmware re-sends it during init. Anything set over I2C should be assumed lost on
  reset unless the docs say otherwise.
- **GPO 31 is active-low and doubles as a hint.** Driving the amplifier enable also tells the DSP
  that a speaker is active, which reduces microphone sensitivity. Leaving it driven when nothing is
  playing makes the device hard of hearing.
- **The factory image is not the voice image.** Units ship with 1.0.4, where channel 0 carried the
  conference output and channel 1 the ASR output. 1.0.9 routes both to ASR beam 0 and retunes the
  AEC and system delay that barge-in depends on. The full reasoning and the DFU procedure are in
  the README — including that `-a 1` is the upgrade slot and `-a 0` destroys the recovery path.

---

## 7. Bring-up checklist for a new unit

The order matters: each step proves one thing, so a failure has exactly one candidate cause. This
is the sequence used here, and every step is reversible.

1. **Toolchain** — `esp-idf` prints the right `idf.py`, compiler, Python and IDF version (§3).
2. **Build** — `idf.py build` completes; the binary is under 1 MB with 12% of the app partition
   free.
3. **Flash and console** — `idf.py -p /dev/ttyACM0 flash monitor` shows a boot banner on the
   **XIAO** port. No banner means the wrong port, a missing cable, or no permission on the device
   node (fix: the `dialout` group, see "`Path '/dev/ttyACM0' is not readable`" above).
4. **Command the chip** — I2C init logs the XVF3800 version and build string. Getting nothing here
   means wiring or address, not firmware logic.
5. **Witness the LED** — `LED_STATE_IDLE` should be a steady blue ring. This proves the write path
   end to end, without touching audio.
6. **Wi-Fi** — the board joins and logs an IP.
7. **Capture** — the peak meter shows a live level when you speak, and silence otherwise; peaks
   should reach the thousands, not the tens. Tens means the I2S format is wrong.
8. **Playback** — a 1 kHz tone every 10 s is audible. Silence means amplifier, GPO, or clocking.
9. **Socket** — the session handshake completes and the server logs a session open. A refusal here
   is almost always the API key or the port; see *Configuration* in
   [`firmware/README.md`](../firmware/README.md).
10. **A full turn** — speak, watch `heard:`, then `reply audio:` and hear the answer.

Then the acceptance tests, which are about endurance rather than features: barge-in, Wi-Fi killed
for 30 s, and a multi-hour soak (§9).

### The device's identity

Two identifiers matter, and they come from different chips:

| Identifier | Where it comes from | Used for |
|---|---|---|
| `device_id`, e.g. `xiao-68ee8f511704` | the ESP32's Wi-Fi MAC address, read at boot and formatted as `xiao-` + 12 hex digits | the `start` frame, then the server's session id and every trace and log line |
| XVF3800 DFU serial, e.g. `114993702262400687` | the audio chip, readable with `dfu-util -l` while in recovery | the physical board identity, if you ever need to distinguish two identical units |

The MAC is stable across reflashes and reboots, which is what makes it usable as a key. Changing the
board means changing the `device_id`, so a session's history does not silently continue under a new
device. The firmware builds the `session_id` it sends in the start frame as `sat-<device_id>`
(`ws_client.c`), and the server adopts it — that prefix is what keeps satellite sessions
distinguishable from browser ones in the trace store.

There is no pairing step and no per-device secret beyond the shared API key: the firmware carries
the key from `menuconfig`, and any device holding it can open a voice session until
`voice_max_sessions` limits are reached.

### When it stops answering

Work down this ladder; each rung rules out everything above it.

1. **Look at the ring.** Blue = booted, no session. Green = session ready, so the fault is in the
   audio path, not the network. Orange frozen = waiting for a reply that never came. Dark = no
   power, or the firmware never started — check the console first.
2. **Read the heartbeat.** `heartbeat: online ready` every 10 s means the socket is up. `offline`
   means it is not, and the supervisor is already retrying; a `session lost` line above says why
   (a close code) or that there was no close frame.
3. **Read the capture lines.** `capture errors=0 (stack free N bytes)` with `N` constant is healthy.
   Repeated errors mean I2S trouble; a falling `N` means a stack problem.
4. **Check the server side.** `docker compose … logs backend` should show `voice session open` for
   this device. No session at all means the device never reached the socket — go back to step 2.
   A session that opens and closes repeatedly is the silent-TCP-death pattern (§9).
5. **Check the key and the port together.** `unauthorized` / close `1008` means one of them is
   wrong: 8080 is nginx and wants the production key, 8000 is uvicorn and wants the development key.
6. **Check the network path.** Zero `wifi:state: run -> init` lines rules out the radio. If the
   losses have no close frame, something in the path is dropping the flow; the router's TCP session
   timeout is the first thing to look at.
7. **Re-flash only after all of the above.** `idf.py app-flash` is the cheap test; a full
   `erase-flash` also wipes NVS, which is only occasionally the culprit.

---

## 8. Troubleshooting catalogue

Everything below actually happened during this build. The pattern is always the same: a symptom
that looks like firmware logic turns out to be one layer lower or higher.

### Toolchain and build

| Symptom | Cause | Fix |
|---|---|---|
| `idf5.4_py3.14_env not found` | `export.sh` derives the venv name from the current `python3`, which changed after install | shim a 3.12 `python3` first on `PATH` (§3 step 2) |
| `idf.py: command not found` right after running `export.sh` | it ran in a child process, so the exports died with it | `source` it, or use the `esp-idf` function (§3 step 4) |
| `cmake` or `ninja` not found | ESP-IDF needs both; installing them system-wide needs `sudo` | `uv venv ~/.local/share/idf-build-tools` (§3 step 3) |
| IntelliSense cannot resolve `#include "esp_log.h"` | `compile_commands.json` does not exist until a build runs | `idf.py build` once, then reload the window |
| build ignores an edit | stale objects after a rename or an `sdkconfig` change | `idf.py fullclean && idf.py build` |

### XVF3800 and DFU

| Symptom | Cause | Fix |
|---|---|---|
| `dfu-util: LIBUSB_ERROR_ACCESS` | the DFU device node is not writable by a normal user | `sudo dfu-util …` |
| `dfu-util -l` lists nothing | the I2S image does not enumerate over USB; only the carrier's recovery image does | hold **Mute**, replug the **XMOS** port, release on the red blink |
| wrong firmware on the chip | units ship 1.0.4, which routes channels differently and lacks `MUTE_FUNCTION_ENABLE` | flash 1.0.9 into `-a 1`; never `-a 0` |
| `version` reads 1.0.4 after flashing | `-a 1` was written but the factory slot booted | power-cycle; the upgrade slot must be selected |

### Audio

| Symptom | Cause | Fix |
|---|---|---|
| peaks in the tens instead of thousands | wrong I2S width or channel | 32-bit stereo, take **left**, `>>16` |
| first frames are garbage | DMA startup transient | drain a few frames at init |
| `***ERROR*** A stack overflow in task capture` | four 640-byte buffers plus soft-float on a 4096-byte stack | buffers to `.bss`, stack 8192 |
| LED ring dark, or one state missing | I2C write errors were discarded | route every call through a helper that logs failures |
| a state lights but is invisible across the room | `LED_EFFECT_BREATH` at low brightness | steady colour, full brightness |
| answer truncated, words spliced together | ring too small; the writer interleaves drops with the reader's drain | size the ring for the longest answer (§5) |
| holes in the middle of sentences | playback started on the first burst | prebuffer before starting |
| `ring full` in the log | the answer exceeded the ring | cap the spoken length server-side, not more buffer |

### Socket and session

| Symptom | Cause | Fix |
|---|---|---|
| close code `1008` / `unauthorized` | dev key against the prod port, or the reverse | 8080 = nginx + prod key, 8000 = uvicorn + dev key |
| `Could not lock ws-client within 20 timeout`, `dropped=N` | sending from the capture task while the library held its lock | own task + ring for the uplink (§5) |
| next turn transcribes as `heard: is` | the previous gap corrupted the utterance the server heard | same fix as above |
| `session lost` every few minutes, zero Wi-Fi drops | the TCP flow stops being forwarded; no CLOSE frame from either end | path problem, not firmware; reconnect handles it |
| the radio returns but the device waits 30 s | backoff kept running through the outage | reset on the radio's **transition** |
| reported session lifetime equals the uptime | microseconds mixed with milliseconds | keep one unit throughout |
| `error: none (socket errno 1007380196)` | fields only populated for transport/handshake errors | read them only for those types |
| `rst:0x15 (USB_UART_CHIP_RESET)` at the top of a log | the monitor reset the chip on attach | not a crash |

### Behaviour that looks like a bug but is not

| Symptom | Explanation |
|---|---|
| `resampling=True` in the server log | the device asked for 16 kHz and the TTS default is 24 kHz; this is the correct, expected conversion |
| `playout ran dry after 160 of 320` | the tail half-frame at the end of an answer |
| `Writing to serial is timing out` | the firmware never reads its console; the host is pushing keystrokes (§4) |
| English speech transcribed in Portuguese | turn one sends no language hint, so the provider auto-detects; a cognate like *"Brasil"* swings it. See [`voice.md`](voice.md) known limitations |
| ambient conversation becomes a turn | fixed: the server-side gate ignores audio without "Hey Jarvis" |

---

## 9. Measuring it

Feature tests say "it worked when I tried it". A soak says "it did not degrade". They find different
bugs, and the second kind needs a procedure.

### Run both captures

**`make soak-up` does all of this**, for the device and the server at once, and puts the files in a
per-run directory with a report tool attached — see [`soak.md`](soak.md#start-to-finish). Use it unless
you specifically want a `screen` window in front of you; the manual recipe below is what it replaces,
and it is still the way to *watch* the device rather than measure it.

The device log and the server log are two halves of the same conversation, and lining them up
afterwards is much easier if both have timestamps and both start at the same moment.

```bash
cd firmware/satellite-xvf3800-xiao
esp-idf
screen -L -Logfile ~/soak.log -S soak idf.py -p /dev/ttyACM0 monitor
# Ctrl-a, d
```

```bash
cd "$(git rev-parse --show-toplevel)"   # the repo root, wherever the clone lives
export APP_VERSION=$(sed -n 's/.*__version__ = "\(.*\)".*/\1/p' backend/app/_version.py)
screen -L -Logfile ~/backend-soak.log -S backend-logs \
  docker compose -p taylored-assistant-prod \
  -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.prod.yml \
  --env-file infra/compose/.env.prod --profile observability \
  logs -f --tail 0 --timestamps backend frontend
# Ctrl-a, d
```

Three traps in that second block:

- **`APP_VERSION` is exported by the Makefile, not by `.env.prod`.** Type the Compose command by
  hand and interpolation fails with *required variable APP_VERSION is missing a value*. The `sed`
  above reproduces what `make` does, taken from `backend/app/_version.py`.
- **`--tail 0`** starts the file at the soak's start; without it you replay hours of history.
- **Capture `backend frontend`, not everything.** `frontend` is the nginx container that publishes
  8080 and proxies `/ws/voice`, so socket problems appear as an nginx access line on one side and a
  session line on the other. ClickHouse and Langfuse only bury it.

Docker's `json-file` driver already keeps these logs (`max-size: 50m`, `max-file: 5`), so a lost
capture is recoverable with `docker logs --since …`.

### Read the device side

With `make soak-up` the file is `~/wake-soak/<run>/satellite-soak.log`; with the manual capture it is
the `-Logfile` you chose. The greps are the same either way.

```bash
grep -c "ring full"                     ~/soak.log   # 0: no answer exceeded the ring
grep -c "playout ran dry"               ~/soak.log   # ~1 per answer: tail half-frame
grep -E "uplink overflow"               ~/soak.log   # 0
grep -E "capture errors"                ~/soak.log | tail -2   # errors=0, stack free flat
grep -cE "Guru Meditation|abort\(\)"    ~/soak.log   # 0
grep -c "session lost"                  ~/soak.log   # every one must be followed by "ws: connected"
grep -E "session ended after"           ~/soak.log   # how long each lost session lived
```

The stack figure is the leak detector: a high-water mark only ever falls, so a constant value hours
apart means the deepest call path never got deeper. A falling value is a slow leak or a newly
reached code path.

### Read the server side

```bash
grep -c "voice session open"   ~/backend-soak.log    # these two must match
grep -c "voice session closed" ~/backend-soak.log
grep -c "voice turn timing"    ~/backend-soak.log    # turns that reached the model
grep "voice turn timing" ~/backend-soak.log | head -5   # latency at the start
grep "voice turn timing" ~/backend-soak.log | tail -5   # and at the end: it must not drift
```

`first_audio_ms` flat across the day means nothing is accumulating. If the last turns are slower
than the first, suspect growing state — a checkpointer table, an unbounded cache.

### What the soak actually found

Recorded in the README's bring-up table, and worth restating because both are easy to miss:

- **Spoken length over ~38 s was truncated.** Six of 51 answers hit `ring full`; the median answer
  was 12.8 s and the longest 48.1 s. No ring size fixes this properly — the answer is too long for
  a voice assistant.
- **Sessions died silently, ~185 times in 19.7 hours, with zero Wi-Fi drops.** Every loss was a
  stalled send with no CLOSE frame: the flow stopped being forwarded rather than being closed.
  Recovery cost about 1.2 s and always succeeded. A property of the network path, not the firmware.

---

## 10. Glossary

| Term | Meaning |
|---|---|
| **AEC** | Acoustic Echo Cancellation — the DSP subtracting the speaker's output from the microphone input so the device does not hear itself |
| **barge-in** | interrupting the assistant while it speaks; implemented here as flushing the playout ring on the server's `speech_start` |
| **beamforming** | combining several microphones to favour one direction and reject the rest |
| **DFU** | Device Firmware Update — flashing the XVF3800 over USB or I2C |
| **DSP** | Digital Signal Processor — here, the XMOS chip doing AEC, beamforming and the LED ring |
| **ESP-IDF** | Espressif IoT Development Framework — the SDK this firmware is built with |
| **I2C** | a two-wire control bus (clock + data, addressed devices) |
| **I2S** | a digital audio bus (bit clock, word select, data), with a hard per-frame deadline |
| **jitter buffer** | a buffer that absorbs bursty arrival so playback is smooth; here, the 350 ms prebuffer |
| **PCM** | raw uncompressed audio samples — what travels on the wire here |
| **PSRAM** | extra RAM on the module, slower than internal RAM but plentiful (8 MB here); the playout ring lives in it |
| **s16le** | signed 16-bit samples, little-endian — the wire format of the audio frames |
| **VAD** | Voice Activity Detection — deciding when speech starts and stops; done server-side |
| **watchdog** | a timer that resets the chip if a task stops yielding; a `Guru Meditation` panic is usually a watchdog or a bad pointer |
| **WS / WebSocket** | the protocol carrying audio and JSON both ways over one TCP connection |
| **XMOS** | the vendor of the XVF3800 chip; also shorthand for "the audio chip" |

### Where to read next

- [`firmware/README.md`](../firmware/README.md) — the reference: versions, pins, commands, log lines
- [`voice.md`](voice.md) — the socket contract, close codes, latency budget and measurement
- [`implementation_plan.md`](implementation_plan.md) — D1.3's goal, steps and acceptance criteria
- The firmware source, in this order: `app_main.c` (the wiring), `satellite.c` (the policy),
  `ws_client.c` (the socket), `audio_i2s.c` + `playout.c` (the audio path), `xvf_control.c` +
  `leds.c` (the chip), `wifi.c` (the link)
