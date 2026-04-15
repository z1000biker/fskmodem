#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║   FSK AUDIO MODEM  v3.1  –  SV / FT-817 Edition             ║
║   1200 · 2400 · 9600 Baud AFSK  ·  CAT PTT  ·  Loopback    ║
║   Live dB Spectrum + Waterfall  ·  Always-on Monitor         ║
╚══════════════════════════════════════════════════════════════╝

Fixes in v3:
  - Always-on audio monitor (spectrum visible at all times)
  - dB-scale FFT (tones now stand out clearly above noise floor)
  - Adaptive normalization (running peak / floor estimation)
  - Peak-hold line (yellow, slow decay) on spectrum
  - Tone detectors: MARK / SPACE LED indicators
  - 9600 baud mode (3000/6000 Hz, needs radio 9600 data port for RF)
  - Wider display range for 9600 baud
  - Contrast-boosted waterfall colormap
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import numpy as np
import sounddevice as sd
import threading
import queue
import struct
import zlib
import time
import os
import io
from PIL import Image, ImageTk

try:
    import serial
    import serial.tools.list_ports
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

# ═══════════════════════════════════════
# MODEM / BAUD CONFIG
# ═══════════════════════════════════════
SAMPLE_RATE  = 48000
MAX_CHUNK    = 200
PREAMBLE_LEN = 32
START_FLAG   = 0x7E
ESCAPE_BYTE  = 0x7D
ESCAPE_XOR   = 0x20
TYPE_TEXT    = 0x01
TYPE_FILE    = 0x02
TYPE_IMAGE   = 0x03
IMG_W, IMG_H = 800, 600
ENERGY_THR   = 0.006
SILENCE_SEC  = 0.9

# Per-baud tone pairs and display range
BAUD_PROFILES = {
    '1200': {'mark': 1200, 'space': 2200, 'disp_hz': 4000},
    '2400': {'mark': 1200, 'space': 2200, 'disp_hz': 4000},
    '9600': {'mark': 3000, 'space': 6000, 'disp_hz': 8000},
}
DEFAULT_BAUD = '1200'

# ═══════════════════════════════════════
# SPECTRUM CONSTANTS
# ═══════════════════════════════════════
FFT_SIZE     = 4096      # larger = finer freq resolution (11.7 Hz/bin @ 48kHz)
MAX_BINS     = FFT_SIZE // 2 + 1
SPEC_H       = 72        # spectrum bar height (px)
WATER_H      = 80        # waterfall height (px)
CANVAS_H     = SPEC_H + WATER_H + 26

# dB normalization
FLOOR_DB      = -80.0    # noise floor reference
CEIL_DB_INIT  = -20.0    # initial ceiling (adapts upward)
DB_RANGE      = 60.0     # total dynamic range shown
PEAK_DECAY    = 0.97     # peak-hold decay per frame (0.97 = slow fall)
SMOOTH_FAST   = 0.65     # rising edge (fast attack)
SMOOTH_SLOW   = 0.25     # falling edge (slow decay)

# ═══════════════════════════════════════
# PALETTE
# ═══════════════════════════════════════
BG     = '#0d0d14'
BG2    = '#12121e'
BG3    = '#1a1a2e'
BG4    = '#252540'
PANEL  = '#111128'
TEXT   = '#c8d0e8'
DIM    = '#4a4a70'
BLUE   = '#4488ff'
CYAN   = '#00e5ff'
GREEN  = '#00ff88'
YELLOW = '#ffee00'
RED    = '#ff3344'
ORANGE = '#ff8800'
MARK_C = '#00ff88'   # 1200 / 3000 Hz marker
SPC_C  = '#ff3366'   # 2200 / 6000 Hz marker


def _wf_color(v: int) -> tuple:
    """0–255 amplitude → RGB with high contrast."""
    v = max(0, min(255, v))
    if v < 40:                               # near-black → dark blue
        return (0, 0, max(10, v // 2))
    elif v < 110:                            # dark blue → blue
        t = (v - 40) / 70
        return (0, int(t * 40), int(20 + t * 200))
    elif v < 170:                            # blue → cyan
        t = (v - 110) / 60
        return (0, int(40 + t * 215), 220)
    elif v < 220:                            # cyan → yellow
        t = (v - 170) / 50
        return (int(t * 255), 255, int(220 - t * 220))
    else:                                    # yellow → white
        t = (v - 220) / 35
        return (255, 255, int(t * 255))


WF_CMAP = [_wf_color(i) for i in range(256)]


def _bar_color(v: float) -> str:
    """v 0.0–1.0 → hex colour for spectrum bars."""
    r, g, b = WF_CMAP[max(0, min(255, int(v * 255)))]
    return f'#{r:02x}{g:02x}{b:02x}'


# ═══════════════════════════════════════
# PROTOCOL
# ═══════════════════════════════════════
def _stuff(d: bytes) -> bytes:
    out = bytearray()
    for b in d:
        if b in (START_FLAG, ESCAPE_BYTE):
            out += bytes([ESCAPE_BYTE, b ^ ESCAPE_XOR])
        else:
            out.append(b)
    return bytes(out)


def _destuff(d: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(d):
        if d[i] == ESCAPE_BYTE:
            i += 1
            if i < len(d):
                out.append(d[i] ^ ESCAPE_XOR)
        else:
            out.append(d[i])
        i += 1
    return bytes(out)


def build_frame(ptype, seq, total, payload):
    hdr  = struct.pack('>BHHH', ptype, seq, total, len(payload))
    body = hdr + payload
    crc  = struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)
    return bytes([0xAA] * PREAMBLE_LEN + [START_FLAG]) + _stuff(body + crc) + bytes([START_FLAG])


def parse_frame(raw):
    try:
        d = _destuff(raw)
        if len(d) < 11:
            return None
        ptype, seq, total, plen = struct.unpack('>BHHH', d[:7])
        if 7 + plen + 4 > len(d):
            return None
        payload = d[7:7 + plen]
        crc_r   = struct.unpack('>I', d[7 + plen:7 + plen + 4])[0]
        if (zlib.crc32(d[:7 + plen]) & 0xFFFFFFFF) != crc_r:
            return None
        return ptype, seq, total, payload
    except Exception:
        return None


def extract_frames(raw):
    frames, i = [], 0
    while i < len(raw):
        if raw[i] == START_FLAG:
            j = i + 1
            while j < len(raw):
                if raw[j] == START_FLAG and j > i + 1:
                    r = parse_frame(raw[i + 1:j])
                    if r:
                        frames.append(r)
                    i = j - 1
                    break
                j += 1
        i += 1
    return frames


def make_payload(ptype, data, name=''):
    if ptype in (TYPE_FILE, TYPE_IMAGE) and name:
        return name.encode() + b'\x00' + data
    return data


def split_frames(ptype, payload):
    chunks = [payload[i:i + MAX_CHUNK] for i in range(0, len(payload), MAX_CHUNK)]
    return [build_frame(ptype, s, len(chunks), c) for s, c in enumerate(chunks)]


def reassemble(chunks, total, ptype):
    if len(chunks) < total:
        return None, None
    data = b''.join(chunks[i] for i in range(total))
    filename = None
    if ptype in (TYPE_FILE, TYPE_IMAGE):
        nul = data.find(b'\x00')
        if nul != -1:
            filename = data[:nul].decode('utf-8', errors='replace')
            data = data[nul + 1:]
    return data, filename


# ═══════════════════════════════════════
# FSK MOD / DEMOD
# ═══════════════════════════════════════
def modulate(data: bytes, baud: int, mark: int, space: int, amp: float = 0.70) -> np.ndarray:
    spb  = SAMPLE_RATE // baud
    bits = []
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    freqs = np.repeat([mark if b else space for b in bits], spb)
    phase = 2 * np.pi * np.cumsum(freqs) / SAMPLE_RATE
    return (amp * np.sin(phase)).astype(np.float32)


def demodulate(samples: np.ndarray, baud: int, mark: int, space: int) -> bytes:
    spb = SAMPLE_RATE // baud
    t   = np.arange(spb) / SAMPLE_RATE
    mk  = np.sin(2 * np.pi * mark  * t)
    sp  = np.sin(2 * np.pi * space * t)
    bits = []
    i = 0
    while i + spb <= len(samples):
        seg = samples[i:i + spb]
        bits.append(1 if abs(np.dot(seg, mk)) >= abs(np.dot(seg, sp)) else 0)
        i += spb
    while len(bits) % 8:
        bits.append(0)
    out = bytearray()
    for k in range(0, len(bits), 8):
        v = 0
        for b in bits[k:k + 8]:
            v = (v << 1) | b
        out.append(v)
    return bytes(out)


# ═══════════════════════════════════════
# CAT PTT (FT-817)
# ═══════════════════════════════════════
class CatPTT:
    def __init__(self, port, baud):
        self._s = serial.Serial(port, baudrate=baud,
                                bytesize=8, parity='N', stopbits=2, timeout=1)

    def tx(self):
        self._s.write(bytes([0x00, 0x00, 0x00, 0x00, 0x08]))
        time.sleep(0.15)

    def rx(self):
        self._s.write(bytes([0x00, 0x00, 0x00, 0x00, 0x88]))
        time.sleep(0.05)

    def close(self):
        if self._s.is_open:
            self._s.close()


# ═══════════════════════════════════════
# MAIN APP
# ═══════════════════════════════════════
class FSKModemApp:
    def __init__(self, root):
        self.root = root
        self.root.title("FSK MODEM  v3.1  ·  SV / FT-817")
        self.root.geometry("1160x880")
        self.root.minsize(980, 760)
        self.root.configure(bg=BG)

        # modem state
        self.rx_active  = False
        self.rx_chunks  = {}
        self.rx_total   = {}
        self.rx_images  = []
        self.log_q      = queue.Queue()
        self.file_path  = ''
        self.img_path   = ''
        self.is_tx      = False
        self.monitor_stream = None
        self.save_dir   = os.path.join(os.path.expanduser('~'), 'Desktop')

        # spectrum state
        self.spec_q      = queue.Queue(maxsize=8)
        self.spec_smooth = None          # init after knowing DISP_BINS
        self.spec_peak   = None          # peak-hold array
        self.spec_db_ceil = CEIL_DB_INIT # adaptive ceiling
        self.spec_bars   = []
        self.spec_peak_lines = []        # canvas line ids for peak hold
        self.spec_bw     = 1.0
        self.spec_cw     = 100
        self.wf_pil      = None
        self.wf_photo    = None
        self.wf_item     = None
        self.disp_bins   = 0             # set when canvas created

        self._apply_styles()
        self._build_ui()
        self._refresh_devices()
        self._poll_log()
        self._spectrum_tick()

    # ─────────────────────────────────────
    # CONTEXT MENU
    # ─────────────────────────────────────
    def _add_context_menu(self, widget):
        menu = tk.Menu(widget, tearoff=0, bg=BG4, fg=TEXT, font=('Consolas', 9),
                       activebackground=BG3, activeforeground=CYAN, borderwidth=0)
        menu.add_command(label="Cut", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="Copy", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Paste", command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Select All", command=lambda: widget.event_generate("<<SelectAll>>"))
        def show_menu(event):
            menu.tk_popup(event.x_root, event.y_root)
        widget.bind("<Button-3>", show_menu)

    # ─────────────────────────────────────
    # STYLES
    # ─────────────────────────────────────
    def _apply_styles(self):
        s = ttk.Style()
        s.theme_use('clam')
        s.configure('.', background=BG, foreground=TEXT, font=('Consolas', 10))
        s.configure('TFrame',       background=BG)
        s.configure('TLabel',       background=BG, foreground=TEXT)
        s.configure('TLabelframe',  background=BG3)
        s.configure('TLabelframe.Label', background=BG3, foreground=CYAN, font=('Consolas', 9))
        s.configure('TNotebook',    background=BG2)
        s.configure('TNotebook.Tab', background=BG4, foreground=DIM,
                    font=('Consolas', 10), padding=[10, 4])
        s.map('TNotebook.Tab',
              background=[('selected', BG3)],
              foreground=[('selected', CYAN)])
        s.configure('TButton', background=BG4, foreground=TEXT,
                    borderwidth=0, font=('Consolas', 10))
        s.map('TButton', background=[('active', BG3)])
        s.configure('TX.TButton',   background='#162a1c', foreground=GREEN,
                    font=('Consolas', 10, 'bold'), borderwidth=0)
        s.map('TX.TButton',  background=[('active', '#1f3d27')])
        s.configure('Stop.TButton', background='#2e1212', foreground=RED,
                    font=('Consolas', 10, 'bold'), borderwidth=0)
        s.map('Stop.TButton', background=[('active', '#3d1c1c')])
        s.configure('RX.TButton',   background='#12203a', foreground=BLUE,
                    font=('Consolas', 10, 'bold'), borderwidth=0)
        s.map('RX.TButton',  background=[('active', '#1a2f50')])
        s.configure('TCombobox',    fieldbackground=BG4, background=BG4,
                    foreground=TEXT, arrowcolor=CYAN,
                    selectbackground=BG4, selectforeground=CYAN)
        s.configure('TProgressbar', background=BLUE, troughcolor=BG4, borderwidth=0)

    # ─────────────────────────────────────
    # UI BUILD
    # ─────────────────────────────────────
    def _build_ui(self):
        # Header
        hdr = tk.Frame(self.root, bg=BG2, height=32)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)
        tk.Label(hdr, text='[ FSK AUDIO MODEM  v3.0 ]',
                 bg=BG2, fg=CYAN, font=('Consolas', 13, 'bold')).pack(side='left', padx=14, pady=4)
        tk.Label(hdr, text='AFSK/FM  ·  1200/2400/9600 Bd  ·  FT-817 / Loopback',
                 bg=BG2, fg=DIM, font=('Consolas', 9)).pack(side='left', padx=4)
        self.clk_lbl = tk.Label(hdr, text='', bg=BG2, fg=DIM, font=('Consolas', 9))
        self.clk_lbl.pack(side='right', padx=12)
        self._tick_clock()

        # Config strip
        cfg = tk.Frame(self.root, bg=BG3, padx=6, pady=5)
        cfg.pack(fill='x')

        hw_fr = ttk.LabelFrame(cfg, text=' Hardware Devices ')
        hw_fr.grid(row=0, column=0, padx=4, pady=2, sticky='n')
        modem_fr = ttk.LabelFrame(cfg, text=' Modem Settings ')
        modem_fr.grid(row=0, column=1, padx=4, pady=2, sticky='n')

        def L(fr, txt, col, row=0):
            tk.Label(fr, text=txt, bg=BG3, fg=DIM, font=('Consolas', 8)).grid(row=row, column=col, sticky='w', padx=(8,2))

        def CB(fr, var, vals, col, w=26):
            cb = ttk.Combobox(fr, textvariable=var, values=vals, width=w, state='readonly', font=('Consolas', 9))
            cb.grid(row=1, column=col, padx=(2, 8), pady=2, sticky='w')
            return cb

        L(hw_fr, 'TX DEVICE', 0);  self.tx_dev_var = tk.StringVar()
        self.tx_combo = CB(hw_fr, self.tx_dev_var, [], 0, 28)

        L(hw_fr, 'RX DEVICE', 1);  self.rx_dev_var = tk.StringVar()
        self.rx_combo = CB(hw_fr, self.rx_dev_var, [], 1, 28)

        L(hw_fr, 'CAT PORT', 2);  self.cat_port_var = tk.StringVar()
        self.cat_combo = ttk.Combobox(hw_fr, textvariable=self.cat_port_var, width=8, state='disabled', font=('Consolas', 9))
        self.cat_combo.grid(row=1, column=2, padx=4, pady=2, sticky='w')

        L(hw_fr, 'CAT BAUD', 3);  self.cat_baud_var = tk.StringVar(value='9600')
        CB(hw_fr, self.cat_baud_var, ['4800', '9600', '38400'], 3, 7)

        tk.Button(hw_fr, text='↻', bg=BG3, fg=CYAN, relief='flat', font=('Consolas', 11),
                  command=self._refresh_devices).grid(row=1, column=4, padx=6)

        L(modem_fr, 'MODE', 0);  self.mode_var = tk.StringVar(value='Loopback')
        mc = CB(modem_fr, self.mode_var, ['Loopback', 'AFSK 1200 (AX.25)', 'AFSK 2400', '9600 Baud (data port)', 'FT-817 (CAT PTT)'], 0, 22)
        mc.bind('<<ComboboxSelected>>', self._on_mode_change)

        L(modem_fr, 'MODEM BAUD', 1);  self.baud_var = tk.StringVar(value='1200')
        baud_cb = CB(modem_fr, self.baud_var, ['1200', '2400', '9600'], 1, 7)
        baud_cb.bind('<<ComboboxSelected>>', self._on_baud_change)

        L(modem_fr, 'VOL', 2)
        self.vol_var = tk.DoubleVar(value=0.70)
        self.vol_lbl = tk.Label(modem_fr, text='70%', bg=BG3, fg=YELLOW, font=('Consolas', 9), width=4)
        self.vol_lbl.grid(row=0, column=2, sticky='w', padx=(8, 0))
        ttk.Scale(modem_fr, from_=0.1, to=1.0, variable=self.vol_var, orient='horizontal', length=70,
                  command=lambda v: self.vol_lbl.config(text=f'{int(float(v)*100)}%')).grid(row=1, column=2, padx=4)

        # Output dir control
        out_fr = tk.Frame(cfg, bg=BG3)
        out_fr.grid(row=0, column=2, padx=12, pady=8, sticky='n')
        tk.Label(out_fr, text='Save RX Files To:', bg=BG3, fg=DIM, font=('Consolas', 8)).pack(anchor='w')
        btn_fr = tk.Frame(out_fr, bg=BG3)
        btn_fr.pack(fill='x')
        disp_dir = self.save_dir if len(self.save_dir) <= 24 else '...' + self.save_dir[-21:]
        self.save_dir_lbl = tk.Label(btn_fr, text=disp_dir, bg=BG3, fg=CYAN, font=('Consolas', 8), width=24, anchor='w')
        self.save_dir_lbl.pack(side='left')
        tk.Button(btn_fr, text='Browse…', bg=BG4, fg=TEXT, relief='flat', font=('Consolas', 8),
                  command=self._browse_save_dir).pack(side='left', padx=4)

        # Tone info label (updates with baud selection)
        self.tone_info_var = tk.StringVar(value='MARK 1200 Hz  ·  SPACE 2200 Hz')
        tk.Label(cfg, textvariable=self.tone_info_var, bg=BG3, fg=CYAN,
                 font=('Consolas', 8)).grid(row=1, column=0, columnspan=2, sticky='w', padx=10, pady=(2,0))

        # Main body
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self._build_tx_panel(body)
        self._build_rx_panel(body)

        # Progress bar
        self.prog_var = tk.DoubleVar(value=0)
        ttk.Progressbar(self.root, variable=self.prog_var, maximum=100).pack(fill='x')

        # Spectrum section
        self._build_spectrum()

        # Status bar
        bot = tk.Frame(self.root, bg=BG2, height=20)
        bot.pack(fill='x')
        bot.pack_propagate(False)
        self.status_var = tk.StringVar(value='READY')
        tk.Label(bot, textvariable=self.status_var, bg=BG2, fg=GREEN,
                 font=('Consolas', 9), anchor='w', padx=10).pack(side='left')

        # Log strip
        logfr = tk.Frame(self.root, bg=BG2)
        logfr.pack(fill='x')
        self.log_txt = tk.Text(logfr, height=3, bg=BG2, fg=DIM,
                               font=('Consolas', 8), state='disabled',
                               wrap='word', borderwidth=0, padx=6, pady=3)
        lsb = tk.Scrollbar(logfr, command=self.log_txt.yview, bg=BG3, width=10)
        self.log_txt['yscrollcommand'] = lsb.set
        self.log_txt.pack(side='left', fill='both', expand=True)
        lsb.pack(side='right', fill='y')

    # ─────────────────────────────────────
    # TX PANEL
    # ─────────────────────────────────────
    def _build_tx_panel(self, parent):
        fr = tk.Frame(parent, bg=PANEL)
        fr.grid(row=0, column=0, sticky='nsew', padx=(0, 1))
        fr.columnconfigure(0, weight=1)
        fr.rowconfigure(2, weight=1)

        # Title
        tb = tk.Frame(fr, bg=BG4, height=28)
        tb.grid(row=0, column=0, sticky='ew')
        tb.pack_propagate(False)
        tk.Label(tb, text='▲  TRANSMIT BUFFER', bg=BG4, fg=GREEN,
                 font=('Consolas', 10, 'bold'), anchor='w', padx=10).pack(side='left', fill='y')
        self.tx_status_lbl = tk.Label(tb, text='IDLE', bg=BG4, fg=DIM,
                                      font=('Consolas', 9), padx=10)
        self.tx_status_lbl.pack(side='right', fill='y')

        # Sub-tabs
        self._nb = ttk.Notebook(fr)
        self._nb.grid(row=1, column=0, sticky='ew')
        t_text  = ttk.Frame(self._nb)
        t_file  = ttk.Frame(self._nb)
        t_image = ttk.Frame(self._nb)
        self._nb.add(t_text,  text=' TEXT ')
        self._nb.add(t_file,  text=' FILE ')
        self._nb.add(t_image, text=' IMAGE ')

        tk.Label(t_text, text='↓ type or paste message in the TX buffer below',
                 bg=BG, fg=DIM, font=('Consolas', 8)).pack(anchor='w', padx=8, pady=3)

        # File tab
        ffr = tk.Frame(t_file, bg=BG)
        ffr.pack(fill='x', padx=8, pady=5)
        self.file_name_lbl = tk.Label(ffr, text='— no file —', bg=BG, fg=ORANGE,
                                      font=('Consolas', 9), anchor='w', width=36)
        self.file_name_lbl.pack(side='left')
        tk.Button(ffr, text='Browse…', bg=BG4, fg=TEXT, relief='flat',
                  font=('Consolas', 9), command=self._browse_file).pack(side='left', padx=6)
        self.file_info_lbl = tk.Label(t_file, text='', bg=BG, fg=DIM, font=('Consolas', 8))
        self.file_info_lbl.pack(anchor='w', padx=8)

        # Image tab
        ifr = tk.Frame(t_image, bg=BG)
        ifr.pack(fill='x', padx=8, pady=5)
        self.img_name_lbl = tk.Label(ifr, text='— no image —', bg=BG, fg=ORANGE,
                                     font=('Consolas', 9), anchor='w', width=32)
        self.img_name_lbl.pack(side='left')
        tk.Button(ifr, text='Browse…', bg=BG4, fg=TEXT, relief='flat',
                  font=('Consolas', 9), command=self._browse_image).pack(side='left', padx=6)
        qfr = tk.Frame(t_image, bg=BG)
        qfr.pack(fill='x', padx=8)
        tk.Label(qfr, text='JPEG Q:', bg=BG, fg=DIM, font=('Consolas', 8)).pack(side='left')
        self.q_var = tk.IntVar(value=30)
        self.q_lbl = tk.Label(qfr, text='30', bg=BG, fg=YELLOW, font=('Consolas', 9), width=3)
        self.q_lbl.pack(side='left')
        ttk.Scale(qfr, from_=5, to=85, variable=self.q_var, orient='horizontal', length=110,
                  command=lambda v: (self.q_var.set(int(float(v))),
                                     self.q_lbl.config(text=str(int(float(v))))
                                     )).pack(side='left')
        self.img_est_lbl = tk.Label(t_image, text='', bg=BG, fg=DIM, font=('Consolas', 8))
        self.img_est_lbl.pack(anchor='w', padx=8)

        # TX text box
        self.tx_box = tk.Text(fr, bg=BG2, fg=TEXT, font=('Consolas', 10),
                              insertbackground=CYAN, wrap='word',
                              padx=8, pady=6, borderwidth=0)
        self.tx_box.grid(row=2, column=0, sticky='nsew', padx=2, pady=(2, 0))
        self.tx_box.insert('end', 'CQ CQ CQ DE SV... PSE K\n')
        self.tx_box.bind('<KeyRelease>', self._update_tx_info)
        self._add_context_menu(self.tx_box)

        # Buttons
        bbr = tk.Frame(fr, bg=PANEL, pady=5)
        bbr.grid(row=3, column=0, sticky='ew', padx=4)
        self.send_btn = ttk.Button(bbr, text='▶  SEND', style='TX.TButton',
                                   command=self._send_current)
        self.send_btn.pack(side='left', padx=4)
        ttk.Button(bbr, text='CLR', command=self._clear_tx).pack(side='left', padx=2)
        self.tx_info_lbl = tk.Label(bbr, text='', bg=PANEL, fg=DIM, font=('Consolas', 8))
        self.tx_info_lbl.pack(side='left', padx=10)

    # ─────────────────────────────────────
    # RX PANEL
    # ─────────────────────────────────────
    def _build_rx_panel(self, parent):
        fr = tk.Frame(parent, bg=PANEL)
        fr.grid(row=0, column=1, sticky='nsew', padx=(1, 0))
        fr.columnconfigure(0, weight=1)
        fr.rowconfigure(1, weight=1)

        # Title
        tb = tk.Frame(fr, bg=BG4, height=28)
        tb.pack(fill='x')
        tb.pack_propagate(False)
        tk.Label(tb, text='▼  RECEIVE BUFFER', bg=BG4, fg=BLUE,
                 font=('Consolas', 10, 'bold'), padx=10).pack(side='left', fill='y')
        self.rx_sig_lbl = tk.Label(tb, text='● IDLE', bg=BG4, fg=DIM,
                                   font=('Consolas', 9), padx=10)
        self.rx_sig_lbl.pack(side='right', fill='y')
        self.rx_btn = ttk.Button(tb, text='▶ RX', style='RX.TButton',
                                 command=self._toggle_rx)
        self.rx_btn.pack(side='right', padx=4, pady=2)

        # RX text box
        rxfr = tk.Frame(fr, bg=PANEL)
        rxfr.pack(fill='both', expand=True, padx=2, pady=(2, 0))
        self.rx_box = tk.Text(rxfr, bg=BG2, fg=TEXT, font=('Consolas', 10),
                              state='disabled', wrap='word', padx=8, pady=6, borderwidth=0)
        rxsb = tk.Scrollbar(rxfr, command=self.rx_box.yview, bg=BG3, width=10)
        self.rx_box['yscrollcommand'] = rxsb.set
        self.rx_box.pack(side='left', fill='both', expand=True)
        rxsb.pack(side='right', fill='y')
        self.rx_box.tag_configure('hdr', foreground=CYAN,  font=('Consolas', 10, 'bold'))
        self.rx_box.tag_configure('msg', foreground=TEXT)
        self.rx_box.tag_configure('ok',  foreground=GREEN)
        self.rx_box.tag_configure('dim', foreground=DIM)
        self._add_context_menu(self.rx_box)

        bbr = tk.Frame(fr, bg=PANEL, pady=5)
        bbr.pack(fill='x', padx=4)
        tk.Button(bbr, text='CLR', bg=BG4, fg=TEXT, relief='flat',
                  font=('Consolas', 9), command=self._clear_rx).pack(side='left', padx=4)
        tk.Button(bbr, text='Copy', bg=BG4, fg=TEXT, relief='flat',
                  font=('Consolas', 9), command=self._copy_rx).pack(side='left', padx=2)

    # ─────────────────────────────────────
    # SPECTRUM + WATERFALL
    # ─────────────────────────────────────
    def _build_spectrum(self):
        sf = tk.Frame(self.root, bg=BG2)
        sf.pack(fill='x')

        # Top bar: labels + tone detector LEDs
        top = tk.Frame(sf, bg=BG2)
        top.pack(fill='x', padx=6, pady=(2, 0))

        tk.Label(top, text='SPECTRUM / WATERFALL', bg=BG2, fg=DIM,
                 font=('Consolas', 8, 'bold')).pack(side='left')
        tk.Label(top, text='  [yellow line = peak hold]', bg=BG2, fg=DIM,
                 font=('Consolas', 7)).pack(side='left')

        # Tone detector LEDs (right side)
        led_fr = tk.Frame(top, bg=BG2)
        led_fr.pack(side='right')

        self.snr_lbl = tk.Label(led_fr, text='SNR: --', bg=BG2, fg=DIM,
                                font=('Consolas', 8))
        self.snr_lbl.pack(side='right', padx=8)

        # SPACE LED
        tk.Label(led_fr, text='SPACE', bg=BG2, fg=DIM,
                 font=('Consolas', 8)).pack(side='right', padx=(6, 0))
        self.space_led = tk.Label(led_fr, text='●', bg=BG2, fg=DIM,
                                   font=('Consolas', 12))
        self.space_led.pack(side='right')

        # MARK LED
        tk.Label(led_fr, text='MARK', bg=BG2, fg=DIM,
                 font=('Consolas', 8)).pack(side='right', padx=(12, 0))
        self.mark_led = tk.Label(led_fr, text='●', bg=BG2, fg=DIM,
                                  font=('Consolas', 12))
        self.mark_led.pack(side='right')

        # Freq range label (updates with baud)
        self.freq_range_var = tk.StringVar(value='0──────2000──────4000 Hz')
        tk.Label(top, textvariable=self.freq_range_var, bg=BG2, fg=DIM,
                 font=('Consolas', 7)).pack(side='left', padx=20)

        # Canvas
        self.spec_canvas = tk.Canvas(sf, bg='#000008', height=CANVAS_H,
                                     bd=0, highlightthickness=0)
        self.spec_canvas.pack(fill='x')
        self.spec_canvas.bind('<Configure>', self._on_canvas_resize)

    def _get_baud_profile(self):
        key = self.baud_var.get() if hasattr(self, 'baud_var') else '1200'
        return BAUD_PROFILES.get(key, BAUD_PROFILES['1200'])

    def _on_canvas_resize(self, event):
        self.spec_canvas.delete('all')
        self.spec_bars = []
        self.spec_peak_lines = []
        self.wf_pil = None
        self._init_spectrum(event.width)

    def _init_spectrum(self, cw: int):
        profile   = self._get_baud_profile()
        disp_hz   = profile['disp_hz']
        mark_hz   = profile['mark']
        space_hz  = profile['space']

        # How many FFT bins to display
        bins_total = FFT_SIZE // 2 + 1
        self.disp_bins = int(disp_hz * FFT_SIZE / SAMPLE_RATE)
        self.disp_bins = min(self.disp_bins, bins_total)
        self.disp_hz   = disp_hz

        # Initialise smoothing arrays if size changed
        if self.spec_smooth is None or len(self.spec_smooth) != self.disp_bins:
            self.spec_smooth = np.zeros(self.disp_bins)
            self.spec_peak   = np.zeros(self.disp_bins)

        bw = max(1.0, cw / self.disp_bins)
        self.spec_bw = bw
        self.spec_cw = cw

        spec_y0 = WATER_H + 2    # top of spectrum bars area
        spec_y1 = WATER_H + 2 + SPEC_H  # bottom

        # Spectrum bars
        for i in range(self.disp_bins):
            x0 = i * bw
            x1 = x0 + bw - 0.5
            rid = self.spec_canvas.create_rectangle(
                x0, spec_y1, x1, spec_y1,
                fill='#000c10', outline='', width=0)
            self.spec_bars.append(rid)

        # Peak hold lines (one per bar, yellow, start at bottom)
        for i in range(self.disp_bins):
            x0 = i * bw
            x1 = x0 + bw
            lid = self.spec_canvas.create_line(
                x0, spec_y1, x1, spec_y1,
                fill='#222200', width=1)
            self.spec_peak_lines.append(lid)

        # Frequency axis grid + labels
        step = 500 if disp_hz <= 4000 else 1000
        for hz in range(0, disp_hz + 1, step):
            x = int(hz / disp_hz * cw)
            self.spec_canvas.create_line(x, WATER_H, x, WATER_H + 6,
                                         fill=DIM, width=1)
            self.spec_canvas.create_text(x, CANVAS_H - 6, text=f'{hz}',
                                         fill=DIM, font=('Consolas', 7), anchor='n')

        # MARK / SPACE marker lines (drawn last, raise to top)
        mx = int(mark_hz  / disp_hz * cw)
        sx = int(space_hz / disp_hz * cw)
        self.spec_canvas.create_line(mx, 0, mx, CANVAS_H - 20,
                                     fill=MARK_C, width=2, dash=(5, 3), tags='markers')
        self.spec_canvas.create_text(mx + 3, WATER_H + 3, text=f'M {mark_hz}',
                                     fill=MARK_C, font=('Consolas', 7), anchor='nw',
                                     tags='markers')
        self.spec_canvas.create_line(sx, 0, sx, CANVAS_H - 20,
                                     fill=SPC_C, width=2, dash=(5, 3), tags='markers')
        self.spec_canvas.create_text(sx + 3, WATER_H + 3, text=f'S {space_hz}',
                                     fill=SPC_C, font=('Consolas', 7), anchor='nw',
                                     tags='markers')

        # Waterfall PIL image
        self.wf_pil   = Image.new('RGB', (cw, WATER_H), (0, 0, 8))
        self.wf_photo = ImageTk.PhotoImage(self.wf_pil)
        self.wf_item  = self.spec_canvas.create_image(0, 1, anchor='nw',
                                                       image=self.wf_photo)
        self.spec_canvas.tag_lower(self.wf_item)
        self.spec_canvas.tag_raise('markers')

        # Update freq range label
        step_k = step / 1000
        self.freq_range_var.set(f'0  {step}  ...  {disp_hz} Hz')

    # ─────────────────────────────────────
    # FFT PUSH (from audio thread)
    # ─────────────────────────────────────
    def _push_fft(self, samples: np.ndarray):
        """Compute dB-scale FFT and push to GUI queue."""
        n = len(samples)
        if n < FFT_SIZE:
            samples = np.pad(samples, (0, FFT_SIZE - n))

        win    = np.hanning(FFT_SIZE)
        mag    = np.abs(np.fft.rfft(samples[:FFT_SIZE] * win))  # full bins
        mag_db = 20.0 * np.log10(np.maximum(mag, 1e-12))        # → dB

        # Adaptive ceiling: rises fast, falls very slowly
        peak_db = float(mag_db.max())
        if peak_db > self.spec_db_ceil:
            self.spec_db_ceil = peak_db
        else:
            self.spec_db_ceil = self.spec_db_ceil * 0.9995 + peak_db * 0.0005

        floor_db = self.spec_db_ceil - DB_RANGE

        # Normalise to 0–1, clip, gamma (^0.55 = lift midtones)
        norm = np.clip((mag_db - floor_db) / DB_RANGE, 0.0, 1.0) ** 0.55

        try:
            self.spec_q.put_nowait(norm)
        except queue.Full:
            pass

    # ─────────────────────────────────────
    # SPECTRUM TICK (GUI timer, ~20 fps)
    # ─────────────────────────────────────
    def _spectrum_tick(self):
        try:
            norm = self.spec_q.get_nowait()   # full FFT bins array (0–1)

            if self.spec_smooth is None or len(self.spec_bars) == 0:
                self.root.after(50, self._spectrum_tick)
                return

            bins = self.disp_bins
            seg  = norm[:bins]

            # Asymmetric smoothing: fast attack, slow decay
            rising  = seg > self.spec_smooth
            self.spec_smooth = np.where(
                rising,
                self.spec_smooth * (1 - SMOOTH_FAST) + seg * SMOOTH_FAST,
                self.spec_smooth * (1 - SMOOTH_SLOW) + seg * SMOOTH_SLOW
            )

            # Peak hold: update when current > hold, else decay
            self.spec_peak = np.where(
                self.spec_smooth > self.spec_peak,
                self.spec_smooth,
                self.spec_peak * PEAK_DECAY
            )

            # Draw bars + peak hold lines
            spec_y1 = WATER_H + 2 + SPEC_H
            for i in range(min(bins, len(self.spec_bars))):
                v  = float(self.spec_smooth[i])
                pv = float(self.spec_peak[i])
                h  = max(1, int(v * SPEC_H))
                y0 = spec_y1 - h
                x0 = i * self.spec_bw
                x1 = x0 + self.spec_bw - 0.5
                # bar
                self.spec_canvas.coords(self.spec_bars[i], x0, y0, x1, spec_y1)
                self.spec_canvas.itemconfigure(self.spec_bars[i], fill=_bar_color(v))
                # peak hold line
                py = spec_y1 - max(1, int(pv * SPEC_H))
                self.spec_canvas.coords(self.spec_peak_lines[i], x0, py, x1, py)
                col = YELLOW if pv > 0.15 else '#332200'
                self.spec_canvas.itemconfigure(self.spec_peak_lines[i], fill=col)

            # Waterfall: scroll up 1 row, add new bottom row
            if self.wf_pil is not None:
                w   = self.wf_pil.width
                pix = np.array(self.wf_pil)
                pix[:-1] = pix[1:]
                # map bins → canvas width via interpolation
                xs  = np.linspace(0, bins - 1, w)
                row_vals = np.clip(
                    np.interp(xs, np.arange(bins), self.spec_smooth) * 255,
                    0, 255).astype(np.uint8)
                pix[-1] = [WF_CMAP[v] for v in row_vals]
                self.wf_pil   = Image.fromarray(pix.astype(np.uint8), 'RGB')
                self.wf_photo = ImageTk.PhotoImage(self.wf_pil)
                self.spec_canvas.itemconfigure(self.wf_item, image=self.wf_photo)
                self.spec_canvas.tag_raise('markers')

            # Tone detector LEDs
            self._update_tone_leds(norm)

        except queue.Empty:
            pass

        self.root.after(50, self._spectrum_tick)

    def _update_tone_leds(self, norm: np.ndarray):
        """Light MARK / SPACE LEDs based on energy in each tone bin."""
        profile  = self._get_baud_profile()
        mark_hz  = profile['mark']
        space_hz = profile['space']
        disp_hz  = profile['disp_hz']
        bins     = len(norm)

        mark_bin  = int(mark_hz  * FFT_SIZE / SAMPLE_RATE)
        space_bin = int(space_hz * FFT_SIZE / SAMPLE_RATE)
        width     = max(2, int(100 * FFT_SIZE / SAMPLE_RATE))  # ±100 Hz window

        def avg_around(b):
            lo = max(0, b - width)
            hi = min(bins, b + width + 1)
            return float(norm[lo:hi].mean()) if hi > lo else 0.0

        mark_v  = avg_around(mark_bin)
        space_v = avg_around(space_bin)
        THR = 0.25

        mk_on = mark_v > THR
        sp_on = space_v > THR

        self.mark_led.config(fg=MARK_C if mk_on else DIM)
        self.space_led.config(fg=SPC_C if sp_on else DIM)

        # SNR estimate (tone peak vs noise mean)
        noise_mean = float(norm.mean())
        best_tone  = max(mark_v, space_v)
        if noise_mean > 0:
            snr = best_tone / noise_mean
            self.snr_lbl.config(
                text=f'SNR: {snr:.1f}x',
                fg=GREEN if snr > 3 else (YELLOW if snr > 1.5 else DIM))

    # ─────────────────────────────────────
    # ALWAYS-ON MONITOR STREAM
    # ─────────────────────────────────────
    def _start_monitor(self):
        """Lightweight always-on input stream that feeds the spectrum."""
        dev = self._dev_idx(self.rx_dev_var)
        if dev is None:
            return
        try:
            def cb(indata, frames, t, status):
                if not self.is_tx:           # TX worker pushes its own FFT
                    self._push_fft(indata[:, 0].copy())

            self.monitor_stream = sd.InputStream(
                device=dev, channels=1, samplerate=SAMPLE_RATE,
                blocksize=FFT_SIZE, dtype='float32', callback=cb)
            self.monitor_stream.start()
            self.log(f'Monitor stream started on device {dev}')
        except Exception as e:
            self.log(f'⚠ Monitor stream: {e}')

    def _stop_monitor(self):
        if self.monitor_stream:
            try:
                self.monitor_stream.stop()
                self.monitor_stream.close()
            except Exception:
                pass
            self.monitor_stream = None

    # ─────────────────────────────────────
    # DEVICE REFRESH
    # ─────────────────────────────────────
    def _refresh_devices(self):
        self._stop_monitor()

        devs = sd.query_devices()
        outs = [f"{i}: {d['name']}" for i, d in enumerate(devs) if d['max_output_channels'] > 0]
        ins  = [f"{i}: {d['name']}" for i, d in enumerate(devs) if d['max_input_channels'] > 0]
        self.tx_combo['values'] = outs
        self.rx_combo['values'] = ins

        for combo, lst in ((self.tx_combo, outs), (self.rx_combo, ins)):
            vb = [n for n in lst if 'CABLE' in n.upper() or 'VB-AUDIO' in n.upper()]
            combo.set(vb[0] if vb else (lst[0] if lst else ''))

        if HAS_SERIAL:
            ports = [p.device for p in serial.tools.list_ports.comports()]
            self.cat_combo['values'] = ports
            if ports:
                self.cat_combo.set(ports[0])

        self.log('Devices refreshed.')
        # Re-start monitor on new device
        self.root.after(300, self._start_monitor)

    def _on_mode_change(self, *_):
        is_cat = self.mode_var.get() == 'FT-817 (CAT PTT)'
        self.cat_combo['state'] = 'readonly' if is_cat else 'disabled'

    def _on_baud_change(self, *_):
        profile = self._get_baud_profile()
        mark, space = profile['mark'], profile['space']
        self.tone_info_var.set(f'MARK {mark} Hz  ·  SPACE {space} Hz')
        if self.baud_var.get() == '9600':
            self.tone_info_var.set(
                f'MARK {mark} Hz  ·  SPACE {space} Hz  '
                f'[9600 needs FT-817 data port pin]')
        # Rebuild spectrum for new freq range
        w = self.spec_canvas.winfo_width()
        if w > 1:
            self.spec_canvas.delete('all')
            self.spec_bars = []
            self.spec_peak_lines = []
            self.wf_pil = None
            self.spec_smooth = None
            self.spec_peak = None
            self._init_spectrum(w)

    def _dev_idx(self, var):
        v = var.get()
        return int(v.split(':')[0]) if v else None

    def _tick_clock(self):
        self.clk_lbl.config(text=time.strftime('%H:%M:%S  %d/%m/%Y'))
        self.root.after(1000, self._tick_clock)

    # ─────────────────────────────────────
    # LOG / STATUS
    # ─────────────────────────────────────
    def log(self, msg):
        self.log_q.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def _poll_log(self):
        while not self.log_q.empty():
            msg = self.log_q.get_nowait()
            self.log_txt['state'] = 'normal'
            self.log_txt.insert('end', msg + '\n')
            self.log_txt.see('end')
            self.log_txt['state'] = 'disabled'
        self.root.after(100, self._poll_log)

    def _status(self, msg):
        self.root.after(0, lambda: self.status_var.set(msg))

    def _progress(self, pct):
        self.root.after(0, lambda: self.prog_var.set(pct))

    def _update_tx_info(self, *_):
        n    = len(self.tx_box.get('1.0', 'end').strip().encode('utf-8'))
        baud = int(self.baud_var.get())
        frms = max(1, -(-n // MAX_CHUNK))
        secs = max(1, n * 8 // baud)
        self.tx_info_lbl.config(text=f'{n}B  {frms} fr  ~{secs}s @ {baud}bd')

    # ─────────────────────────────────────
    # FILE / IMAGE BROWSE
    # ─────────────────────────────────────
    def _browse_file(self):
        p = filedialog.askopenfilename(
            filetypes=[('Text', '*.txt'), ('All files', '*.*')])
        if p:
            self.file_path = p
            sz   = os.path.getsize(p)
            baud = int(self.baud_var.get())
            frms = max(1, -(-sz // MAX_CHUNK))
            self.file_name_lbl.config(text=os.path.basename(p)[:34])
            self.file_info_lbl.config(text=f'{sz}B  {frms} fr  ≈{sz*8//baud}s')

    def _browse_image(self):
        p = filedialog.askopenfilename(
            filetypes=[('Images', '*.jpg *.jpeg *.png *.bmp *.gif *.webp'), ('All', '*.*')])
        if p:
            self.img_path = p
            self.img_name_lbl.config(text=os.path.basename(p)[:28])
            self._estimate_image()

    def _estimate_image(self):
        if not os.path.isfile(self.img_path):
            return
        try:
            img = Image.open(self.img_path).convert('RGB').resize(
                (IMG_W, IMG_H), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=self.q_var.get(), optimize=True)
            sz   = len(buf.getvalue())
            baud = int(self.baud_var.get())
            frms = max(1, -(-sz // MAX_CHUNK))
            self.img_est_lbl.config(
                text=f'800×600 q={self.q_var.get()}  {sz//1024}KB  {frms}fr  ≈{sz*8//baud}s')
        except Exception as e:
            self.img_est_lbl.config(text=str(e))

    def _browse_save_dir(self):
        d = filedialog.askdirectory(initialdir=self.save_dir, title='Select Save Folder')
        if d:
            self.save_dir = d
            disp = d if len(d) <= 24 else '...' + d[-21:]
            self.save_dir_lbl.config(text=disp)

    # ─────────────────────────────────────
    # CLEAR / COPY
    # ─────────────────────────────────────
    def _clear_tx(self):
        self.tx_box.delete('1.0', 'end')
        self.tx_info_lbl.config(text='')

    def _clear_rx(self):
        self.rx_box['state'] = 'normal'
        self.rx_box.delete('1.0', 'end')
        self.rx_box['state'] = 'disabled'

    def _copy_rx(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.rx_box.get('1.0', 'end'))
        self._status('RX buffer copied to clipboard')

    # ─────────────────────────────────────
    # TX ENGINE
    # ─────────────────────────────────────
    def _send_current(self):
        tab = self._nb.index('current')
        if tab == 0:   self._send_text()
        elif tab == 1: self._send_file()
        else:          self._send_image()

    def _open_cat(self):
        if self.mode_var.get() != 'FT-817 (CAT PTT)':
            return None
        if not HAS_SERIAL:
            self.log('⚠ pyserial missing — no CAT PTT')
            return None
        try:
            return CatPTT(self.cat_combo.get(), int(self.cat_baud_var.get()))
        except Exception as e:
            self.log(f'⚠ CAT: {e}')
            return None

    def _tx_worker(self, frames, label):
        profile = self._get_baud_profile()
        baud    = int(self.baud_var.get())
        mark    = profile['mark']
        space   = profile['space']
        dev     = self._dev_idx(self.tx_dev_var)
        cat     = self._open_cat()
        total   = len(frames)
        self.is_tx = True
        self.root.after(0, lambda: self.tx_status_lbl.config(text='TX…', fg=GREEN))

        try:
            if cat:
                cat.tx()
                time.sleep(0.5)

            for i, frame in enumerate(frames):
                audio = modulate(frame, baud, mark, space, self.vol_var.get())
                # Feed TX audio to spectrum
                self._push_fft(audio[:FFT_SIZE])
                self._status(f'TX {label}  {i+1}/{total}')
                self._progress(100 * (i + 1) / total)
                sd.play(audio, samplerate=SAMPLE_RATE, device=dev, blocking=True)
                time.sleep(0.04)

            # Tail
            tail = modulate(bytes([0xAA] * 8), baud, mark, space, self.vol_var.get())
            sd.play(tail, samplerate=SAMPLE_RATE, device=dev, blocking=True)

            if cat:
                cat.rx(); cat.close()

            self._status(f'TX OK — {total} frames')
            self.log(f'✓ {label}  ({total} fr)')

        except Exception as e:
            self.log(f'✗ TX: {e}')
            self._status('TX ERROR')
            if cat:
                try: cat.rx(); cat.close()
                except Exception: pass
        finally:
            self.is_tx = False
            self._progress(0)
            self.root.after(0, lambda: self.tx_status_lbl.config(text='IDLE', fg=DIM))

    def _transmit(self, frames, label):
        threading.Thread(target=self._tx_worker, args=(frames, label),
                         daemon=True).start()

    def _send_text(self):
        msg = self.tx_box.get('1.0', 'end').strip()
        if not msg:
            messagebox.showwarning('Empty', 'Type a message in the TX buffer.')
            return
        data   = msg.encode('utf-8')
        frames = split_frames(TYPE_TEXT, make_payload(TYPE_TEXT, data))
        self.log(f'Text TX: {len(msg)}ch → {len(frames)} fr')
        self._transmit(frames, f'Text({len(msg)}B)')

    def _send_file(self):
        if not os.path.isfile(self.file_path):
            messagebox.showwarning('No file', 'Browse a file in the FILE tab first.')
            return
        with open(self.file_path, 'rb') as f:
            data = f.read()
        name   = os.path.basename(self.file_path)
        frames = split_frames(TYPE_FILE, make_payload(TYPE_FILE, data, name))
        self.log(f'File TX: {name} ({len(data)}B) → {len(frames)} fr')
        self._transmit(frames, f'File:{name}')

    def _send_image(self):
        if not os.path.isfile(self.img_path):
            messagebox.showwarning('No image', 'Browse an image in the IMAGE tab first.')
            return
        q = self.q_var.get()
        try:
            img = Image.open(self.img_path).convert('RGB').resize(
                (IMG_W, IMG_H), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=q, optimize=True)
            data   = buf.getvalue()
            name   = os.path.splitext(os.path.basename(self.img_path))[0] + '.jpg'
            frames = split_frames(TYPE_IMAGE, make_payload(TYPE_IMAGE, data, name))
            self.log(f'Image TX: {name} 800×600 q={q} {len(data)}B → {len(frames)} fr')
            self._transmit(frames, f'Img:{name}')
        except Exception as e:
            self.log(f'✗ Image: {e}')
            messagebox.showerror('Error', str(e))

    # ─────────────────────────────────────
    # RX ENGINE
    # ─────────────────────────────────────
    def _toggle_rx(self):
        if self.rx_active:
            self._stop_rx()
        else:
            self._start_rx()

    def _start_rx(self):
        self.rx_active = True
        self.rx_btn.config(text='⏹ STOP', style='Stop.TButton')
        self.rx_sig_lbl.config(text='● LISTEN', fg=GREEN)
        self.rx_chunks.clear()
        self.rx_total.clear()
        # Stop monitor — RX worker will drive the spectrum instead
        self._stop_monitor()
        self.log('RX: listening for carrier…')
        threading.Thread(target=self._rx_worker, daemon=True).start()

    def _stop_rx(self):
        self.rx_active = False
        self.rx_btn.config(text='▶ RX', style='RX.TButton')
        self.rx_sig_lbl.config(text='● IDLE', fg=DIM)
        self.log('RX stopped.')
        # Restart background monitor
        self.root.after(400, self._start_monitor)

    def _rx_worker(self):
        profile = self._get_baud_profile()
        baud    = int(self.baud_var.get())
        mark    = profile['mark']
        space   = profile['space']
        dev     = self._dev_idx(self.rx_dev_var)
        blk     = 2048
        sil_lim = int(SAMPLE_RATE * SILENCE_SEC / blk)

        collecting, collected, sil_cnt = False, [], 0

        def cb(indata, frames, t, status):
            nonlocal collecting, collected, sil_cnt
            chunk = indata[:, 0].copy()
            rms   = float(np.sqrt(np.mean(chunk ** 2)))

            # Always feed spectrum in RX mode
            self._push_fft(chunk)

            if rms > ENERGY_THR:
                if not collecting:
                    collecting = True
                    collected  = []
                    self.log_q.put(f"[{time.strftime('%H:%M:%S')}] Carrier ▼")
                    self.root.after(0, lambda: self.rx_sig_lbl.config(
                        text='● CAPTURE', fg=RED))
                collected.extend(chunk.tolist())
                sil_cnt = 0
            elif collecting:
                collected.extend(chunk.tolist())
                sil_cnt += 1
                # safety limit: ~42 seconds of float32 array prevents memory explosion
                if len(collected) > 2_000_000:
                    self.log_q.put(f"[{time.strftime('%H:%M:%S')}] ⚠ Burst size limit reached, forcing decode.")
                    sil_cnt = sil_lim
                if sil_cnt >= sil_lim:
                    snap = np.array(collected, dtype=np.float32)
                    dur  = len(snap) / SAMPLE_RATE
                    self.log_q.put(
                        f"[{time.strftime('%H:%M:%S')}] Burst {dur:.1f}s → decoding…")
                    self.root.after(0,
                        lambda s=snap, bd=baud, mk=mark, sp=space:
                            self._process(s, bd, mk, sp))
                    collecting, collected, sil_cnt = False, [], 0
                    self.root.after(0, lambda: self.rx_sig_lbl.config(
                        text='● LISTEN', fg=GREEN))

        try:
            with sd.InputStream(device=dev, channels=1, samplerate=SAMPLE_RATE,
                                blocksize=blk, dtype='float32', callback=cb):
                while self.rx_active:
                    time.sleep(0.1)
        except Exception as e:
            self.log(f'✗ RX stream: {e}')
            self.root.after(0, self._stop_rx)

    def _process(self, samples, baud, mark, space):
        raw    = demodulate(samples, baud, mark, space)
        frames = extract_frames(raw)
        if not frames:
            self.log(f'  No valid frames in burst ({len(samples)/SAMPLE_RATE:.1f}s)')
            return
        self.log(f'  → {len(frames)} frame(s) decoded')
        for ptype, seq, total, payload in frames:
            self.rx_chunks.setdefault(ptype, {})[seq] = payload
            self.rx_total[ptype] = total
            have = len(self.rx_chunks[ptype])
            self._progress(100 * have / total)
            self.log(f'    [{ptype:#04x}] seq={seq+1}/{total}  have={have}')
            if have >= total:
                self._finish(ptype)

    def _finish(self, ptype):
        data, fname = reassemble(self.rx_chunks[ptype], self.rx_total[ptype], ptype)
        del self.rx_chunks[ptype]
        del self.rx_total[ptype]
        self._progress(0)
        if data is None:
            self.log('✗ Reassembly failed — missing chunks')
            return

        ts = time.strftime('%H:%M:%S')
        self.rx_box['state'] = 'normal'

        if ptype == TYPE_TEXT:
            txt = data.decode('utf-8', errors='replace')
            self.log(f'✓ Text: {len(txt)} chars')
            self.rx_box.insert('end', f'\n─── TEXT  {ts} ─────────────────────\n', 'hdr')
            self.rx_box.insert('end', txt + '\n', 'msg')

        elif ptype == TYPE_FILE:
            fn   = fname or 'received.bin'
            dest = os.path.join(self.save_dir, fn)
            with open(dest, 'wb') as f:
                f.write(data)
            self.log(f'✓ File: {dest}  ({len(data)}B)')
            self.rx_box.insert('end', f'\n─── FILE  {ts} ─────────────────────\n', 'hdr')
            self.rx_box.insert('end', f'📄  {dest}\n', 'ok')

        elif ptype == TYPE_IMAGE:
            fn   = fname or 'rx_image.jpg'
            dest = os.path.join(self.save_dir, fn)
            with open(dest, 'wb') as f:
                f.write(data)
            self.log(f'✓ Image: {dest}  ({len(data)}B)')
            self.rx_box.insert('end', f'\n─── IMAGE  {ts} ────────────────────\n', 'hdr')
            try:
                img   = Image.open(io.BytesIO(data))
                thumb = img.copy()
                thumb.thumbnail((480, 270))
                photo = ImageTk.PhotoImage(thumb)
                self.rx_images.append(photo)
                self.rx_box.image_create('end', image=photo)
                self.rx_box.insert('end', f'\n🖼  {fn}  →  {self.save_dir}\n', 'ok')
            except Exception as e:
                self.rx_box.insert('end', f'(preview err: {e})\n', 'dim')

        self.rx_box.see('end')
        self.rx_box['state'] = 'disabled'


# ═══════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════
def main():
    root = tk.Tk()
    try:
        root.iconbitmap(default='')
    except Exception:
        pass
    app = FSKModemApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()