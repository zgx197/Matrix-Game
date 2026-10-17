# Matrix-Game 4.0 → Windows + ROCm 适配：计划与执行记录

目标：在 **Windows 11 + Radeon RX 7900 XTX（gfx1100）** 上跑通 Matrix-Game 4.0 的
**离线神经渲染**（低模视频 → 高画质视频），并尽可能接近可交互帧率。

- 路线：A（Windows 原生 ROCm 7.2 + PyTorch 2.9.1）；备选 B（WSL2 + ROCm）
- 本文档既是计划，也是执行日志。每完成一个阶段，在 §7 追加记录并更新 §1 的状态表。

## 1. 状态摘要

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 阶段 0 | Windows 引擎（游戏）构建与录制 | ✅ 完成，已提交并推送 |
| P0 | ROCm + PyTorch 环境、数值与算力验证 | ✅ 完成，门 G0 通过 |
| P1 | 其余依赖安装、绕开 torch 版本锁 | ✅ 完成，门 G1 通过 |
| P2 | 单场景权重下载与校验（22.5 GB） | ✅ 完成，门 G2 通过 |
| P3 | 首次离线渲染 | ✅ 完成（lean 配置 48 帧），门 G3 通过 |
| P4 | 性能基线与优化 | 🟡 进行中：需先做"一次加载、多配置"测量 harness |
| P5 | 交互与集成（原"≥15 FPS"已重新定义） | ⬜ 目标改为 Windows 常驻渲染服务 + Godot 集成，见 §16 |

系统级准备：页面文件已由 2.9 GB 改为 `E:` 上 32–64 GB，重启后实测生效（见 §3 S1）。
**新增风险**：750 W 电源 + 7900 XTX 在 GPU/CPU 同时满载时发生过一次瞬间黑屏重启（§16.1），
须先完成电源侧防护（S5/S6）再继续重负载实验。

---

## 2. 目标与非目标

### 目标

1. 用官方 ROCm Windows 轮子在原生 Windows 上创建设备可用的 PyTorch 环境。
2. 跑通 `matrix-game render` 的**离线路径**，对已录制的低模视频产出渲染结果。
3. 记录可复现的性能基线（每阶段耗时、显存峰值、FPS）。
4. 把为跑通所做的改动收敛成**可提交、可回退的补丁集**。

### 非目标

- 不追求 FP8 / INT8 量化（gfx1100 无对应硬件单元，`rtx5090` / `rtx5070ti` 路径不适用）。
- 不做浏览器 demo（需要 GStreamer + MediaMTX + EGL，Linux 专属）。
- 不做训练或多卡。
- 不改模型权重、不改数值精度语义（保持 BF16）。

---

## 3. 一次性系统准备

| # | 项目 | 状态 | 说明 |
| --- | --- | --- | --- |
| S1 | 页面文件 `E:` 32768–65536 MB | ✅ 已设置（待重启生效） | 原为 2.9 GB；放 E: 而非 D:，因为 C:/D: 同属 Disk 0，E: 是独立的 Disk 1，可避免与权重 I/O 争抢 |
| S2 | 电源计划改「高性能」 | ⬜ 待办 | 当前「平衡」；长时间推理避免降频 |
| S3 | Defender 排除 `resources` | ⬜ 待办 | 22 GB 权重加载会被实时扫描拖慢 |
| S4 | HF / pip / torch 缓存放 D: | ✅ 完成 | 见 §8 缓存重定向 |
| S5 | GPU 功耗上限 -10%~-15% | ⬜ **待办（优先）** | 750 W 电源 + 7900 XTX 的瞬态尖峰，见 §16.1 |
| S6 | 禁止 GPU/CPU 同时满载 | ⬜ 待办 | 预热、预处理、抓屏都放到推理之外的时间片 |
| S7 | 运行期内存/温度看门狗 | ⬜ 待办 | commit 或温度超阈值即中止，避免再次硬断电 |

---

## 4. 硬件与软件基线

### 4.1 硬件（实测）

| 部件 | 值 | 对推理的含义 |
| --- | --- | --- |
| CPU | Ryzen 7 5800X3D，8C/16T，L3 96 MB，3.4 GHz 基频 | Zen3，AVX2/FMA/F16C，**无 AVX-512**；大 L3 利于 host 侧规划 |
| 内存 | 2×16 GB DDR4-3200 双通道（A2/B2） | 32 GB，T5 与 DiT 不可同时驻留 |
| GPU | Radeon RX 7900 XTX，**24 GB**，gfx1100 | 显存足够（估算峰值 ~16 GB） |
| PCIe | Gen4 ×16（CurrentLinkSpeed=4, Width=16） | 满速 |
| 显存带宽 | 960 GB/s | 与 RTX 5080 同级 |
| 驱动 | 32.0.31041.1004（2026-08） | 高于 ROCm 7.2 要求的 26.1.1 |
| C: | 119 GB，剩 16.6 GB | **不使用**，只放系统 |
| D: | 833 GB（Predator GM7000 NVMe），剩 169.6 GB | 环境、权重、缓存 |
| E: | 931 GB（Samsung 990 PRO NVMe），剩 78.9 GB | 页面文件原位置 |
| 显示 | 3840×2160@60 | 与推理无关 |
| 电源 | **750 W** | 7900 XTX TBP 约 355 W + CPU 约 105 W；瞬态尖峰下余量偏紧，已发生一次黑屏重启（§16.1） |
| 目标游戏引擎 | **Godot**（后续改造目标） | 决定帧传输方式，见 §16.6 |

> WMI 的 `AdapterRAM` 报 4 GB 是 32 位字段溢出，注册表 `qwMemorySize` 才是真值 24 GB。

### 4.2 软件（实测）

| 项 | 值 |
| --- | --- |
| OS | Windows 11 Pro，build 26300 |
| Git remote | `origin` = `zgx197/Matrix-Game`（可推送），`upstream` = `SkyworkAI/Matrix-Game` |
| 分支 | `windows-native-build`（已推送） |
| Python | 3.12.12（`uv` 管理） |
| 旧 ROCm | `C:\Program Files\AMD\ROCm\5.7`（HIP SDK 5.7，不支持 gfx1100，**不使用**） |

---

## 5. 路线选择

| | A. Windows 原生 ROCm（选定） | B. WSL2 + ROCm |
| --- | --- | --- |
| 改动量 | 小（依赖版本 + 加载顺序 + device 可配置） | 中（装 Ubuntu、GPU 直通、重下资产） |
| 成熟度 | 官方支持但较新 | Linux 栈更成熟，Triton 可用 |
| 磁盘 | 复用 D: | 额外 100+ GB VHDX |
| 风险 | bf16 / 算子缺失 | WSL 显存与 I/O 开销 |

判定依据：AMD 官方 [Windows 兼容性矩阵](https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2/docs/compatibility/compatibilityrad/windows/windows_compatibility.html)
明确列出 ROCm 7.2 支持 **gfx1100 / RX 7900 XTX**，且 [PyTorch 安装页](https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2/docs/install/installrad/windows/install-pytorch.html)
提供 Python 3.12 的可用轮子。P0 实测通过后，A 路线已无需回溯 B（除 G0 之外的门失败时才重新评估）。

---

## 6. 阶段计划与门禁

| 阶段 | 内容 | 门 | 失败动作 |
| --- | --- | --- | --- |
| P0 | 环境冒烟：设备、bf16、SDPA、conv3d、graph、显存 | G0 | 转 B 路线（WSL2） |
| P1 | 依赖安装（diffusers/transformers 等），绕开 torch 锁 | G1 | 逐个降版本，不改仓库代码 |
| P2 | 单场景权重 22.5 GB 下载 + SHA256 校验 | G2 | 重下；或换场景 |
| P3 | 首次离线渲染 48 帧 | G3 | 二分定位算子，必要时 FP16 |
| P4 | 性能基线与优化（单步 + TAEHV 等） | G4 | 接受"仅离线可用" |
| P5 | 交互可行性（≥15 FPS 才做） | G5 | 放弃实时，只保留离线 |

### 决策门汇总

| 门 | 条件 | 状态 |
| --- | --- | --- |
| G0 | 设备可见 + BF16 可用 | ✅ 通过 |
| G1 | 依赖导入无冲突 | ✅ 通过 |
| G2 | 权重 SHA256 全通过 | ✅ 通过（官方 verify，16/16） |
| G3 | 48 帧渲染成功 | ✅ 通过（lean 配置，48 帧完整输出） |
| G4 | 形成性能基线 | ⬜ |
| G5 | ≥15 FPS 且稳定 60 秒 | ⬜ |

---

## 7. 代码改动清单

全部在分支 `rocm-gfx1100` 上进行，逐个提交，可独立回退。

| ID | 级别 | 文件 | 改动 | 状态 |
| --- | --- | --- | --- | --- |
| C1 | 必须 | `pyproject.toml` | 新增 `rocm` extra（不锁 torch） | ✅ `4abdd59` |
| C2 | 必须 | `inference/loader.py` | prompt 编码提前到 DiT 构建之前 | ✅ `ed47573` |
| C3 | 必须 | `inference/loader.py`、`scenes.py` | 设备/精度可配置（默认仍 cuda/bf16） | ✅ `9f76a2f` |
| C4 | 建议 | `inference/text.py` | CUDA Graph 能力探测 + 自动回退 | ⬜ |
| C5 | 建议 | `scenes.py` / CLI | 增加 `--preset rocm` | ⬜ |
| C6 | 可选 | `docs/installation.md` | 补 Windows + ROCm 安装说明 | ⬜ |
| C7 | 可选 | `scripts/check_rocm.py` | 环境自检脚本（P0 验收固化） | 草稿已存在（`D:\work\matrix-game-artifacts\check_rocm.py`） |

约束：**不改** `rtx5090` / `rtx5080` / `rtx5070ti` 三个包；**不改** `releases/*.json`。

C2 的依据：通用路径在 [loader.py:41](../matrix_game/inference/loader.py) 先 `build_dit()`，
到 [loader.py:57](../matrix_game/inference/loader.py) 才 `_encode_fixed_prompt()`，
导致 T5（11 GB）与 DiT（10 GB）同时驻留；`rtx5080` 分支是为 16 GB 卡设计的反向顺序，可直接复用。

---

## 8. 执行日志

### 阶段 0 · Windows 引擎基线（已完成）

目标：先确认游戏侧（非模型部分）在本机可用，并固定其产物形态。

**结果**

| 项 | 实测 |
| --- | --- |
| 构建 | CMake 3.31.6 + MSVC 14.44 + Win SDK 10.0.26100，产物 `game/bin/MedievalCastleBBoxMap.exe` |
| 场景解析 | 37246 物体 / 3883482 三角形 / 4225 地形 / 608 树 |
| 渲染 | D3D11，8245 帧，**1648.9 FPS**，帧时间 mean 0.598 ms |
| 录制 | 48 帧 / 1280×720 / 24fps / h264 / `valid=true` / 无 HUD |

**踩到的 5 个坑**（详见 §9）

1. 代理环境变量大小写重复 → MSBuild 无法启动 cl.exe
2. 同一原因导致 HF 下载的 httpx 报 `Invalid port`
3. 代理剥离 HF 响应头 → `hf_hub_download` 判定"资源不在 HF 上"
4. VS 多配置生成器把 exe 放到 `bin/Release/` → 资产根错位
5. Windows 入口不认 `MATRIX_GAME_DATA_DIR`；且录制需要 FFmpeg

**产出的提交**（分支 `windows-native-build`，已推送到 `origin`）

| 提交 | 内容 |
| --- | --- |
| `ecaf68c` | `build: keep multi-config output in game/bin` |
| `a061d9a` | `game: honor MATRIX_GAME_DATA_DIR on Windows` |
| `f83c358` | `scripts: add build_game.ps1 for the Windows entry` |
| `b724608` | `docs: document the Windows D3D11 build` |
| `0e11513` | `release: regenerate the source manifest`（补上 25 个漏登记文件） |

### 阶段 1 · 硬件盘点（已完成）

采集 CPU/RAM/GPU/PCIe/磁盘/页面文件/电源计划，结论：显存与带宽充足，算力可用，
缺口在软件栈；页面文件过小。数据见 §4.1。

### 阶段 2 · 路线决策（已完成）

基于 AMD 官方 Windows 兼容性矩阵与 PyTorch 安装页，选定 A 路线。依据见 §5。

### 阶段 3 · P0 环境搭建（已完成）

**步骤与体积**

| 步骤 | 内容 | 体积 |
| --- | --- | --- |
| 1 | `uv venv .venv-rocm --python 3.12 --seed` | — |
| 2 | ROCm SDK：`rocm_sdk_core` 636.8 MB、`rocm_sdk_devel` 1434.3 MB、`rocm_sdk_libraries_custom` 492.4 MB、`rocm` 15 kB | ~2.6 GB |
| 3 | `torch` 821.2 MB、`torchvision` 1.9 MB、`torchaudio` 0.5 MB | ~0.8 GB |
| 4 | 传递依赖（numpy 12.6 MB、sympy、networkx、jinja2 等） | ~0.1 GB |
| 5 | 安装后占用 | **10.82 GB** |

安装后体积分布：`rocm_sdk_devel` 6.15 GB、`_rocm_sdk_core` 1.74 GB、
`_rocm_sdk_libraries_custom` 1.61 GB、`torch` 1.14 GB。

**P0 验收结果 → 门 G0 PASS**

| 检查 | 结果 |
| --- | --- |
| `torch.__version__` | `2.9.1+rocmsdk20260116` |
| HIP runtime | `7.2.26024-f6f897bd3d` |
| 设备 | `AMD Radeon RX 7900 XTX`，capability 11.0，24.0 GiB |
| `torch.cuda.is_available()` | `True` |
| 空闲显存 | 23.8 GiB |
| 12 GiB 分配探测 | 成功 |

**数值精度与吞吐**

| 类型 | 相对误差 | 4096³ GEMM | 判定 |
| --- | --- | --- | --- |
| float32 | 1.2e-06 | 24.0 TFLOPS | 基线 |
| **bfloat16** | **3.7e-03** | **82.9 TFLOPS** | 达标（<1e-2），采用 |
| float16 | 4.9e-04 | 92.3 TFLOPS | 备选，未启用 |

小尺寸对照：2048³ bf16 为 29.7 TFLOPS（受启动开销限制，不代表峰值）。

DiT 真实形状（405 token × 3072 dim × 14336 FFN）实测 **57.8 TFLOPS**，
单层约 1.23 ms，30 层单步 FFN 约 37 ms。

**风险项提前验证**

| 风险 | 结果 | 影响 |
| --- | --- | --- |
| R1 BF16 不可用 | ✅ 通过 | 保持 BF16，无需降精度 |
| R2 MIOpen conv3d | ✅ bf16 / fp16 均正常 | Wan VAE 可用，TAEHV 非必需 |
| R3 hipGraph 不完整 | ✅ capture + replay 成功 | graph 路径可尝试，但仍先走 native |
| flash-attn 缺失 | ✅ SDPA bf16/fp16 正常 | 注意力路径成立 |

**结论**：计算不是瓶颈。按 2 步 + Wan VAE 的保守估算仍有 10–25 FPS 空间（待 P4 实测）。

### 阶段 4 · P1 依赖与代码改动（已完成）

**问题**：`inference` extra 锁死 `torch==2.12.0` + `torchvision==0.27.0`，而 ROCm 的
torch 由 AMD 发布、不在 PyPI 上，直接安装会尝试拉 CUDA 版并覆盖现有环境。

**做法**

1. **C1**：新增 `rocm` extra，与 `inference` 的内容一致但**故意不含 torch/torchvision**，
   因此安装它永远不会替换已装的 ROCm 构建（提交 `4abdd59`）。
2. 用 `--no-deps` 方式安装依赖与仓库本体，避免解析器回溯 torch 版本。
3. **C3**：`--device` / `--dtype` 可配置，并在参数解析阶段拒绝不适用的组合（提交 `9f76a2f`）。

**安装结果**（全部位于 `D:\work\Matrix-Game\.venv-rocm`）

| 包 | 版本 |
| --- | --- |
| torch / torchvision / torchaudio | `2.9.1+rocmsdk20260116` / `0.24.1` / `2.9.1` |
| diffusers | `0.39.0` |
| transformers | `5.17.0` |
| accelerate | `1.15.0` |
| safetensors | `0.8.0` |
| tokenizers | `0.23.3` |
| huggingface_hub | `1.33.0` |
| numpy | `2.5.4` |
| pillow | `13.0.0` |
| einops / ftfy / regex / sentencepiece / tqdm / imageio-ffmpeg | `0.8.2` / `6.3.1` / `2026.9.29` / `0.2.2` / `4.70.1` / `0.6.0` |
| matrix-game-4.0（可编辑安装） | `4.0.0rc1` |

venv 体积从 10.82 GB 增至 **11.1 GB**。

**验收 → 门 G1 PASS**

| 检查 | 结果 |
| --- | --- |
| `import matrix_game, diffusers, transformers, accelerate, safetensors, einops, ftfy, sentencepiece, imageio_ffmpeg` | 全部通过 |
| `torch.__version__` 是否被改动 | 未变，仍为 `2.9.1+rocmsdk20260116` |
| GPU 可见性 | `True`，`AMD Radeon RX 7900 XTX` |
| 控制台入口 `matrix-game.exe` | 已生成 |
| CLI 新参数 | `--device`、`--dtype {bf16,fp16,fp32}` 出现在帮助中 |

**组合校验（三条规则实测）**

| 命令 | 预期 | 实测 |
| --- | --- | --- |
| `--dtype fp16 --dit-runtime ring-fused` | 拒绝 | ✅ `The ring-fused runtime requires --dtype bf16; use --dit-runtime native for fp16 or fp32` |
| `--device cpu --dit-runtime ring-fused` | 拒绝 | ✅ `The ring-fused runtime requires --device cuda; use --dit-runtime native on other devices` |
| `--dtype fp16 --dit-runtime native` | 通过校验 | ✅ 继续执行到"缺少模型资源"（属预期，权重尚未下载） |

**已知偏差**：本次用 `--no-deps` 安装，`pillow` 保留 13.0.0，而 `pyproject.toml` 声明
`<12`。原因是 torchvision 0.24.1 带入 pillow 13；本项目不走图像处理路径，暂不影响。
若改用标准 `pip install -e '.[rocm]'`，pip 会把 pillow 降到 11.x，torchvision 仍可用。

**下一步**：重启使页面文件扩容生效，然后进入 P2 权重下载（22.5 GB）。

### 阶段 5 · P2 权重下载（进行中）

重启后复核页面文件：`AllocatedBaseSize` = 32768 MB，虚拟内存总量 63.9 GB，S1 关闭。

下载 **16 个文件 / 22.6 GiB**，其中 6 个来自 `alibaba-pai/Wan2.2-Fun-5B-Control`
（T5、Wan2.2 VAE、umT5 tokenizer），10 个来自 `Skywork/Matrix-Game-4.0`。

| 文件 | 大小 |
| --- | --- |
| `base/models_t5_umt5-xxl-enc-bf16.pth` | 10,835.6 MiB |
| `models/ancient-egypt/model.safetensors` | ~9.3 GiB |
| `base/Wan2.2_VAE.pth` | ~1 GiB |
| 其余 13 个（tokenizer、taehv、sink、许可、配置） | 合计 <100 MiB |

**实测网络**（本机代理 `127.0.0.1:7897`）

| 路线 | 速率 |
| --- | --- |
| huggingface.co 直连 | ❌ 连接超时（被墙） |
| huggingface.co 经代理（单连接） | ✅ **约 4 MB/s**（峰值曾达 18.6 MB/s） |
| `hf-mirror.com`（直连或经代理） | 1.7 MB/s，更慢 |
| 8 路并行 Range 请求 | 聚合 3.6 MB/s，无提升 |

结论：瓶颈是代理总带宽，不是单连接限制，并行与镜像都无收益。
按 4 MB/s 估算，22.6 GiB 约需 **60–90 分钟**。

下载脚本 `fetch_assets.py` 本次增强：支持 `Range` 断点续传、每个文件最多 6 次重试、
每 256 MiB 打印进度、按条目解析 `repo_id`/`repo_type`/`revision`。

### 阶段 6 · P3 首次离线渲染（已完成）

**第一次尝试：`quality` 预设（Wan VAE + 2 步 + native DiT）—— 不可用**

48 帧输入，跑 15 分钟只出了 **1 帧**，随后被判为异常终止。实测：

| 指标 | 数值 |
| --- | --- |
| 处理速度 | **0.01 FPS**（约 100 秒/帧） |
| 提交内存 | **73.4 GB / 83.1 GB**（物理 32 GB + 页文件 32 GB） |
| 页面文件占用 | 12.5 GB（持续换页） |
| GPU 常驻显存 | 20–21 GB |
| CPU | 单核占用 95% |

结论：不是算力问题，而是 **Wan2.2 VAE 的增量解码器把内存推到换页**，整机进入抖动状态。
该配置在本机（32 GB 内存）不可用。

**第二次尝试：`realtime` 预设的等价参数（TAEHV + 单步 + native DiT）—— 可用**

命令：

```powershell
matrix-game render --scene ancient-egypt --input <48 帧低模视频> `
  --resources resources --preset realtime --dit-runtime native --no-vae-graph `
  --t5-runtime native --output outputs\egypt-full
```

| 指标 | 数值 |
| --- | --- |
| 输出 | 48 帧 / 864×480 / 24fps，`status: complete`，`padded_frames: 1` |
| 纯渲染耗时 | **7.23 秒**（约 6.6 FPS） |
| 端到端耗时 | 137 秒（其中约 130 秒是模型加载） |
| GPU 显存 | **3.0 GB**（对比 quality 的 20+ GB） |
| 提交内存 | 14.5 GB（无换页） |
| 注意力后端 | `sdpa`（无 flash-attn） |

视觉结果：低模骨架被转成写实风格的古埃及场景，布局、视角、角色位置完全对齐；
单步 + TAEHV 下细节偏软，符合该配置的预期。

**门 G3 通过**：48 帧完整输出，帧数一致，无 NaN/黑帧。

**关键结论**：本机的瓶颈是**内存**而不是算力。同一份权重、同一张卡，
仅仅把编解码器从 Wan VAE 换成 TAEHV，显存从 20+ GB 降到 3 GB，速度从 0.01 FPS 升到 6.6 FPS。

---

## 9. 问题与解决记录

| # | 现象 | 根因 | 解决 |
| --- | --- | --- | --- |
| 1 | CMake 报 `No CMAKE_CXX_COMPILER could be found`，cl.exe 明明存在 | 环境里同时存在 `HTTP_PROXY` 与 `http_proxy`；Windows 变量名大小写不敏感，.NET 建子进程环境时抛 `ArgumentException` | 构建前移除小写副本；已固化进 `scripts/build_game.ps1` |
| 2 | `hf_hub_download` 报 httpx `Invalid port: ':1]'` | `no_proxy` 中含 `[::1]`，httpx2 解析失败 | 覆盖 `NO_PROXY`/`no_proxy`，或改用直连脚本 |
| 3 | `FileMetadataError: Distant resource does not seem to be on huggingface.co` | 代理剥离了 `x-linked-etag` / `x-linked-size` 响应头 | 改为按 manifest 的 SHA256 直连下载（`fetch_assets.py`），已校验 17/17 通过 |
| 4 | 构建成功但运行报缺少场景资产 | VS 多配置生成器把 exe 放入 `bin/Release/`，而入口按 `exe/../..` 推导资产根 | 修改 CMake 输出目录（提交 `ecaf68c`） |
| 5 | `--record` 静默失败（退出码 1，无输出） | 该 exe 为 WIN32 子系统，stderr 不连控制台；实际缺 FFmpeg | 装 FFmpeg 到 `game/bin`；同时改用 `Start-Process -Wait` 取退出码 |
| 6 | `--capture` 产出全黑 PNG | 该分支只渲染不 `present()`，读的是未呈现的 swapchain 后缓冲 | 改用 `--benchmark`（逐帧 present）或系统级窗口截图 |
| 7 | PowerShell 调用 exe 后退出码为空、文件"没生成" | WIN32 子系统程序用 `&` 调用不阻塞 | 统一用 `Start-Process -Wait` |
| 8 | Windows 上 `source-manifest.json` 大量校验不匹配 | 全局 `core.autocrlf=true` 把 LF 转成 CRLF | 仓库级 `core.autocrlf=false` 并重新检出；校验恢复 773/780，剩余 7 条为真实的清单过期 |
| 9 | 用 PowerShell 批量替换文档内容后中文全部变成乱码 | PowerShell 5.1 的 `Set-Content` 默认按 ANSI 写盘，UTF-8 内容被破坏 | `git checkout` 回滚后改用编辑工具逐处修改；此后所有文本改动都走编辑工具，并校验 `U+FFFD` 计数为 0 |
| 10 | 页面文件只有 2.9 GB，加载 22 GB 权重时可能 OOM | `PagingFiles` 为 `E:\pagefile.sys 0 0`（系统托管），Windows 按内存比例给了小值 | 以管理员权限把注册表改为 `E:\pagefile.sys 32768 65536`（`set-pagefile.ps1`），重启生效 |
| 11 | 重启后权重下载全部 `ConnectTimeout` | 重启后 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量消失（代理客户端只注入会话级变量）；而脚本又单独设置了 `NO_PROXY`，requests 见到"只有 no 键"的环境代理字典便不再读取 Windows 注册表代理，于是走了直连 | 下载命令显式传 `HTTP(S)_PROXY=http://127.0.0.1:7897`，且**不要**单独设 `NO_PROXY`；已写入脚本注释 |
| 12 | 下载基础模型文件报 `HTTP 404` | `models.json` 里 T5/VAE/tokenizer 有**条目级** `repo_id`/`revision` 覆盖（来自 `alibaba-pai/Wan2.2-Fun-5B-Control`），脚本只用了命令行给的仓库 | 改为逐条目解析 `repo_id` + `repo_type` + `revision` + `repo_path` 生成 URL |
| 13 | 测试套件 3 个用例报 `ModuleNotFoundError: aiohttp` | `rocm` extra 不含 `server` extra，而 `test_chunk_profile` / `test_display` / `test_cli` 会导入 `matrix_game.server.app` | 在 venv 内补装 `aiohttp>=3.10,<4`；测试 77 项中 75 项通过 |
| 14 | 2 个测试报 `WinError 1314`（客户端没有所需的特权） | `test_reject_link` / `test_path_boundary` 会创建符号链接，Windows 需要管理员或开发者模式 | 属环境限制而非代码缺陷；以管理员运行或开启开发者模式即可通过，本次记为已知偏差 |
| 15 | 渲染在建 DiT 时崩：`AttributeError: module 'torch.distributed' has no attribute 'is_initialized'` | ROCm Windows 版 torch 的 `torch.distributed` 是空壳（`is_available()` 为 False，无 `is_initialized`），而 `_rank0()` 无条件调用它 | `loading.py` 与 `wrappers.py` 的 `_rank0()` 改为先 `hasattr` 探测；无分布式即为单进程、恒为 rank 0（提交 `2664a5e`） |
| 16 | `quality` 预设 15 分钟只出 1 帧，整机换页 | Wan2.2 VAE 增量解码器 + 2 步去噪把提交内存推到 73.4 GB（共 83.1 GB 可用），触发持续换页；GPU 常驻 20+ GB | 改用 `realtime` 等价参数（TAEHV + 单步 + native）：显存 3 GB、渲染 6.6 FPS、无换页。Wan VAE 路径留待 P4 评估是否需要降低分辨率或分块解码 |

**页面文件变更记录（S1）**

| 项 | 值 |
| --- | --- |
| 变更前 | `E:\pagefile.sys 0 0`（系统托管，实占 2944 MB） |
| 变更后 | `E:\pagefile.sys 32768 65536` |
| 为什么放 E: | C: 与 D: 同属 Disk 0，E: 是 Disk 1；页面文件与模型权重分处不同物理盘，避免 I/O 争抢 |
| 执行方式 | `Start-Process -Verb RunAs` 提权运行 `D:\work\matrix-game-artifacts\set-pagefile.ps1`，日志见同目录 `pagefile-change.log` |
| 重启后验证 | ✅ 已通过：重启后 `Win32_PageFileUsage` 的 `AllocatedBaseSize` = **32768 MB**，虚拟内存总量 63.9 GB，E: 余量由 78.8 GB 降至 49.8 GB |

---

## 10. 风险登记

| # | 风险 | 概率 | 影响 | 状态 / 缓解 |
| --- | --- | --- | --- | --- |
| R1 | BF16 不支持或极慢 | 中 | 高 | ✅ 已排除（3.7e-3，82.9 TFLOPS） |
| R2 | MIOpen conv3d 回退/报错 | 中 | 高 | ✅ 已排除（bf16/fp16 均正常） |
| R3 | hipGraph 不完整 | 中 | 中 | ✅ 已排除（capture+replay 成功） |
| R4 | 32 GB 内存 + 22 GB 权重换页 | 中 | 高 | ⚠️ **已实测命中**：Wan VAE 路径提交 73.4 GB、整机换页；TAEHV 路径 14.5 GB、无换页。S1 与 C2 已缓解加载期，解码器仍需 P4 处理 |
| R5 | `diffusers 0.39` / `transformers 5.17` 与 torch 2.9 不兼容 | 中 | 中 | ✅ 未见问题：导入正常，权重建库与渲染均通过 |
| R6 | ROCm 轮子下载慢/损坏 | 低 | 中 | 已成功安装；保留命令记录便于重装 |
| R7 | 驱动过新导致兼容问题 | 低 | 中 | 实测正常；必要时回退 26.1.1 |
| R8 | 性能低于预期（<3 FPS） | 低 | 中 | 算力实测乐观；仍可用于离线渲染 |
| R9 | **电源瞬态导致黑屏重启** | 中 | 高 | 750 W 搭配 7900 XTX；防护 S5/S6/S7，重负载实验前必须完成 |
| R10 | 降分辨率到 512×288 后画质崩坏（分布外） | 高 | 中 | 实测抽帧对比；退路 640×360 或保持 864×480 |
| R11 | 注意力后端在 ROCm 上只有 math 通道 | 中 | 高 | 先探测可用后端；若无则需自研或接受上限 |

---

## 11. 资源预算与实测占用

| 项目 | 预算 | 实测 |
| --- | --- | --- |
| ROCm SDK + torch（venv） | 8–12 GB | **10.82 GB** |
| 模型权重（单场景） | 22.5 GB | — |
| 下载峰值（缓存 + 拷贝） | ~45 GB | — |
| 输出视频 | <100 MB | — |
| D: 当前可用 | — | 169.6 GB |

---

## 12. 环境档案（可复现）

| 项 | 值 |
| --- | --- |
| venv 路径 | `D:\work\Matrix-Game\.venv-rocm` |
| Python | 3.12.12 |
| torch / torchvision / torchaudio | `2.9.1+rocmsdk20260116` / `0.24.1` / `2.9.1` |
| ROCm SDK | `rocm-sdk-core / devel / libraries-custom` 均 `7.2.0.dev0`，`rocm 7.2.0.dev0` |
| 关键传递依赖 | numpy 2.5.4、pillow 13.0.0、sympy 1.14.0、networkx 3.7、filelock 4.2.0、fsspec 2026.9.0、jinja2 3.1.6 |
| 推理依赖（P1） | diffusers 0.39.0、transformers 5.17.0、accelerate 1.15.0、safetensors 0.8.0、tokenizers 0.23.3、huggingface_hub 1.33.0；完整列表见 §8 阶段 4 |
| 仓库本体 | `matrix-game-4.0 4.0.0rc1`，可编辑安装（`pip install -e . --no-deps`） |
| venv 体积 | 11.1 GB（P0 时为 10.82 GB） |
| 自检脚本 | `D:\work\matrix-game-artifacts\check_rocm.py` |

**缓存重定向（用户级环境变量，已生效）**

```
PIP_CACHE_DIR = D:\pip-cache
UV_CACHE_DIR  = D:\uv-cache
HF_HOME       = D:\hf-cache
HF_HUB_CACHE  = D:\hf-cache\hub
TORCH_HOME    = D:\torch-cache
```

pip 临时目录不设全局，按命令临时指定到 `D:\work\Matrix-Game\.tmp`，避免污染系统临时目录。

---

## 13. 待办

1. **S5/S6/S7 电源与护栏**（优先）：GPU 功耗 -10%~-15%、避免 CPU/GPU 同时满载、加内存与温度看门狗。
2. **P4 测量 harness**：一次加载、同进程内跑多组配置，记录 encode/denoise/decode 分段耗时、显存、提交内存与抽帧画质。
3. 依据 harness 结果决定：是否降分辨率到 512×288、是否启用 hipGraph、是否有更快的注意力后端。
4. 实施 C5（`--preset rocm`）与 C9（Godot 集成，见 §16.6）。
5. 把 `check_rocm.py` 收敛为仓库内 `scripts/check_rocm.py`（C7）。

---

## 14. 附录 A：P0 完整命令

```powershell
cd D:\work\Matrix-Game
uv venv .venv-rocm --python 3.12 --seed
$py = ".venv-rocm\Scripts\python.exe"

# ROCm SDK
& $py -m pip install --no-cache-dir --progress-bar off `
  https://repo.radeon.com/rocm/windows/rocm-rel-7.2/rocm_sdk_core-7.2.0.dev0-py3-none-win_amd64.whl `
  https://repo.radeon.com/rocm/windows/rocm-rel-7.2/rocm_sdk_devel-7.2.0.dev0-py3-none-win_amd64.whl `
  https://repo.radeon.com/rocm/windows/rocm-rel-7.2/rocm_sdk_libraries_custom-7.2.0.dev0-py3-none-win_amd64.whl `
  https://repo.radeon.com/rocm/windows/rocm-rel-7.2/rocm-7.2.0.dev0.tar.gz

# torch 栈
& $py -m pip install --no-cache-dir --progress-bar off `
  "https://repo.radeon.com/rocm/windows/rocm-rel-7.2/torch-2.9.1%2Brocmsdk20260116-cp312-cp312-win_amd64.whl" `
  "https://repo.radeon.com/rocm/windows/rocm-rel-7.2/torchaudio-2.9.1%2Brocmsdk20260116-cp312-cp312-win_amd64.whl" `
  "https://repo.radeon.com/rocm/windows/rocm-rel-7.2/torchvision-0.24.1%2Brocmsdk20260116-cp312-cp312-win_amd64.whl"

# 自检
& $py D:\work\matrix-game-artifacts\check_rocm.py
```

## 14b. 附录 A2：P1 依赖安装命令

```powershell
cd D:\work\Matrix-Game
$env:TMP = "D:\work\Matrix-Game\.tmp"; $env:TEMP = $env:TMP   # 不污染 C: 临时目录
$py = ".venv-rocm\Scripts\python.exe"

# 推理依赖（不带 --deps 的 torch；版本与仓库 inference extra 一致）
& $py -m pip install --no-cache-dir --progress-bar off `
  "diffusers==0.39.0" "transformers==5.17.0" "accelerate==1.15.0" "safetensors==0.8.0" `
  "einops>=0.8" "ftfy>=6" "regex" "sentencepiece>=0.2" "tqdm>=4.66" "imageio-ffmpeg>=0.5"

# 仓库本体：可编辑、不解析依赖，避免回溯到 PyPI 的 CUDA 版 torch
cd Matrix-Game-4
& $py -m pip install -e . --no-deps --no-build-isolation --progress-bar off

# 验收 G1
& $py -c "import torch, matrix_game, diffusers, transformers; print(torch.__version__, diffusers.__version__, transformers.__version__)"
```

## 15. 附录 B：阶段 0 引擎命令

```powershell
cd D:\work\Matrix-Game\Matrix-Game-4
powershell -ExecutionPolicy Bypass -File scripts\build_game.ps1
$env:MATRIX_GAME_DATA_DIR = "$PWD\resources\game"
.\game\bin\MedievalCastleBBoxMap.exe --scene ancient-egypt
.\game\bin\MedievalCastleBBoxMap.exe --scene ancient-egypt --record --duration 10 --recording-dir ..\captures\rec
.\game\bin\MedievalCastleBBoxMap.exe --scene ancient-egypt --benchmark ..\captures\bench.json
```

## 16. 性能目标、集成方案与稳定性（2026-10-17 讨论纪要）

### 17.1 事故记录：瞬间黑屏后自动重启

| 项 | 内容 |
| --- | --- |
| 现象 | GPU 满载推理 + CPU 多线程预处理同时进行时，**瞬间黑屏后自动重启**（非蓝屏、非卡死后重启） |
| 判定 | 更符合**电源瞬态保护**（PSU OCP/OPP）而非软件崩溃；内存压力通常表现为卡死、蓝屏或 OOM 报错，不会像拔电一样断电 |
| 硬件 | 7900 XTX（TBP 约 355 W，瞬态尖峰显著更高）+ 5800X3D（约 105 W）+ **750 W 电源** |
| 事发负载 | 模型加载 + 渲染 + 屏幕抓取 + 游戏引擎同时运行；提交内存一度达 73.4 GB（Wan VAE 路径） |
| 处置 | 先完成 S5（功耗 -10%~-15%）、S6（避免 CPU/GPU 同时满载）、S7（看门狗），再继续重负载实验 |

### 17.2 指标重新定义

| 环节 | 指标 | 说明 |
| --- | --- | --- |
| 游戏逻辑 | **30 Hz** | 用户定义的硬指标；120 Hz 物理每 4 个 tick 出一帧 |
| 模型渲染 | **10–15 FPS 可用，20+ 更好** | 与逻辑帧解耦，允许丢帧 |
| 显示 | 展示最新可用渲染帧 | 不排队、不追帧 |
| 延迟 | 暂不设指标 | 用户明确先不讨论延迟与帧率的关系 |

### 17.3 现状基线（均已实测）

| 项 | 数值 |
| --- | --- |
| 引擎渲染 | 1648 FPS（0.6 ms/帧），30 FPS 时 GPU 占用可忽略 |
| 模型渲染（无争抢） | **6.6 FPS**（48 帧 / 7.23 s），0.6 s/块 |
| 模型渲染（并排预览、引擎未限帧） | 1.0–1.1 FPS；抓屏仅 4.3 FPS |
| `quality` 预设 | 0.01 FPS，提交内存 73.4 GB → 不可用 |
| 模型加载 | 约 130 s（每次启动的固定成本） |
| bf16 GEMM | 82.9 TFLOPS（4096³）；DiT FFN 真实形状 37 ms/步 |
| 显存占用 | lean 3.0 GB；quality 16–21 GB |
| 提交内存 | lean 14.5 GB；quality 73.4 GB |

**核心矛盾**：纯算力推算每块约 50–80 ms（15–20 FPS），实测 600 ms，**有 8 倍差距尚未归因**。
在测量之前任何优化都是猜测。

### 17.4 分辨率选项与风险

| 输出 | latent 网格 | patch 后 token | 相对 864×480 |
| --- | --- | --- | --- |
| 864×480（训练分辨率） | 54×30 | 405 | 基线 |
| 640×360（折中，待测） | 40×22 | 220 | -46% |
| **512×288（用户提议）** | 32×18 | **144** | **-64%** |

风险：模型按 864×480 训练，改分辨率属于**分布外输入**——RoPE 频率、sink 图、文本条件
都按 480p 校准。速度会明显提升，但画质可能显著退化。**必须实测抽帧对比，不能只看帧率。**

### 17.5 优化候选（待 harness 验证，按预期收益排序）

| 手段 | 预期 | 代价 | 验证方式 |
| --- | --- | --- | --- |
| 降分辨率到 512×288 | 大 | 小 | 速度 + 抽帧画质 |
| hipGraph 捕获 DiT + codec | 大 | 中 | 分段耗时对比（本机 capture/replay 已验证可用） |
| 注意力后端（SDPA 的 flash/efficient 通路） | 大 | 小 | `torch.backends.cuda` 后端探测 + 计时 |
| 流水线重叠（编码/去噪/解码） | 中 | 中 | 端到端块耗时 |
| TAEHV 改用 fp16 / 图捕获 | 中 | 小 | 画质 + 速度 |
| `torch.compile` 关键块 | 中 | 中 | 编译耗时与稳定性 |

### 17.6 与 Godot 的集成路线

目标形态：**常驻渲染服务**，模型只加载一次；游戏逻辑 30 Hz 与渲染帧率彻底解耦。

| 方案 | 做法 | 优点 | 缺点 |
| --- | --- | --- | --- |
| A. 窗口捕获 | DXGI Desktop Duplication 抓游戏窗口 | 不改游戏代码，通用 | 会抓到 UI；窗口被遮挡即失效；延迟多一帧 |
| **B. 离屏视口 + 共享内存（推荐）** | Godot 用 `SubViewport` 渲染无 UI 画面，回读后写入共享内存环形缓冲 | **UI 天然不进模型**，符合 v4 的硬规则；分辨率可控；跨 API | 需要 GPU→CPU 回读（每帧 1280×720×3 B ≈ 2.6 MB） |
| C. 共享 GPU 纹理 | D3D12/Vulkan external memory 直接共享纹理 | 最低延迟、零回读 | 跨 API 复杂，Godot 侧需 GDExtension |

方案 B 的数据量估算：30 Hz × 2.6 MB ≈ **78 MB/s**，共享内存完全够用。
回读建议用 Godot 4 的 `RenderingDevice.texture_get_data_async`（异步回读），版本不支持时退回
`get_image()` 同步回读并接受一次 GPU 同步。

链路草图：

```
Godot（30 Hz 逻辑）
  SubViewport 1280×720（隐藏 UI 图层）
    → 回读 RGB24
    → 共享内存环形缓冲（3 槽，丢旧帧）
渲染服务（常驻进程，模型只加载一次）
    → 取最新帧 → TAEHV encode → DiT（1 步）→ TAEHV decode
    → 共享内存输出（864×480 或 512×288）
Godot → 用 CanvasLayer / TextureRect 显示渲染结果
```

### 17.7 Linux 方案评估（结论：不采用）

| 方案 | 能解锁 | 代价 | 结论 |
| --- | --- | --- | --- |
| WSL2 + ROCm | Triton（AMD 后端）、部分融合 kernel | 显存/I/O 开销、GStreamer/EGL 仍麻烦、磁盘翻倍 | ❌ 收益不确定 |
| 原生双系统 Ubuntu | 官方 ROCm 全栈、Triton、AITER/CK 注意力 | 每次测试切系统、环境重建 | 🟡 仅适合"上限探测" |
| 远程 Linux GPU | 同上，不折腾本机 | 需额外机器与带宽 | ❌ 不具备 |

关键事实：**Linux 也救不了 v4 最快的那条路**。仓库的 `rtx5090` 预设依赖 FP8/INT8 融合 kernel，
是 NVIDIA 专属；`flash-attn` 同样不支持 AMD。Linux 的真实收益大致是注意力环节 1.5–2.5 倍，
但方向与"Windows + 自研游戏"的目标相反，因此**确定继续走 Windows**。

### 17.8 下一步

1. 完成 S5–S7 防护。
2. 做"一次加载、多配置"测量 harness，产出 §16.5 所需的分段耗时与画质数据。
3. 依据数据决定：分辨率、是否上 hipGraph、是否需要自研注意力优化。
4. 按 §16.6 方案 B 实现渲染服务与 Godot 侧联调。

---

## 17. 文档维护约定

- 每完成一个阶段：更新 §1 状态表、§6 门禁表、在 §8 追加该阶段记录。
- 新增问题时：先记入 §9，写明现象 / 根因 / 解决，避免重复踩坑。
- 风险状态变化时：更新 §10 的"状态"列，已排除的保留记录而不是删除。
- 本文档不随源码发布打包（不在 `package_release.py` 的 `PUBLIC_DOCS` 名单内）。
