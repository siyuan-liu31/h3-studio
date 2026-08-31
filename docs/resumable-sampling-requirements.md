# H3 生成任务持续续跑需求

> 状态：需求草案
>
> 日期：2026-08-30
>
> 范围：MiniMax H3 Video Studio 服务端、ComfyUI 工作流、前端和 `h3ctl`

## 1. 目标

用户生成完成后，可以自行指定再跑多少步，服务端从该任务最新 checkpoint 继续，不重算已经完成的步骤。

首次步数和续跑步数都不固定：

```text
首次 3 步 → 继续 2 步 → 当前共 5 步
首次 4 步 → 继续 6 步 → 当前共 10 步
首次 7 步 → 继续 1 步 → 当前共 8 步
```

## 2. 使用方式

首次生成沿用现有步数参数：

```bash
h3ctl generate video --steps 5 ...
```

继续生成新增命令：

```bash
h3ctl job resume JOB_ID --additional-steps 3
```

规则：

1. `--steps` 由用户自行设置，不固定为 4。
2. `--additional-steps` 由用户自行设置，不固定为 4。
3. 用户只提供任务 ID 和追加步数，不提供 checkpoint ID。
4. 传入同一任务链中的任意任务 ID，服务端都自动使用该链最新 checkpoint。
5. 新总步数等于当前总步数加追加步数。
6. 新总步数不得超过当前 Profile 声明的最大值。

可结合等待和下载：

```bash
h3ctl job resume JOB_ID \
  --additional-steps 3 \
  --wait \
  --download continued.mp4
```

## 3. 服务端行为

续跑请求到达后，服务端必须：

1. 找到任务所属的续跑链。
2. 获取该链最新有效 checkpoint。
3. 校验 Profile、模型、LoRA、Prompt、Seed、参考素材和采样参数未变化。
4. 从当前采样位置继续执行用户指定的追加步数。
5. 创建新的结果任务，旧结果保持不变。
6. 成功后保存新的最新 checkpoint。

示例：

```text
任务 A：首次 3 步
  └─ 任务 B：继续 2 步，当前 5 步
       └─ 任务 C：继续 4 步，当前 9 步
```

用户对 A、B 或 C 发起续跑时，都从任务 C 对应的最新 checkpoint 继续。

同一任务链同时只能运行一个续跑任务。

## 4. 真续跑要求

真续跑必须使用中间采样状态，不能把已经生成的视频重新加噪后当作续跑。

服务端至少保存：

- 最新 latent。
- 当前 sigma 位置和续跑所需的 sigma 调度。
- Seed、噪声或 RNG 状态。
- Profile、模型、LoRA、Sampler 和 Scheduler 摘要。
- Prompt、参考素材和工作流摘要。

首次生成必须使用当前 Profile 定义的可续跑调度。Profile 负责声明最大总步数和允许的追加步数范围，前端与 CLI 不写死具体步数。

用于继续采样的 latent 和用于展示的预览结果必须分开。预览可以解码当前 denoised estimate，但不能把预览视频重新编码成续跑 latent。

只有经过测试并声明支持续跑的 Profile 才开放该能力。Turbo4 LoRA 能否超过 4 步需要单独验证，不能默认认为步数越多越好。

## 5. API

新增：

```text
POST /api/jobs/:id/resume
```

请求：

```json
{
  "additional_steps": 3,
  "request_id": "stable-request-id"
}
```

回执：

```json
{
  "job_id": "NEW_JOB_ID",
  "parent_job_id": "LATEST_JOB_ID",
  "steps_before": 5,
  "additional_steps": 3,
  "steps_after": 8,
  "status": "queued"
}
```

任务查询增加：

- 是否可以续跑及不可续跑原因。
- 当前总步数和最大总步数。
- checkpoint 创建时间和过期时间。
- 最新任务 ID。

## 6. Checkpoint 存储策略

采用以下确定策略：

1. 每个任务链只保留最新续跑点，不保存每一步。
2. 任务完成后保留 24–72 小时，默认 48 小时。
3. 续跑完成后，新 checkpoint 覆盖旧 checkpoint。
4. 定期清理已过期或已删除任务关联的 latent。

补充规则：

- 新 checkpoint 完整写入并校验成功前保留旧 checkpoint。
- 续跑失败或取消时保留旧 checkpoint，允许重试。
- 续跑成功后重新计算过期时间。
- 模型、LoRA 和参考素材不重复复制，只保存摘要和引用。
- checkpoint 有效期内，关联参考素材不能被物理清理。
- 不支持续跑的普通任务不产生 checkpoint。

## 7. 自动清理

服务端建议每 30 分钟执行一次 checkpoint GC，并在启动时执行恢复扫描。

GC 清理：

- 已过期 checkpoint。
- 已删除任务链的 checkpoint。
- 写入失败留下的临时文件。
- 没有有效任务记录的孤儿 checkpoint。

GC 必须跳过正在运行或写入的 checkpoint，并且只能操作受控数据目录。

## 8. 前端

支持续跑的结果显示：

```text
当前总步数：5
最大总步数：12
追加步数：[ 3 ]
续跑点有效至：2026-09-01 18:30
[继续生成]
```

要求：

- 追加步数由用户输入，不提供固定 4 步按钮。
- 提交前显示续跑后的总步数。
- 同一任务链正在续跑时禁止重复提交。
- checkpoint 过期或不可用时显示真实原因。
- 刷新后从服务端恢复最新续跑状态。

## 9. 主要错误

- 当前 Profile 不支持续跑。
- checkpoint 不存在、已过期或损坏。
- 追加步数非法或超过 Profile 最大总步数。
- 任务状态与 checkpoint 不一致。
- 同一任务链已有续跑任务正在运行。
- 模型、LoRA 或参考素材已不可用。
- checkpoint 存储空间不足。

发生以上错误时必须明确失败，不允许静默从头重新生成。

## 10. 验收标准

1. 首次步数可以是 Profile 范围内的任意正整数。
2. 每次追加步数可以是 Profile 范围内的任意正整数。
3. 对同一任务链中的任意任务 ID 发起续跑，都使用最新 checkpoint。
4. 续跑只执行新增步骤，不重复计算已完成步骤。
5. 续跑产生新任务和新结果，旧结果保持不变。
6. 每条任务链始终只保留一个最新 checkpoint。
7. 新 checkpoint 成功后覆盖旧 checkpoint；失败或取消时旧 checkpoint 仍可使用。
8. checkpoint 按 24–72 小时配置过期，默认 48 小时。
9. 定期 GC 能清理过期、已删除和孤儿 latent，并跳过运行任务。
10. 修改 Prompt、Seed、参考素材、模型、LoRA 或采样配置后拒绝续跑。
11. 服务重启后，未过期的最新 checkpoint 仍可恢复。
12. CLI、前端和 API 显示的当前步数、追加步数和新总步数一致。

## 11. 测试要求

- 覆盖不同首次步数和不同追加步数组合。
- 验证旧任务 ID 自动解析到最新 checkpoint。
- 验证同一任务链不能并发续跑。
- 验证失败、取消和重启不会损坏旧 checkpoint。
- 验证 checkpoint 原子覆盖、TTL 和定期 GC。
- 验证超出最大步数和状态不一致时明确失败。
- 验证 `h3ctl job resume` 的 JSON、JSONL、等待和下载行为。
