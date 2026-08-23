export type PromptOutput = "video" | "image";

export const H3_REFERENCE_PROMPT_TEMPLATE = `subject_definitions:
<Subject 1> is the target subject whose appearance comes from <Picture 1> and whose motion comes from <Video 1>.
<Video 1> is the source video for the target video edit.

summary:
[video editing + reference generation] Describe the intended edit and the relationships that must remain unchanged.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - preserve the identity and appearance from <Picture 1>.
<Video 1> (source video editing): fully_preserved - preserve the required action, timing, framing, and camera structure.

detailed_description:
[Shot 1] Describe the visible action in playback order. Keep every reference label consistent with its definition above.

overall_soundscape:
Describe dialogue, ambience, and physical sounds, or write N/A.

non_diegetic_music:
N/A`;

export function promptModePayload(output: PromptOutput, prompt: string): { prompt_mode?: "preserve_tags_only" } {
  void prompt;
  return output === "video" ? { prompt_mode: "preserve_tags_only" } : {};
}

export function promptForOutput(output: PromptOutput, videoPrompt: string, imagePrompt: string): string {
  return (output === "image" ? imagePrompt : videoPrompt).trim();
}

export function hasPromptForOutput(
  output: PromptOutput,
  videoPrompt: string,
  imagePrompt: string,
  videoParts: Record<string, string>,
): boolean {
  void videoParts;
  return Boolean(promptForOutput(output, videoPrompt, imagePrompt));
}
