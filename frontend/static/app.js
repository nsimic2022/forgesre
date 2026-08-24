const THEMES = ["light", "dark", "system"];
const THEME_LABELS = { light: "Light", dark: "Dark", system: "System" };
const THEME_KEY = "forgesre-theme";

function normalizeTheme(theme) {
  if (theme === "high-contrast") return "dark";
  return THEMES.includes(theme) ? theme : "light";
}

function currentTheme() {
  return normalizeTheme(document.documentElement.getAttribute("data-theme"));
}

function applyTheme(theme) {
  const next = normalizeTheme(theme);
  document.documentElement.setAttribute("data-theme", next);
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (err) {
    /* private mode */
  }
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.setAttribute("aria-label", "Theme: " + THEME_LABELS[next]);
  });
}

document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
  button.addEventListener("click", () => {
    const index = THEMES.indexOf(currentTheme());
    applyTheme(THEMES[(index + 1) % THEMES.length]);
  });
});
applyTheme(currentTheme());

(function bindDemoPanel() {
  const dialog = document.getElementById("demo-panel");
  if (!dialog || typeof dialog.showModal !== "function") return;
  document.querySelectorAll("[data-demo-open]").forEach((button) => {
    button.addEventListener("click", () => dialog.showModal());
  });
  document.querySelectorAll("[data-demo-close]").forEach((button) => {
    button.addEventListener("click", () => dialog.close());
  });
  dialog.addEventListener("click", (event) => {
    const rect = dialog.getBoundingClientRect();
    const inside =
      event.clientX >= rect.left &&
      event.clientX <= rect.right &&
      event.clientY >= rect.top &&
      event.clientY <= rect.bottom;
    if (!inside) dialog.close();
  });
  try {
    if (new URLSearchParams(window.location.search).get("demo") === "1") {
      dialog.showModal();
    }
  } catch (err) {
    /* ignore */
  }
})();

document.querySelectorAll(".node").forEach((button) => {
  button.addEventListener("click", () => {
    const box = document.getElementById("node-detail");
    if (box) box.textContent = button.dataset.detail || "";
  });
});

const preset = document.getElementById("playrule-preset");
if (preset) {
  preset.addEventListener("change", () => {
    const opt = preset.selectedOptions[0];
    if (!opt || !opt.dataset.metric) return;
    const form = document.getElementById("playrule-form");
    const set = (name, value) => {
      const field = form.querySelector(`[name="${name}"]`);
      if (field) field.value = value;
    };
    set("name", opt.dataset.name || "");
    set("alertname", opt.dataset.alertname || "");
    set("metric", opt.dataset.metric || "");
    set("operator", opt.dataset.operator || ">");
    set("value", opt.dataset.value || "80");
    set("severity", opt.dataset.severity || "warning");
    const book = form.querySelector("[name=playbook_id]");
    if (book && opt.dataset.playbook) {
      for (const option of book.options) {
        if (option.dataset.slug === opt.dataset.playbook) {
          book.value = option.value;
          break;
        }
      }
    }
  });
}

(function bindExporterDetect() {
  const ip = document.querySelector("[data-detect-ip]");
  const type = document.querySelector("[data-detect-type]");
  const out = document.querySelector("[data-detect-out]");
  if (!ip) return;
  const run = () => {
    const value = (ip.value || "").trim();
    if (!value) return;
    const current = String((type && type.value) || "");
    const hint = current && !current.toLowerCase().startsWith("auto")
      ? "&hint_type=" + encodeURIComponent(current)
      : "";
    fetch("/api/v1/detect-exporter?ip=" + encodeURIComponent(value) + hint, { headers: { Accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        if (!data) return;
        if (out) out.textContent = data.message || "";
        if (type && current.toLowerCase().startsWith("auto") && data.asset_type) {
          type.value = data.asset_type;
        }
      })
      .catch(() => {});
  };
  ip.addEventListener("blur", run);
  ip.addEventListener("change", run);
})();

(function bindAssetReachability() {
  const table = document.querySelector("[data-asset-reachability]");
  if (!table) return;
  const paint = (row) => {
    const box = table.querySelector('[data-asset-reach="' + row.asset_id + '"]');
    if (!box) return;
    const ping = box.querySelector(".ping");
    if (ping) {
      ping.className = "reach-dot ping " + (row.ping || "yellow");
      ping.title = "Ping: " + (row.ping_detail || "");
    }
    const exporter = box.querySelector(".exporter");
    if (exporter) {
      exporter.className = "reach-dot exporter " + (row.exporter || "yellow");
      exporter.textContent = row.exporter_label || "exp.";
      exporter.title = (row.exporter_label || "exporter") + ": " + (row.exporter_detail || "");
    }
  };
  fetch("/api/v1/assets/reachability", { headers: { Accept: "application/json" } })
    .then((response) => (response.ok ? response.json() : null))
    .then((rows) => {
      if (!Array.isArray(rows)) return;
      rows.forEach(paint);
    })
    .catch(() => {});
})();
