"""Play the game while a neural-rendered preview follows along.

The Windows entry is an ordinary desktop window, so this bridge captures its
client area, pushes frames through the same CastleStream the offline renderer
uses, and shows the result in a second window placed beside it.

Gameplay stays on the low-poly window: the engine runs at hundreds of FPS and
the model at a dozen, so the rendered view trails the input. Controls are
handed to the player from the first frame (--manual), and the preview always
consumes the newest frames instead of queuing, which keeps the lag roughly
constant instead of growing.

Both windows are forced always-on-top and placed side by side so neither can
occlude the captured region - occlusion would feed the preview back into the
model.

Example:
    python scripts/play_rendered.py --scene ancient-egypt --resources resources \
        --preset rocm --size 352x640 --capture-fps 12
"""
import argparse
import ctypes
import time
from collections import deque
from ctypes import wintypes
from pathlib import Path
from queue import Empty, Queue
import threading

import numpy as np
from PIL import Image, ImageTk
import mss

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
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetFocus.argtypes = [wintypes.HWND]
user32.SetFocus.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
kernel32 = ctypes.windll.kernel32
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
# A preview window that can be activated would silently eat the player's keys.
WS_EX_NOACTIVATE = 0x08000000

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
    proc = WNDENUMPROC

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


def activate(hwnd):
    """Give the game keyboard focus from another process.

    SetForegroundWindow alone only raises the window; the engine also needs to
    own the keyboard focus, which cross-process SetFocus can only borrow through
    a temporary thread-input attachment.
    """
    target = user32.GetWindowThreadProcessId(hwnd, None)
    mine = kernel32.GetCurrentThreadId()
    if target and target != mine:
        user32.AttachThreadInput(mine, target, True)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(hwnd)
    if target and target != mine:
        user32.AttachThreadInput(mine, target, False)


def place_topmost(hwnd, x, y, width, height):
    """Move and resize the client area to (x, y) with the given size, on top."""
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    client_x, client_y, client_w, client_h = window_rect(hwnd)
    # Window frame is larger than the client area; offset so the client lands on x,y.
    border_x = client_x - rect.left
    border_y = client_y - rect.top
    user32.SetWindowPos(hwnd, HWND_TOPMOST, x - border_x, y - border_y,
                        width + (rect.right - rect.left - client_w),
                        height + (rect.bottom - rect.top - client_h), SWP_SHOWWINDOW)


class Bridge:
    """Capture the game window and feed a CastleStream with the newest frames."""

    def __init__(self, stream, bbox, capture_fps, on_frames):
        self.stream = stream
        self.bbox = bbox
        self.interval = 1.0 / capture_fps
        self.on_frames = on_frames
        self.frames = deque(maxlen=16)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.written = 0
        self.chunks = 0
        self.errors = []
        self.capture_fps = 0.0
        self.render_fps = 0.0
        self.latency_ms = 0.0
        self.threads = []

    def _capture_loop(self):
        # Pillow's grab snapshots the whole 4000x2560 desktop before cropping, so
        # its cost is independent of the region (~275 ms here). mss captures only
        # the requested rectangle (~39 ms at 1280x720).
        grabbed = 0
        started = time.perf_counter()
        monitor = {"left": self.bbox[0], "top": self.bbox[1],
                   "width": self.bbox[2] - self.bbox[0], "height": self.bbox[3] - self.bbox[1]}
        with mss.mss() as sct:
            while not self.stop.is_set():
                begin = time.perf_counter()
                try:
                    shot = sct.grab(monitor)
                    frame = np.asarray(shot)[:, :, :3].copy()
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"capture: {exc}")
                    return
                with self.lock:
                    self.frames.append((begin, frame))
                grabbed += 1
                elapsed = time.perf_counter() - started
                if elapsed > 1:
                    self.capture_fps = grabbed / elapsed
                time.sleep(max(0.0, self.interval - (time.perf_counter() - begin)))

    def _render_loop(self):
        from matrix_game.inference.preprocessing import crop_and_resize, frames_to_tensor, tensor_to_uint8
        height, width = self.stream.size
        count = 1
        started = time.perf_counter()
        while not self.stop.is_set():
            with self.lock:
                if len(self.frames) < count:
                    batch = None
                else:
                    batch = list(self.frames)[-count:]
                    self.frames.clear()
            if batch is None:
                time.sleep(0.01)
                continue
            taken_at = batch[-1][0]
            try:
                images = [crop_and_resize(Image.fromarray(frame), height, width) for _, frame in batch]
                result = self.stream.push(frames_to_tensor(images), frame_start=self.written)
                rgb = tensor_to_uint8(result.pixels)
            except Exception as exc:  # noqa: BLE001 - surface it in the window
                self.errors.append(f"render: {type(exc).__name__}: {exc}")
                self.stop.set()
                return
            self.written += len(batch)
            self.chunks += 1
            self.latency_ms = (time.perf_counter() - taken_at) * 1000
            elapsed = time.perf_counter() - started
            if elapsed > 1:
                self.render_fps = self.written / elapsed
            self.on_frames(rgb)
            count = 4

    def start(self):
        self.threads = [threading.Thread(target=self._capture_loop, name="capture", daemon=True),
                        threading.Thread(target=self._render_loop, name="render", daemon=True)]
        for thread in self.threads:
            thread.start()

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    from matrix_game.scenes import add_model_arguments, resolve_model_arguments
    add_model_arguments(parser)
    parser.set_defaults(preset="rocm")
    parser.add_argument("--game-exe", type=Path,
                        default=Path(__file__).resolve().parents[1] / "game/bin/MedievalCastleBBoxMap.exe")
    parser.add_argument("--no-launch", action="store_true", help="Attach to a running game window")
    parser.add_argument("--window-title", default="Medieval Castle BBox")
    parser.add_argument("--capture-fps", type=float, default=30.0)
    parser.add_argument("--game-size", default="1280x720")
    args = parser.parse_args()

    resolve_model_arguments(args)
    game_width, game_height = (int(v) for v in args.game_size.lower().split("x"))
    out_height, out_width = args.size

    import torch
    from matrix_game.inference.loader import ModelComponents
    from matrix_game.inference.stream import CastleStream

    print("loading model (about two minutes)...", flush=True)
    components = ModelComponents(args)
    try:
        components.load()
    except Exception as exc:  # noqa: BLE001 - the ROCm stack can fail at load
        raise SystemExit(f"model load failed: {type(exc).__name__}: {exc}")
    stream = CastleStream(components.gen, components.vae, components.prompt,
                          seed=0, vae_backend=args.vae_backend, steps=args.steps,
                          first_steps=args.first_steps, sink_latent=components.sink_latent,
                          sink_mode=args.sink_mode, sink_rope_mode=args.sink_rope_mode,
                          size=args.size)
    print(f"model ready ({torch.cuda.get_device_name(0)}), launching game", flush=True)

    game = None
    if not args.no_launch:
        import subprocess
        command = [str(args.game_exe), "--scene", args.scene, "--no-hud", "--manual", "--vsync",
                   "--width", str(game_width), "--height", str(game_height)]
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

    place_topmost(hwnd, 0, 0, game_width, game_height)
    time.sleep(0.5)
    x, y, client_w, client_h = window_rect(hwnd)
    bbox = (x, y, x + client_w, y + client_h)
    print(f"capturing {client_w}x{client_h} at ({x},{y})", flush=True)

    import tkinter as tk
    root = tk.Tk()
    root.title("Matrix-Game 4.0 - rendered preview")
    # The preview must never take keyboard focus: the engine ignores the keyboard
    # while its app is inactive, and a cross-process SetFocus cannot give it back.
    # WS_EX_NOACTIVATE has to be set before the window is first mapped.
    root.withdraw()
    root.update_idletasks()
    preview_hwnd = user32.GetAncestor(root.winfo_id(), 2)
    if preview_hwnd:
        style = user32.GetWindowLongPtrW(preview_hwnd, -20)
        user32.SetWindowLongPtrW(preview_hwnd, -20, style | WS_EX_NOACTIVATE)
    label = tk.Label(root, text="waiting for frames...", width=out_width, height=out_height,
                     bg="black", fg="white")
    label.pack()
    status = tk.Label(root, text="", anchor="w", fg="#333333")
    status.pack(fill="x")
    # Sit to the right of the game window so neither occludes the other.
    root.geometry(f"{out_width}x{out_height + 28}+{client_w + 24}+{max(0, y)}")
    root.attributes("-topmost", True)
    root.deiconify()
    root.update_idletasks()
    activate(hwnd)

    display = Queue(maxsize=2)
    photos = []

    def on_frames(rgb):
        try:
            display.put_nowait(rgb)
        except Exception:  # noqa: BLE001 - drop when the UI is behind
            pass

    bridge = Bridge(stream, bbox, args.capture_fps, on_frames)
    bridge.start()

    def tick():
        try:
            rgb = display.get_nowait()
        except Empty:
            rgb = None
        if rgb is not None:
            frame = rgb[-1] if rgb.ndim == 4 else rgb
            photo = ImageTk.PhotoImage(Image.fromarray(frame))
            photos.append(photo)
            del photos[:-2]
            label.configure(image=photo, text="")
        if bridge.errors:
            status.configure(text="ERROR: " + bridge.errors[0][:80])
        else:
            status.configure(
                text=f"capture {bridge.capture_fps:4.1f} fps | render {bridge.render_fps:4.1f} fps | "
                     f"lag {bridge.latency_ms:4.0f} ms | chunks {bridge.chunks} | "
                     f"WASD move, Space jump, Shift dash, mouse look")
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
