"""System-tray app (pystray): runs the collector 24/7 and exposes a menu.

The tray icon owns the main thread (required on Windows); the collector,
GPU, maintenance, and Flask threads run as daemons underneath it.
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser

import pystray
from PIL import Image, ImageDraw

from . import config


def make_icon() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 61, 61], radius=14, fill=(18, 24, 38, 255),
                        outline=(37, 48, 74, 255), width=2)
    # little bar chart in the accent palette
    bars = [(14, 40, "#5b9dff"), (26, 28, "#7c5cff"),
            (38, 20, "#2dd4bf"), (50, 33, "#f59e0b")]
    for x, top, color in bars:
        d.rectangle([x, top, x + 8, 52], fill=color)
    return img


def run_tray(collector, open_browser: bool = True) -> None:
    icon = pystray.Icon(config.APP_NAME, make_icon(), title=f"{config.APP_NAME} — system dashboard")

    def do_open(_=None, __=None):
        webbrowser.open(config.URL)

    def do_toggle(_, item):
        collector.resume() if collector.paused else collector.pause()

    def do_folder(_=None, __=None):
        try:
            os.startfile(str(config.DATA_DIR))  # type: ignore[attr-defined]
        except Exception:
            pass

    def do_quit(_=None, __=None):
        collector.stop()
        icon.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", do_open, default=True),
        pystray.MenuItem(
            lambda item: f"Status: {'Paused' if collector.paused else 'Collecting'}",
            None, enabled=False),
        pystray.MenuItem("Pause collection", do_toggle,
                         checked=lambda item: collector.paused),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open data folder", do_folder),
        pystray.MenuItem(f"Quit {config.APP_NAME}", do_quit),
    )

    if open_browser:
        threading.Timer(1.2, do_open).start()
    icon.run()
