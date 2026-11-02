import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 后端地址可覆盖：本机 8000 被别的服务占用时（实测：OrbStack 转发了一个容器到 8000），
// 用 LONGSPAN_BACKEND_URL=http://127.0.0.1:8010 bash start.sh 起，前端代理跟着走。
const BACKEND = process.env.LONGSPAN_BACKEND_URL || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      // 开发期把 /api 代理到后端，避免 CORS 与 SSE 跨域问题
      '/api': {
        target: BACKEND,
        changeOrigin: true,
      },
    },
  },
})
