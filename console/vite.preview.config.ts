// 本地交互预览专用构建配置（不参与 npm run build）。
// 用途：把 preview/index.html + preview/main.tsx 打成静态包，用真实浏览器检查
// 「执行链路折叠行」的展开/收起与步骤实时追加，避免只靠类型检查判断交互。
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: `${root}.preview-dist`,
    emptyOutDir: true,
    rollupOptions: { input: `${root}preview/index.html` },
  },
});
