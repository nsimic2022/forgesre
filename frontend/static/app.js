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

(function bindReportSchedule() {
  const form = document.getElementById("report-schedule-form");
  if (!form) return;
  const pick = form.querySelector("[data-schedule]");
  const custom = form.querySelector("[data-custom-interval]");
  const days = form.querySelector("[data-weekdays]");
  const sync = () => {
    const value = pick ? pick.value : "";
    if (custom) custom.hidden = value !== "custom";
    if (days) days.hidden = value === "once";
  };
  if (pick) pick.addEventListener("change", sync);
  sync();
})();

(function bindFlashDismiss() {
  const keyName = "forgesre-flash-dismiss";
  let saved = [];
  try {
    saved = JSON.parse(sessionStorage.getItem(keyName) || "[]");
  } catch (err) {
    saved = [];
  }
  if (!Array.isArray(saved)) saved = [];
  const seen = new Set(saved.map(String));
  const keyOf = (el) => {
    const text = (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 400);
    let hash = 5381;
    for (let i = 0; i < text.length; i++) hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
    return String(hash);
  };
  const remember = (id) => {
    if (seen.has(id)) return;
    seen.add(id);
    try {
      sessionStorage.setItem(keyName, JSON.stringify(Array.from(seen)));
    } catch (err) {
      /* private mode */
    }
  };
  document.querySelectorAll(".banner").forEach((el) => {
    const id = keyOf(el);
    if (seen.has(id)) {
      el.hidden = true;
      return;
    }
    if (el.querySelector("[data-flash-dismiss]")) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "banner-x";
    button.setAttribute("data-flash-dismiss", "");
    button.setAttribute("aria-label", "Dismiss");
    button.textContent = "\u00d7";
    button.addEventListener("click", () => {
      el.hidden = true;
      remember(id);
    });
    el.appendChild(button);
  });
})();

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

// Pager tabs and Rows N reload the page with new ?page= / ?per_page=. Remember where the window was and put it
// back after the load, so changing page never jumps to the top (or to the list's #fragment, kept for no-JS).
(function bindPagerScroll() {
  const KEY = "forgesre-pager-scroll";
  const MAX_AGE_MS = 15000;
  const target = (url) => url.pathname + url.search;

  const remember = (url) => {
    try {
      sessionStorage.setItem(KEY, JSON.stringify({ to: target(url), y: window.scrollY, at: Date.now() }));
    } catch (err) {
      /* storage off: plain navigation */
    }
  };

  const go = (url) => {
    url.hash = "";
    remember(url);
    window.location.assign(url.href);
  };

  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(KEY) || "null");
    sessionStorage.removeItem(KEY);
  } catch (err) {
    saved = null;
  }
  if (saved && saved.to === target(window.location) && Date.now() - Number(saved.at || 0) < MAX_AGE_MS) {
    const y = Math.max(0, Number(saved.y) || 0);
    const put = () => {
      const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
      window.scrollTo(0, Math.min(y, max));
    };
    put();
    if (document.readyState !== "complete") window.addEventListener("load", put, { once: true });
  }

  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest(".pager-bar .pager a[href]");
    if (!link) return;
    event.preventDefault();
    go(new URL(link.href, window.location.href));
  });

  const submitSize = (form) => {
    const url = new URL(form.getAttribute("action") || window.location.pathname, window.location.href);
    const params = new URLSearchParams();
    Array.from(form.elements).forEach((field) => {
      if (field.name && !field.disabled) params.append(field.name, field.value);
    });
    url.search = params.toString();
    go(url);
  };
  document.querySelectorAll("form[data-pager-size-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      submitSize(form);
    });
    const select = form.querySelector("[data-pager-size]");
    if (select) select.addEventListener("change", () => submitSize(form));
  });
})();

(function bindRowSelect() {
  const scopeOf = (el) => el.closest("[data-select-scope]") || el.closest("table");
  const rowsIn = (scope) => (scope ? scope.querySelectorAll("[data-select-row]") : []);
  const flashSelectOne = (form) => {
    const scope = form.closest("[data-list-actions]") || form.parentElement;
    if (!scope) return;
    let note = scope.querySelector("[data-select-one]");
    if (!note) {
      note = document.createElement("span");
      note.className = "select-one-flash";
      note.setAttribute("data-select-one", "");
      note.setAttribute("role", "status");
      form.insertAdjacentElement("afterend", note);
    }
    note.textContent = "Select one";
  };
  const syncBulk = (scope) => {
    if (!scope) return;
    const bar = scope.querySelector("[data-list-actions]");
    if (!bar) return;
    const on = Array.from(rowsIn(scope)).filter((box) => box.checked);
    bar.classList.toggle("is-live", on.length > 0);
    const edit = bar.querySelector("[data-bulk-edit] button");
    if (edit) {
      const one = on.length === 1;
      edit.classList.toggle("is-muted", !one);
      edit.title = one ? "Edit notes on this incident" : "Select one";
    }
  };
  const sync = (scope) => {
    if (!scope) return;
    const head = scope.querySelector("[data-select-page]");
    if (head) {
      const rows = Array.from(rowsIn(scope));
      const on = rows.filter((box) => box.checked).length;
      head.checked = rows.length > 0 && on === rows.length;
      head.indeterminate = on > 0 && on < rows.length;
    }
    syncBulk(scope);
  };
  document.addEventListener("change", (event) => {
    const el = event.target;
    if (!(el instanceof HTMLInputElement)) return;
    if (el.hasAttribute("data-select-page")) {
      rowsIn(scopeOf(el)).forEach((box) => {
        box.checked = el.checked;
      });
      el.indeterminate = false;
      syncBulk(scopeOf(el));
    } else if (el.hasAttribute("data-select-row")) {
      sync(scopeOf(el));
    }
  });
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.hasAttribute("data-bulk-delete") && !form.hasAttribute("data-bulk-export") && !form.hasAttribute("data-bulk-edit")) return;
    const scope = form.closest("[data-select-scope]");
    if (!scope) return;
    const ids = Array.from(rowsIn(scope))
      .filter((box) => box.checked)
      .map((box) => box.value)
      .filter(Boolean);
    if (form.hasAttribute("data-bulk-edit") && ids.length !== 1) {
      event.preventDefault();
      flashSelectOne(form);
      return;
    }
    if (!ids.length) {
      event.preventDefault();
      return;
    }
    form.querySelectorAll("input[data-bulk-id]").forEach((node) => node.remove());
    ids.forEach((id) => {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "selected";
      input.value = id;
      input.setAttribute("data-bulk-id", "");
      form.appendChild(input);
    });
  });
})();

(function bindAssetPickers() {
  const norm = (value) => (value || "").trim().toLowerCase();
  document.querySelectorAll("[data-asset-picker]").forEach((picker) => {
    const rows = () => Array.from(picker.querySelectorAll("[data-asset-row]"));
    const chips = picker.querySelector("[data-asset-chips]");
    const empty = picker.querySelector("[data-asset-empty]");
    const field = (name) => picker.querySelector("[data-asset-" + name + "]");
    const apply = () => {
      const needle = norm(field("q") && field("q").value);
      const type = norm(field("type") && field("type").value);
      const source = norm(field("source") && field("source").value);
      const site = norm(field("site") && field("site").value);
      const vlan = norm(field("vlan") && field("vlan").value);
      const customer = norm(field("customer") && field("customer").value);
      let shown = 0;
      rows().forEach((row) => {
        const blob = norm(row.getAttribute("data-search"));
        const sources = norm(row.getAttribute("data-sources")).split(/\s+/).filter(Boolean);
        const match =
          (!needle || blob.includes(needle)) &&
          (!type || norm(row.getAttribute("data-type")) === type) &&
          (!source || sources.includes(source)) &&
          (!site || norm(row.getAttribute("data-site")) === site) &&
          (!vlan || norm(row.getAttribute("data-vlan")) === vlan) &&
          (!customer || norm(row.getAttribute("data-customer")) === customer);
        row.hidden = !match;
        if (match) shown += 1;
      });
      if (empty) empty.hidden = shown !== 0 || rows().length === 0;
    };
    const paintChips = () => {
      if (!chips) return;
      chips.replaceChildren();
      rows().forEach((row) => {
        const box = row.querySelector('input[name="asset_id"]');
        if (!box || !box.checked) return;
        const chip = document.createElement("span");
        chip.className = "asset-chip";
        chip.setAttribute("data-asset-chip", box.value);
        const text = document.createElement("span");
        text.textContent = row.getAttribute("data-label") || box.value;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "asset-chip-x";
        remove.setAttribute("aria-label", "Remove " + text.textContent);
        remove.textContent = "×";
        remove.addEventListener("click", () => {
          box.checked = false;
          paintChips();
        });
        chip.append(text, remove);
        chips.append(chip);
      });
    };
    const filterBtn = picker.querySelector("[data-asset-filter]");
    if (filterBtn) filterBtn.addEventListener("click", apply);
    picker.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      const target = event.target;
      if (!(target instanceof HTMLElement) || !target.closest("[data-asset-filters]")) return;
      event.preventDefault();
      apply();
    });
    picker.addEventListener("change", (event) => {
      const target = event.target;
      if (target instanceof HTMLInputElement && target.name === "asset_id") paintChips();
    });
    paintChips();
  });
})();

(function focusIncidentNotes() {
  let focus = window.location.hash === "#notes";
  try {
    focus = focus || new URLSearchParams(window.location.search).get("focus") === "notes";
  } catch (err) {
    /* ignore */
  }
  if (!focus) return;
  const box = document.querySelector("#notes textarea[name=body], #notes [data-note-body]");
  const section = document.getElementById("notes");
  if (section && section.scrollIntoView) section.scrollIntoView({ block: "start" });
  if (box) box.focus();
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
          if (!box) return;
          box.checked = !!on;
          box.dispatchEvent(new Event("change", { bubbles: true }));
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

(function bindAssetFormPicks() {
  const form = document.getElementById("asset-form");
  if (!form) return;
  form.addEventListener("change", (event) => {
    const select = event.target.closest("[data-email-select]");
    if (!select || !form.contains(select)) return;
    const box = select.closest("[data-email-pick]");
    const typed = box && box.querySelector("[data-email-typed]");
    if (!typed) return;
    if (select.value) {
      typed.value = select.value;
      typed.hidden = true;
      const fill = box.getAttribute("data-email-fill-name");
      const name = fill ? form.querySelector('[name="' + fill + '"]') : null;
      const option = select.options[select.selectedIndex];
      const label = option ? option.getAttribute("data-label") || "" : "";
      if (name && !name.value.trim() && label) name.value = label;
    } else {
      typed.value = "";
      typed.hidden = false;
      typed.focus();
    }
  });

  const kind = form.querySelector("[data-support-kind]");
  const customRow = form.querySelector("[data-support-custom-row]");
  const customInput = form.querySelector("[data-support-custom]");
  if (kind && customRow && customInput) {
    const syncSupport = () => {
      const on = kind.value === "custom";
      customRow.hidden = !on;
      customInput.required = on;
    };
    kind.addEventListener("change", syncSupport);
    syncSupport();
  }

  const snmpVersion = form.querySelector("[data-snmp-version]");
  if (snmpVersion) {
    const v12 = form.querySelector("[data-snmp-v12]");
    const v3 = form.querySelector("[data-snmp-v3]");
    const mode = form.querySelector("[data-snmp-community-mode]");
    const communityRow = form.querySelector("[data-snmp-community-row]");
    const level = form.querySelector("[data-snmp-v3-level]");
    const authPair = form.querySelector("[data-snmp-v3-auth]");
    const privPair = form.querySelector("[data-snmp-v3-priv]");
    const syncSnmp = () => {
      const isV3 = snmpVersion.value === "v3";
      if (v12) v12.hidden = isV3;
      if (v3) v3.hidden = !isV3;
      if (communityRow && mode) communityRow.hidden = mode.value !== "custom";
      const lvl = level ? level.value : "noAuthNoPriv";
      if (authPair) authPair.hidden = lvl === "noAuthNoPriv";
      if (privPair) privPair.hidden = lvl !== "authPriv";
    };
    [snmpVersion, mode, level].forEach((el) => el && el.addEventListener("change", syncSnmp));
    syncSnmp();
  }

  const type = form.querySelector("[data-detect-type]");
  const ip = form.querySelector("[data-detect-ip]");
  const scrape = form.querySelector("[data-scrape-address]");
  const snmp = form.querySelector("[data-snmp-port]");
  if (!type) return;
  const lower = () => String(type.value || "").toLowerCase();
  const exporterPort = () => {
    const t = lower();
    if (t.includes("windows")) return "9182";
    if (t.includes("linux")) return "9100";
    return "";
  };
  const snmpFamily = () => {
    const t = lower();
    if (t.startsWith("auto") || t.includes("windows") || t.includes("linux") || t.includes("web")) return false;
    return /network|switch|router|firewall|storage|qnap|printer|\bnas\b|\bsan\b/.test(t);
  };
  type.addEventListener("change", () => {
    const host = ip ? (ip.value || "").trim() : "";
    if (scrape && host && (scrape.value === host + ":9100" || scrape.value === host + ":9182")) {
      const port = exporterPort();
      scrape.value = port ? host + ":" + port : "";
    }
    if (snmp) {
      const def = snmp.getAttribute("data-snmp-default") || "161";
      snmp.placeholder = snmpFamily() ? def + " (default)" : "off";
    }
  });
})();

(function bindAssetLadder() {
  const column = document.querySelector("[data-asset-ladder]");
  if (!column) return;
  const clearRow = (row) => {
    const select = row.querySelector("[data-email-select]");
    const typed = row.querySelector("[data-email-typed]");
    if (select) select.value = "";
    if (typed) {
      typed.value = "";
      typed.hidden = false;
    }
  };
  column.addEventListener("click", (event) => {
    const add = event.target.closest("[data-ladder-add-email]");
    if (add) {
      const level = add.closest("[data-ladder-level]");
      const list = level && level.querySelector("[data-ladder-emails]");
      const sample = list && list.querySelector("[data-ladder-email]");
      if (!list || !sample) return;
      const row = sample.cloneNode(true);
      clearRow(row);
      const remove = row.querySelector("[data-ladder-remove-email]");
      if (remove) remove.hidden = false;
      list.appendChild(row);
      list.querySelectorAll("[data-ladder-remove-email]").forEach((button) => {
        button.hidden = false;
      });
      return;
    }
    const remove = event.target.closest("[data-ladder-remove-email]");
    if (!remove) return;
    const row = remove.closest("[data-ladder-email]");
    const list = row && row.parentElement;
    if (!row || !list) return;
    const rows = list.querySelectorAll("[data-ladder-email]");
    if (rows.length <= 1) {
      clearRow(row);
      remove.hidden = true;
      return;
    }
    row.remove();
    const left = list.querySelectorAll("[data-ladder-email]");
    if (left.length === 1) {
      const only = left[0].querySelector("[data-ladder-remove-email]");
      if (only) only.hidden = true;
    }
  });
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

// Standard-alarm enable is an ON/OFF button. The checkbox stays in the form so an OFF
// still means "mute this host" — it does not change the Prometheus rule.
(function bindAlarmOnOff() {
  const sync = (box) => {
    const row = box.closest(".alarm-row");
    const button = row && row.querySelector("[data-onoff]");
    if (!button) return;
    const on = !!box.checked;
    button.classList.toggle("is-on", on);
    button.classList.toggle("is-off", !on);
    button.textContent = on ? "ON" : "OFF";
    button.setAttribute("aria-pressed", on ? "true" : "false");
  };
  document.querySelectorAll(".onoff-check").forEach(sync);
  document.addEventListener("change", (event) => {
    const el = event.target;
    if (el instanceof HTMLInputElement && el.classList.contains("onoff-check")) sync(el);
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-onoff]");
    if (!button || button.tagName !== "BUTTON") return;
    const row = button.closest(".alarm-row");
    const box = row && row.querySelector(".onoff-check");
    if (!box) return;
    event.preventDefault();
    box.checked = !box.checked;
    sync(box);
  });
})();

(function bindAssetReachability() {
  const boxes = document.querySelectorAll("[data-asset-reach]");
  if (!boxes.length) return;
  const digits = (value) => {
    let text = String(value || "").trim();
    text = text.replace(/^snmp\s*/i, "");
    if (text.charAt(0) === ":") text = text.slice(1);
    return /^\d+$/.test(text) ? text : "";
  };
  const paint = (row) => {
    document.querySelectorAll('[data-asset-reach="' + row.asset_id + '"]').forEach((box) => {
      const ping = box.querySelector(".ping");
      if (ping) {
        ping.className = "reach-sq icmp ping " + (row.ping || "yellow");
        ping.textContent = "ICMP";
        ping.title = "ICMP: " + (row.ping_detail || "");
      }
      const exporter = box.querySelector(".exporter");
      if (exporter) {
        const port = row.port_text != null && row.port_text !== "" ? String(row.port_text) : digits(row.exporter_label);
        const show = row.show_port != null ? !!row.show_port : !!port;
        exporter.hidden = !show;
        if (show) {
          exporter.className = "reach-sq port exporter " + (row.exporter || "yellow");
          exporter.textContent = port;
          exporter.title = "Port " + port + " — " + (row.exporter_detail || "");
        }
      }
      const snmpEl = box.querySelector("[data-snmp-label]");
      if (snmpEl) {
        const show = row.show_snmp != null ? !!row.show_snmp : !!(row.snmp_label || row.exporter_label === "SNMP");
        snmpEl.hidden = !show;
        if (show) {
          const primary = row.snmp_primary != null ? !!row.snmp_primary : row.exporter_label === "SNMP";
          const port = row.snmp_port_text != null && row.snmp_port_text !== "" ? String(row.snmp_port_text) : digits(row.snmp_label);
          snmpEl.className = "reach-sq snmp" + (primary ? " " + (row.exporter || "yellow") : "");
          snmpEl.textContent = "SNMP";
          snmpEl.title = port ? "SNMP UDP " + port : "SNMP";
        }
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

// One row picker for every list box. A click on a row (not on its links, buttons or inputs) only selects it;
// Up / Down, j / k, Home / End move the selection and scroll only the box; Enter opens the row's [data-list-open]
// link (or toggles its [data-list-open] summary). Inputs keep their keys, Tab leaves, a mousedown outside the box
// hands keyboard scrolling back to the page. onSelect(row, options) runs when the selection changes.
function listPicker(root, opts) {
  const options = opts || {};
  const box = options.box === undefined ? root : options.box;
  const rowSel = options.rows || "[data-list-row]";
  const rows = Array.from(root.querySelectorAll(rowSel));
  const onSelect = options.onSelect || null;
  let selected = null;
  if (box) box.setAttribute("data-list-bound", "");
  rows.forEach((r, i) => {
    if (!r.hasAttribute("tabindex")) r.tabIndex = i === 0 ? 0 : -1;
  });

  // Scroll only the list box; scrollIntoView would also move the window when the box is partly off-screen.
  const keepInList = (row) => {
    if (!box || typeof row.getBoundingClientRect !== "function") return;
    const table = "tHead" in root ? root : root.querySelector(":scope > table");
    const head = table && table.tHead ? table.tHead.getBoundingClientRect().height : 0;
    const frame = box.getBoundingClientRect();
    const r = row.getBoundingClientRect();
    if (r.top < frame.top + head) box.scrollTop -= frame.top + head - r.top;
    else if (r.bottom > frame.bottom) box.scrollTop += r.bottom - frame.bottom;
  };

  const select = (row, how) => {
    if (!row) return;
    const flags = how || {};
    rows.forEach((r) => {
      r.classList.toggle("is-selected", r === row);
      r.tabIndex = r === row ? 0 : -1;
      if (r === row) {
        r.setAttribute("aria-current", "true");
        r.setAttribute("aria-selected", "true");
      } else {
        r.removeAttribute("aria-current");
        r.setAttribute("aria-selected", "false");
      }
    });
    if (flags.focus && typeof row.focus === "function") row.focus({ preventScroll: true });
    keepInList(row);
    if (selected === row && !flags.force) return;
    selected = row;
    if (onSelect) onSelect(row, flags);
  };

  root.addEventListener("click", (event) => {
    if (event.target.closest("a, input, button, label, select, textarea, summary")) return;
    // Dragging to copy text out of a row is not a pick.
    if (typeof window.getSelection === "function" && String(window.getSelection() || "")) return;
    const row = event.target.closest(rowSel);
    if (row && rows.includes(row)) select(row, { focus: true });
  });

  const STEP = { ArrowDown: 1, j: 1, ArrowUp: -1, k: -1 };
  root.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const target = event.target;
    if (target.closest("input, select, textarea, [contenteditable]")) return;
    const row = target.closest(rowSel);
    const index = rows.indexOf(row);
    if (index < 0) return;
    let next = null;
    if (event.key in STEP) next = rows[Math.max(0, Math.min(rows.length - 1, index + STEP[event.key]))];
    else if (event.key === "Home") next = rows[0];
    else if (event.key === "End") next = rows[rows.length - 1];
    if (next) {
      event.preventDefault();
      select(next, { focus: true, defer: true });
      return;
    }
    if (target !== row) return;
    if (event.key === " ") {
      event.preventDefault();
      select(row);
    } else if (event.key === "Enter") {
      const open = row.querySelector("[data-list-open]");
      if (!open) return;
      event.preventDefault();
      if (open.href) window.location.assign(open.href);
      else if (typeof open.click === "function") open.click();
    }
  });

  document.addEventListener("mousedown", (event) => {
    if (!box || box.contains(event.target)) return;
    const active = document.activeElement;
    if (active && box.contains(active) && typeof active.blur === "function") active.blur();
  });

  return { rows, select };
}

// Host metric charts for one pane (Dashboard Host metrics, incident detail). Same JSON as
// GET /api/v1/assets/{id}/metrics: Prometheus tiles, or Zabbix trend.get tiles (source "zabbix").
function graphPane(pane) {
  const empty = pane.querySelector("[data-dash-graph-empty]");
  const list = pane.querySelector("[data-dash-graph-list]");
  const SVG = "http://www.w3.org/2000/svg";
  const W = 200;
  const H = 48;
  let span = "last hour";

  const showEmpty = (text) => {
    if (list) list.replaceChildren();
    if (!empty) return;
    empty.textContent = text || "";
    empty.hidden = !text;
  };

  const svgEl = (name, attrs) => {
    const el = document.createElementNS(SVG, name);
    Object.keys(attrs).forEach((key) => el.setAttribute(key, String(attrs[key])));
    return el;
  };

  const yFor = (tile, v) => {
    if (tile.kind === "up") return v >= 1 ? 4 : H - 4;
    const clamped = Math.max(0, Math.min(100, v));
    return H - 2 - (clamped / 100) * (H - 4);
  };

  const chart = (tile, values) => {
    const svg = svgEl("svg", {
      class: "dash-chart",
      viewBox: "0 0 " + W + " " + H,
      preserveAspectRatio: "none",
      role: "img",
      "aria-label": tile.name + " " + span,
    });
    if (tile.kind !== "up" && tile.threshold != null && tile.alarm_enabled !== false) {
      const y = yFor(tile, Number(tile.threshold)).toFixed(1);
      svg.appendChild(svgEl("line", { class: "dash-chart-threshold", x1: 0, x2: W, y1: y, y2: y }));
    }
    const last = values.length - 1;
    const pts = [];
    values.forEach((v, i) => {
      const x = (i * W) / last;
      const y = yFor(tile, v);
      if (tile.kind === "up" && pts.length) pts.push(x.toFixed(1) + "," + pts[pts.length - 1].split(",")[1]);
      pts.push(x.toFixed(1) + "," + y.toFixed(1));
    });
    svg.appendChild(svgEl("polyline", { class: "dash-chart-line " + (tile.tone || "warn"), points: pts.join(" ") }));
    return svg;
  };

  const tileRow = (tile, labelEmpty) => {
    const box = document.createElement("div");
    box.className = "dash-graph";
    box.setAttribute("data-graph", tile.key);
    box.setAttribute("data-tone", tile.tone || "warn");
    const head = document.createElement("div");
    head.className = "dash-graph-head";
    const name = document.createElement("span");
    name.className = "metric-name";
    name.textContent = tile.name;
    const value = document.createElement("span");
    value.className = "metric-value " + (tile.tone || "warn");
    value.textContent = tile.value == null ? "—" : tile.display;
    head.append(name, value);
    box.appendChild(head);
    const values = (Array.isArray(tile.series) ? tile.series : []).map(Number).filter((v) => isFinite(v));
    if (values.length >= 2) {
      box.appendChild(chart(tile, values));
    } else if (labelEmpty) {
      const none = document.createElement("p");
      none.className = "muted dash-graph-none";
      none.textContent = "No samples yet";
      box.appendChild(none);
    }
    return box;
  };

  const paint = (data) => {
    if (!data || !Array.isArray(data.tiles)) {
      showEmpty("Metrics unavailable.");
      return;
    }
    const tiles = data.tiles;
    span = data.source === "zabbix" ? "last 24 h (Zabbix trends)" : "last hour";
    pane.setAttribute("data-graph-source", data.source || "prometheus");
    const anySeries = tiles.some((t) => Array.isArray(t.series) && t.series.length >= 2);
    if (data.collecting === null && data.error) {
      showEmpty("Prometheus is unreachable — no samples.");
      return;
    }
    if (empty) {
      const line = data.collecting_line || "";
      empty.textContent = anySeries ? line : "No samples yet" + (line ? " — " + line : "");
      empty.hidden = false;
    }
    if (list) list.replaceChildren(...tiles.map((tile) => tileRow(tile, anySeries)));
  };

  return { paint, showEmpty };
}

(function bindDashGraphs() {
  const pane = document.querySelector("[data-dash-graphs]");
  const table = document.querySelector("[data-dash-incident-table]");
  if (!pane || !table) return;
  const subject = pane.querySelector("[data-dash-graph-subject]");
  const assetLink = pane.querySelector("[data-dash-graph-asset]");
  const { paint, showEmpty } = graphPane(pane);
  let selected = null;
  let timer = 0;
  let seq = 0;

  const load = () => {
    if (!selected) return;
    const asset = selected.getAttribute("data-asset") || "";
    if (!asset) return;
    const mine = ++seq;
    fetch("/api/v1/assets/" + encodeURIComponent(asset) + "/metrics", { headers: { Accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        if (mine === seq) paint(data);
      })
      .catch(() => {
        if (mine === seq) showEmpty("Metrics unavailable.");
      });
  };

  let pending = 0;

  const graph = (row, options) => {
    selected = row;
    seq++;
    if (timer) window.clearInterval(timer);
    timer = 0;
    if (pending) window.clearTimeout(pending);
    pending = 0;
    const number = row.getAttribute("data-dash-incident") || "";
    const asset = row.getAttribute("data-asset") || "";
    const host = row.getAttribute("data-host") || "";
    if (subject) subject.textContent = number + (asset || host ? " · " + (asset || host) : "");
    if (assetLink) {
      assetLink.hidden = !asset;
      assetLink.href = asset ? "/assets/" + encodeURIComponent(asset) : "#";
    }
    if (!asset) {
      showEmpty("No host to chart");
      return;
    }
    showEmpty("Loading…");
    const start = () => {
      pending = 0;
      load();
      timer = window.setInterval(load, 30000);
    };
    // Holding an arrow key walks many rows; only the row it settles on fetches.
    if (options.defer) pending = window.setTimeout(start, 180);
    else start();
  };

  const picker = listPicker(table, {
    box: table.closest("[data-dash-list]"),
    rows: "tr[data-dash-incident]",
    onSelect: graph,
  });
  const rows = picker.rows;
  if (!rows.length) {
    showEmpty("No host to chart");
    return;
  }
  picker.select(rows.find((r) => r.getAttribute("data-active") === "true") || rows[0]);
})();

// Incident detail: fetch the host charts once, when the card scrolls into view (no background polling).
(function bindIncidentGraphs() {
  const pane = document.querySelector("[data-incident-graphs]");
  if (!pane) return;
  const asset = pane.getAttribute("data-asset") || "";
  const { paint, showEmpty } = graphPane(pane);
  if (!asset) {
    showEmpty("No host to chart");
    return;
  }
  const load = () => {
    fetch("/api/v1/assets/" + encodeURIComponent(asset) + "/metrics", { headers: { Accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => (data ? paint(data) : showEmpty("No samples yet")))
      .catch(() => showEmpty("No samples yet"));
  };
  if ("IntersectionObserver" in window) {
    const seen = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        seen.disconnect();
        load();
      }
    });
    seen.observe(pane);
  } else {
    load();
  }
})();

// Every other list box: select-only rows. A box with more than ten rows is sized to about ten, so a longer page
// (Rows 20 / 50 / 100) scrolls inside the box instead of stretching the window. The row the server marks .selected
// (the one being edited) starts selected and is scrolled into view.
(function bindListPick() {
  const TEN = 10;
  const picked = [];
  document.querySelectorAll("[data-list-scroll]").forEach((box) => {
    if (box.hasAttribute("data-list-bound")) return;
    picked.push({ box, picker: listPicker(box) });
  });
  if (!picked.length) return;

  const fit = () => {
    picked.forEach(({ box, picker }) => {
      if (picker.rows.length <= TEN) return;
      box.style.maxHeight = "";
      const top = box.getBoundingClientRect().top - box.scrollTop;
      const tenth = picker.rows[TEN - 1].getBoundingClientRect().bottom;
      const chrome = box.offsetHeight - box.clientHeight;
      box.style.maxHeight = "min(" + Math.ceil(tenth - top + chrome + 1) + "px, 72vh)";
    });
  };
  fit();
  picked.forEach(({ picker }) => {
    const start = picker.rows.find((r) => r.classList.contains("selected"));
    if (start) picker.select(start);
  });

  let frame = 0;
  const refit = () => {
    if (frame) return;
    frame = window.requestAnimationFrame(() => {
      frame = 0;
      fit();
    });
  };
  window.addEventListener("resize", refit);
  if (document.readyState !== "complete") window.addEventListener("load", refit, { once: true });
})();

// Engineer evidence: a single click only selects (listPicker); double-click anywhere on the row (the open text too)
// or Enter folds the stored text open or shut in place. Press-and-drag selects text to copy and never toggles.
(function bindEvidenceRows() {
  const table = document.querySelector("[data-evidence-table]");
  if (!table) return;
  const DRAG_PX = 4;
  let down = null;
  let dragged = false;
  const rowOf = (event) => {
    if (event.target.closest("a, input, button, select, textarea")) return null;
    return event.target.closest("[data-evidence-row]");
  };
  const toggle = (row) => {
    const open = !row.classList.contains("is-expanded");
    row.classList.toggle("is-expanded", open);
    row.setAttribute("aria-expanded", open ? "true" : "false");
  };
  const at = (event) => ({ x: Number(event.clientX) || 0, y: Number(event.clientY) || 0 });
  table.addEventListener("mousedown", (event) => {
    if (event.detail > 1) {
      if (rowOf(event)) event.preventDefault();
      return;
    }
    down = at(event);
    dragged = false;
  });
  table.addEventListener("mouseup", (event) => {
    if (!down) return;
    const up = at(event);
    if (Math.abs(up.x - down.x) + Math.abs(up.y - down.y) > DRAG_PX) dragged = true;
  });
  table.addEventListener("dblclick", (event) => {
    const row = rowOf(event);
    if (row && !dragged) toggle(row);
  });
  table.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.altKey || event.ctrlKey || event.metaKey) return;
    const row = event.target.closest("[data-evidence-row]");
    if (!row || event.target !== row) return;
    event.preventDefault();
    toggle(row);
  });
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
