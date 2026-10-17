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


def run_config(torch, components, args, size, steps, guard, out_dir, frames, clip):
    from PIL import Image
    from matrix_game.inference.preprocessing import crop_and_resize, frames_to_tensor, tensor_to_uint8
    from matrix_game.inference.stream import CastleStream, encode_sink_image

    height, width = size
    label = f"{height}x{width}_{len(steps)}step"
    guard.check("before " + label)

    sink = components.sink_latent if tuple(size) == (480, 864) else None
    if args.sink_image is not None and tuple(size) != (480, 864):
        try:
            sink = encode_sink_image(components.vae, args.sink_image, args.vae_backend,
                                     first_frame=args.sink_mode == "first_frame", size=size)
        except Exception as exc:  # noqa: BLE001 - a missing sink only costs identity anchoring
            print(f"    sink encode failed for {size}: {exc}", flush=True)
            sink = None
    stream = CastleStream(components.gen, components.vae, components.prompt,
                          seed=0, vae_backend=args.vae_backend, steps=steps,
                          first_steps=steps, profile=True, sink_latent=sink,
                          sink_mode=args.sink_mode, sink_rope_mode=args.sink_rope_mode,
                          size=size)
    torch.cuda.reset_peak_memory_stats()
    timings = {"encode": [], "denoise": [], "decode": []}
    written = index = 0
    sample = None
    wall = 0.0
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
            for name, value in result.timings_ms.items():
                timings.setdefault(name, []).append(value)
            if sample is None and written >= 9:
                sample = tensor_to_uint8(result.pixels)[-1]
            guard.check(f"{label} chunk {written // 4}")
        if sample is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            Image.fromarray(sample).save(out_dir / f"{label}.png")
    finally:
        stream.close()
        del stream
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
    return dict(label=label, size=list(size), steps=list(steps), frames=written,
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
    parser.add_argument("--configs", default="full1,half1,mid1,half2,full2",
                        help="full1/full2=480x864 one/two steps, mid1=360x640, half1/half2=288x512")
    args = parser.parse_args()
    resolve_model_arguments(args)

    matrix = {
        "full1": ((480, 864), (1000,)),
        "full2": ((480, 864), (1000, 500)),
        "mid1": ((352, 640), (1000,)),
        "half1": ((288, 512), (1000,)),
        "half2": ((288, 512), (1000, 500)),
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
    try:
        for name in chosen:
            size, steps = matrix[name]
            print(f"\n--- {name}: {size[0]}x{size[1]}, {len(steps)} step(s) ---", flush=True)
            entry = run_config(torch, components, args, size, steps, guard, args.out, args.frames, clip)
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
    print(f"{'config':8} {'size':>10} {'steps':>5} {'fps':>6} {'chunk ms':>9} "
          f"{'denoise':>8} {'encode':>7} {'decode':>7} {'vram GB':>8}", flush=True)
    for entry in results:
        stages = entry["stages"]
        print(f"{entry['name']:8} {entry['size'][0]:4}x{entry['size'][1]:<5} {len(entry['steps']):5} "
              f"{entry['fps']:6.2f} {entry['chunk_ms']:9.1f} "
              f"{stages.get('denoise', {}).get('mean', 0):8.1f} "
              f"{stages.get('encode', {}).get('mean', 0):7.1f} "
              f"{stages.get('decode', {}).get('mean', 0):7.1f} "
              f"{entry['peak_vram_gb']:8.2f}", flush=True)
    print(f"\npeak commit {guard.peak:.1f} GB; report: {args.out / 'harness.json'}", flush=True)


if __name__ == "__main__":
    main()
