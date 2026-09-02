import type { Metadata } from "next";
import Studio from "./studio";

export const metadata: Metadata = {
  title: { absolute: "MiniMax H3 Video Studio · Agent-ready AI Video Workspace" },
  description: "A visual MiniMax H3 and ComfyUI workspace for text-to-video, image-to-video, multimodal references, storyboards, long-form video, and Agent automation.",
};

export default function Home() {
  return <Studio />;
}
