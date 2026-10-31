# Satellite firmware

The satellite is the XIAO ESP32-S3 module seated on the reSpeaker XVF3800
carrier. It captures audio and plays the spoken answer. Speech recognition, the
model and speech synthesis all run on the server.

| Part | Firmware | Role |
|---|---|---|
| XIAO ESP32-S3 | `satellite-xvf3800-xiao/` in this directory | Wi-Fi, WebSocket, state machine, LED policy |
| XVF3800 | `respeaker_xvf3800_i2s_slave_v1.0.9_16k.bin` | microphones, echo cancellation, beamforming, amplifier, LED ring, mute button |

Two buses connect them:

| Bus | Pins | Carries |
|---|---|---|
| I2C | SDA GPIO5, SCL GPIO6, address `0x2C` | control: version, LEDs, amplifier, mute |
| I2S | BCLK GPIO8, WS GPIO7, DIN GPIO43, DOUT GPIO44 (MCLK unused) | audio: 16 kHz, 32-bit stereo |

The ESP32 is the I2S **master**; the XVF3800 runs the I2S **slave** image and
receives the clocks. Nothing is resampled anywhere: the server contract is
16 kHz and so is the bus.

## XVF3800 firmware

Required image: `respeaker_xvf3800_i2s_slave_v1.0.9_16k.bin`, from
`github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY` under `xmos_firmwares/i2s/`.

| Field | Value |
|---|---|
| Build string | `intdev-lr16-sqr-i2c` (`intdev` = I2S mode, `lr16` = 16 kHz, `sqr` = square array, `i2c` = I2C control) |
| Version | 1.0.9 |
| DFU serial | `114993702262400687` |
| Image size | 888832 bytes |
| SHA-256 | `b606c6b7e5d2c8fa219fbbf2700fd7a939e12b68efc36c3739ce6a7f2ec74e9f` |

### Why 1.0.9 rather than the shipped image

Units ship with 1.0.4. Three changes matter here:

- **1.0.7** routed **both** I2S output channels to ASR beam 0. Before that,
  channel 0 carried the conference output and channel 1 the ASR output, so the
  channel to read depended on the firmware version.
- **1.0.7** retuned the DSP: the AEC 125 Hz high-pass filter is off by default,
  the post-processing AGC maximum gain is 32 instead of 64, and the system delay
  is -30 instead of 12. Barge-in depends on those values.
- **1.0.9** added `MUTE_FUNCTION_ENABLE`. With it set to `0` the mute button
  becomes a pure event source, so the host owns mute. The value is not persisted
  in flash and must be re-sent after every boot.

### Flashing

The I2S firmware does not enumerate as a USB device, so it cannot be reflashed
over USB DFU. The board keeps a recovery image in a **factory partition** that
supports both USB DFU and I2C DFU; that is "safe mode".

1. Disconnect the **XMOS USB-C** port (next to the 3.5 mm jack). That port
   powers the whole board; the XIAO port powers only the ESP32.
2. Hold **Mute** and reconnect the XMOS cable to the computer.
3. Release the button when the red LED blinks. The board now runs safe mode.
4. `dfu-util -l` must list both slots:

   ```text
   Found DFU: [2886:001a] ... alt=1, name="reSpeaker DFU Upgrade", serial="..."
   Found DFU: [2886:001a] ... alt=0, name="reSpeaker DFU Factory", serial="..."
   Found DFU: [2886:001a] ... alt=2, name="reSpeaker DFU Data", serial="..."
   ```

   The serial is the board's identity; the backend logs it as `device_id`.
5. Write the upgrade slot:

   ```bash
   sudo dfu-util -d 2886:001a -a 1 -D respeaker_xvf3800_i2s_slave_v1.0.9_16k.bin -R
   ```

   `-a 1` is the **upgrade** slot. `-a 0` is the factory image — writing that
   destroys the recovery path and is the one mistake that cannot be undone
   without a second board. `-R` resets into the new image when the transfer
   completes. `sudo` is needed because the DFU device node is not writable by a
   normal user; without it `dfu-util` fails with `LIBUSB_ERROR_ACCESS`.
6. Verify on the next boot: `xvf_control` logs `version 1.0.9` and the build
   string `intdev-lr16-sqr-i2c`.

## Building and flashing the ESP32 side

ESP-IDF v5.4 lives in `~/esp/esp-idf` and its tools in `~/.espressif`. The two
`PATH` prefixes are not optional:

- `~/.local/share/esp-idf-python` holds a `python3` symlink to 3.12. ESP-IDF
  builds its virtualenv per Python minor version; if the system `python3` is
  newer, `export.sh` looks for a virtualenv that does not exist and refuses to
  run.
- `~/.local/share/idf-build-tools/bin` holds `cmake` and `ninja` installed
  without root.

```bash
cd firmware/satellite-xvf3800-xiao
export PATH="$HOME/.local/share/idf-build-tools/bin:$HOME/.local/share/esp-idf-python:$PATH"
. ~/esp/esp-idf/export.sh
idf.py build
idf.py -p /dev/ttyACM0 flash monitor
```

The **XIAO** USB-C port is the one that flashes and prints the console. The
**XMOS** port (next to the 3.5 mm jack) powers the whole board and is the one
used for DFU. Power the board from one source at a time.

IntelliSense reads `build/compile_commands.json`, so a build must have run once
before the editor resolves any `#include`.

## Configuration

`idf.py menuconfig` → **Satellite**. Four values, all stored in `sdkconfig`,
which is gitignored so credentials never reach the repository:

| Option | Meaning |
|---|---|
| `SATELLITE_WIFI_SSID` / `_PASSWORD` | the network to join (2.4 GHz only) |
| `SATELLITE_SERVER_URI` | `ws://<host>:8080/ws/voice` |
| `SATELLITE_API_KEY` | bearer token, sent as a query parameter |

Port **8080** is the nginx front end and expects the **production** key; port
8000 is the development uvicorn and expects the **development** key. They are
different lengths (43 vs 64 characters) and using one against the other closes
the socket with `unauthorized` — see `docs/voice.md` for the close codes.

Bump `CONFIG_ESP_MAIN_TASK_STACK_SIZE` before adding work to `app_main`. A
capture task with 2560 bytes of local buffers overflowed a 4096-byte stack and
reset the chip; audio buffers now live in `.bss` and the stack is 8192.

## LED ring

The ring is driven over I2C by the XVF3800, not by an ESP32 GPIO. Every state is
a **steady** colour: a slow pulse at low brightness is invisible across a room,
which cost an hour of debugging twice before the animation was removed.

| `led_state_t` | Value | Colour | Meaning |
|---|---|---|---|
| `LED_STATE_OFF` | 0 | — | ring dark |
| `LED_STATE_IDLE` | 1 | blue | booted, no session |
| `LED_STATE_LISTENING` | 2 | green | session ready, streaming |
| `LED_STATE_THINKING` | 3 | orange | utterance sent, waiting for the reply |
| `LED_STATE_SPEAKING` | 4 | cyan | playing the answer |
| `LED_STATE_ERROR` | 5 | red | fault |
| `LED_STATE_MUTED` | 6 | red | mute button latched |

## Reading the log

| Line | Period | Healthy value |
|---|---|---|
| `capture errors=N (stack free M bytes)` | 1 s | `errors=0`, `M` not falling |
| `uplink sent=N failed=M` | 10 s | `failed=0` |
| `heartbeat: online ready` / `offline` | 10 s | tracks the socket |
| `uplink overflow=N, playout shortfalls=M` | 60 s | both zero |
| `ring full` | on event | never — the answer exceeded the ring |
| `playout ran dry` | on event | only the final half-frame of an answer |
| `session lost` / `radio is up, connecting now` | on event | a network outage, and its recovery |

`playout ran dry after 160 of 320 samples` at the end of an answer is the tail
half-frame, not a fault. A shortfall in the *middle* of an answer is audible as
a hole in the speech and means the ring emptied — raise `PLAYOUT_SECONDS`.

## Bring-up results

Measured on the reference unit (`xiao-68ee8f511704`), with the backend on the
same LAN:

| Test | Result |
|---|---|
| Two-way turn, English and Portuguese | pass |
| Barge-in: speaking over the answer stops it and starts a new turn | pass |
| LED ring matches every state in the table above | pass |
| Wi-Fi down 83 s, then restored | pass — session re-established without a power cycle, and the next utterance worked |
| 24 h unattended soak, counters clean | _pending_ |

End-to-end latency, first audio, is about 4.5 s on this hardware: ~0.27 s
transcribe, ~2.5–2.8 s to the graph's first token, ~1.0–1.7 s to the first TTS
byte. `docs/voice.md` carries the budget and the measurement tool; the firmware
is not the bottleneck.
