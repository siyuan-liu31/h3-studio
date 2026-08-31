import type { Metadata } from "next";
import Studio from "./studio";

export const metadata: Metadata = {
  title: { absolute: "MiniMax H3 Video Studio · AI 视频工作台" },
  description: "用可视化节点编排 MiniMax H3 与 ComfyUI，支持文生视频、图生视频、多模态参考、长视频分镜和 Agent 自动化。",
};

export default function Home() {
  return <Studio />;
}
