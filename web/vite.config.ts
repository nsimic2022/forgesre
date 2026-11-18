/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Core serves the build at /app. In `npm run dev`, API and sign-in calls go to a running
// Core (FORGESRE_DEV_CORE, default http://127.0.0.1:8080) so the session cookie works.
const core = process.env.FORGESRE_DEV_CORE ?? "http://127.0.0.1:8080";

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: {
    proxy: {
      "/api": core,
      "/login": core,
      "/logout": core,
    },
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
  test: { environment: "jsdom", globals: false },
});
