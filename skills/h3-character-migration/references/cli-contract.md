# h3ctl 人物迁移 CLI 与运行合同

仅在实际规划、提交、恢复或分段交付人物迁移时读取。

## 快速命令

```bash
h3ctl --output jsonl video migrate-character \
  --source SOURCE_VIDEO \
  --character TARGET_IMAGE \
  --source-subject "the only performer centered in frame" \
  --details "Keep the target identity and observed outfit stable" \
  --profile minimax-h3-ref2va \
  --steps 4 \
  --lora-strength 1 \
  --segment-frames 243 \
  --overlap-frames 39 \
  --audio copy-source \
  --to OUTPUT.mp4 \
  --timeout 0 \
  --poll-interval 5s
```

连接远程时使用用户当前已授权的 `--context` 或 `--server`。不要在技能中硬编码主机、端口、密码或数据目录。

## 参数含义

| 参数 | 含义与合同 |
|---|---|
| `--source` | 源视频的本地路径、`asset:ID` 或其他受支持 locator。 |
| `--character` | 一个目标角色图片 locator。v1 只接受一个目标。 |
| `--source-subject` | 唯一识别待替换人物的可见描述；“person/人物”等泛称应拒绝。 |
| `--details` | 追加到系统构建的完整替换提示词；适合普通外观稳定要求。 |
| `--details-file` | 从文件读取 details；与 `--details` 互斥。 |
| `--prompt-file` | 完全接管专家 prompt；与两个 details 参数互斥，并遵守稳定别名规则。 |
| `--profile` | H3 Ref2VA Base 或 Turbo Profile；默认 `minimax-h3-ref2va`。 |
| `--steps` | Profile 允许范围内的采样步数；Turbo 推荐 4，但 Turbo 并非只能 4 步。 |
| `--lora-strength` | Turbo LoRA 强度，当前合同范围 0–2；Base 必须为 0。 |
| `--seed` | `-1` 表示随机，否则为非负整数。 |
| `--segment-frames` | 每次 H3 生成帧数，默认 243；合法值为 124 至 362 的 `17k+5`。 |
| `--overlap-frames` | 普通相邻段基础 overlap，默认 39；合法值为 5、22、39、56。 |
| `--audio` | `copy-source`、`reference-source`、`generate` 或 `mute`。 |
| `--plan-only` | 解析资源并返回规划，不创建项目、不运行 GPU。不能与 `--detach` 同用。 |
| `--detach` | 创建并启动项目后立即返回项目 ID；不等待下载。 |
| `--to` | 最终本地路径；前台执行必填，plan-only/detach 可省略。 |
| `--force` | 允许覆盖已有输出；没有明确授权不要使用。 |
| `--timeout` | 本地等待上限；`0` 表示不限等待时间。 |
| `--poll-interval` | 项目轮询间隔，默认 5 秒。 |
| `--spec` | 使用版本化 JSON 输入；不能再混用同义的快捷输入参数。 |

合法 `segment_frames` 为：

```text
124, 141, 158, 175, 192, 209, 226, 243,
260, 277, 294, 311, 328, 345, 362
```

## 音频策略

- `copy-source`：最终丢弃生成音频，精确 pad/trim 后复用源视频音轨。舞蹈和动作复刻默认用它。
- `reference-source`：每段把相应源音频提供给 H3，并保留生成音频。
- `generate`：不引用源音频，保留 H3 生成音频。
- `mute`：最终移除音频。

`copy-source` 和 `reference-source` 要求源视频存在可用音轨，应在规划阶段失败而不是生成后才失败。

## 分段和尾段回填

设源视频在规范化 24 FPS 时间线上有 `F` 帧，每段生成 `G` 帧，基础重叠为 `O`，普通步长为：

```text
stride = G - O
```

第一段从 0 开始。普通后续段起点是前序输出游标减去基础 overlap。最后一段选择不小于基础 overlap 的最大合法 overlap，只要 `G - terminal_overlap` 足以覆盖剩余帧；因此尾窗向前移动，尽量用真实源帧填满整个 H3 窗口。

以 `F=311, G=124, O=5` 为例：

```text
窗口                    实际裁头       拥有输出
[0, 124)                   0             124
[119, 243)                 5             119
[187, 311)                56              68
```

检查：

```text
124 + 119 + 68 = 311
input_padding_frames = 0
final_trim_frames = 0
```

若源长度不落在可由合法 overlap 精确覆盖的网格上，可以保留少量不可避免的私有输入补帧与最终尾裁；不能用这条例外掩盖可通过向前回填避免的大量补帧。

## plan-only 验收

至少检查：

- Profile `available=true`，版本和 digest 已固定。
- `source_frames`、输出宽高和时长符合源素材。
- 所有窗口满足 `start < end`、时间有序且终窗结束于 `source_frames`。
- 后续段 `source_start_frame = prior_output_cursor - trim_head_frames`。
- `input_padding_frames = generated_frames - source_frames_in_window`。
- `sum(owned_output_frames) = composed_frames`。
- `composed_frames - final_trim_frames = source_frames`。
- 每个实际 Motion Context overlap 为 5、22、39 或 56。
- 磁盘、merge quota 和 Motion Context storage 预估通过。

## 专家 prompt 的稳定别名

普通 H3 prompt 文档使用 `<Picture 1>`、`<Video 1>` 等最终标签。人物迁移不同：`source_range` 会在每段运行时生成临时视频 asset，执行器才知道该段的最终引用编号。

因此持久项目 request 中：

```text
错误：Completely replace the dancer in <Video 1> with <Picture 1>.
正确：Bind <Subject 2> to @{PERSISTENT_CHARACTER_ASSET_ID}.
正确：Preserve motion from the source video reference.
```

执行器会把持久图片 alias 和该段派生视频 alias 一起编译成最终 `<Picture 1>`、`<Video 1>`。提交前检查专家 prompt：

```bash
rg -n '<(Picture|Video|Audio) [0-9]+>' PROMPT_FILE
```

有匹配就不要创建项目。先改为稳定图片 alias 和中性源视频描述。`--plan-only` 当前可能没有执行创建层的这项检查，因此它通过不代表字面标签安全。

## 恢复命令

```bash
h3ctl project get PROJECT_ID
h3ctl project run PROJECT_ID
h3ctl project wait PROJECT_ID --timeout 0 --poll-interval 5s
h3ctl project merge PROJECT_ID
h3ctl project download PROJECT_ID --to OUTPUT.mp4
```

恢复前先看状态：

- `running`：继续等待，不重复 run。
- 部分段 `completed`、后续段 pending/failed：运行或等待同一项目，完成段应复用。
- 全部段 `completed` 但未 merge：只执行 merge。
- 已有 merged receipt：只下载，不重新生成。

## 分段文件交付

项目的 segment `download_url` 指向实际进入 concat 的生成结果：第一段通常为 `G` 帧，后续段已经在 Comfy graph 中裁去实际 Motion Context 头部。

运行时派生的源区间资产可能在 attempt 完成后回收。若用户需要每段参考视频，依据已冻结的 plan，从规范化后的 24 FPS 源视频按 `source_start_frame/source_end_frame` 精确切出；不要用大致秒数或关键帧 copy 代替帧精确区间。

最终 concat 顺序必须等于项目 `segments` 数组顺序。合并后才执行精确总帧裁剪和所选音频策略。
