import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The backend FastAPI runs separately (uvicorn). Proxy /api to it in dev so the
// browser can call the control plane without CORS friction.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
