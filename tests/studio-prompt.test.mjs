import assert from "node:assert/strict";
import test from "node:test";

import { H3_REFERENCE_PROMPT_TEMPLATE, hasPromptForOutput, promptForOutput, promptModePayload } from "../app/studio-prompt.ts";

test("image generation uses its own positive prompt rather than video or negative prompt", () => {
  assert.equal(promptForOutput("image", "video scene", "  image scene  "), "image scene");
  assert.equal(hasPromptForOutput("image", "video scene", "", { subject: "video subject" }), false);
  assert.equal(hasPromptForOutput("image", "", "image scene", {}), true);
});

test("video generation requires the editable prompt and ignores legacy structured parts", () => {
  assert.equal(promptForOutput("video", "  moving camera  ", "image scene"), "moving camera");
  assert.equal(hasPromptForOutput("video", "", "image scene", { subject: "robot" }), false);
  assert.equal(hasPromptForOutput("video", "", "image scene", { subject: "   " }), false);
});

test("every H3 video prompt is submitted read-only and only resolves asset tags", () => {
  assert.deepEqual(promptModePayload("video", "A short ordinary prompt."), { prompt_mode: "preserve_tags_only" });
  assert.deepEqual(promptModePayload("video", H3_REFERENCE_PROMPT_TEMPLATE), { prompt_mode: "preserve_tags_only" });
});

test("image prompts keep their existing compilation mode", () => {
  assert.deepEqual(promptModePayload("image", "subject_definitions: literal poster text"), {});
});

test("the read-only reference template explains all official Ref2VA sections", () => {
  for (const section of ["subject_definitions:", "summary:", "retention_analysis:", "detailed_description:", "overall_soundscape:", "non_diegetic_music:"]) {
    assert.match(H3_REFERENCE_PROMPT_TEMPLATE, new RegExp(section));
  }
});
