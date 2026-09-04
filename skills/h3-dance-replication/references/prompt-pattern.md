# H3 舞蹈复刻提示词骨架

仅在编译新的舞蹈复刻 prompt 时读取。把占位符替换为本次素材的真实标签、时间与外观，不把占位符提交给 H3。

## Subject 定义

```text
subject_definitions:
<Subject 1> is the complete original dancer ensemble in <Video 1>. It contains [SOURCE DANCER COUNT AND ROLE/SLOT DESCRIPTION]. It is used only for choreography, timing, spatial roles, formation changes, camera, and edit rhythm. Its identities, appearances, clothing, accessories, environment, watermark, logos, and visible text are not target content.

<Subject 2> is the complete target ensemble containing exactly [TARGET COUNT] characters. [SOURCE-ROLE TO TARGET-CHARACTER SLOT MAP]. Each target identity is exclusive; no identity blending, duplication, additional person, disappearance, or unintended position swap.

For each target character, allocate one independent numeric Subject starting at <Subject 3>:

<Subject [3 + INDEX]> is [CHARACTER NAME], whose identity and full appearance come only from <Picture [1 + INDEX]>. [OBSERVED APPEARANCE]. This character occupies stable formation slot [SLOT ID AND OPENING SCREEN POSITION], follows [SOURCE DANCER OR CHOREOGRAPHIC ROLE], and changes position only when the reference choreography explicitly requires it.

[OPTIONAL LEAD/CENTER CLAUSE] [CHARACTER] is the lead dancer and remains [USER-SPECIFIED ANCHOR, SUCH AS EXACT CENTER OR FRONT-CENTER] for the entire video. This character never swaps that anchored role.

After the last character Subject, allocate the next numeric Subject to the environment:

<Subject [NEXT ID]> is the only target environment: [POSITIVE NEW BACKGROUND]. It contains no other people, no readable signs, and none of [SOURCE BACKGROUND FEATURES].

<Video 1> is the exact source for choreography, pose order, footwork, weight shifts, all formation-slot roles and changes, synchronization, camera height and movement, shot boundaries, hard cuts, and edit rhythm. It is not a source for target identity, clothing, environment, watermark, logos, or text.

<Audio 1> is the separately extracted complete audio from <Video 1>. It is the sole audio source and exact timing reference.
```

## Summary 与时间段

```text
summary:
[video editing + character replacement + exact dance replication + background replacement + complete source-audio reuse]
Create one continuous [DURATION]-second [ASPECT] video. Replace every mapped original dancer or choreographic role with the assigned target character while copying the reference choreography and edit timing exactly. Keep exactly [TARGET COUNT] target characters. Preserve target identities, outfits, stable slot assignments, formation logic, and the new environment throughout.

At 00:00.000, establish every target identity and the opening formation immediately: [OPENING SLOT MAP]. Do not show any source dancer, source outfit, source background, watermark, logo, subtitle, or visible text.

[Shot 1] At 00:00.000–[CUT]: reproduce [OPENING SHOT, CAMERA, FOOTWORK, POSES]. Keep [REQUIRED CHARACTERS OR BODY PARTS] visible and preserve [SLOT/ANCHOR REQUIREMENTS] even when faces are cropped.

[Shot 2] At [CUT]–[END]: hard cut exactly as in <Video 1> to [GROUP SHOT]. Copy the full choreography, group timing, arm sequence, steps, weight shifts, formation changes, and ending pose. Preserve every Subject-to-slot assignment; apply any lead or center anchor only when specified.

environment_and_exclusions:
Use only <Subject [ENVIRONMENT ID]>. Completely remove [SOURCE ARCHITECTURE], [SOURCE FLOOR], [SOURCE LIGHTING], [SOURCE PROPS], and the source watermark, especially at [WATERMARK LOCATION]. No watermark, logo, UI, subtitle, caption, signature, readable text, extra person, duplicate limb, or identity merge.

audio:
Copy <Audio 1> from start to end as the only audio. Do not generate or add dialogue, narration, music, ambience, foley, or sound effects.
```

## 编译检查

- Picture、Video、Audio 标签与 `--ref` 顺序一致。
- 精确人数、源舞蹈角色、目标 Subject 与编队槽位映射无歧义。
- 用户指定的主舞、中央位、前后排或其他固定位置在 0.00 秒和每个相关 shot 中都被锚定；未指定时不强加中央位。
- 新背景是正向唯一环境，原背景与水印被显式排除。
- 独立音轨是唯一声音来源。
- 时长、切点、画幅和步数符合用户要求与当前 H3 Profile。

这些是生成前的 prompt preflight；不要把它扩展为生成后的成片校验。
