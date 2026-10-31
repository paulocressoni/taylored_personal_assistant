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
| DFU serial | _record the `dfu-util -l` value; the backend will use it as `device_id`_ |
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
