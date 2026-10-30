/*
 * Naked Connect sidebar panel for Home Assistant.
 * Plain web component, no build step. Uses Home Assistant theme variables so it
 * follows the user's light/dark theme. Talks to the integration through the
 * "naked_connect/devices" websocket command and normal service calls.
 */

const ICON_RULES = [
  [/light/i, "mdi:lightbulb"],
  [/heater|heat/i, "mdi:water-boiler"],
  [/blower|air/i, "mdi:weather-windy"],
  [/jet/i, "mdi:hydro-power"],
  [/pump|filter/i, "mdi:pump"],
];

const MODES = ["On", "Off", "Auto"];

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);

class NakedPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._devices = null;
    this._error = null;
    this._sig = "";
    this._busy = new Map(); // entity_id -> timestamp of last command
    this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._init();
    this._render();
  }

  get hass() {
    return this._hass;
  }

  set narrow(v) {
    this._narrow = v;
    this._sig = "";
    this._render();
  }

  set panel(v) {
    this._panel = v;
  }

  async _init() {
    await this._load();
    try {
      this._unsub = await this._hass.connection.subscribeEvents(
        () => this._load(),
        "entity_registry_updated"
      );
    } catch (_e) {
      /* non-admin users may not be allowed to subscribe; polling below covers it */
    }
    this._timer = setInterval(() => this._load(), 60000);
    // Boost countdown ticks every second without a full re-render.
    this._tick = setInterval(() => {
      for (const el of this.shadowRoot.querySelectorAll("[data-ends]")) {
        el.textContent = fmtLeft(Number(el.dataset.ends) - Date.now());
      }
    }, 1000);
  }

  disconnectedCallback() {
    if (this._unsub) this._unsub();
    if (this._timer) clearInterval(this._timer);
    if (this._tick) clearInterval(this._tick);
    this._tick = null;
    this._unsub = null;
    this._timer = null;
    this._hass = null;
  }

  connectedCallback() {
    if (this._hass && !this._timer) this._init();
  }

  async _load() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "naked_connect/devices" });
      this._devices = res.devices || [];
      this._version = res.version;
      this._error = null;
    } catch (err) {
      this._error = (err && err.message) || String(err);
    }
    this._sig = "";
    this._render();
  }

  // ------------------------------------------------------------ helpers

  _st(eid) {
    return eid ? this._hass.states[eid] : undefined;
  }

  _shortName(eid, fallback) {
    const st = this._st(eid);
    const reg = this._hass.entities && this._hass.entities[eid];
    if (reg && reg.name) return reg.name;
    let name = (st && st.attributes.friendly_name) || fallback || eid;
    const dev = reg && reg.device_id && this._hass.devices && this._hass.devices[reg.device_id];
    const devName = dev && (dev.name_by_user || dev.name);
    if (devName && name.startsWith(devName + " ")) name = name.slice(devName.length + 1);
    return name;
  }

  _value(eid) {
    const st = this._st(eid);
    if (!st || st.state === "unavailable" || st.state === "unknown") return null;
    return st;
  }

  _fmtNumber(eid) {
    const st = this._value(eid);
    if (!st) return { num: "–", unit: "" };
    const n = Number(st.state);
    const num = Number.isFinite(n) ? (Number.isInteger(n) ? String(n) : n.toFixed(1)) : st.state;
    return { num, unit: st.attributes.unit_of_measurement || "" };
  }

  _icon(name) {
    for (const [re, icon] of ICON_RULES) if (re.test(name)) return icon;
    return "mdi:power-socket-au";
  }

  _isLight(name) {
    return /light/i.test(name);
  }

  // ------------------------------------------------------------ actions

  _onClick(ev) {
    const el = ev.composedPath().find((n) => n.dataset && n.dataset.action);
    if (!el || !this._hass) return;
    const { action, entity, option } = el.dataset;
    ev.stopPropagation();

    if (action === "toggle" && entity) {
      this._mark(entity);
      this._hass.callService("switch", "toggle", { entity_id: entity }).catch((e) => this._fail(entity, e));
    } else if (action === "mode" && entity && option) {
      this._mark(entity);
      this._hass
        .callService("select", "select_option", { entity_id: entity, option })
        .catch((e) => this._fail(entity, e));
    } else if (action === "effect" && entity && el.dataset.effect) {
      this._mark(entity);
      this._hass
        .callService("light", "turn_on", { entity_id: entity, effect: el.dataset.effect })
        .then(() => { this._busy.delete(entity); this._sig = ""; this._render(); })
        .catch((e) => this._fail(entity, e));
    } else if (action === "switch" && entity) {
      this._mark(entity);
      this._hass.callService("switch", "toggle", { entity_id: entity }).catch((e) => this._fail(entity, e));
    } else if (action === "press" && entity) {
      this._mark(entity);
      this._hass.callService("button", "press", { entity_id: entity }).catch((e) => this._fail(entity, e));
    } else if (action === "nudge" && entity) {
      const st = this._st(entity);
      const cur = st ? Number(st.state) : NaN;
      if (!Number.isFinite(cur)) return;
      const min = Number(st.attributes.min ?? 0);
      const max = Number(st.attributes.max ?? 100);
      const value = Math.min(max, Math.max(min, cur + Number(el.dataset.delta || 0)));
      if (value === cur) return;
      this._mark(entity);
      this._hass
        .callService("number", "set_value", { entity_id: entity, value })
        .catch((e) => this._fail(entity, e));
    } else if (action === "more-info" && entity) {
      this.dispatchEvent(
        new CustomEvent("hass-more-info", { detail: { entityId: entity }, bubbles: true, composed: true })
      );
    }
  }

  _mark(eid) {
    this._busy.set(eid, Date.now());
    this._sig = "";
    this._render();
    setTimeout(() => {
      this._busy.delete(eid);
      this._sig = "";
      this._render();
    }, 8000);
  }

  _fail(eid, err) {
    this._busy.delete(eid);
    this._error = `Command failed: ${(err && err.message) || err}`;
    this._sig = "";
    this._render();
    setTimeout(() => {
      this._error = null;
      this._sig = "";
      this._render();
    }, 6000);
  }

  // ------------------------------------------------------------ render

  _signature() {
    const ids = [];
    for (const d of this._devices || []) {
      ids.push(...Object.values(d.readings));
      for (const s of d.sockets) ids.push(s.switch, s.mode, s.light);
    }
    const parts = ids.map((id) => {
      const st = this._st(id);
      return st
        ? `${id}=${st.state}|${st.attributes.friendly_name}|${st.attributes.mode || ""}|${st.attributes.effect || ""}`
        : `${id}=?`;
    });
    return JSON.stringify([
      parts, [...this._busy.keys()], this._error, this._narrow,
      (this._devices || []).map((d) => [d.entry_id, d.sockets.length, d.last_update_success]),
    ]);
  }

  _render() {
    if (!this._hass) return;
    const sig = this._signature();
    if (sig === this._sig && this.shadowRoot.childElementCount) return;
    this._sig = sig;

    const body =
      this._devices === null
        ? `<div class="empty">Loading…</div>`
        : this._devices.length === 0
          ? this._renderEmpty()
          : this._devices.map((d) => this._renderDevice(d)).join("");

    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title">Naked Connect</div>
      </div>
      <div class="content">
        ${this._error ? `<div class="banner">${esc(this._error)}</div>` : ""}
        ${body}
      </div>`;

    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) {
      menu.hass = this._hass;
      menu.narrow = this._narrow;
    }
  }

  _renderEmpty() {
    return `
      <div class="card empty">
        <ha-icon icon="mdi:pool"></ha-icon>
        <h2>No Naked Connect device set up yet</h2>
        <p>Add one under <a href="/config/integrations/integration/naked_connect">Settings → Devices &amp; services → Naked Connect</a>.</p>
      </div>`;
  }

  _renderDevice(d) {
    const r = d.readings;
    if (r.mode) return this._renderChlorDevice(d);
    // Pool probe (B) first when labelled; skip probes the device doesn't report.
    const temps = [r.temp_b, r.temp_a].filter((eid) => eid && this._value(eid));
    const stale = !d.last_update_success;

    const tempTiles = temps
      .map((eid) => {
        const { num, unit } = this._fmtNumber(eid);
        return `
          <button class="temp" data-action="more-info" data-entity="${esc(eid)}">
            <div class="temp-label">${esc(this._shortName(eid))}</div>
            <div class="temp-value">${esc(num)}<span>${esc(unit)}</span></div>
          </button>`;
      })
      .join("");

    const stat = (eid, label, icon) => {
      if (!eid) return "";
      const st = this._value(eid);
      if (!st) return ""; // not reported by this device
      let text = st.state;
      if (st && st.attributes.unit_of_measurement) text += ` ${st.attributes.unit_of_measurement}`;
      if (eid === r.fault && st) text = Number(st.state) === 0 ? "OK" : `Code ${st.state}`;
      if (eid === r.pump && st) text = st.state === "on" ? "Running" : "Stopped";
      if (eid === r.work_mode && st && /^(null|none)$/i.test(st.state)) text = "Normal";
      const warn =
        (eid === r.fault && st && Number(st.state) !== 0) ||
        (eid === r.fault_text && st && !["OK", "unknown", "unavailable"].includes(st.state));
      return `
        <button class="stat ${warn ? "warn" : ""}" data-action="more-info" data-entity="${esc(eid)}">
          <ha-icon icon="${icon}"></ha-icon>
          <div><div class="stat-label">${esc(label)}</div><div class="stat-value">${esc(text)}</div></div>
        </button>`;
    };

    const stats = [
      stat(r.salt, "Salt", "mdi:shaker-outline"),
      stat(r.output, "Chlorinator output", "mdi:water-percent"),
      stat(r.cell, "Cell", "mdi:flash-outline"),
      stat(r.pump, "Chlorinator pump", "mdi:pump"),
      stat(r.power_pack, "Power pack", "mdi:thermometer"),
      stat(r.work_mode, "Work mode", "mdi:cog-outline"),
      r.fault_text
        ? stat(r.fault_text, "Status", "mdi:alert-circle-outline")
        : stat(r.fault, "Status", "mdi:alert-circle-outline"),
    ].join("");

    const sockets = d.sockets.length
      ? d.sockets.map((s) => this._renderSocket(s)).join("")
      : `<div class="hint">No sockets reported yet. Toggle each socket once in the Naked Connect app and they will appear here within a few minutes.</div>`;

    const dev = d.device_id && this._hass.devices && this._hass.devices[d.device_id];
    const title = (dev && (dev.name_by_user || dev.name)) || d.title;
    const devLink = d.device_id
      ? `<a class="link" href="/config/devices/device/${esc(d.device_id)}">Device page</a>`
      : "";

    return `
      <section class="device">
        <div class="device-head">
          <div>
            <h1>${esc(title)}</h1>
            <div class="sub">${d.sw_version ? `Firmware ${esc(d.sw_version)} · ` : ""}${
              stale ? `<span class="bad">Cloud not reachable, showing last known values</span>` : "Connected"
            }</div>
          </div>
          ${devLink}
        </div>
        <div class="temps">${tempTiles || `<div class="hint">No temperature readings yet.</div>`}</div>
        ${stats.trim() ? `<div class="card stats">${stats}</div>` : ""}
        ${r.mode ? this._renderChlorinator(r) : ""}
        ${d.sockets.length || !r.mode ? `<h2 class="section">Equipment</h2>
        <div class="grid">${sockets}</div>` : ""}
      </section>`;
  }

  _deviceHead(d) {
    const dev = d.device_id && this._hass.devices && this._hass.devices[d.device_id];
    const title = (dev && (dev.name_by_user || dev.name)) || d.title;
    const devLink = d.device_id
      ? `<a class="link" href="/config/devices/device/${esc(d.device_id)}">Device page</a>` : "";
    return `
      <div class="device-head">
        <div>
          <h1>${esc(title)}</h1>
          <div class="sub">${d.sw_version ? `Firmware ${esc(d.sw_version)} · ` : ""}${
            !d.last_update_success ? `<span class="bad">Cloud not reachable, showing last known values</span>` : "Connected"
          }</div>
        </div>
        ${devLink}
      </div>`;
  }

  _renderChlorDevice(d) {
    const r = d.readings;
    const val = (eid) => this._value(eid);
    const busy = (eid) => (eid && this._busy.has(eid)) ? "busy" : "";
    const special = val(r.special);
    const specialText = special ? special.state : "Normal";
    const boosting = specialText === "Boost";
    const testing = specialText === "Salt test";
    const set = val(r.setpoint);

    // ---- controls (left)
    const segment = (eid, label, options) => eid
      ? `<div class="seg-row"><span class="stat-label">${esc(label)}</span>
          <div class="seg ${busy(eid)}" role="group" aria-label="${esc(label)}">${options.map(
          (m) => `<button class="${val(eid) && val(eid).state === m ? "sel" : ""}" data-action="mode"
                   data-entity="${esc(eid)}" data-option="${m}">${m}</button>`).join("")}</div></div>`
      : "";
    const appliesTo = set && set.attributes.applies_to;
    const outLabel = appliesTo && appliesTo !== "Normal" ? `${appliesTo} output` : "Output";
    const output = r.setpoint ? `<div class="setpoint ${busy(r.setpoint)}">
        <span class="stat-label">${esc(outLabel)}</span>
        <button class="step" data-action="nudge" data-entity="${esc(r.setpoint)}" data-delta="-5" aria-label="Lower output">−</button>
        <span class="set-val">${esc(set ? Math.round(Number(set.state)) : "–")}%</span>
        <button class="step" data-action="nudge" data-entity="${esc(r.setpoint)}" data-delta="5" aria-label="Raise output">+</button>
      </div>` : "";

    const action = (eid, label, icon, cls = "") => eid
      ? `<button class="act ${cls} ${busy(eid)}" data-action="press" data-entity="${esc(eid)}">
           <ha-icon icon="${icon}"></ha-icon><span>${esc(label)}</span></button>` : "";

    const ends = val(r.boost_ends);
    const endsMs = ends ? Date.parse(ends.state) : NaN;
    const hours = val(r.boost_hours);
    const boost = boosting
      ? `<div class="boost on">
          <div class="boost-info">
            <span class="stat-label">Chlorine boost</span>
            <span class="countdown" ${Number.isFinite(endsMs) ? `data-ends="${endsMs}"` : ""}>${
              Number.isFinite(endsMs) ? esc(fmtLeft(endsMs - Date.now())) : "Running"}</span>
            <span class="hint-sm">${Number.isFinite(endsMs) ? "remaining" : "time left unknown"}</span>
          </div>
          ${action(r.boost_stop, "Stop Chlorine Boost", "mdi:stop-circle-outline", "full")}
        </div>`
      : `<div class="boost">
          ${r.boost_hours ? `<div class="setpoint ${busy(r.boost_hours)}">
            <span class="stat-label">Boost timer</span>
            <button class="step" data-action="nudge" data-entity="${esc(r.boost_hours)}" data-delta="-1" aria-label="Shorter boost">−</button>
            <span class="set-val">${esc(hours ? Math.round(Number(hours.state)) : "–")}<small> h</small></span>
            <button class="step" data-action="nudge" data-entity="${esc(r.boost_hours)}" data-delta="1" aria-label="Longer boost">+</button>
          </div>` : ""}
          ${action(r.boost_start, "Start Chlorine Boost", "mdi:rocket-launch", "full")}
        </div>`;

    // ---- readings (right): small outlined cards
    const mc = (eid, label, icon, text, warn) => {
      if (!eid) return "";
      const st = val(eid);
      if (!st && text === undefined) return "";
      let shown = text;
      if (shown === undefined) {
        shown = st.state;
        if (st.attributes.unit_of_measurement) shown += ` ${st.attributes.unit_of_measurement}`;
      }
      return `<button class="mc ${warn ? "warn" : ""}" data-action="more-info" data-entity="${esc(eid)}">
          <ha-icon icon="${icon}"></ha-icon>
          <span class="mc-label">${esc(label)}</span>
          <span class="mc-value">${esc(shown)}</span>
        </button>`;
    };
    const salt = val(r.salt_test_result);
    const saltCard = r.salt_test_result || r.salt_test ? `
      <div class="mc salt">
        <button class="mc-main" data-action="more-info" data-entity="${esc(r.salt_test_result || "")}">
          <ha-icon icon="mdi:shaker-outline"></ha-icon>
          <span class="mc-label">Salt Reading</span>
          <span class="mc-value">${testing ? "Testing…" : salt ? `${esc(salt.state)} ppm` : "- - ppm"}</span>
          ${!testing && salt && val(r.salt_condition) ? `<span class="mc-sub">${esc(val(r.salt_condition).state)}</span>` : ""}
        </button>
        ${action(r.salt_test, "Test Salt Level", "mdi:shaker-outline", "full")}
      </div>` : "";
    const temps = [r.temp_b, r.temp_a].filter((eid) => eid && val(eid));
    const pump = val(r.pump);
    const fault = val(r.fault_text);
    const cards = [
      saltCard,
      // One probe is just "Water Temp"; two keep their own names (e.g. Pool / Spa).
      ...temps.map((eid) => mc(eid, temps.length === 1 ? "Water Temp" : this._shortName(eid),
        "mdi:thermometer-water")),
      mc(r.output, "Actual Output", "mdi:water-percent"),
      mc(r.cell, "Cell Status", "mdi:flash-outline", specialText !== "Normal" ? specialText : undefined),
      mc(r.pump, "Pump Status", "mdi:pump", pump ? (pump.state === "on" ? "Running" : "Stopped") : undefined),
      mc(r.power_pack, "Power Pack Temp", "mdi:thermometer"),
      r.fault_text
        ? mc(r.fault_text, "Status", "mdi:alert-circle-outline", undefined, fault && fault.state !== "OK")
        : mc(r.fault, "Status", "mdi:alert-circle-outline"),
    ].join("");

    return `
      <section class="device">
        ${this._deviceHead(d)}
        <div class="card chlor2 ${boosting ? "boosting" : ""}">
          <div class="ctl-col">
            ${segment(r.mode, "Power", ["On", "Off", "Auto"])}
            ${segment(r.work_mode_ctl, "Mode", ["Normal", "Winter", "Spa"])}
            ${output}
            ${boost}
          </div>
          <div class="mcards-wrap"><div class="mcards">${cards}</div></div>
        </div>
        ${this._renderCopper(r)}
        ${this._renderPh(r)}
      </section>`;
  }

  _renderCopper(r) {
    if (!r.copper_output) return "";
    const val = (eid) => this._value(eid);
    const busy = (eid) => (eid && this._busy.has(eid)) ? "busy" : "";
    const boosting = val(r.copper_boost) && val(r.copper_boost).state === "on";
    const running = val(r.copper_running) && val(r.copper_running).state === "on";
    const out = val(r.copper_output);
    const outNum = out ? Math.round(Number(out.state)) : null;
    const level = val(r.copper_level);
    const ends = val(r.copper_boost_ends);
    const endsMs = ends ? Date.parse(ends.state) : NaN;
    const sub = boosting ? "boost running" : running ? "ioniser working" : "idle";
    const output = boosting
      ? `<div class="mini">
          <div class="mini-row"><span class="mini-val">100<small> %</small></span></div>
          <div class="stat-label">Copper output · boost</div>
        </div>`
      : `<div class="mini ${busy(r.copper_output)}">
          <div class="mini-row">
            <button class="step sm" data-action="nudge" data-entity="${esc(r.copper_output)}" data-delta="-5" aria-label="Lower copper output">−</button>
            <span class="mini-val">${esc(outNum ?? "–")}<small> %</small></span>
            <button class="step sm" data-action="nudge" data-entity="${esc(r.copper_output)}" data-delta="5" aria-label="Raise copper output">+</button>
          </div>
          <div class="stat-label">Copper output</div>
        </div>`;
    const levelText = level && level.state !== "unknown" && level.state !== "unavailable"
      ? `${Number(level.state).toFixed(2)}<small> ppm</small>` : "- -<small> ppm</small>";
    const go = boosting
      ? `<div class="boost-info">
           <span class="stat-label">Copper boost</span>
           <span class="countdown" ${Number.isFinite(endsMs) ? `data-ends="${endsMs}"` : ""}>${
             Number.isFinite(endsMs) ? esc(fmtLeft(endsMs - Date.now())) : "Running"}</span>
           <span class="hint-sm">${Number.isFinite(endsMs) ? "remaining" : ""}</span>
         </div>
         <button class="act full ${busy(r.copper_boost_stop)}" data-action="press" data-entity="${esc(r.copper_boost_stop)}">
           <ha-icon icon="mdi:stop-circle-outline"></ha-icon><span>Stop Copper Boost</span></button>`
      : `<button class="act full ${busy(r.copper_boost_start)}" data-action="press" data-entity="${esc(r.copper_boost_start)}">
           <ha-icon icon="mdi:rocket-launch"></ha-icon><span>Start Copper Boost</span></button>
         <span class="hint-sm">Runs at full output for 27 hours</span>`;
    return `
      <div class="card ph copper ${boosting ? "boosting" : ""}">
        <div class="ph-head">
          <span class="ph-title"><ha-icon icon="mdi:flask-outline"></ha-icon> Copper ioniser
            <span class="sub">${sub}</span></span>
        </div>
        <div class="ph-body">
          <div class="cu-left">
            ${output}
            ${r.copper_level ? `<button class="mini cu-level" data-action="more-info" data-entity="${esc(r.copper_level)}">
              <div class="mini-row"><span class="mini-val">${levelText}</span></div>
              <div class="stat-label">Copper level</div>
            </button>` : ""}
          </div>
          <div class="ph-demand cu-boost">${go}</div>
        </div>
      </div>`;
  }

  _renderPh(r) {
    if (!r.ph) return "";
    const val = (eid) => this._value(eid);
    const busy = (eid) => (eid && this._busy.has(eid)) ? "busy" : "";
    const phSwitch = this._st(r.ph);
    const phOn = phSwitch && phSwitch.state === "on";
    const dosing = val(r.ph_status) && val(r.ph_status).state === "on";
    const left = val(r.ph_left);
    const leftNum = left ? Number(left.state) : 0;
    const running = Number.isFinite(leftNum) && leftNum > 0;
    const mini = (eid, label, unit, delta) => {
      if (!eid) return "";
      const st = val(eid);
      return `<div class="mini ${busy(eid)}">
        <div class="mini-row">
          <button class="step sm" data-action="nudge" data-entity="${esc(eid)}" data-delta="-${delta}" aria-label="Lower ${esc(label)}">−</button>
          <span class="mini-val">${esc(st ? Math.round(Number(st.state)) : "–")}<small> ${esc(unit)}</small></span>
          <button class="step sm" data-action="nudge" data-entity="${esc(eid)}" data-delta="${delta}" aria-label="Raise ${esc(label)}">+</button>
        </div>
        <div class="stat-label">${esc(label)}</div>
      </div>`;
    };
    const go = running
      ? `<button class="act full ${busy(r.ph_stop)}" data-action="press" data-entity="${esc(r.ph_stop)}">
           <ha-icon icon="mdi:stop-circle-outline"></ha-icon><span>Stop Prime / Demand Dosing</span></button>`
      : `<button class="act full ${busy(r.ph_start)}" data-action="press" data-entity="${esc(r.ph_start)}">
           <ha-icon icon="mdi:play-circle-outline"></ha-icon><span>Start Prime / Demand Dosing</span></button>`;
    return `
      <div class="card ph">
        <div class="ph-head">
          <span class="ph-title"><ha-icon icon="mdi:beaker-outline"></ha-icon> pH dosing
            <span class="sub">${phOn ? (running ? `dosing · ${esc(left.state)} left` : dosing ? "pump running" : "on") : "off"}</span></span>
          <button class="toggle ${phOn ? "on" : ""} ${busy(r.ph)}" data-action="switch" data-entity="${esc(r.ph)}"
            role="switch" aria-checked="${phOn}" aria-label="pH dosing"><span class="pill"><span></span></span></button>
        </div>
        ${phOn ? `<div class="ph-body">
          ${mini(r.ph_runtime, "pH1 run time", "min/day", 1)}
          <div class="ph-demand">
            ${mini(r.ph_amount, "pH2 Prime / Demand amount", "ml", 10)}
            ${go}
          </div>
        </div>` : ""}
      </div>`;
  }

  _renderSocket(s) {
    const sw = this._st(s.switch);
    const mode = this._st(s.mode);
    const name = this._shortName(s.switch, s.socket);
    const on = sw && sw.state === "on";
    const unavailable = !sw || sw.state === "unavailable";
    const busy = this._busy.has(s.switch) || this._busy.has(s.mode);
    const current = mode && mode.state;
    const timers = (sw && sw.attributes.timers) || [];
    const kind = this._isLight(name) ? "light" : "power";

    const light = s.light ? this._st(s.light) : null;
    const effects = (light && light.attributes.effect_list) || [];
    const effect = light && light.attributes.effect;
    const lightBusy = s.light && this._busy.has(s.light);

    let status = "Unavailable";
    if (!unavailable) {
      if (current === "Auto") status = `Auto · ${on ? "running on timer" : "idle"}`;
      else status = on ? "On" : "Off";
      if (on && effect) status += ` · ${effect}`;
    }

    const swatches = effects.length
      ? `<div class="colours ${lightBusy ? "busy" : ""}" role="group" aria-label="Light colour">
          ${effects.filter((e) => LIGHT_COLOURS[e]).map((e) => `
            <button class="swatch ${e === effect ? "sel" : ""}" style="--sw:${LIGHT_COLOURS[e]}"
              data-action="effect" data-entity="${esc(s.light)}" data-effect="${esc(e)}"
              title="${esc(e)}" aria-label="${esc(e)}"></button>`).join("")}
          ${effects.filter((e) => !LIGHT_COLOURS[e]).map((e) => `
            <button class="show ${e === effect ? "sel" : ""}"
              data-action="effect" data-entity="${esc(s.light)}" data-effect="${esc(e)}">${esc(e)}</button>`).join("")}
        </div>`
      : "";
    const glow = on && effect && LIGHT_COLOURS[effect] ? `style="--c:${LIGHT_COLOURS[effect]}"` : "";

    const seg = s.mode
      ? `<div class="seg" role="group" aria-label="Mode">${MODES.map(
          (m) =>
            `<button class="${current === m ? "sel" : ""}" data-action="mode" data-entity="${esc(
              s.mode
            )}" data-option="${m}" ${unavailable ? "disabled" : ""}>${m}</button>`
        ).join("")}</div>`
      : "";

    return `
      <div class="tile ${on ? "on" : ""} ${kind} ${busy ? "busy" : ""} ${unavailable ? "na" : ""}" ${glow}>
        <button class="tile-main" data-action="toggle" data-entity="${esc(s.switch)}" ${unavailable ? "disabled" : ""}>
          <span class="bubble"><ha-icon icon="${this._icon(name)}"></ha-icon></span>
          <span class="tile-text">
            <span class="tile-name">${esc(name)}</span>
            <span class="tile-status">${esc(busy ? "Sending…" : status)}</span>
          </span>
          <span class="pill" aria-hidden="true"><span></span></span>
        </button>
        ${seg}
        ${swatches}
        <div class="tile-foot">
          <span>${timers.length ? `Timer ${esc(timers.join(", "))}` : "No timer"}</span>
          <button class="info" data-action="more-info" data-entity="${esc(s.switch)}" aria-label="Details">
            <ha-icon icon="mdi:information-outline"></ha-icon>
          </button>
        </div>
      </div>`;
  }
}

// Swatch colours for light colours (shows are listed by name).
const LIGHT_COLOURS = {
  Aqua: "#00c4d4", Blue: "#2563ff", Green: "#1fbf5a", Gold: "#f2b705",
  Magenta: "#e0218a", Red: "#e8262b", White: "#f4f4f4",
};

function fmtLeft(ms) {
  if (!(ms > 0)) return "0:00:00";
  const t = Math.floor(ms / 1000);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

const STYLES = `
  :host {
    display: block;
    min-height: 100vh;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
    font-family: var(--paper-font-body1_-_font-family, inherit);
    --pq-card: var(--ha-card-background, var(--card-background-color, #fff));
    --pq-radius: var(--ha-card-border-radius, 12px);
    --pq-border: var(--ha-card-border-width, 1px) solid var(--ha-card-border-color, var(--divider-color, #e0e0e0));
    --pq-accent: var(--primary-color, #03a9f4);
    --pq-light: var(--state-light-on-color, var(--amber-color, #ffb300));
    --pq-power: var(--state-switch-on-color, var(--pq-accent));
  }
  * { box-sizing: border-box; }
  button { font: inherit; color: inherit; background: none; border: 0; padding: 0; cursor: pointer; text-align: left; }
  button:disabled { cursor: default; }
  a { color: var(--pq-accent); }
  .toolbar {
    display: flex; align-items: center; gap: 8px;
    height: var(--header-height, 56px); padding: 0 12px;
    background: var(--app-header-background-color, var(--primary-color));
    color: var(--app-header-text-color, #fff);
    border-bottom: var(--app-header-border-bottom, none);
    position: sticky; top: 0; z-index: 2;
  }
  .title { font-size: 20px; font-weight: 400; margin-left: 4px; }
  .content { max-width: 1100px; margin: 0 auto; padding: 16px; }
  .banner { background: var(--error-color, #db4437); color: #fff; padding: 10px 14px; border-radius: var(--pq-radius); margin-bottom: 16px; }
  .card { background: var(--pq-card); border: var(--pq-border); border-radius: var(--pq-radius); box-shadow: var(--ha-card-box-shadow, none); }
  .empty { text-align: center; padding: 48px 16px; color: var(--secondary-text-color); }
  .empty ha-icon { --mdc-icon-size: 48px; color: var(--pq-accent); }
  .device + .device { margin-top: 40px; }
  .device-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; margin: 4px 0 16px; }
  h1 { font-size: 24px; font-weight: 500; margin: 0; }
  .sub { color: var(--secondary-text-color); font-size: 14px; margin-top: 2px; }
  .bad { color: var(--error-color, #db4437); }
  .link { font-size: 14px; white-space: nowrap; }
  .temps { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 12px; }
  .temp {
    background: var(--pq-card); border: var(--pq-border); border-radius: var(--pq-radius);
    padding: 18px 20px; position: relative; overflow: hidden;
  }
  .temp::after {
    content: ""; position: absolute; inset: auto -40px -60px auto; width: 160px; height: 160px; border-radius: 50%;
    background: radial-gradient(circle, color-mix(in srgb, var(--pq-accent) 28%, transparent), transparent 70%);
    pointer-events: none;
  }
  .temp-label { color: var(--secondary-text-color); font-size: 14px; }
  .temp-value { font-size: 44px; font-weight: 300; line-height: 1.1; margin-top: 6px; letter-spacing: -1px; }
  .temp-value span { font-size: 20px; margin-left: 4px; color: var(--secondary-text-color); letter-spacing: 0; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 4px; padding: 8px; }
  .stat { display: flex; align-items: center; gap: 12px; padding: 10px; border-radius: 10px; }
  .stat:hover, .temp:hover { background: color-mix(in srgb, var(--primary-text-color) 4%, var(--pq-card)); }
  .stat ha-icon { color: var(--secondary-text-color); }
  .stat.warn ha-icon, .stat.warn .stat-value { color: var(--error-color, #db4437); }
  .stat-label { font-size: 12px; color: var(--secondary-text-color); }
  .stat-value { font-size: 15px; font-weight: 500; }
  .section { font-size: 16px; font-weight: 500; margin: 24px 4px 12px; color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: .6px; }
  .chlor { padding: 16px; display: flex; flex-direction: column; gap: 14px; }
  .chlor.boost { box-shadow: 0 0 0 1px color-mix(in srgb, var(--pq-accent) 40%, transparent), 0 8px 28px -10px color-mix(in srgb, var(--pq-accent) 60%, transparent); }
  .chlor-top { display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap; align-items: center; }
  .chlor-out { min-width: 160px; }
  .chlor-ctl { display: flex; flex-direction: column; gap: 10px; min-width: 240px; flex: 1; max-width: 360px; }
  .setpoint { display: flex; align-items: center; gap: 10px; justify-content: space-between; }
  .setpoint.busy, .chip.busy, .act.busy { opacity: .6; }
  .step { width: 36px; height: 36px; border-radius: 50%; text-align: center; font-size: 20px; line-height: 36px;
    background: color-mix(in srgb, var(--secondary-text-color) 12%, transparent); }
  .set-val { font-size: 20px; font-weight: 500; min-width: 56px; text-align: center; }
  .chips, .actions { display: flex; flex-wrap: wrap; gap: 8px; }
  .chip, .act { display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 18px; font-size: 14px;
    background: color-mix(in srgb, var(--secondary-text-color) 10%, transparent); color: var(--secondary-text-color); }
  .chip ha-icon, .act ha-icon { --mdc-icon-size: 18px; }
  .chip.on { background: color-mix(in srgb, var(--pq-accent) 18%, transparent); color: var(--pq-accent); }
  .act { color: var(--primary-text-color); border: 1px solid var(--divider-color, rgba(127,127,127,.35)); background: none; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }
  .hint { color: var(--secondary-text-color); padding: 12px 4px; }
  .tile {
    --c: var(--pq-power);
    background: var(--pq-card); border: var(--pq-border); border-radius: var(--pq-radius);
    padding: 12px; display: flex; flex-direction: column; gap: 10px;
    transition: box-shadow .25s, border-color .25s;
  }
  .tile.light { --c: var(--pq-light); }
  .tile.on {
    border-color: color-mix(in srgb, var(--c) 55%, transparent);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--c) 25%, transparent), 0 6px 24px -8px color-mix(in srgb, var(--c) 55%, transparent);
  }
  .tile.na { opacity: .55; }
  .tile.busy { opacity: .75; }
  .tile-main { display: flex; align-items: center; gap: 12px; width: 100%; }
  .bubble {
    width: 42px; height: 42px; border-radius: 50%; display: grid; place-items: center; flex: none;
    background: color-mix(in srgb, var(--secondary-text-color) 12%, transparent); color: var(--secondary-text-color);
    transition: background .25s, color .25s;
  }
  .tile.on .bubble { background: color-mix(in srgb, var(--c) 20%, transparent); color: var(--c); }
  .tile-text { display: flex; flex-direction: column; flex: 1; min-width: 0; }
  .tile-name { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tile-status { font-size: 13px; color: var(--secondary-text-color); }
  .pill { width: 40px; height: 22px; border-radius: 11px; flex: none; position: relative;
    background: color-mix(in srgb, var(--secondary-text-color) 30%, transparent); transition: background .2s; }
  .pill span { position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: transform .2s; box-shadow: 0 1px 2px rgba(0,0,0,.3); }
  .tile.on .pill { background: var(--c); }
  .tile.on .pill span { transform: translateX(18px); }
  .seg-row { display: flex; flex-direction: column; gap: 4px; }

  .ph-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .ph-title { display: flex; align-items: center; gap: 8px; font-weight: 500; }
  .ph-title .sub { font-weight: 400; opacity: .7; }
  .toggle { display: flex; align-items: center; background: none; border: 0; padding: 4px; cursor: pointer; color: inherit; }
  .toggle.on .pill { background: var(--primary-color, #03a9f4); }
  .toggle.on .pill span { transform: translateX(18px); }
  .chlor2 { display: grid; grid-template-columns: minmax(260px, 1fr) minmax(0, 1.2fr); gap: 20px; padding: 16px; }
  .chlor2.boosting { border-color: var(--warning-color, #ff9800); }
  .ctl-col { display: flex; flex-direction: column; gap: 12px; }
  /* Readings: thin lines between cells only (the outer edge is clipped away). */
  .mcards-wrap { overflow: hidden; align-self: start; }
  .mcards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: -1px 0 0 -1px; }
  .mc { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; padding: 12px 14px;
    border: 0; border-radius: 0; min-width: 0;
    border-left: 1px solid var(--divider-color, rgba(127,127,127,.25));
    border-top: 1px solid var(--divider-color, rgba(127,127,127,.25)); }
  .mc ha-icon { --mdc-icon-size: 20px; color: var(--secondary-text-color); margin-bottom: 2px; }
  .mc-label { font-size: 12px; color: var(--secondary-text-color); }
  .mc-value { font-size: 17px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
  .mc.warn .mc-value { color: var(--error-color, #db4437); white-space: normal; overflow: visible; }
  .mc.salt { grid-column: span 2; gap: 8px; }
  .mc-main { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; width: 100%; }
  .act.full { width: 100%; justify-content: center; box-sizing: border-box; }
  .boost { display: flex; flex-direction: column; gap: 10px; padding-top: 12px;
    border-top: 1px solid var(--divider-color, rgba(127,127,127,.25)); }
  .boost-info { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .countdown { font-size: 24px; font-weight: 500; font-variant-numeric: tabular-nums; color: var(--warning-color, #ff9800); }
  .hint-sm { font-size: 12px; color: var(--secondary-text-color); }
  .card.ph { padding: 14px 16px; margin-top: 12px; }
  .card.copper.boosting { border-color: var(--warning-color, #ff9800); }
  .cu-left { display: flex; gap: 12px; flex-wrap: wrap; }
  .cu-level { background: none; border: 0; color: inherit; font: inherit; cursor: pointer; padding: 0; }
  .cu-boost { display: flex; flex-direction: column; gap: 8px; }
  .mc-sub { font-size: 12px; opacity: .75; }
  .card.ph .ph-body { display: grid; grid-template-columns: 1fr 1fr; align-items: start; gap: 14px 24px; margin-top: 14px; }
  .card.ph .mini { flex: none; }
  .ph-demand { display: flex; flex-direction: column; align-items: stretch; gap: 10px; }
  @media (max-width: 760px) {
    .chlor2 { grid-template-columns: 1fr; }
  }
  /* Four across when there's room; otherwise two (both fill every row evenly). */
  @media (max-width: 520px), (min-width: 761px) and (max-width: 1180px) {
    .mcards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  .colours { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 10px; }
  .swatch { width: 26px; height: 26px; border-radius: 50%; background: var(--sw);
    border: 1px solid var(--divider-color, rgba(127,127,127,.35)); flex: none; }
  .swatch.sel { outline: 2px solid var(--primary-text-color); outline-offset: 2px; }
  .show { font-size: 12px; padding: 4px 9px; border-radius: 13px; text-align: center;
    border: 1px solid var(--divider-color, rgba(127,127,127,.35)); color: var(--secondary-text-color); }
  .show.sel { color: var(--primary-text-color); border-color: var(--primary-text-color); }
  .colours.busy { opacity: .6; pointer-events: none; }
  .ph-body { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 24px; margin-top: 12px; }
  .mini { display: flex; flex-direction: column; align-items: center; gap: 2px; flex: 1 1 140px; }
  .mini-row { display: flex; align-items: center; gap: 10px; }
  .mini-val { font-size: 20px; font-weight: 500; min-width: 72px; text-align: center; white-space: nowrap; }
  @media (max-width: 520px) {
    .ph-body { gap: 12px 8px; }
    .mini-row { gap: 6px; }
    .card.ph .ph-body { grid-template-columns: 1fr; }
  }
  .mini-val small { font-size: 12px; opacity: .7; font-weight: 400; }
  .step.sm { width: 30px; height: 30px; line-height: 30px; font-size: 18px; }
  .wide .act { width: 100%; justify-content: center; box-sizing: border-box; }
  .act.small { padding: 6px 10px; font-size: 13px; flex: none; }
  .act.small ha-icon { --mdc-icon-size: 16px; }
  .set-val small { font-size: 12px; opacity: .7; }
  .seg { display: grid; grid-template-columns: repeat(3, 1fr); border-radius: 8px; padding: 2px;
    background: color-mix(in srgb, var(--secondary-text-color) 10%, transparent); }
  .seg button { text-align: center; padding: 5px 0; border-radius: 6px; font-size: 13px; color: var(--secondary-text-color); }
  .seg button.sel { background: var(--pq-card); color: var(--primary-text-color); font-weight: 500; box-shadow: 0 1px 3px rgba(0,0,0,.18); }
  .tile-foot { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: var(--secondary-text-color); }
  .info ha-icon { --mdc-icon-size: 18px; color: var(--secondary-text-color); }
  @media (max-width: 600px) {
    .content { padding: 12px; }
    .temp-value { font-size: 38px; }
    .grid { grid-template-columns: 1fr; }
  }
`;

if (!customElements.get("naked-connect-panel")) {
  customElements.define("naked-connect-panel", NakedPanel);
}
