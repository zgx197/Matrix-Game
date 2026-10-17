"""Measure the streaming renderer across configurations in one process.

Loading the 5B DiT takes about two minutes, so every experiment that restarts
the process pays that twice. This harness loads the weights once and then walks
a small configuration matrix, recording per-stage CUDA timings, peak VRAM,
process commit charge and a sample frame from each run.

Safety: the machine has 32 GB of RAM. Before every configuration, and after
every chunk, the harness checks the system commit charge (RAM + page file) and
aborts instead of driving Windows into a paging storm. The Wan VAE path is not
part of the matrix for that reason.

Example:
    python scripts/perf_harness.py --scene ancient-egypt --resources resources \
        --input captures/rec/<take>_P1_bounds.mp4 --out captures/harness
"""
import argparse
import json
import time
from pathlib import Path

import psutil


def commit_state():
    """(used_gb, limit_gb) of the system commit charge."""
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    used = (memory.used + swap.used) / 1024 ** 3
    limit = (memory.total + swap.total) / 1024 ** 3
    return used, limit


class Guard:
    def __init__(self, max_commit_gb):
        self.max_commit_gb = max_commit_gb
        self.peak = 0.0

    def check(self, where):
        used, limit = commit_state()
        self.peak = max(self.peak, used)
        if used > self.max_commit_gb:
            raise SystemExit(f"aborting at {where}: commit {used:.1f} GB exceeds the "
                             f"{self.max_commit_gb:.1f} GB limit (system limit {limit:.1f} GB)")
        if used > 0.85 * limit:
            raise SystemExit(f"aborting at {where}: commit {used:.1f} GB is above 85% of {limit:.1f} GB")


def probe_attention(torch):
    """Report which SDPA backends this build can take."""
    backends = {}
    try:
        backends["flash"] = bool(torch.backends.cuda.flash_sdp_enabled())
        backends["mem_efficient"] = bool(torch.backends.cuda.mem_efficient_sdp_enabled())
        backends["math"] = bool(torch.backends.cuda.math_sdp_enabled())
    except Exception as exc:  # noqa: BLE001
        backends["error"] = str(exc)
    return backends


def bench_attention(torch, height_width, window=21, dtype=None):
    """Time one attention op at the shapes a chunk actually uses."""
    height, width = height_width
    tokens = (height // 16 // 2) * (width // 16 // 2)
    keys = tokens * window
    dtype = dtype or torch.bfloat16
    q = torch.randn(1, 24, tokens, 128, device="cuda", dtype=dtype)
    k = torch.randn(1, 24, keys, 128, device="cuda", dtype=dtype)
    v = torch.randn(1, 24, keys, 128, device="cuda", dtype=dtype)
    for _ in range(2):
        torch.nn.functional.scaled_dot_product_attention(q, k, v)
    torch.cuda.synchronize()
    started = time.perf_counter()
    for _ in range(5):
        torch.nn.functional.scaled_dot_product_attention(q, k, v)
    torch.cuda.synchronize()
    milliseconds = (time.perf_counter() - started) / 5 * 1000
    del q, k, v
    torch.cuda.empty_cache()
    return dict(tokens=tokens, keys=keys, attention_ms=round(milliseconds, 3))


def looks_degenerate(frame):
    """Flag a sample that is indistinguishable from noise.

    Switching the attention window between configurations in one process has been
    observed to leave the model emitting noise for one configuration, and a
    rendered frame that is merely wrong does not raise anywhere. Noise has a much
    higher share of energy at the Nyquist frequency than a rendered scene, so a
    coarse high-frequency ratio separates the two well enough to warn.
    """
    import numpy as np
    frame = np.asarray(frame)
    grey = frame.astype(np.float32) if frame.ndim == 2 else frame[..., :3].astype(np.float32).mean(axis=2)
    dx = np.abs(np.diff(grey, axis=1)).mean()
    # Neighbouring pixels in a real frame are strongly correlated; in noise they are not.
    return float(dx) > 40.0


def run_config(torch, components, args, size, steps, runtime, guard, out_dir, frames, clip, window=21):
    from PIL import Image
    from matrix_game.inference.preprocessing import crop_and_resize, frames_to_tensor, tensor_to_uint8
    from matrix_game.inference.runtime import install_runtime
    from matrix_game.inference.stream import CastleStream, encode_sink_image

    height, width = size
    label = (f"{height}x{width}_{len(steps)}step_{runtime}"
             + (f"_w{window}" if window != 21 else "")
             + (f"_c{args.clean_every}" if args.clean_every != 1 else "")
             + (f"_a{args.clean_threshold}" if args.clean_adaptive else ""))
    guard.check("before " + label)

    sink = components.sink_latent if tuple(size) == (480, 864) else None
    if args.sink_image is not None and tuple(size) != (480, 864):
        try:
            sink = encode_sink_image(components.vae, args.sink_image, args.vae_backend,
                                     first_frame=args.sink_mode == "first_frame", size=size)
        except Exception as exc:  # noqa: BLE001 - a missing sink only costs identity anchoring
            print(f"    sink encode failed for {size}: {exc}", flush=True)
            sink = None
    install_runtime(components.gen, runtime)
    stream = CastleStream(components.gen, components.vae, components.prompt,
                          seed=0, vae_backend=args.vae_backend, steps=steps,
                          first_steps=steps, profile=True, sink_latent=sink,
                          sink_mode=args.sink_mode, sink_rope_mode=args.sink_rope_mode,
                          size=size, window=window,
                          clean_every=args.clean_every, clean_adaptive=args.clean_adaptive,
                          clean_threshold=args.clean_threshold, clean_max_gap=args.clean_max_gap,
                          track_motion=True)
    torch.cuda.reset_peak_memory_stats()
    timings = {"encode": [], "denoise": [], "decode": []}
    written = index = 0
    sample = None
    wall = 0.0
    # Temporal fidelity: how much the output frame-to-frame change tracks the proxy's.
    # A drifting KV (skipped clean passes) shows up as output moving more than the proxy.
    out_diffs, proxy_diffs = [], []
    prev_out = prev_proxy = None
    try:
        while written < min(frames, len(clip)):
            count = 1 if written == 0 else 4
            batch = list(clip[index:index + count])
            if not batch:
                break
            index += len(batch)
            batch += [batch[-1]] * (count - len(batch))
            images = [crop_and_resize(Image.fromarray(frame), height, width) for frame in batch]
            started = time.perf_counter()
            result = stream.push(frames_to_tensor(images), frame_start=written)
            wall += time.perf_counter() - started
            written += count
            if args.track_quality and result.pixels is not None:
                import numpy as np
                rgb = tensor_to_uint8(result.pixels)
                for frame in rgb:
                    if prev_out is not None:
                        out_diffs.append(float(np.abs(frame.astype(np.int16) - prev_out).mean()))
                    prev_out = frame.astype(np.int16)
                for image in images:
                    proxy = np.asarray(image, dtype=np.int16)
                    if prev_proxy is not None:
                        proxy_diffs.append(float(np.abs(proxy - prev_proxy).mean()))
                    prev_proxy = proxy
            for name, value in result.timings_ms.items():
                timings.setdefault(name, []).append(value)
            if sample is None and written >= 9:
                sample = tensor_to_uint8(result.pixels)[-1]
            guard.check(f"{label} chunk {written // 4}")
        if sample is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            Image.fromarray(sample).save(out_dir / f"{label}.png")
            degenerate = looks_degenerate(sample)
            if degenerate:
                print(f"    WARNING: {label} sample looks like noise - do not trust this row", flush=True)
    finally:
        try:
            motion = stream.motion_values()
            cleans = list(getattr(stream, "clean_history", []))
        except Exception:  # noqa: BLE001 - a failed load must not mask the real error
            motion, cleans = [], []
        stream.close()
        del stream
        install_runtime(components.gen, "native")
        torch.cuda.empty_cache()

    def summary(values):
        if not values:
            return {}
        ordered = sorted(values)
        return dict(mean=round(sum(ordered) / len(ordered), 2),
                    p50=round(ordered[len(ordered) // 2], 2),
                    p95=round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2))

    stages = {name: summary(values) for name, values in timings.items() if values}
    chunk_ms = sum(stage.get("mean", 0) for stage in stages.values())
    chunks = max(1, written // 4)
    quality = None
    if out_diffs and proxy_diffs:
        out_mean, proxy_mean = sum(out_diffs) / len(out_diffs), sum(proxy_diffs) / len(proxy_diffs)
        quality = dict(output_motion=round(out_mean, 2), proxy_motion=round(proxy_mean, 2),
                       # >1 means the render invents motion the proxy does not have.
                       flicker=round(out_mean / proxy_mean, 3) if proxy_mean else None)
    return dict(label=label, size=list(size), steps=list(steps), runtime=runtime,
                window=window, frames=written,
                clean_every=args.clean_every, clean_adaptive=args.clean_adaptive,
                clean_threshold=args.clean_threshold, clean_max_gap=args.clean_max_gap,
                clean_passes=len(cleans), clean_rate=round(len(cleans) / chunks, 3),
                motion=summary(motion), quality=quality,
                seconds=round(wall, 2), fps=round(written / wall, 2) if wall else 0,
                chunk_ms=round(chunk_ms, 2), stages=stages,
                peak_vram_gb=round(torch.cuda.max_memory_allocated() / 1024 ** 3, 2),
                commit_gb=round(commit_state()[0], 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    from matrix_game.scenes import add_model_arguments, resolve_model_arguments
    add_model_arguments(parser)
    parser.set_defaults(preset="realtime", dit_runtime="native", vae_backend="taehv",
                        t5_runtime="native", prompt_mode="fixed", vae_graph=False)
    parser.add_argument("--input", type=Path, required=True, help="Clean low-poly RGB video")
    parser.add_argument("--out", type=Path, default=Path("captures/harness"))
    parser.add_argument("--frames", type=int, default=48)
    parser.add_argument("--max-commit-gb", type=float, default=40.0)
    parser.add_argument("--configs", default="full1,kern1,graph1",
                        help="full1/native, kern1/kernels, graph1/ring-graph, half1=288x512, full2=two steps")
    parser.add_argument("--windows", default="21",
                        help="Comma-separated KV attention windows in latent frames (default 21, the released setting)")
    # --clean-every / --clean-adaptive / --clean-threshold / --clean-max-gap come from
    # add_model_arguments, so they are not repeated here.
    parser.add_argument("--track-quality", action="store_true",
                        help="Measure output-vs-proxy frame-to-frame motion (adds a little CPU work)")
    args = parser.parse_args()
    resolve_model_arguments(args)

    # name -> (size, steps, dit runtime). The runtime is installed per configuration;
    # "kernels" and "ring-graph" replace CausalWanModel.forward with host-planned KV.
    matrix = {
        "full1": ((480, 864), (1000,), "native"),
        "kern1": ((480, 864), (1000,), "kernels"),
        "graph1": ((480, 864), (1000,), "ring-graph"),
        "cg1": ((480, 864), (1000,), "cudagraph"),
        "ring1": ((480, 864), (1000,), "ring"),
        "full2": ((480, 864), (1000, 500), "native"),
        "mid1": ((352, 640), (1000,), "native"),
        "half1": ((288, 512), (1000,), "native"),
        "khalf1": ((288, 512), (1000,), "kernels"),
        "khalf2": ((288, 512), (1000, 500), "kernels"),
        "kmid1": ((352, 640), (1000,), "kernels"),
        "half2": ((288, 512), (1000, 500), "native"),
    }
    chosen = [name.strip() for name in args.configs.split(",") if name.strip()]
    unknown = [name for name in chosen if name not in matrix]
    if unknown:
        parser.error(f"unknown configs: {unknown}")

    import imageio_ffmpeg
    import torch
    from matrix_game.inference.loader import ModelComponents

    guard = Guard(args.max_commit_gb)
    guard.check("startup")

    import numpy as np

    reader = imageio_ffmpeg.read_frames(str(args.input), pix_fmt="rgb24")
    info = next(reader)
    width, height = info["size"]
    clip = [np.frombuffer(frame, np.uint8).reshape(height, width, 3) for frame in reader]
    print(f"clip {width}x{height}, {len(clip)} frames", flush=True)

    print("\n=== attention backends ===", flush=True)
    backends = probe_attention(torch)
    print(json.dumps(backends), flush=True)
    attention = {}
    for size in ((480, 864), (360, 640), (288, 512)):
        attention[f"{size[0]}x{size[1]}"] = bench_attention(torch, size)
        print(f"  {size[0]}x{size[1]}: {attention[f'{size[0]}x{size[1]}']}", flush=True)

    print("\n=== loading model (about two minutes) ===", flush=True)
    started = time.perf_counter()
    components = ModelComponents(args)
    components.load()
    print(f"loaded in {time.perf_counter() - started:.0f}s", flush=True)

    results = []
    windows = [int(w) for w in str(args.windows).split(",") if w.strip()]
    try:
        for name in chosen:
            for window in windows:
                size, steps, runtime = matrix[name]
                print(f"\n--- {name} window={window}: {size[0]}x{size[1]}, {len(steps)} step(s), {runtime} ---",
                      flush=True)
                try:
                    entry = run_config(torch, components, args, size, steps, runtime, guard,
                                       args.out, args.frames, clip, window)
                except SystemExit:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad runtime must not end the matrix
                    entry = dict(label=f"{name}_w{window}", size=list(size), steps=list(steps),
                                 runtime=runtime, window=window,
                                 error=f"{type(exc).__name__}: {exc}")
                    print(f"    FAILED: {entry['error']}", flush=True)
                    torch.cuda.empty_cache()
                entry["name"] = name
                results.append(entry)
                print(json.dumps(entry), flush=True)
    finally:
        args.out.mkdir(parents=True, exist_ok=True)
        report = dict(attention_backends=backends, attention_bench=attention,
                      frames_per_config=args.frames, peak_commit_gb=round(guard.peak, 1),
                      configs=results, device=torch.cuda.get_device_name(0),
                      torch=torch.__version__)
        (args.out / "harness.json").write_text(json.dumps(report, indent=2) + "\n")

    print("\n=== summary ===", flush=True)
    print(f"{'config':8} {'size':>10} {'steps':>5} {'runtime':>11} {'fps':>6} {'chunk ms':>9} "
          f"{'denoise':>8} {'clean_kv':>8} {'encode':>7} {'decode':>7} {'vram GB':>8}", flush=True)
    for entry in results:
        head = (f"{entry['name']:8} {entry['size'][0]:4}x{entry['size'][1]:<5} "
                f"{len(entry['steps']):5} {entry.get('runtime', '?'):>11} ")
        if "error" in entry:
            print(head + f"FAILED: {entry['error']}", flush=True)
            continue
        stages = entry["stages"]
        print(head + f"{entry['fps']:6.2f} {entry['chunk_ms']:9.1f} "
              f"{stages.get('denoise', {}).get('mean', 0):8.1f} "
              f"{stages.get('clean_kv', {}).get('mean', 0):8.1f} "
              f"{stages.get('encode', {}).get('mean', 0):7.1f} "
              f"{stages.get('decode', {}).get('mean', 0):7.1f} "
              f"{entry['peak_vram_gb']:8.2f}", flush=True)
    print(f"\npeak commit {guard.peak:.1f} GB; report: {args.out / 'harness.json'}", flush=True)


if __name__ == "__main__":
    main()
