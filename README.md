# FSK Audio Modem  —  v3.1
**AFSK · 1200 / 2400 / 9600 Baud · FT-817 CAT PTT · Loopback · Live Spectrum + Waterfall**

---

## Quick Start

```
install.bat          ← run once to install Python packages
python fsk_modem.py  ← launch the application
```

---

## Requirements

| Package | Purpose |
|---|---|
| `sounddevice` | Audio I/O (TX playback, RX capture, spectrum monitor) |
| `numpy` | FFT, modulation / demodulation math |
| `Pillow` | Image resize + JPEG compress · waterfall rendering |
| `pyserial` | CAT PTT control for FT-817 (optional) |

Install all at once:
```
pip install sounddevice numpy pillow pyserial
```

---

## Loopback Test — single PC, no radio

1. Install **VB-Audio Virtual Cable** (free) → https://vb-audio.com/Cable/
2. Reboot Windows
3. Open the app → Mode = **Loopback**
4. TX Device = **CABLE Input**  ·  RX Device = **CABLE Output**
5. Click **▶ RX** in the Receive panel
6. Type a message in the TX buffer → click **▶ SEND**
7. Decoded text / file / image appears immediately in the RX panel

The audio spectrum and waterfall are always active from startup — no need to start RX first.

---

## FT-817 Hardware Setup

### Audio connections (rear panel — 6-pin mini-DIN data port)

| Signal | FT-817 pin | PC connection |
|---|---|---|
| Audio IN (TX) | Pin 1 — PKT-DIN | PC Line Out (3.5mm) |
| Audio OUT (RX) | Pin 4 — PKT-OUT | PC Line In (3.5mm) |
| Ground | Pin 2 — GND | Cable sleeve |

> The data port bypasses the mic preamp and speaker de-emphasis, giving a flat audio path.
> The mic jack (8-pin RJ45 front) also works for a first test — keep PC output volume at 20–30%
> to avoid overdeviation.

### CAT PTT (front panel)

| Signal | Details |
|---|---|
| Connector | 3.5mm stereo jack, **front panel** CAT port |
| Tip | TX data to radio |
| Ring | RX data from radio |
| Sleeve | GND |
| Adapter | Any CP2102 / CH340 USB-serial dongle |

**FT-817 menu settings:**
- Menu **14** (CAT RATE): match the CAT Baud selector in the app (9600 recommended)
- Radio mode: **FM** on your chosen VHF/UHF simplex frequency
- Serial framing (handled automatically): **8N2**, no hardware flow control

**CAT PTT command bytes (FT-817 protocol):**
```
PTT ON  → 0x00 0x00 0x00 0x00 0x08
PTT OFF → 0x00 0x00 0x00 0x00 0x88
```

### Signal chain

```
[App TX] → Sound Card Line Out → FT-817 Data Port Pin 1 → FM modulates → RF
      RF → FM demodulates → FT-817 Data Port Pin 4 → Sound Card Line In → [App RX]
                              ↑
               CAT port (front) ← USB-Serial ← App (PTT control only)
```

---

## Baud Rates and Tone Pairs

| Baud | Mark | Space | VHF/UHF path | Notes |
|---|---|---|---|---|
| **1200** | 1200 Hz | 2200 Hz | Mic/speaker or data port | Bell 202 AFSK — rock solid on NFM |
| **2400** | 1200 Hz | 2200 Hz | Data port preferred | Same tones, twice the speed |
| **9600** | 3000 Hz | 6000 Hz | **9600 baud data port pin required** | Bypasses de-emphasis; loopback works fine |

> **9600 baud note:** NFM audio bandwidth (~2.5 kHz) cannot pass 3–6 kHz tones via the
> mic/speaker path. For RF use the FT-817's dedicated 9600 baud input (pin 5 on the data port)
> is required — it feeds the modulator directly, bypassing all audio filtering.
> Loopback testing on the PC works at any baud rate without restriction.

---

## Spectrum Display

The spectrum and waterfall update from a background audio monitor stream from the moment the
app opens — no need to start TX or RX first.

| Feature | Description |
|---|---|
| **Scale** | dB (logarithmic) — 60 dB dynamic range — tones stand well above noise floor |
| **Normalization** | Running adaptive ceiling with slow decay — automatic gain control |
| **Gamma** | Power curve (^0.55) lifts midtones without blowing out peaks |
| **Peak hold** | Yellow line per bin — fast attack, slow decay (~3 s fall) |
| **Waterfall** | Scrolling colour history — black → blue → cyan → yellow → white |
| **Markers** | Dashed vertical lines at MARK (green) and SPACE (pink) frequencies |
| **Tone LEDs** | MARK / SPACE indicators light when energy detected ±100 Hz around each tone |
| **SNR** | Ratio of peak tone energy to mean noise floor, updated live |
| **Freq range** | 0–4000 Hz at 1200/2400 baud · 0–8000 Hz at 9600 baud (auto-switches) |

---

## Protocol

| Parameter | Value |
|---|---|
| Frame sync | 32 × 0xAA preamble bytes + 0x7E start flag |
| Header | `[type 1B][seq 2B][total 2B][len 2B]` |
| Payload | Up to 200 bytes per frame |
| Error check | CRC-32 per frame (over header + payload) |
| Byte stuffing | HDLC-style: 0x7E and 0x7D escaped with 0x7D XOR 0x20 |
| Content types | 0x01 Text · 0x02 File · 0x03 Image |
| Image format | Resized to 800×600, JPEG compressed (adjustable quality 5–85) |
| Reassembly | Out-of-order chunks accepted; complete when all seq numbers received |

---

## Transfer Time Estimates

### 1200 baud

| Content | Compressed size | Approx. time |
|---|---|---|
| 160-char text message | ~160 B | ~2 s |
| 1 KB text file | 1 KB | ~7 s |
| 800×600 image (JPEG q=15) | ~8 KB | ~55 s |
| 800×600 image (JPEG q=30) | ~15 KB | ~100 s |

### 2400 baud — halve all times above

### 9600 baud (loopback / data port)

| Content | Compressed size | Approx. time |
|---|---|---|
| 800×600 image (JPEG q=30) | ~15 KB | ~13 s |
| 800×600 image (JPEG q=50) | ~25 KB | ~21 s |

---

## Changelog

### v3.1
- Configurable Save folder for received files and images.
- Reorganized UI Config Strip with logical grouping.
- Right-click context menus added to TX and RX text buffers.
- RX burst capture size bounded to guard against memory exhaustion on continuous noise.
- Default transmitted image resolution adjusted to 800x600.

### v3.0
- Always-on background audio monitor stream — spectrum visible from startup
- dB-scale FFT (60 dB range) — FSK tones clearly visible above noise floor
- Adaptive normalization with running ceiling — automatic gain control
- Gamma correction (^0.55) for better midtone contrast
- Peak hold lines (yellow, fast attack / slow ~3 s decay)
- MARK / SPACE tone detector LEDs with live SNR ratio display
- 9600 baud mode — tone pair 3000/6000 Hz, display range 0–8000 Hz
- Spectrum frequency range and marker positions update automatically with baud selection
- Tone pair info label in config strip reflects current selection

### v2.0
- Live FFT spectrum bars + scrolling colour waterfall
- TX / RX panels displayed side by side
- Mode sub-tabs (Text / File / Image) in TX panel
- CAT PTT via pyserial (FT-817 front panel CAT port)
- Phase-continuous FSK modulation

### v1.0
- Initial release — FSK modem core, chunked framing with CRC-32, loopback mode
