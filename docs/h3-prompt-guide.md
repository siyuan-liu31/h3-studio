# MiniMax H3 提示词与素材引用指南

> 资料快照：2026-08-19。本文只采用 MiniMax、ComfyUI、Comfy-Org 的官方仓库、官方模型页和官方文档。凡非官方明示的产品决策均标记为“工程推断”。

## 1. 先选对模型族

| 前端意图 | H3 模式 | 本地 ComfyUI 节点 | 选择理由 |
| --- | --- | --- | --- |
| 只输入文字 | T2VA，使用 FL2VA 权重 | `MiniMaxH3ImageToVideo`，不接图片 | FL2VA 官方支持 0 张图，即文生音视频 |
| 1 张图作为精确首帧 | I2VA，使用 FL2VA 权重 | `MiniMaxH3ImageToVideo.first_frame` | 图片是第 0 秒的像素级端点锚点 |
| 1 张图作为精确尾帧 | L2VA，使用 FL2VA 权重 | `MiniMaxH3ImageToVideo.last_frame` | 视频应逐步收敛到图片 |
| 2 张图作为精确首、尾帧 | FL2VA，使用 FL2VA 权重 | 同时接 `first_frame`、`last_frame` | 模型补全两个端点之间的连续变化 |
| 图片用于人物、产品、场景、风格或构图参考 | Ref2VA | `MiniMaxH3ReferenceToVideo.ref_images` | 参考图不是精确的首尾帧 |
| 输入参考视频和/或参考音频 | Ref2VA | `ref_videos`、`ref_video_audios`、`ref_audios` | Ref2VA 才理解多模态参考关系 |

硬规则：一次请求只选一种模式。首/尾帧模式与全参考模式不混用。音频不能是 Ref2VA 的唯一参考输入，必须同时有至少一张图片或一段视频。

## 2. 官方提示词结构

### 2.1 T2VA / I2VA / FL2VA / L2VA

官方提示词技能要求主体使用英语，用户指定的对白、歌词和画面文字保留原语言。核心结构固定为：

```text
integrated_multimodal_description: [Shot 1] ...

overall_soundscape: ...

non_diegetic_music: ...
```

- `integrated_multimodal_description`：按播放顺序写风格、构图、主体、动作、镜头、对白、演唱和画内声音。
- `overall_soundscape`：用 1–4 句总结环境声、物理动作声和非语言人声；不要重复对白、歌唱或画内音乐。
- `non_diegetic_music`：用 1–3 句写只有观众能听见的配乐，重点写乐器、速度、节奏和动态；无配乐写 `N/A`。

关键帧模式还要在最前面加官方对齐指令，随后空一行：

```text
# I2VA
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

# FL2VA
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.

# L2VA
How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.
```

`N` 必须替换成真实的最终镜头编号；`S.SS` 必须替换成后端实际采用的有效时长并保留两位小数，不能直接使用用户输入但未经过帧网格换算的名义时长。

### 2.2 Ref2VA

官方全参考改写格式有六段，并要求六段主体均为英语：

```text
subject_definitions: ...

summary: ...

retention_analysis: ...

detailed_description: ...

overall_soundscape: ...

non_diegetic_music: ...
```

完整格式适合“提示词增强”或高级模式。直接生成时仍要确保 `detailed_description` 内的每个素材标签、镜头、动作和声音关系明确；不要只给剧情摘要。

## 3. 素材标签和 `@素材` 的映射

本地 ComfyUI 原生 `MiniMaxH3ReferenceToVideo` 明确使用以下标签，且每种模态独立从 1 开始编号：

| 前端显示 | 发给模型 | 用途 |
| --- | --- | --- |
| `@图片1` | `<Picture 1>` | 一张已连接的参考图片 |
| `@视频1` | `<Video 1>` | 整段视频的剪辑、延续、运镜、节奏或时间结构 |
| `@音频1` | `<Audio 1>` | 音频复制或音色、节奏、音乐风格、声音纹理参考 |
| 自动抽象的“主体1” | `<Subject 1>` | 来自图片/视频、会在目标视频中复用的可见人物、物体、环境、服装、风格、动作、表情或姿势 |

重要区别：

- `<Picture N>` 是素材文件/具体帧锚点；若图片只提供人物形象，不必另写一个独立 Picture 定义，而应写成 `<Subject 1> is the person in <Picture 1> ...`。
- `<Video N>` 只表示整段视频关系；从视频中复用的人物、物体或动作仍应定义成 `<Subject N>`。
- 普通参考视频带有声音，不代表一定要创建 `<Audio N>`。只有需要复用或参考其声音时，才连接对应音轨并产生 Audio 标签。
- 本地节点的展示顺序固定为图片，然后是视频（视频启用的配对音轨标签紧邻相应视频），最后是独立音频。编号按模态独立计算。前端必须根据实际连线解析标签，生成前展示“最终标签预览”，不得根据文件名猜编号。

前端的 `@图片1`、`@视频1`、`@音频1` 是易读别名；提交前必须编译成尖括号标签，且在素材被删除、重排或重新连线后同步更新。禁止留下指向不存在素材的悬空标签。

### 3.1 参考角色写法

每个素材都应说明“参考什么”和“保留到什么程度”：

```text
<Subject 1> is the woman whose appearance and blue coat come from <Picture 1>.
<Subject 2> is the walking motion and arm timing shown by the performer in <Video 1>.
<Video 1> provides the camera path and pacing structure, not the source identity.
<Audio 1> is the voice-timbre and measured delivery reference for <Subject 1> (S1).
```

官方可见内容关系词：`fully_preserved`、`partially_preserved`、`attribute_transfer`、`weak_reference`。官方音频关系词：`fully_copy`、`partially_copy`、`reference`、`weak_reference`。

示例：

```text
retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - preserve facial identity, blue coat, and silver necklace.
<Subject 2> ([Shot 1] motion): attribute_transfer - transfer the walking cadence and arm timing to <Subject 1>.
<Video 1> (camera and pacing): weak_reference - follow the broad tracking direction and beat timing without reproducing every source frame.
<Audio 1>: reference - follow the voice timbre and delivery without directly copying the source signal.
```

## 4. 镜头、动作、对白和声音

### 4.1 时间线

- `[Shot 1]` 不加时间戳；开头先写整体风格和初始构图。
- 后续镜头编号连续，切点严格递增且位于有效时长内：`[Shot 2] At 00:03.500, the camera cuts to...`。
- 只有新主体、新空间、新状态、新视点或新时间信息才值得切镜。只是景别或小角度变化时，优先用运镜。
- 每个事件都应可见或可听，按发生顺序写，避免“很感人”“高级感”等不可观察的抽象结论。

### 4.2 运镜公式

官方建议把“运动类型＋必要时的幅度＋必要时的速度”自然地写进镜头：

```text
The camera pushes in with small amplitude at slow speed toward the letter in her hands.
The camera pans right with large amplitude at fast speed, revealing the doorway.
The camera holds a static shot as the runner exits the frame.
```

可用运动包括：Zoom In/Out、Push In/Pull Out、Pan Left/Right、Truck Left/Right、Tilt Up/Down、Pedestal Up/Down、Arc Shot、Tracking Shot、Static Shot、Shake Slightly/Strongly、POV、Roll Clockwise/Counterclockwise。

### 4.3 首尾帧动作路径

- I2VA：`首帧锚点 → 动作开始 → 连续发展 → 结果/反应`。保持身份、服装、色彩、关键物体和空间关系。
- FL2VA：`首帧状态 → 可观察的中间变化 → 逐步缩小差异 → 尾帧状态`。官方一般偏好单镜头，除非用户明确要求切镜。
- L2VA：`合理的先前状态 → 明确动作与过渡 → 最终镜头逐步收敛 → 落到尾帧`。

### 4.4 对白与画面文字

- 发声主体按第一次发声顺序获得稳定 ID：`(S1)`、`(S2)`；沉默角色不编号；多人同声写 `(S1,S2)`。
- `<d>` 内只放语言标签和用户原话，不翻译、不改写：`<d>[Chinese] 我们现在出发。</d>`。
- 画外音使用 `says in an off-screen voiceover`，并明确画面人物嘴唇保持闭合。
- 跨切镜的同一句话使用 `<scenetrans>` 并明确声音连续；结尾截断使用 `<cutoff>`。
- 实际可见的招牌、字幕或标签用英文双引号包住并逐字保留，例如 `a sign reading "营业中"`。

## 5. 官方能力与本地实现限制

| 项目 | 官方 H3 能力 | 本项目当前本地配置建议 |
| --- | --- | --- |
| 输出 | 视频＋原生 32 kHz 立体声音频 | 视频和音频一起解码并封装 MP4 |
| 帧率 | 24 FPS | 固定 24 FPS |
| 时长 | 4–15 秒 | 当前 ComfyUI 节点训练区间约 124–362 帧；项目开放完整网格，生成输出约 5.17–15.08 秒 |
| 帧网格 | ComfyUI 原生节点采用 `17k+5` | `frames = min(362, ceil((seconds*24-5)/17)*17+5)`；UI 直接列真实帧数/有效时长 |
| 分辨率 | H3-Base 默认短边 768；官方完整系统可到 2K | 本地 H3-Base 主预设 `1344×768`、`768×1344`、`768×768`；宽高为 32 的倍数 |
| 图片参考 | Ref2VA 最多 9 张 | 同官方 |
| 视频参考 | 最多 3 段；每段 2–15 秒；合计不超过 15 秒 | 上传时用 `ffprobe` 预检并拒绝超限 |
| 音频参考 | 最多 3 段；每段 2–15 秒；合计不超过 15 秒 | 不允许音频单独作为唯一参考 |
| 混合参考 | 所有文件合计最多 12 个 | 连线时实时计数并在提交前二次校验 |

官方完整系统的 H3-Context-IR 和 H3-Regenerate-2K 没有随开源权重发布。本地 ComfyUI 跑的是 768p H3-Base；因此本项目的“提示词增强”是根据公开写法实现的工程近似，不能宣传为官方 Context-IR；本地输出也不能冒充官方 2K 再生成结果。

## 6. 采样步数与 Turbo LoRA

Comfy-Org 当前模板列出 FL2VA 的 4 步与 8 步 Turbo LoRA，以及 Ref2VA 的 4 步 Turbo LoRA。这些 LoRA 与对应步数是配套采样配置，不应把“步数越多”当成无条件提质。

工程规则：

- Turbo LoRA 配置默认并推荐 `steps=4`，但开发机工作流允许在 Profile 校验范围内调整 `BasicScheduler.steps`；LoRA 模型强度独立映射 `LoraLoaderModelOnly.strength_model`。更多步数并不保证更好。
- 关闭 Turbo 后使用当前 ComfyUI 官方模板基线：默认 20 步、`res_multistep`、`simple`、`BasicGuider`，并完全绕过 LoRA Loader；其它步数同样属于可复现实验参数。
- “Base”只表示关闭少步 Turbo LoRA；开源 checkpoint 本身仍是 CFG-distilled，不能写成 non-distilled H3。Sampler、Scheduler、Sigma shift 必须作为一个已测试的采样 profile 保存，而不是前端任意拼装。
- 前端展示最终使用的权重文件、LoRA、步数、Sampler、Scheduler 与有效时长，任务历史保存这些值。

## 7. 提示词增强模板

### 7.1 无参考素材

用户简述：`雨夜便利店门口，一个女孩撑伞等车，镜头缓慢靠近，只有雨声，不要配乐。`

可编译为：

```text
integrated_multimodal_description: [Shot 1] Live-action, cinematic night photography, a medium-wide shot frames a young woman standing beneath the awning of a small convenience store on a rain-soaked street. Cool fluorescent light from the storefront outlines her dark umbrella while red and white reflections ripple across the wet pavement. The camera pushes in with small amplitude at slow speed as she checks the empty road, adjusts her grip on the umbrella handle, and exhales softly. A distant bus turns the corner near the end of the shot, its headlights growing brighter in the rain.

overall_soundscape: Steady rain strikes the umbrella, awning, and pavement while water runs along the curb. Distant tires hiss on the wet road and the woman breathes softly.

non_diegetic_music: N/A
```

### 7.2 图片＋视频＋音频参考

```text
subject_definitions:
<Subject 1> is the adult woman in <Picture 1>; preserve her facial identity, short black hair, red jacket, and silver earrings.
<Subject 2> is the walking cadence and hand movement shown in <Video 1>.
<Video 1> provides the tracking-camera direction and broad beat timing.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1).

summary: [reference generation + audio reference] Generate a new cinematic street scene preserving <Subject 1>, transferring <Subject 2>'s movement, following <Video 1>'s broad camera rhythm, and using <Audio 1> as the voice reference.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - preserve identity, hairstyle, jacket, and earrings.
<Subject 2> ([Shot 1] movement): attribute_transfer - transfer the walking cadence and hand timing to <Subject 1>.
<Video 1> (camera and pacing): weak_reference - follow the broad tracking direction and beat timing without copying source frames.
<Audio 1>: reference - follow the timbre and measured delivery without copying the source waveform.

detailed_description: Cinematic live-action street photography with soft morning light and restrained handheld movement. [Shot 1] A medium tracking shot follows <Subject 1> walking along a quiet covered market corridor. Her facial identity, short black hair, red jacket, and silver earrings remain consistent with <Picture 1>. She follows <Subject 2>'s relaxed cadence and synchronized right-hand gesture from <Video 1> while the camera follows <Video 1>'s broad forward tracking direction. The adult woman with the referenced calm, low voice (S1) says: <d>[Chinese] 今天早点回家。</d>

overall_soundscape: Footsteps echo lightly beneath the covered walkway while fabric moves against the jacket. Distant vendors and a soft city hum remain in the background.

non_diegetic_music: N/A
```

## 8. 静态图片生成的诚实边界

MiniMax H3 官方开源说明把 H3 定义为生成“视频和音频”的模型，FL2VA 与 Ref2VA 的输出也都是 Video + Audio；MiniMax 官方 `mmx-h3-video` 技能还明确说明它不处理图片生成。因此：

- H3 本身不作为本项目的静态生图引擎。
- “图片生成”必须路由到独立的 ComfyUI 图像模型 profile，然后仍通过同一个后端提交、查询、预览和下载。
- 推荐第一个可配置 profile 为 FLUX.1 Schnell：ComfyUI 官方教程提供完整工作流，官方说明其 Apache-2.0 版本以 4 步生成、适合较低配置。若实际机器未安装所需权重，前端必须显示“未安装”，不能伪装成功。
- 未来可增加 `flux1_dev`、`flux_kontext` 或其他 profile，但必须分别声明许可证、模型文件、输入能力（纯文本/参考图/编辑）、默认采样参数和已验证分辨率。

## 9. 内容安全与所谓 “NSFW 支持”

官方说明：提交给托管 H3-Context-IR/API 的文字、图片、视频及增强提示会经过自动审核，涉嫌违法、色情或侵犯第三方权利的内容可能被拦截；开源许可证仍要求合法使用和尊重第三方权利。官方本地 ComfyUI 节点没有一个可承诺的“NSFW 开关”。

所以本项目不应宣传“官方支持 NSFW”，也不实现绕过托管审核的功能。工程上可以不硬编码一个笼统的成人关键词黑名单，并允许部署者配置内容政策；但必须拒绝涉及未成年人、非自愿私密内容、未经授权的真实人物色情深伪、违法内容和侵权内容，并保留来源/授权记录。这是合规设计，不是对任意成人内容生成质量或可用性的保证。

## 10. 一手资料

- MiniMax H3 官方仓库与能力说明：https://github.com/MiniMax-AI/MiniMax-H3
- MiniMax 官方基础/关键帧提示词指南：https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/base-en.txt
- MiniMax 官方全参考提示词指南：https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/ref-en.txt
- MiniMax 官方提示词技能：https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/SKILL.md
- MiniMax 官方 CLI H3 视频技能：https://github.com/MiniMax-AI/cli/blob/main/skill/h3-video/SKILL.md
- MiniMax H3 官方许可证：https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE
- ComfyUI 原生 H3 节点实现：https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_minimax_h3.py
- Comfy-Org H3 模型与工作流索引：https://huggingface.co/Comfy-Org/MiniMax-H3/blob/main/README.md
- Comfy-Org T2V 模板：https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_t2v.json
- Comfy-Org I2V 模板：https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_i2v.json
- Comfy-Org Ref2VA 模板：https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_r2v.json
- ComfyUI FLUX.1 生图教程：https://docs.comfy.org/tutorials/flux/flux-1-text-to-image

## 11. 明确标注的工程推断

以下不是 MiniMax 官方产品承诺，而是为本项目可预测运行制定的实现决策：

1. UI 开放 124–362 帧的有效网格；最后一档是本地节点的 362 帧端点，24 FPS 下生成输出约 15.08 秒。官方 H3 总能力仍标称为 4–15 秒。
2. UI 直接列出 `17k+5` 的帧数与有效时长，避免把 362 帧误标成精确 15 秒。该输出档位不改变用户上传视频/音频参考单段及各自合计不超过 15 秒的限制。
3. 以 `1344×768`、`768×1344`、`768×768` 为首批本地预设，是对官方短边 768、宽高 32 倍数和已部署机器测试成本的折中，并不代表 H3 只支持这三种尺寸。
4. `@素材` 到 `<Picture>/<Video>/<Audio>` 的编译、稳定编号、悬空引用校验属于本项目 UI 语法。
5. 采样参数被封装成成套 profile，避免 LoRA 与步数错配；这是一项可靠性设计。
6. 静态图片生成采用独立模型注册表并共享 ComfyUI 后端；H3 自身不承担静态生图。
