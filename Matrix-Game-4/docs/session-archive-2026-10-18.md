# 会话归档 · 2026-10-17 ~ 2026-10-18

> 本文件归档这一段工作的完整过程：目标、时间线、决策、实测数据、事故、产出物与遗留问题。
> 面向"几个月后重新接手"的读者——只看这一份应该能知道发生了什么、为什么这么定、从哪里继续。

---

## 1. 会话概览

| 项 | 内容 |
| --- | --- |
| 目标 | 在 **原生 Windows + AMD RX 7900 XTX (gfx1100)** 上跑通 Matrix-Game 4.0（离线渲染 + 实时交互），并探索能否改画风 |
| 结果 | ✅ 离线渲染可用；✅ 交互预览真人确认 **27.6 FPS / 167 ms**；❌ 风格无法在推理时改变（已用实验证明） |
| 仓库 | `git@github.com:zgx197/Matrix-Game.git`，分支 `rocm-gfx1100` 与 `windows-native-build` |
| 关键约束 | 只用 Windows（不迁 Linux）；目标引擎是 Godot；游戏逻辑 30 Hz；硬件为消费级单卡 + 750 W 电源 |

---

## 2. 时间线

### 阶段 A · 调研与路线选择（10-17）

1. 通读 Matrix-Game 4.0 代码：`matrix_game/inference/`（DiT 运行时、流式推理）、`server/`（会话、IPC、WebRTC 输出）、`game/`（C++ 引擎）。
2. 对比 **A 路线（原生 Windows ROCm）** 与 **B 路线（WSL2/Linux）** 后选择 A——理由是用户后续要改造这套技术适配自己的 Windows + Godot 管线。
3. 产出 `docs/rocm-windows-plan.md`（后续所有执行记录都写在这里）。

### 阶段 B · 环境搭建（10-17）

| 步骤 | 结果 |
| --- | --- |
| ROCm 7.2 + PyTorch 2.9.1 (`2.9.1+rocmsdk20260116`) | ✅ bf16 GEMM **82.9 TFLOPS**，FP16 92.3，fp32 24.0 |
| gfx1100 设备可用性 | ✅ `torch.cuda.is_available()`，SDPA、conv3d、hipGraph 全部可用 |
| 页面文件 | 由 2.9 GB 扩到 `E:\pagefile.sys` 32–64 GB（Wan VAE 路径会让提交内存冲到 70 GB+） |
| 权重下载 | 22.5 GB / 16 文件，官方 `verify` 16/16 通过 |
| 坑 | HF 代理会剥离 `x-linked-etag` → 改用清单 SHA256 直连下载器 |

### 阶段 C · 离线渲染（10-17）

- 首次成片 48 帧，画质与官方一致。
- 发现问题：`CastleStream` 的 KV 缓存长度**写死为 30×54**，换分辨率后与模型网格不符（`kernels` 运行时直接报错，`native` 静默用错配缓存）。修正为按 `latent_size` 推导。

### 阶段 D · 性能测量与优化（10-17 ~ 10-18）

用自建 harness（`scripts/perf_harness.py`，一次加载多配置 + 提交内存护栏）测出：

| 手段 | 480p | 288p |
| --- | --- | --- |
| `native` 基线 | 6.6 → 7.88 FPS | — |
| `kernels` 运行时 | 10.45 | 20.20 |
| hipGraph（`ring-graph`） | +5% | +5% |
| 分辨率下调 | — | +93% |

### 阶段 E · 交互预览（10-18）

复刻官方调度思想：先加载模型 → 启动游戏（`--manual --no-hud --vsync`）→ 抓屏 → 推流 → 旁窗显示。

| 版本 | 抓屏 | 渲染 | 延迟 |
| --- | --- | --- | --- |
| Pillow `ImageGrab` | 4.9 FPS | 3.5 FPS | 1519 ms |
| **mss 区域抓取** | 23.8 | 17.6 | **258 ms** |
| **+ `rocm-fast` 预设** | 29.8 | **27.6** | **167 ms** |

关键发现：Pillow 的 `grab` 每次都先快照整个 4000×2560 桌面再裁剪（固定 275 ms），mss 只截目标矩形（39 ms）。

### 阶段 F · 硬件事故（10-18）

三次瞬间黑屏重启。事件日志把原因分成两类：

| 时间 | 记录 | 判定 |
| --- | --- | --- |
| 07-04 / 08-15 | WHEA 18 `Cache Hierarchy Error` | CPU/内存不稳定，**早于本项目** |
| 10-17 22:19 | 意外关机 + 重启后 WHEA 18 | MCE 触发 |
| 10-18 00:24 / 00:31 | 意外关机，**无 WHEA** | 供电瞬态（750 W + 7900 XTX） |

处置建议写入 plan §17.1：BIOS 关 PBO/Curve Optimizer、内存关 DOCP、显卡功耗 -20%。

### 阶段 G · 推理优化批次（10-18）

按 `docs/inference-optimization-survey.md` 逐项实测（每项独立进程）：

| 候选 | 结论 |
| --- | --- |
| O1 fp16 | ❌ 无收益（前向 p50 不变），且有偶发停顿 |
| O2 流水线重叠 | ❌ 无收益（GPU 已饱和，clean 吸收了解码时间） |
| **O3 KV 窗口 21→12** | ✅ 480p +14%、288p +11%，显存 -1.3 GB |
| **O4 clean 每 4 块** | ✅ 480p +42%、288p +39% |
| O3+O4 组合 | ✅ **480p 17.68 / 288p 31.45 FPS** |
| O5 split-K | 未做：Windows 无 Triton |
| O6 token 压缩 | 不投入：+14% 预期 / 高风险 |
| O7 超分 | ⚠️ 纯插值放大更糊，真 SR 需第三方模型 |
| O8 帧插值 | ⚠️ CPU 63 ms/帧，不可行 |

### 阶段 H · 风格可控性实验（10-18）

**结论：prompt 与代理配色都无法改变画风。**

| 组 | 做法 | 结果 |
| --- | --- | --- |
| A | 换 prompt（水彩 / 赛博朋克 / 中世纪城堡+雪景+赛璐璐） | 平均 RGB 与饱和度差 1–4/255，**无变化** |
| B | 代理视频色相偏移 140° | 渲染仍是原沙岩色 |

根因：每场景训练时 caption 与代理配色都是常量，文本分支退化成常量。

### 阶段 I · Godot 工程接入（10-18，已结案）

驱动 `D:\work\wuxia`（Godot 4.7.1 .NET）截图：

- 本机只有 4.6.1 → C# 程序集版本不匹配 → `MissingMethodException` → 角色 T-pose
- 安装 Godot **4.7.1**，跑 `--headless --editor --quit` 完成资产导入（129 个 Mixamo 动作）
- 截取三个视角后确认：**场景只有 100×100 m 网格 + 一个 Y Bot，不适合做风格实验**
- 决定：**后续风格探索不在本仓库进行**，改走外部图像/视频模型 API

---

## 3. 关键决策

| 决策 | 理由 | 后果 |
| --- | --- | --- |
| 选原生 Windows ROCm，不走 WSL2 | 用户要改造技术适配 Windows + Godot | 拿到可用环境，但缺 Triton/flash-attn/FP8 |
| 自建 harness 而非逐次跑 CLI | 模型加载 2–3 分钟且不稳定，必须一次加载多配置 | 后续所有对比数据都来自它 |
| 只做"减少工作量"类优化 | O1/O2 证明 GPU 已饱和 | O3+O4 拿到 +63% |
| 用"跟随代理"而非"对齐基线"验收画质 | 自回归轨迹必然分叉 | 避免误杀 c4、正确否决 c8 |
| 风格实验用独立进程 + 噪声自检 | 同进程切窗口曾产出静默噪声 | 数据可信 |
| 暂停实时交互、先做离线 | 加载约 1/2 概率 `0xC0000005` | 交付了可重复的离线渲染路径 |

---

## 4. 实测数据总表

### 4.1 吞吐（144 帧稳态，`kernels`）

| 配置 | 480×864 | 288×512 |
| --- | --- | --- |
| 基线（窗口 21，每块 clean） | 10.84 FPS / 349.5 ms | 20.84 FPS / 182.4 ms |
| 窗口 12 | 12.35 / 304.1 | 23.12 / 163.8 |
| clean 每 4 块 | 15.48 / 240.5 | 28.97 / 130.1 |
| **组合（`rocm-fast`）** | **17.68 / 209.0** | **31.45 / 118.9** |

### 4.2 交互链路

| 指标 | 优化前 | `rocm-fast` | 真人试玩 |
| --- | --- | --- | --- |
| 渲染 FPS | 17.6 | 27.2 | 27.6 |
| 延迟中位数 | 258 ms | 163 ms | 167 ms（p95 226） |
| 连续运行 | — | — | 1248 块 / 约 5000 帧无崩溃 |

### 4.3 硬件基线

| 项 | 数值 |
| --- | --- |
| bf16 GEMM | 82.9 TFLOPS |
| 注意力（405 查询 × 8505 key） | 3.02 ms/层（flash 后端，30 层约 90 ms） |
| 峰值显存 | 10.3–12.8 GB |

---

## 5. 事故与教训

### 5.1 三次黑屏重启
两个独立问题叠加：CPU `Cache Hierarchy Error`（历史）+ 供电瞬态（本次负载暴露）。

### 5.2 模型加载不稳定
约 1/2 概率在本机 ROCm Windows 栈上崩 `0xC0000005`（c10.dll）。**加载成功后长会话稳定**——问题在初始化，不在推理。

### 5.3 交互预览"无法操作"
根因是**预览窗抢走键盘焦点**，引擎在 `!active` 时 `WaitMessage()` 挂起主循环。
修复：预览窗创建时标记 `WS_EX_NOACTIVATE`，再用 `AttachThreadInput` 把焦点借给游戏。

### 5.4 DPI 感知缺失
Python 进程未声明 DPI 感知时，窗口坐标、抓屏区域全部错位 1.5 倍。必须调 `SetProcessDpiAwareness(2)`。

### 5.5 同进程切换 KV 窗口会产出噪声
`21→16` 时静默输出纯噪声（PNG 1.17 MB vs 干净 607 KB），根因未定位。
已加 `looks_degenerate` 自动检测（干净帧高频能量 ≈4.2 / 噪声 ≈74.7）。**扫描必须每窗口一个独立进程。**

### 5.6 环境层面的坑（可复用）
- 大小写重复的代理变量（`http_proxy` + `HTTP_PROXY`）会让 `Start-Process`、MSBuild 直接失败。
- Godot 工程 `.godot/` 缺导入缓存时，跑游戏只会看到空地面——先跑 `--headless --editor --quit`。
- PowerShell 的 `Set-Content -Encoding UTF8` 会写 BOM，污染提交信息；改用 `[System.IO.File]::WriteAllText` + `UTF8Encoding($false)`。

---

## 6. 产出物清单

### 6.1 代码（`rocm-gfx1100`）

| 文件 | 改动 |
| --- | --- |
| `matrix_game/inference/stream.py` | 尺寸参数化、KV 缓存按网格推导、`window`、`clean_every/adaptive`、运动统计 |
| `matrix_game/inference/loader.py` | `resolve_device_dtype`、提前编码 prompt、透传 size/window/clean、config 记录 |
| `matrix_game/inference/runtime.py` | 允许 fp16（仅 `ring-fused` 仍锁 bf16） |
| `matrix_game/scenes.py` | `--size`、`--window`、`--clean-*`、`rocm` / `rocm-fast` 预设 |
| `matrix_game/inference/offline.py` | 预处理与写出按 `size` |
| `scripts/perf_harness.py` | 多配置矩阵、窗口扫描、噪声自检、帧间运动指标 |
| `scripts/play_rendered.py` | mss 抓屏、焦点修复、状态日志 |
| `game/src/Main.cpp` | `--no-hud`、`--manual`、`MATRIX_GAME_DEBUG` 输入诊断 |

### 6.2 文档

| 文档 | 内容 |
| --- | --- |
| `docs/rocm-windows-plan.md` | 环境适配全记录、事故分析、暂停条件（1007 行） |
| `docs/inference-optimization-survey.md` | 优化候选清单与结论（320 行） |
| `docs/inference-optimization-matrix.md` | 实验数据登记表（185 行） |
| `docs/style-control-experiment.md` | 风格可控性实验与仓库边界（142 行） |

### 6.3 提交（最新在前）

```
517724b docs: close the Godot capture attempt and mark the experiment boundary
7b99817 docs: test whether style can be steered without retraining, and record that it cannot
1f22938 docs: record the first human trial of the interactive preview
4a78acc scripts: forward the window and clean settings into the preview bridge
e2f72f0 inference: add a tuned rocm-fast preset and record the final numbers
100f703 docs: close out every inference optimization candidate
5056332 inference: add clean-KV refresh control and record the O4 sweep
e364d32 inference: make the KV attention window a CLI option
8d231c0 inference: allow fp16 on the host-planned runtimes, and record batch 1
39596d2 docs: survey the inference-side optimizations and open a test matrix
5bf5825 docs: pause the effort and record what it proved
98454d9 scripts: stop the preview window from stealing the player's keyboard
709a43f game: log the input gates behind MATRIX_GAME_DEBUG
faab5f0 docs: record the interactive preview validation
fc1e925 docs: record what the event log says about the three resets
ef647e0 scripts: capture the preview with mss and measure real lag
12f4d1f game: add --manual so a capture bridge can hand over control
7dab4ab docs: record the related-work comparison
eb08122 docs: record the rocm preset and the first full render with it
dc6fc6b inference: add a rocm preset and a selectable output size
429c21a scripts: add a play-with-preview bridge
bccd2fc game: add --no-hud for capture pipelines
ca4d97a docs: record the runtime and resolution measurements, correcting two claims
d060fd0 inference: size the streaming KV cache from the configured output size
b507744 docs: record the performance measurements and the revised targets
```

`windows-native-build` 分支（Windows D3D11 构建）：

```
0e11513 release: regenerate the source manifest
b724608 docs: document the Windows D3D11 build
f83c358 scripts: add build_game.ps1 for the Windows entry
a061d9a game: honor MATRIX_GAME_DATA_DIR on Windows
ecaf68c build: keep multi-config output in game/bin
```

### 6.4 环境变更（本机）

| 项 | 变更 |
| --- | --- |
| 页面文件 | `E:\pagefile.sys` 32–64 GB |
| Python 环境 | `D:\work\Matrix-Game\.venv-rocm`（11.1 GB，ROCm 轮子） |
| HF 缓存 | `HF_HOME=D:\hf-cache`，pip/uv/torch 缓存均在 D: |
| 模型资源 | `resources/` 22.6 GB（古埃及场景） |
| **Godot 4.7.1 .NET** | `D:\GodotCSharp\Godot_v4.7.1-stable_mono_win64`（用户要求保留） |
| 外部脚本与日志 | `D:\work\matrix-game-artifacts\` |

---

## 7. 遗留问题

| 问题 | 状态 | 恢复条件 |
| --- | --- | --- |
| 模型加载约 1/2 概率崩 `0xC0000005` | 未解决 | 需换 ROCm 版本或平台 |
| 硬件不稳（CPU MCE + 供电瞬态） | 未处理 | BIOS 关 PBO/CO、内存关 DOCP、显卡功耗 -20% |
| 同进程切换 KV 窗口产出噪声 | 根因未定位 | 已绕过（每窗口一进程 + 自动检测） |
| 长时程记忆未验证（窗口 12 ≈ 2 秒上下文） | 未测 | 需要"走开再回头"的素材 |
| 多场景未验证（只测古埃及） | 未测 | 需要赛车/空战场景权重 |
| 风格无法改 | 已证明 | 只能 LoRA 微调或后处理 |
| O5 split-K | 未做 | 换 Linux/WSL2 拿到 Triton |

---

## 8. 下次接手怎么开始

```powershell
cd D:\work\Matrix-Game\Matrix-Game-4
$env:TMP='D:\work\Matrix-Game\.tmp'; $env:TEMP=$env:TMP

# 1) 最稳的验证：离线渲染（约 2.5 分钟）
.\.venv-rocm\Scripts\matrix-game.exe render --scene ancient-egypt `
  --input <低模视频> --resources resources --preset rocm-fast --size 288x512 --output <目录>

# 2) 交互预览（真人可玩，27 FPS / 167 ms）
$env:MATRIX_GAME_DATA_DIR="$PWD\resources\game"
.\.venv-rocm\Scripts\python.exe scripts\play_rendered.py --scene ancient-egypt `
  --resources resources --preset rocm-fast --size 288x512 --capture-fps 30

# 3) 重新测量（一次加载多配置）
.\.venv-rocm\Scripts\python.exe scripts\perf_harness.py --input <视频> --resources resources `
  --preset rocm --frames 144 --configs kern1,khalf1 --windows 12 --clean-every 4
```

**注意**：跑交互预览前先确认显卡功耗上限已下调；`play_rendered.py` 启动后**点击低模窗口**才能接管键盘。
