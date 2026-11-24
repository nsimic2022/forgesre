import { paintSevCells } from "./lib/sevcells";
import "@fontsource/inter/400.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/jetbrains-mono/400.css";
import "./styles.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { savedTheme, showTheme } from "./lib/theme";

showTheme(savedTheme());
paintSevCells(); // paint only: a browser that never chose keeps "no choice" (the TV uses Navy then)

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter basename="/app">
      <App />
    </BrowserRouter>
  </StrictMode>,
);

// Installable on a phone (on-call) and a clear offline page; the worker never caches API data.
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => void navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" }).catch(() => undefined));
}
