# H3 Studio 安装与运行指南

本文档面向首次部署和运维人员。快速命令见项目根目录的 `README.md`；本文档说明安装脚本的边界、完整依赖、ComfyUI 节点/模型和远程访问方式。

## 1. 先说结论

H3 Studio 目前提供“一条命令安装应用、一条命令启动应用”，但不是从空机器开始的全自动安装器：

```bash
python3 scripts/h3studio.py install
python3 scripts/h3studio.py start
```

`install` 只执行锁定的 `npm ci` 和 `npm run build`。它不会安装或更改以下内容：

- Python、Node.js、npm、FFmpeg 等系统软件；
- GPU 驱动、CUDA、PyTorch 或 ComfyUI；
- ComfyUI 自定义节点、SageAttention 内核或模型；
- `.env.local`、API Key、数据目录或 SSH 隧道；
- systemd、Docker、开机自启或 TLS 反向代理。

ComfyUI 和模型是最大的机器相关部分。不应在未确认授权、显存、磁盘和 GPU 架构时无提示下载它们。

## 2. 部署层级

| 目标 | 需要的内容 | 能做什么 |
| --- | --- | --- |
| 只启动界面/API | Python、Node.js/npm、FFmpeg、前端构建 | 管理界面和数据；没有 ComfyUI 时不能生成 |
| H3 Base 视频 | 上述内容 + ComfyUI + H3/KJNodes 节点 + FL2VA 或 Ref2VA 模型 + 文本编码器 + 两个 VAE | 使用 Base Profile 生成音视频 |
| H3 Turbo 视频 | H3 Base + 对应 Turbo LoRA | 使用默认的 4 步 Turbo Profile |
| 图片生成/编辑 | 上述应用 + 所选图片 Profile 的节点和模型 | Z-Image、Qwen-Image、FLUX.2 或兼容 checkpoint |
| 更佳智能分镜 | 可选 `scenedetect>=0.6.4,<0.8` | 优先使用 PySceneDetect；否则自动用 FFmpeg |

只安装当前需要的 Profile。缺少某个模型不应影响其他 Profile；应用会将不完整的 Profile 显示为不可用，不会偷换成其他模型。

## 3. 主机依赖

### 必需

| 依赖 | 要求 | 用途 |
| --- | --- | --- |
| Python | `>=3.11` | 管理脚本和后端 API；后端主体无强制第三方 pip 依赖 |
| Node.js | `>=22.13` | 前端安装、构建和生产服务 |
| npm | 随 Node.js 提供 | 按 `package-lock.json` 安装锁定依赖 |
| FFmpeg | `ffmpeg` 和 `ffprobe` 都必须在 `PATH` | 上传校验、探测、归一化、抽帧、裁剪、音轨和长视频合并 |
| ComfyUI | `COMFY_URL` 可访问 | 实际运行 GPU 工作流 |

Linux 是远程 GPU 机的主要部署环境。macOS 可运行 Studio 或通过 SSH 访问远程 ComfyUI，但 H3 是大型 GPU 工作负载，实际可用性取决于 ComfyUI、PyTorch、GPU 和所选量化模型。项目不用一个未经实机验证的统一显存数字作为保证。

### 可选

```bash
python3 -m pip install "scenedetect>=0.6.4,<0.8"
```

PySceneDetect 只用于智能分镜建议。未安装或运行失败时，后端会在同一超时预算内回退到 FFmpeg，不影响其他功能。建议把它安装到运行 H3 Studio API 的同一 Python 环境。

## 4. ComfyUI 、节点与 SageAttention

H3 Studio 不执行用户上传的任意 ComfyUI graph，而是生成受控工作流。因此，ComfyUI `/object_info` 必须报告当前 Profile 所需的类型和精确模型文件名。

项目不用一个推测的 ComfyUI 版本号代替运行时能力检测。使用包含 H3 原生节点的当前 ComfyUI，并在每次更新 ComfyUI/KJNodes 后重新检查 `/api/capabilities` 和实际 GPU 任务。

H3 视频必需的关键节点包括：

- ComfyUI 原生 H3 节点：`MiniMaxH3ImageToVideo`、`MiniMaxH3ReferenceToVideo`；
- ComfyUI 原生加载、采样、VAE、视频/音频输入和保存节点；
- [ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes) 的 `PathchSageAttentionKJ` 和 `MiniMaxH3MemoryEfficientSageAttentionPatch`；
- KJNodes 对应的 SageAttention Python/内核依赖。

安装 KJNodes 时应使用 ComfyUI 实际运行的 Python 环境，然后重启 ComfyUI：

```bash
cd /path/to/ComfyUI/custom_nodes
git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes.git
COMFY_PYTHON=/path/to/ComfyUI/.venv/bin/python
"$COMFY_PYTHON" -m pip install -r ComfyUI-KJNodes/requirements.txt
```

`COMFY_PYTHON` 右侧是示意占位：普通虚拟环境常位于 `.venv/bin/python`，便携版则使用其自带 Python。SageAttention 必须与 PyTorch、CUDA/ROCm 和 GPU 架构匹配；“可以 import”不代表首次 GPU 调用一定成功。请优先遵循 KJNodes 和当前 ComfyUI/PyTorch 的安装说明。

本项目的 R2V/V2V/RV2V 使用 ComfyUI 原生 `MiniMaxH3ReferenceToVideo`，不需要安装 `MiniMaxH3Director` 自定义节点。

一手来源：

- [ComfyUI 原生 H3 节点实现](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_minimax_h3.py)
- [Comfy-Org H3 模型与工作流索引](https://huggingface.co/Comfy-Org/MiniMax-H3/blob/main/README.md)
- [ComfyUI 官方工作流模板](https://github.com/Comfy-Org/workflow_templates/tree/main/templates)

## 5. H3 模型目录

默认配置期望下列文件名。文件名可在 `.env.local` 中替换，但必须与 ComfyUI `/object_info` 返回的选项完全一致。

```text
ComfyUI/models/
├── diffusion_models/
│   ├── minimax_h3_fl2va_pruned_int8_convrot.safetensors
│   └── minimax_h3_ref2va_pruned_int8_convrot.safetensors
├── text_encoders/
│   └── qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
├── vae/
│   ├── minimax_h3_video_vae_fp16.safetensors
│   └── minimax_h3_audio_vae_fp32.safetensors
└── loras/
    ├── minimax_h3_fl2v_lightx2v_turbo_4step_v1.0_768p_resized_avg_rank_31_bf16.safetensors
    └── minimax_h3_ref2v_lightx2v_turbo_4step_v0.1_resized_avg_rank_20_bf16.safetensors
```

Profile 与模型关系：

| Profile | 必需模型 |
| --- | --- |
| T2V/I2V/FL2V Base | FL2VA + 文本编码器 + 视频 VAE + 音频 VAE |
| R2V/V2V/RV2V Base | Ref2VA + 文本编码器 + 视频 VAE + 音频 VAE |
| FL2VA Turbo | 对应 Base 文件 + FL Turbo LoRA |
| Ref2VA Turbo | 对应 Base 文件 + Ref Turbo LoRA |

上述 Turbo LoRA 名称是开发机已验证的绑定，不代表上游仓库必然提供同名文件。如果使用不同的官方/已审核 LoRA，应显式修改 `.env.local` 或外部 Profile，记录来源、许可证和 SHA-256，并通过实际 GPU 任务验证；不要通过重命名文件伪装成既有绑定。

图片 Profile 的完整文件表、许可证和参数见 [图片工作流](image-workflows.md)。MiniMax H3 权重及使用受其上游许可证约束，部署前必须查看当前模型页。

## 6. 配置

复制模板，然后编辑本机配置：

```bash
cp .env.example .env.local
```

至少确认：

- `COMFY_URL`：ComfyUI HTTP 地址；
- `H3_STUDIO_DATA_ROOT`：持久资产、任务、派生媒体和项目目录；
- `H3_STUDIO_COMFY_INPUT` / `H3_STUDIO_COMFY_OUTPUT`：ComfyUI input/output 目录；
- `H3_STUDIO_*_MODEL` / `H3_STUDIO_*_LORA`：与实际文件一致的模型绑定；
- `H3_STUDIO_API_KEY`：用强随机值替换 `change-me`；
- `H3_STUDIO_PROXY_API_KEY`：如显式设置，必须与 API Key 相同。

`.env.local` 不得提交到 Git。建议把数据目录和 ComfyUI input/output 放在持久数据盘，不要放在会被新 release 替换的代码目录中。

## 7. 安装、检查与启动

在 H3 Studio 项目根目录执行：

```bash
# 1. 安装 package-lock.json 锁定的 Node 依赖并生产构建
python3 scripts/h3studio.py install

# 2. 检查主机依赖、配置和 ComfyUI 连通性
python3 scripts/h3studio.py doctor --check-comfy

# 3. 前台启动 API 和生产前端
python3 scripts/h3studio.py start
```

默认打开 `http://127.0.0.1:3013`。`start` 会监督 Python API 与前端；任意一个子进程退出都会停止另一个，`Ctrl-C` 会清理整个进程组。

`doctor --check-comfy` 的成功只证明：

- Python、Node.js、npm、FFmpeg/FFprobe 和工作区基本正常；
- 前端依赖/构建存在；
- ComfyUI `/system_stats` 可访问。

它不会证明所有模型或节点都已安装。启动后请打开模型选择器，或访问：

```bash
curl -fsS http://127.0.0.1:3013/api/capabilities
```

每个 Profile 都应通过 `available` 和不可用原因检查。“首页返回 200”不代表 GPU 生成已经通过；生产验收还应提交一个实际任务并检查输出。

## 8. 远程 GPU 机与 SSH 隧道

生产默认只监听 loopback，这是预期的安全配置。

远程 GPU 机：

```bash
cd /path/to/h3-studio
python3 scripts/h3studio.py start
```

本地电脑：

```bash
ssh -N -L 16020:127.0.0.1:3013 -p <SSH_PORT> <USER>@<HOST>
```

然后打开 `http://127.0.0.1:16020`。如果浏览器显示 `ERR_CONNECTION_REFUSED`，先在本地检查隧道是否仍在监听：

```bash
lsof -nP -iTCP:16020 -sTCP:LISTEN
```

不要仅为了省略 SSH 隧道就把 Web/API 绑定到 `0.0.0.0`。如确实需要公网入口，应另行配置防火墙、TLS 反向代理、强 API Key 和访问审计。

## 9. 常见问题

### `doctor` 报 Node.js 版本过低

安装 Node.js `>=22.13`，并确认运行 H3 Studio 的同一 shell/服务能在 `PATH` 中找到它。交互终端可以找到 Node 不代表 systemd、`nohup` 或调度器也使用相同 `PATH`。

### `doctor --check-comfy` 报 ComfyUI 不可达

确认 ComfyUI 已启动，并在 H3 Studio 运行环境中执行：

```bash
curl -fsS http://127.0.0.1:6006/system_stats
```

命令行不会自动将 `.env.local` 导入当前 shell，因此上面直接写了默认地址；如果配置了其他 `COMFY_URL`，请替换该地址。如两个服务在不同容器/机器，`127.0.0.1` 指向的是各自容器/机器，需要改成 H3 Studio 真正可访问的 ComfyUI 地址。

### Profile 显示“未安装”

这通常是 ComfyUI `/object_info` 缺少所需节点或精确文件名。按界面/`/api/capabilities` 返回的原因检查：

1. 模型是否在正确的 ComfyUI 目录；
2. `.env.local` 文件名是否与 ComfyUI 选项完全一致；
3. KJNodes 是否加载成功；
4. 安装或更换节点/模型后是否重启 ComfyUI。

### 页面可打开，生成才失败

依次查看 H3 Studio 日志、ComfyUI 日志和失败 job 的工作流证据。常见原因是 SageAttention 内核与 GPU 架构不匹配、显存/内存不足、文件损坏，或 ComfyUI 节点输入合同变化。接口返回 200 不能替代实际 GPU 验收。

## 10. 升级原则

- 更新代码前保存 `.env.local` 和持久数据目录；
- 使用不可变 release 目录，不要直接改动正在运行的 release；
- 新 release 重新执行 `install`、`doctor --check-comfy` 和关键测试；
- 原子切换 `current` 指针，失败时恢复上一个 release；
- 模型、用户素材、生成结果、日志、API Key 和机器配置不随代码 release 覆盖。

开发机的具体不可变 release 布局见 [LLM Wiki](llm-wiki.md)。
