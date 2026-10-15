
# Z-Torque Console

阶段 10 轻量控制台（React 18 + TypeScript + Vite + React Router）。

## 本地启动

前置：后端先启动在 `http://localhost:8000`（默认）。

在本目录执行：

```bash
npm install
npm run dev
```

浏览器访问：`http://localhost:5173`

## API 约定

- 所有 API 统一走后端前缀：`/api/z-torque/*`
- 开发环境通过 Vite 代理转发到后端（见 `vite.config.ts`）
- 认证通过 HttpOnly Cookie；前端不持久化密码/密钥/Connector 凭证

