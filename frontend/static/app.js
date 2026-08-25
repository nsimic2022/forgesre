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
        if (out) {
          const fam = data.families || {};
          const bits = [];
          if (fam.cpu) bits.push("CPU");
          if (fam.memory) bits.push("Memory");
          if (fam.disk) bits.push("Disk");
          if (fam.up) bits.push("up");
          const familyLine = bits.length
            ? " Bundled families on /metrics: " + bits.join(", ") + "."
            : " No bundled cpu/mem/disk series on /metrics yet.";
          out.textContent = (data.message || "") + familyLine;
        }
        if (type && current.toLowerCase().startsWith("auto") && data.asset_type) {
          type.value = data.asset_type;
        }
        const fam = data.families || {};
        const mark = (sel, on) => {
          const box = document.querySelector(sel);
          if (box) box.checked = !!on;
        };
        if (Object.keys(fam).length) {
          mark("[data-alarm-up]", fam.up !== false);
          mark("[data-alarm-cpu]", !!fam.cpu);
          mark("[data-alarm-memory]", !!fam.memory);
          mark("[data-alarm-disk]", !!fam.disk);
        }
      })
      .catch(() => {});
  };
  ip.addEventListener("blur", run);
  ip.addEventListener("change", run);
})();

(function bindAssetReachability() {
  const boxes = document.querySelectorAll("[data-asset-reach]");
  if (!boxes.length) return;
  const paint = (row) => {
    document.querySelectorAll('[data-asset-reach="' + row.asset_id + '"]').forEach((box) => {
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
    });
  };
  fetch("/api/v1/assets/reachability", { headers: { Accept: "application/json" } })
    .then((response) => (response.ok ? response.json() : null))
    .then((rows) => {
      if (!Array.isArray(rows)) return;
      rows.forEach(paint);
    })
    .catch(() => {});
})();

(function bindHostDownBanner() {
  const box = document.querySelector("[data-host-down-banner]");
  if (!box) return;
  const countEl = box.querySelector("[data-host-down-count]");
  const listEl = box.querySelector("[data-host-down-list]");
  const paint = (rows) => {
    if (!Array.isArray(rows) || !rows.length) {
      box.hidden = true;
      if (listEl) listEl.replaceChildren();
      return;
    }
    box.hidden = false;
    const n = rows.length;
    if (countEl) {
      countEl.textContent =
        n + " open incident" + (n === 1 ? "" : "s") + " for unreachable host / SNMP down.";
    }
    if (!listEl) return;
    listEl.replaceChildren();
    rows.forEach((row) => {
      const li = document.createElement("li");
      const link = document.createElement("a");
      link.href = "/incidents/" + encodeURIComponent(row.number || "");
      link.textContent = row.number || "";
      li.appendChild(link);
      if (row.demo) {
        const demo = document.createElement("span");
        demo.className = "pill demo";
        demo.textContent = "DEMO";
        li.appendChild(document.createTextNode(" "));
        li.appendChild(demo);
      }
      const bits = [];
      if (row.title) bits.push(row.title);
      if (row.hostname) bits.push(row.hostname);
      if (bits.length) li.appendChild(document.createTextNode(" " + bits.join(" · ")));
      listEl.appendChild(li);
    });
  };
  const load = () => {
    fetch("/api/v1/incidents/down", { headers: { Accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : null))
      .then((rows) => {
        if (Array.isArray(rows)) paint(rows);
      })
      .catch(() => {});
  };
  load();
  setInterval(load, 20000);
})();

(function bindDiscoveryScan() {
  const form = document.querySelector("[data-discovery-scan]");
  const panel = document.querySelector("[data-scan-panel]");
  if (!form || !panel) return;
  const stepsBox = panel.querySelector("[data-scan-steps]");
  const summary = panel.querySelector("[data-scan-summary]");
  const detail = panel.querySelector("[data-scan-detail]");
  const paint = (scan) => {
    if (!scan) return;
    panel.setAttribute("data-scan-status", scan.status || "idle");
    const cidrs = Array.isArray(scan.cidrs) ? scan.cidrs.join(", ") : "";
    if (summary) {
      if (scan.status === "idle") {
        summary.textContent = "No Scan now in this Core process yet. Click Scan now — pills update while it runs.";
      } else if (scan.status === "running") {
        summary.textContent =
          "Scanning " + (scan.probed || 0) + "/" + (scan.total || 0) +
          (scan.current_ip ? " · " + scan.current_ip : "") +
          " · found " + (scan.found || 0) +
          " · skipped " + (scan.skipped || 0);
      } else {
        summary.textContent =
          (scan.status || "") +
          " · probed " + (scan.probed || 0) + "/" + (scan.total || 0) +
          " · found " + (scan.found || 0) + " (waiting for Approve)" +
          " · skipped " + (scan.skipped || 0) +
          (scan.lab_skipped ? " (lab " + scan.lab_skipped + ")" : "") +
          (cidrs ? " · CIDRs " + cidrs : "");
      }
    }
    if (stepsBox && Array.isArray(scan.steps)) {
      stepsBox.replaceChildren();
      scan.steps.forEach((item) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "reach-dot scan-step " + (item.color || "yellow");
        btn.setAttribute("data-scan-step", item.id || "");
        btn.title = item.detail || "";
        btn.textContent = item.label || item.id || "";
        btn.addEventListener("click", () => {
          if (detail) detail.textContent = (item.label || "") + ": " + (item.detail || "");
        });
        stepsBox.appendChild(btn);
      });
    }
  };
  if (stepsBox) {
    stepsBox.querySelectorAll("[data-scan-step]").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (detail) detail.textContent = (btn.textContent || "") + ": " + (btn.title || "");
      });
    });
  }
  let timer = 0;
  let sawRunning = panel.getAttribute("data-scan-status") === "running";
  const poll = () => {
    fetch("/api/v1/discovery/scan", { headers: { Accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : null))
      .then((scan) => {
        if (!scan) return;
        paint(scan);
        if (scan.status === "running") {
          sawRunning = true;
          return;
        }
        if (timer) window.clearInterval(timer);
        timer = 0;
        if (sawRunning && (scan.status === "done" || scan.status === "error")) {
          window.location.reload();
        }
      })
      .catch(() => {});
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    sawRunning = true;
    const button = form.querySelector("button");
    if (button) button.disabled = true;
    fetch("/api/v1/discovery/scan?background=true", {
      method: "POST",
      headers: { Accept: "application/json" },
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((scan) => {
        paint(scan);
        if (timer) window.clearInterval(timer);
        timer = window.setInterval(poll, 400);
        poll();
      })
      .catch(() => {
        if (button) button.disabled = false;
        form.submit();
      });
  });
  if (panel.getAttribute("data-scan-status") === "running") {
    timer = window.setInterval(poll, 400);
    poll();
  }
})();
