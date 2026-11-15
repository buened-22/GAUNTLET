import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to the FastAPI backend (python -m gauntlet serve).
//
// Security (audit finding F-01): any web page open in the operator's browser can send requests to this
// server, so it is configured not to help a page on another site read anything:
//   * host "localhost": loopback only, never a network interface;
//   * cors false: no Access-Control-Allow-Origin header on any response, including proxied API
//     responses. The dashboard is served from this origin and needs no CORS;
//   * fs.strict: only files inside the dashboard project are served (vite also refuses requests whose
//     Host is not a local name, which stops DNS rebinding);
//   * changeOrigin false: the API sees the browser's real Host header, so its own Host and Origin checks
//     (backend/gauntlet/api/local_guard.py) still apply to proxied requests.
// The same applies to `vite preview`.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "localhost",
    port: 5173,
    strictPort: true,
    cors: false,
    fs: { strict: true },
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
  preview: {
    host: "localhost",
    cors: false,
  },
});
