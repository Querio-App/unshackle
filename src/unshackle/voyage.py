"""The Endurance in the ice: a banner and a progress animation for interactive terminals.

Everything here writes to stderr and only when it's a real terminal, so piped output, CI logs
and scripts see plain text. `--quiet`, NO_COLOR, CI and TERM=dumb turn it off.
"""

from __future__ import annotations

import os
import random
import sys
import threading
import time
from typing import TextIO

SHIP = r"""
                    │              │              │
                 ───┼───        ───┼───        ───┼───
                    │           ───┼───           │
                 ───┼───           │           ───┼───
                    │              │              │
         ▁▁▁▁▁▁▁▁▁▁▁│▁▁▁▁▁▁▁▁▁▁▁▁▁▁│▁▁▁▁▁▁▁▁▁▁▁▁▁▁│▁▁▁▁▁▁▁▁▁▁▁▁▁▁
         ╲  ▪    ▪    ▪    ▪    ▪    ▪    ▪    ▪    ▪    ▪   ╱═══
     ░▒▓▒░╲▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁╱░▒▓█▓▒░
  ░▒▓██▓▒░░▒▒▓▓▒░  ░▒▒░░▒▓▒░   ░▒▓▓▒░░  ░▒▒▓▒░  ░░▒▓▓█▓▒▒░░▒▓▓▒░░▒▓░
""".strip("\n").split("\n")

WORDMARK = r"""
  ██╗   ██╗███╗   ██╗███████╗██╗  ██╗ █████╗  ██████╗██╗  ██╗██╗     ███████╗
  ██║   ██║████╗  ██║██╔════╝██║  ██║██╔══██╗██╔════╝██║ ██╔╝██║     ██╔════╝
  ██║   ██║██╔██╗ ██║███████╗███████║███████║██║     █████╔╝ ██║     █████╗
  ██║   ██║██║╚██╗██║╚════██║██╔══██║██╔══██║██║     ██╔═██╗ ██║     ██╔══╝
  ╚██████╔╝██║ ╚████║███████║██║  ██║██║  ██║╚██████╗██║  ██╗███████╗███████╗
   ╚═════╝ ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚══════╝
""".strip("\n").split("\n")

TAGLINE = "  Unshackle your metrics, dashboards and permissions from your BI tool."

ICE_COLOURS = {"░": 24, "▒": 67, "▓": 110, "█": 153}
SHIP_COLOUR, WORD_COLOUR, DIM, WATER_COLOUR, DONE_COLOUR = 252, 195, 245, 31, 114
RESET = "\x1b[0m"


def enabled(stream: TextIO | None = None, quiet: bool = False) -> bool:
    """True only for a real, colour-capable terminal that hasn't asked for quiet output."""
    stream = stream or sys.stderr
    if quiet or os.environ.get("NO_COLOR") or os.environ.get("CI") or os.environ.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _fg(code: int, text: str) -> str:
    return f"\x1b[38;5;{code}m{text}{RESET}"


def _paint_ice(line: str, default: int) -> str:
    return "".join(_fg(ICE_COLOURS[c], c) if c in ICE_COLOURS else (_fg(default, c) if c.strip() else c) for c in line)


def banner(stream: TextIO | None = None) -> None:
    stream = stream or sys.stderr
    lines = [_paint_ice(line, SHIP_COLOUR) for line in SHIP] + [""]
    lines += [_fg(WORD_COLOUR, line) for line in WORDMARK] + ["", _fg(DIM, TAGLINE), ""]
    stream.write("\n".join(lines) + "\n")
    stream.flush()


MASTS = "  ┼   ┼   ┼  "
HULL = "╲▁▁▁▁▁▁▁▁▁▁▁▁╱═"
WIDTH = 64


class Voyage:
    """A small ship pushing through pack ice, one leg per step, drawn on two lines below the banner.

    The ship drifts forward during a step but never past that step's leg, and jumps to the end
    of the leg when the next step starts, so the ice only cracks open as work actually finishes.
    """

    FPS = 10

    def __init__(self, steps: list[str], stream: TextIO | None = None) -> None:
        self.steps = steps or ["working"]
        self.stream = stream or sys.stderr
        self.index = 0
        self.pos = 0.0
        self.drawn = False
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        rnd = random.Random(1914)  # the year Endurance sailed: same ice every run
        self.ice = "".join(rnd.choice("░░▒▒▓▓█ ") for _ in range(WIDTH + len(HULL)))
        self.frame = 0

    # -- public API ---------------------------------------------------------
    def __enter__(self) -> Voyage:
        self.stream.write("\x1b[?25l")  # hide the cursor while animating
        self.thread.start()
        return self

    def __exit__(self, exc_type, *_exc) -> None:
        self.stop_event.set()
        self.thread.join()
        with self.lock:
            if exc_type is None:
                self.index, self.pos = len(self.steps), float(self._span())
                self._draw(final="made it out")
            else:
                self._clear()
            self.stream.write("\x1b[?25h")
            self.stream.flush()

    def step(self, label: str) -> None:
        """Move on to the next leg, labelled LABEL (or add it if the plan didn't list it)."""
        with self.lock:
            if label in self.steps[self.index + 1:]:
                self.index = self.steps.index(label, self.index + 1)
            elif self.index + 1 < len(self.steps):
                self.index += 1
            self.pos = max(self.pos, self._leg_start(self.index))

    def say(self, message: str) -> None:
        """Print a line above the animation without tearing it."""
        with self.lock:
            self._clear()
            self.stream.write(message + "\n")
            self.drawn = False
            self._draw()

    # -- drawing ------------------------------------------------------------
    def _span(self) -> int:
        return WIDTH - len(HULL)

    def _leg_start(self, i: int) -> float:
        return self._span() * i / len(self.steps)

    def _loop(self) -> None:
        while not self.stop_event.wait(1 / self.FPS):
            with self.lock:
                cap = self._leg_start(self.index + 1) - 1
                self.pos = min(cap, self.pos + 0.25)
                self.frame += 1
                self._draw()

    def _clear(self) -> None:
        if self.drawn:
            self.stream.write("\x1b[2F\x1b[J")  # up two lines, clear to the end of the screen
            self.drawn = False

    def _draw(self, final: str = "") -> None:
        self._clear()
        p = int(self.pos)
        water = "".join("~" if (x + self.frame // 3) % 4 == 0 else " " for x in range(p))
        ahead = self.ice[p + len(HULL):WIDTH]
        top = " " * p + MASTS[: len(HULL)]
        bottom = _fg(WATER_COLOUR, water) + _fg(SHIP_COLOUR, HULL) + _paint_ice(ahead, SHIP_COLOUR)
        if final:
            label = _fg(DONE_COLOUR, f"✔ {final}")
        else:
            dots = "." * (self.frame // 4 % 4)
            label = _fg(DIM, f"{self.steps[self.index]}{dots}")
        self.stream.write(f"  {_fg(SHIP_COLOUR, top)}\n  {bottom}  {label}\x1b[K\n")
        self.stream.flush()
        self.drawn = True
