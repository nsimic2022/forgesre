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

(function bindClock() {
  const clocks = document.querySelectorAll("[data-clock]");
  if (!clocks.length) return;
  const tick = () => {
    let text;
    try {
      text = new Date().toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      });
    } catch (err) {
      text = new Date().toTimeString().slice(0, 8);
    }
    clocks.forEach((el) => {
      el.textContent = text;
    });
  };
  tick();
  window.setInterval(tick, 1000);
})();

(function bindApplianceResources() {
  const box = document.querySelector("[data-appliance-resources]");
  if (!box) return;
  const row = (key) => box.querySelector('[data-metric="' + key + '"]');
  const gib = (n) => {
    const v = Number(n) / 1073741824;
    if (!isFinite(v) || v < 0) return "—";
    return (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + " GB";
  };
  const pct = (n) => (n != null && isFinite(Number(n)) ? Math.round(Number(n)) + "%" : "");
  const set = (key, level, text) => {
    const el = row(key);
    if (!el) return;
    el.setAttribute("data-level", level || "crit");
    const value = el.querySelector("[data-metric-value]");
    if (value) value.textContent = text;
  };
  const blocked = () => {
    ["cpu", "ram", "hdd", "net"].forEach((key) => set(key, "crit", "no reading"));
  };
  const paint = (data) => {
    if (!data) {
      blocked();
      return;
    }
    const levels = data.levels || {};
    set("cpu", levels.cpu, data.cpu_percent != null ? pct(data.cpu_percent) : "no reading");
    set(
      "ram",
      levels.ram,
      data.ram_total_bytes
        ? gib(data.ram_used_bytes) + " / " + gib(data.ram_total_bytes) + " · " + pct(data.ram_percent)
        : "no reading"
    );
    set(
      "hdd",
      levels.hdd,
      data.hdd_total_bytes
        ? gib(data.hdd_used_bytes) + " / " + gib(data.hdd_total_bytes) + " · " + pct(data.hdd_percent)
        : "no reading"
    );
    const net = data.net || {};
    set("net", levels.net, net.reading || "no reading");
  };
  const load = () => {
    fetch("/api/v1/system/resources", { headers: { Accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : null))
      .then(paint)
      .catch(blocked);
  };
  load();
  window.setInterval(load, 8000);
})();

(function bindPagerSize() {
  document.querySelectorAll("[data-pager-size]").forEach((select) => {
    select.addEventListener("change", () => {
      if (select.form) select.form.submit();
    });
  });
})();

(function bindRowSelect() {
  const scopeOf = (el) => el.closest("[data-select-scope]") || el.closest("table");
  const rowsIn = (scope) => (scope ? scope.querySelectorAll("[data-select-row]") : []);
  const sync = (scope) => {
    if (!scope) return;
    const head = scope.querySelector("[data-select-page]");
    if (!head) return;
    const rows = Array.from(rowsIn(scope));
    const on = rows.filter((box) => box.checked).length;
    head.checked = rows.length > 0 && on === rows.length;
    head.indeterminate = on > 0 && on < rows.length;
  };
  document.addEventListener("change", (event) => {
    const el = event.target;
    if (!(el instanceof HTMLInputElement)) return;
    if (el.hasAttribute("data-select-page")) {
      rowsIn(scopeOf(el)).forEach((box) => {
        box.checked = el.checked;
      });
      el.indeterminate = false;
    } else if (el.hasAttribute("data-select-row")) {
      sync(scopeOf(el));
    }
  });
})();

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
    const alertField = form.querySelector("[data-alertname-input]");
    if (alertField) alertField.dispatchEvent(new Event("input"));
  });
}

const alertRulesData = document.getElementById("alert-rules-data");
const alertNameInput = document.querySelector("[data-alertname-input]");
const rulePreviewBody = document.querySelector("[data-rule-preview-body]");
if (alertRulesData && alertNameInput && rulePreviewBody) {
  let table = {};
  try {
    table = JSON.parse(alertRulesData.textContent || "{}");
  } catch (err) {
    table = {};
  }
  const lookup = (name) => {
    const wanted = (name || "").trim().toLowerCase();
    for (const key of Object.keys(table)) {
      if (key.toLowerCase() === wanted) return table[key];
    }
    return [];
  };
  alertNameInput.addEventListener("input", () => {
    const name = alertNameInput.value.trim();
    const rules = lookup(name);
    rulePreviewBody.replaceChildren();
    if (!rules.length) {
      const span = document.createElement("span");
      span.className = "muted";
      span.textContent = name ? "none in alerts.yml for this alertname" : "pick an alertname";
      rulePreviewBody.appendChild(span);
      return;
    }
    rules.forEach((rule, index) => {
      if (index) rulePreviewBody.appendChild(document.createElement("br"));
      const code = document.createElement("code");
      code.textContent = rule.expr;
      rulePreviewBody.appendChild(code);
      if (rule.for) {
        const span = document.createElement("span");
        span.className = "muted";
        span.textContent = ` for ${rule.for}`;
        rulePreviewBody.appendChild(span);
      }
    });
  });
}

document.querySelectorAll("[data-asset-id]").forEach((field) => {
  field.addEventListener("input", () => {
    const start = field.selectionStart;
    const end = field.selectionEnd;
    field.value = (field.value || "").toLowerCase();
    if (start != null && end != null) {
      field.setSelectionRange(start, end);
    }
  });
});

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

(function bindClientPlayrules() {
  const box = document.querySelector("[data-client-playrules]");
  if (!box) return;
  const select = box.querySelector("[data-playrule-add]");
  const list = box.querySelector("[data-playrule-list]");
  const empty = box.querySelector("[data-playrule-empty]");
  if (!select || !list) return;
  const unsaved = box.querySelector("[data-playrule-unsaved]");
  const sync = () => {
    if (empty) empty.hidden = list.children.length > 0;
    if (unsaved) unsaved.hidden = false;
  };
  const optionFor = (id) => select.querySelector('option[value="' + id + '"]');
  list.addEventListener("click", (event) => {
    const button = event.target.closest("[data-playrule-remove]");
    if (!button) return;
    const row = button.closest("[data-playrule-id]");
    if (!row) return;
    const option = optionFor(row.getAttribute("data-playrule-id"));
    if (option) option.disabled = false;
    row.remove();
    sync();
  });
  select.addEventListener("change", () => {
    const id = select.value;
    if (!id) return;
    const option = optionFor(id);
    select.value = "";
    if (!option || list.querySelector('[data-playrule-id="' + id + '"]')) return;
    const row = document.createElement("li");
    row.className = "playrule-chip";
    row.setAttribute("data-playrule-id", id);
    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = "playrule_ids";
    hidden.value = id;
    const text = document.createElement("span");
    text.className = "playrule-chip-text";
    text.textContent = (option.getAttribute("data-name") || option.textContent) + " ";
    const meta = document.createElement("span");
    meta.className = "muted";
    meta.textContent = "· " + (option.getAttribute("data-meta") || "");
    text.appendChild(meta);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "playrule-remove";
    remove.setAttribute("data-playrule-remove", "");
    remove.setAttribute("aria-label", "Remove " + (option.getAttribute("data-name") || ""));
    remove.title = "Remove";
    remove.textContent = "×";
    row.append(hidden, text, remove);
    list.appendChild(row);
    option.disabled = true;
    sync();
  });
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

(function bindInfoTips() {
  const DELAY_MS = 400;
  const GAP = 8;
  let timer = 0;
  let active = null;
  const bubble = document.createElement("div");
  bubble.className = "info-tip-bubble";
  bubble.setAttribute("role", "tooltip");
  bubble.hidden = true;
  document.body.appendChild(bubble);

  const tipText = (el) =>
    String(el.getAttribute("data-tip") || el.getAttribute("aria-label") || el.getAttribute("title") || "")
      .replace(/\s+/g, " ")
      .trim();

  const restoreTitle = (el) => {
    if (!el) return;
    const saved = el.getAttribute("data-native-title");
    if (saved != null) {
      el.setAttribute("title", saved);
      el.removeAttribute("data-native-title");
    }
  };

  const hide = () => {
    if (timer) {
      window.clearTimeout(timer);
      timer = 0;
    }
    bubble.hidden = true;
    bubble.textContent = "";
    if (active) {
      active.classList.remove("is-open");
      restoreTitle(active);
      active = null;
    }
  };

  const place = (el) => {
    const text = tipText(el);
    if (!text) return;
    bubble.textContent = text;
    bubble.hidden = false;
    bubble.style.left = "0px";
    bubble.style.top = "0px";
    const rect = el.getBoundingClientRect();
    const size = bubble.getBoundingClientRect();
    let left = rect.left;
    let top = rect.bottom + GAP;
    if (left + size.width > window.innerWidth - GAP) {
      left = window.innerWidth - size.width - GAP;
    }
    if (left < GAP) left = GAP;
    if (top + size.height > window.innerHeight - GAP) {
      top = rect.top - size.height - GAP;
    }
    if (top < GAP) top = GAP;
    bubble.style.left = Math.round(left) + "px";
    bubble.style.top = Math.round(top) + "px";
  };

  const show = (el) => {
    if (active && active !== el) {
      active.classList.remove("is-open");
      restoreTitle(active);
    }
    active = el;
    el.classList.add("is-open");
    if (el.hasAttribute("title") && !el.hasAttribute("data-native-title")) {
      el.setAttribute("data-native-title", el.getAttribute("title") || "");
      el.removeAttribute("title");
    }
    place(el);
  };

  document.addEventListener("pointerover", (event) => {
    const el = event.target.closest(".info-tip");
    if (!el) return;
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(() => show(el), DELAY_MS);
  });
  document.addEventListener("pointerout", (event) => {
    const el = event.target.closest(".info-tip");
    if (!el) return;
    if (el.contains(event.relatedTarget)) return;
    hide();
  });
  document.addEventListener("focusin", (event) => {
    const el = event.target.closest(".info-tip");
    if (el) show(el);
  });
  document.addEventListener("focusout", (event) => {
    const el = event.target.closest(".info-tip");
    if (!el) return;
    if (el.contains(event.relatedTarget)) return;
    hide();
  });
  document.addEventListener("click", (event) => {
    const el = event.target.closest(".info-tip");
    if (!el) return;
    event.preventDefault();
    event.stopPropagation();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hide();
  });
  window.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
})();
