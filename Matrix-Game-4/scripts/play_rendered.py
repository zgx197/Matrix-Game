"""Play the game while a neural-rendered preview follows along.

The Windows game is a normal desktop window, so this bridge captures its client
area, pushes frames through the same CastleStream the offline renderer uses, and
shows the result in a second window. Gameplay stays on the low-poly window: the
network renders at single-digit FPS, the engine runs three orders of magnitude
faster, so this is a live preview rather than a playable rendered frame rate.

Both windows are forced always-on-top and placed side by side so neither can
cover the other - occlusion would feed the captured region back into the model.

Example:
    python scripts/play_rendered.py --scene ancient-egypt --resources resources \
        --preset realtime --dit-runtime native --no-vae-graph --t5-runtime native
"""
import argparse
import ctypes
import time
from ctypes import wintypes
from pathlib import Path
from queue import Empty, Queue
import threading

import numpy as np
from PIL import Image, ImageGrab, ImageTk

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
user32 = ctypes.windll.user32

# Without explicit prototypes ctypes passes HWND as a 32-bit int on 64-bit
# Python, so every window call silently addressed the wrong handle.
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]

# Screen coordinates must be physical pixels; otherwise a scaled desktop makes
# placement and GetClientRect disagree by the scale factor.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001 - older Windows
    try:
        user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def window_rect(hwnd):
    """Client area origin and size in screen coordinates."""
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GetClientRect failed")
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise RuntimeError("ClientToScreen failed")
    return origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top


def find_window(title_part):
    """Top-level window whose title contains ``title_part``."""
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if title_part.lower() in buffer.value.lower():
            found.append((hwnd, buffer.value))
        return True

    user32.EnumWindows(proc(callback), 0)
    return found[0] if found else (None, None)


def place_topmost(hwnd, x, y, width, height):
    """Move and resize the client area to (x, y) with the given size, on top."""
    present = user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    frame_w = rect.right - rect.left
    frame_h = rect.bottom - rect.top
    client_x, client_y, client_w, client_h = window_rect(hwnd)
    # Window frame is larger than the client area; offset so the client lands on x,y.
    border_x = client_x - rect.left
    border_y = client_y - rect.top
    user32.SetWindowPos(hwnd, HWND_TOPMOST, x - border_x, y - border_y,
                        width + (frame_w - client_w), height + (frame_h - client_h),
                        SWP_SHOWWINDOW)
    return present


class GameBridge:
    def __init__(self, stream, bbox, capture_fps, on_frames, counts=(1, 4)):
        self.stream = stream
        self.bbox = bbox
        self.interval = 1.0 / capture_fps
        self.on_frames = on_frames
        self.counts = counts
        self.frames = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.written = 0
        self.errors = []
        self.capture_fps_measured = 0.0
        self.render_fps_measured = 0.0

    def _capture(self):
        grabbed = 0
        started = time.perf_counter()
        while not self.stop.is_set():
            begin = time.perf_counter()
            try:
                image = ImageGrab.grab(bbox=self.bbox, all_screens=True)
            except Exception as exc:  # noqa: BLE001 - reported through errors
                self.errors.append(f"capture: {exc}")
                return
            with self.lock:
                self.frames.append(np.asarray(image.convert("RGB")))
                if len(self.frames) > 12:
                    del self.frames[:-12]
            grabbed += 1
            elapsed = time.perf_counter() - started
            if elapsed > 1:
                self.capture_fps_measured = grabbed / elapsed
            time.sleep(max(0.0, self.interval - (time.perf_counter() - begin)))

    def _render(self):
        from matrix_game.inference.preprocessing import crop_and_resize, frames_to_tensor, tensor_to_uint8
        count = self.counts[0]
        started = time.perf_counter()
        chunks = 0
        while not self.stop.is_set():
            with self.lock:
                if len(self.frames) < count:
                    available = False
                else:
                    batch = self.frames[-count:]
                    self.frames.clear()
                    available = True
            if not available:
                time.sleep(0.02)
                continue
            try:
                images = [crop_and_resize(Image.fromarray(frame), 480, 864) for frame in batch]
                result = self.stream.push(frames_to_tensor(images), frame_start=self.written)
                rgb = tensor_to_uint8(result.pixels)
            except Exception as exc:  # noqa: BLE001 - surface it in the UI
                self.errors.append(f"render: {type(exc).__name__}: {exc}")
                self.stop.set()
                return
            self.written += len(batch)
            chunks += 1
            elapsed = time.perf_counter() - started
            if elapsed > 1:
                self.render_fps_measured = self.written / elapsed
            self.on_frames(rgb)
            count = self.counts[1]

    def start(self):
        self.threads = [threading.Thread(target=self._capture, name="capture", daemon=True),
                        threading.Thread(target=self._render, name="render", daemon=True)]
        for thread in self.threads:
            thread.start()

    def close(self):
        self.stop.set()
        for thread in getattr(self, "threads", []):
            thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    from matrix_game.scenes import add_model_arguments, resolve_model_arguments
    add_model_arguments(parser)
    parser.set_defaults(preset="realtime")
    parser.add_argument("--game-exe", type=Path,
                        default=Path(__file__).resolve().parents[1] / "game/bin/MedievalCastleBBoxMap.exe")
    parser.add_argument("--no-launch", action="store_true", help="Use an already running game window")
    parser.add_argument("--window-title", default="Medieval Castle BBox")
    parser.add_argument("--capture-fps", type=float, default=12.0)
    parser.add_argument("--no-vsync", action="store_true", help="Leave the engine uncapped (starves the model)")
    parser.add_argument("--game-size", default="1280x720")
    parser.add_argument("--preview-at", default="1300,80", help="Preview window position")
    args = parser.parse_args()

    resolve_model_arguments(args)
    width, height = (int(v) for v in args.game_size.lower().split("x"))
    preview_x, preview_y = (int(v) for v in args.preview_at.split(","))

    # Load the model before the game exists: T5 alone needs ~11 GB, and starting
    # the engine first costs a few GB of commit charge right when it matters.
    import torch
    from matrix_game.inference.loader import ModelComponents
    from matrix_game.inference.stream import CastleStream

    print("loading model (this is the slow part)...", flush=True)
    components = ModelComponents(args)
    components.load()
    stream = CastleStream(components.gen, components.vae, components.prompt,
                          seed=0, vae_backend=args.vae_backend, steps=args.steps,
                          first_steps=args.first_steps, sink_latent=components.sink_latent,
                          sink_mode=args.sink_mode, sink_rope_mode=args.sink_rope_mode)

    game = None
    if not args.no_launch:
        import subprocess
        command = [str(args.game_exe), "--scene", args.scene, "--no-hud",
                   "--width", str(width), "--height", str(height)]
        # Uncapped, the engine renders thousands of frames per second and starves
        # the model on the same GPU; vsync costs nothing visible and keeps both alive.
        if not args.no_vsync:
            command.append("--vsync")
        print("launching:", " ".join(command), flush=True)
        game = subprocess.Popen(command, cwd=str(Path(__file__).resolve().parents[1]))
        deadline = time.time() + 30
        hwnd = None
        while time.time() < deadline:
            hwnd, _ = find_window(args.window_title)
            if hwnd:
                break
            time.sleep(0.5)
        if not hwnd:
            game.terminate()
            raise SystemExit("game window did not appear")
    else:
        hwnd, title = find_window(args.window_title)
        if not hwnd:
            raise SystemExit("no game window found; start the game first")
        print("attached to:", title, flush=True)

    place_topmost(hwnd, 0, 0, width, height)
    time.sleep(0.5)
    x, y, client_w, client_h = window_rect(hwnd)
    bbox = (x, y, x + client_w, y + client_h)
    print(f"capturing {client_w}x{client_h} at ({x},{y})", flush=True)

    import tkinter as tk
    root = tk.Tk()
    root.title("Matrix-Game 4.0 - rendered preview")
    label = tk.Label(root, text="waiting for frames...", width=864, height=480, bg="black", fg="white")
    label.pack()
    status = tk.Label(root, text="", anchor="w", fg="gray20")
    status.pack(fill="x")
    root.geometry(f"864x510+{preview_x}+{preview_y}")
    root.attributes("-topmost", True)

    display = Queue(maxsize=2)
    photos = []

    def on_frames(rgb):
        try:
            display.put_nowait(rgb)
        except Exception:  # noqa: BLE001 - drop when the UI is behind
            pass

    bridge = GameBridge(stream, bbox, args.capture_fps, on_frames)
    bridge.start()

    def tick():
        try:
            rgb = display.get_nowait()
        except Empty:
            rgb = None
        if rgb is not None:
            image = Image.fromarray(rgb[0] if rgb.ndim == 4 else rgb)
            photo = ImageTk.PhotoImage(image)
            photos.append(photo)
            del photos[:-2]
            label.configure(image=photo, text="")
        if bridge.errors:
            status.configure(text="ERROR: " + bridge.errors[0])
        else:
            status.configure(text=f"captured {bridge.capture_fps_measured:4.1f} fps | "
                                  f"rendered {bridge.render_fps_measured:4.1f} fps | "
                                  f"frames {bridge.written} | play in the LEFT window")
        if not bridge.stop.is_set():
            root.after(20, tick)
        else:
            root.after(100, root.destroy)

    root.after(20, tick)
    try:
        root.mainloop()
    finally:
        bridge.close()
        stream.close()
        if game is not None:
            game.terminate()


if __name__ == "__main__":
    main()
