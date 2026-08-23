import type { Metadata } from "next";
import Studio from "./studio";

export const metadata: Metadata = {
  title: { absolute: "H3 Studio · AI 视频工作台" },
  description: "用节点连接素材、提示词与 MiniMax H3 工作流。",
};

export default function Home() {
  return <Studio />;
}
