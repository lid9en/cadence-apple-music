/* Cadence front end.
   Talks to Python over window.pywebview.api. No build step, no network,
   no external assets -- everything ships in the package. */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const app = $("#app");

let API = null;
let CFG = null;
let STATE = {};
let artToken = "";
let scrubbing = false;
let scrubValue = 0;
let statsDays = 0;
let lastTheme = "";

/* ---------------- helpers ---------------- */

const fmtTime = (s) => {
  s = Math.max(0, Math.floor(Number(s) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
};

const fmtHours = (n) => (n >= 10 ? Math.round(n) : Math.round(n * 10) / 10);

const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let toastTimer = null;
function toast(msg, ms = 2600) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), ms);
}

/* Deep-set a value like set(patch, "window.opacity", 0.8) */
function patchFor(path, value) {
  const parts = path.split(".");
  const root = {};
  let node = root;
  parts.forEach((p, i) => {
    if (i === parts.length - 1) node[p] = value;
    else node = node[p] = {};
  });
  return root;
}

function cfgGet(path, dflt) {
  return path.split(".").reduce(
    (o, k) => (o && o[k] !== undefined ? o[k] : undefined), CFG) ?? dflt;
}

async function saveCfg(path, value) {
  CFG = await API.update_settings(patchFor(path, value));
  applyConfigToDom();
  return CFG;
}

/* ---------------- config -> DOM ---------------- */

function applyConfigToDom() {
  if (!CFG) return;
  const d = CFG.display || {};
  app.dataset.layout = cfgGet("window.layout", "card");
  app.dataset.showart = d.show_artwork === false ? "0" : "1";
  app.dataset.showprogress = d.show_progress === false ? "0" : "1";
  app.dataset.showcontrols = d.show_controls === false ? "0" : "1";
  app.dataset.showmeta = d.show_meta === false ? "0" : "1";
  app.dataset.showbadge = d.show_source_badge === false ? "0" : "1";
  app.dataset.bgart = cfgGet("theme.background_art", true) ? "1" : "0";
  $("#lyrics").hidden = !(d.show_lyrics !== false && cfgGet("lyrics.enabled", true));

  let css = $("#user-css");
  if (!css) {
    css = document.createElement("style");
    css.id = "user-css";
    document.head.appendChild(css);
  }
  css.textContent = cfgGet("theme.custom_css", "") || "";
}

function refreshTheme() {
  const name = cfgGet("theme.name", "midnight");
  const theme = resolveTheme(name, CFG, STATE.palette);
  const sig = JSON.stringify(theme) + JSON.stringify(CFG && CFG.theme);
  if (sig !== lastTheme) {
    applyTheme(theme, CFG);
    lastTheme = sig;
  }
}

/* ---------------- player rendering ---------------- */

function setScrollable(el, text) {
  const wanted = text || "";
  if (el.dataset.text === wanted) return;
  el.dataset.text = wanted;
  el.textContent = wanted;
  el.classList.remove("marquee");
  if (cfgGet("display.marquee_long_titles", true)) {
    requestAnimationFrame(() => {
      if (el.scrollWidth > el.clientWidth + 4) {
        el.classList.add("marquee");
        el.innerHTML = `<span>${esc(wanted)}</span>`;
      }
    });
  }
}

async function refreshArtwork(token) {
  if (token === artToken) return;
  artToken = token;
  if (!token) {
    app.dataset.hasart = "0";
    return;
  }
  try {
    const { uri } = await API.get_artwork();
    if (uri) {
      $("#art").src = uri;
      $("#backdrop-img").src = uri;
      app.dataset.hasart = "1";
    } else {
      app.dataset.hasart = "0";
    }
  } catch (e) {
    app.dataset.hasart = "0";
  }
}

function renderLyrics(st) {
  const box = $("#lyrics");
  if (box.hidden) return;
  const ly = st.lyrics || {};
  const inner = $("#lyrics-inner");

  if (!ly.found || !ly.lines.length) {
    if (inner.dataset.key !== "none") {
      inner.dataset.key = "none";
      inner.innerHTML = `<div class="lyric plain">No lyrics found</div>`;
      inner.style.transform = "translateY(0)";
    }
    return;
  }

  const key = st.track_key + ":" + ly.lines.length;
  if (inner.dataset.key !== key) {
    inner.dataset.key = key;
    inner.innerHTML = ly.lines
      .map((l) => `<div class="lyric${ly.synced ? "" : " plain"}">${
        esc(l.text || " ")}</div>`)
      .join("");
  }

  if (!ly.synced) return;
  const nodes = inner.children;
  const idx = ly.active;
  if (inner.dataset.active === String(idx)) return;
  inner.dataset.active = String(idx);

  for (let i = 0; i < nodes.length; i++) nodes[i].classList.toggle("active", i === idx);

  if (idx >= 0 && nodes[idx]) {
    const target = nodes[idx];
    const offset = target.offsetTop + target.offsetHeight / 2 - box.clientHeight / 2;
    inner.style.transform = `translateY(${-Math.max(0, offset)}px)`;
  }
}

function renderPlayer(st) {
  app.dataset.status = st.playing ? "playing" : st.connected ? "paused" : "idle";

  $("#source-badge").textContent = st.connected ? st.source_label : "No session";
  $("#offline-note").hidden = st.connected;

  setScrollable($("#title"), st.title || "Nothing playing");
  setScrollable($("#artist"), st.artist || (st.connected ? "" : "Start Apple Music"));

  const m = st.meta || {};
  const bits = [st.album, m.genre, m.year].filter(Boolean);
  $("#meta").textContent = bits.join("  ·  ");

  const dur = Number(st.duration) || 0;
  const pos = scrubbing ? scrubValue : Number(st.position) || 0;
  const pct = dur > 0 ? Math.min(100, (pos / dur) * 100) : 0;
  $("#scrub-fill").style.width = pct + "%";
  $("#scrub-knob").style.left = pct + "%";
  $("#t-elapsed").textContent = fmtTime(pos);
  const style = cfgGet("display.time_style", "elapsed");
  $("#t-total").textContent =
    style === "remaining" ? "-" + fmtTime(Math.max(0, dur - pos)) : fmtTime(dur);
  $("#scrub").setAttribute("aria-disabled", st.can_seek ? "false" : "true");

  // These are <svg>, and `hidden` is an HTMLElement property that SVGElement
  // does not implement -- assigning it would silently set a JS expando and
  // never touch the attribute the CSS matches on.
  $("#icon-play").toggleAttribute("hidden", st.playing);
  $("#icon-pause").toggleAttribute("hidden", !st.playing);
  $('[data-ctl="next"]').disabled = st.connected && !st.can_next;
  $('[data-ctl="previous"]').disabled = st.connected && !st.can_prev;
  $('[data-ctl="shuffle"]').classList.toggle("on", !!st.shuffle);
  const rep = $('[data-ctl="repeat"]');
  rep.classList.toggle("on", st.repeat && st.repeat !== "none");
  rep.classList.toggle("track", st.repeat === "track");

  renderLyrics(st);
}

/* ---------------- polling ---------------- */

async function poll() {
  try {
    const st = await API.get_state();
    STATE = st;
    refreshTheme();
    if (app.dataset.view === "player") renderPlayer(st);
    refreshArtwork(st.art_token || "");
  } catch (e) {
    /* window closing */
  }
}

/* ---------------- navigation ---------------- */

async function setView(name) {
  app.dataset.view = name;
  $$("[data-nav]").forEach((b) => b.classList.toggle("on", b.dataset.nav === name));
  await API.set_view(name);
  if (name === "stats") loadStats();
  if (name === "fix") loadFix();
  if (name === "settings") renderSettings();
  if (name === "player") renderPlayer(STATE);
}

/* ---------------- stats ---------------- */

function rankList(rows, labelFn, valFn) {
  if (!rows || !rows.length) return `<p class="dim">Nothing yet.</p>`;
  const max = Math.max(...rows.map((r) => Number(valFn(r).raw) || 0), 1);
  return (
    `<div class="rank">` +
    rows
      .map((r, i) => {
        const v = valFn(r);
        const w = ((Number(v.raw) || 0) / max) * 100;
        return `<div class="rank-row">
          <div class="bar" style="width:${w}%"></div>
          <span class="i">${i + 1}</span>
          <span class="t">${labelFn(r)}</span>
          <span class="v">${esc(v.text)}</span>
        </div>`;
      })
      .join("") +
    `</div>`
  );
}

function heatmap(rows) {
  if (!rows || !rows.length) return `<p class="dim">Nothing yet.</p>`;
  const byDay = Object.fromEntries(rows.map((r) => [r.day, r.plays]));
  const max = Math.max(...rows.map((r) => r.plays), 1);
  const cols = [];
  const today = new Date();
  const start = new Date(today);
  start.setDate(start.getDate() - 25 * 7 - today.getDay());

  for (let w = 0; w < 26; w++) {
    let cells = "";
    for (let d = 0; d < 7; d++) {
      const day = new Date(start);
      day.setDate(start.getDate() + w * 7 + d);
      const iso = day.toISOString().slice(0, 10);
      const n = byDay[iso] || 0;
      const a = n ? 0.2 + 0.8 * (n / max) : 0;
      const bg = n
        ? `background:color-mix(in srgb, var(--accent) ${Math.round(a * 100)}%,
           var(--surface))`
        : "";
      cells += `<div class="heat-cell" style="${bg}" title="${iso}: ${n} plays"></div>`;
    }
    cols.push(`<div class="heat-col">${cells}</div>`);
  }
  return `<div class="heat">${cols.join("")}</div>`;
}

function hoursChart(buckets) {
  const max = Math.max(...buckets, 1);
  return (
    `<div class="hours">` +
    buckets
      .map((n, h) => {
        const pk = n === max && n > 0 ? " peak" : "";
        return `<div class="h${pk}" style="height:${(n / max) * 100}%"
                 title="${h}:00 — ${n} plays"></div>`;
      })
      .join("") +
    `</div>`
  );
}

async function loadStats() {
  const body = $("#stats-body");
  body.innerHTML = `<p class="dim">Loading…</p>`;
  const s = await API.get_stats(statsDays);
  const t = s.totals;

  body.innerHTML = `
    <div class="stat-grid">
      <div class="stat"><div class="n">${t.plays}</div><div class="k">Plays</div></div>
      <div class="stat"><div class="n">${fmtHours(t.hours)}</div>
        <div class="k">Hours</div></div>
      <div class="stat"><div class="n">${t.artists}</div>
        <div class="k">Artists</div></div>
      <div class="stat"><div class="n">${t.tracks}</div>
        <div class="k">Tracks</div></div>
      <div class="stat"><div class="n">${t.skips}</div><div class="k">Skips</div></div>
    </div>

    <h2>Top artists</h2>
    ${rankList(s.top_artists,
      (r) => esc(r.artist),
      (r) => ({ raw: r.plays, text: r.plays + " plays" }))}

    <h2>Top tracks</h2>
    ${rankList(s.top_tracks,
      (r) => `${esc(r.title)} <small>— ${esc(r.artist)}</small>`,
      (r) => ({ raw: r.plays, text: r.plays + " plays" }))}

    <h2>Top albums</h2>
    ${rankList(s.top_albums,
      (r) => `${esc(r.album)} <small>— ${esc(r.artist)}</small>`,
      (r) => ({ raw: r.plays, text: r.plays + " plays" }))}

    <h2>Last 26 weeks</h2>
    ${heatmap(s.heatmap)}

    <h2>By hour of day</h2>
    ${hoursChart(s.by_hour)}

    <h2>Recent</h2>
    ${rankList(s.recent.slice(0, 15),
      (r) => `${esc(r.title)} <small>— ${esc(r.artist)}</small>`,
      (r) => ({
        raw: 1,
        text: new Date(r.ts * 1000).toLocaleString([], {
          month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
        }),
      }))}

    <h2>Danger zone</h2>
    <button class="btn danger" id="clear-history">Delete all listening history</button>
  `;

  $("#clear-history").onclick = async () => {
    if (!confirm("Delete all listening history? This cannot be undone.")) return;
    await API.clear_history();
    toast("History cleared");
    loadStats();
  };
}

/* ---------------- fix panel ---------------- */

function findingHtml(f) {
  const act = f.remedy
    ? `<div class="acts">
         <button class="btn" data-remedy="${esc(f.remedy)}">
           ${esc(f.remedy_label || "Apply")}
         </button>
       </div>`
    : "";
  return `<div class="finding">
    <div class="top">
      <span class="sev ${esc(f.severity)}">${esc(f.severity)}</span>
      <span class="ttl">${esc(f.title)}</span>
    </div>
    <div class="det">${esc(f.detail)}</div>
    ${f.note ? `<div class="note">${esc(f.note)}</div>` : ""}
    ${act}
  </div>`;
}

async function loadFix() {
  const body = $("#fix-body");
  body.innerHTML = `<p class="dim">Scanning…</p>`;
  const r = await API.fix_scan();
  const sr = r.skip_rate || {};

  const storm = sr.storm
    ? `<div class="verdict"><b>Skip storm detected</b>
        ${sr.skips} tracks were abandoned in the last
        ${Math.round(sr.window_seconds / 60)} minutes, median
        ${sr.median_skip_seconds}s each. That matches the bug you are
        chasing. Run the live watch below to find out what is causing it.</div>`
    : "";

  body.innerHTML = `
    ${storm}
    <div class="finding">
      <div class="top"><span class="sev good">step 1</span>
        <span class="ttl">Watch it happen</span></div>
      <div class="det">
        Start playback in Apple Music, then run this. It records media-key
        traffic (media keys only — never ordinary typing) and every track
        change, and tells you whether something outside Apple Music is
        pressing next, or whether Apple Music is dropping tracks by itself.
      </div>
      <div class="acts">
        <button class="btn primary" id="fix-watch">Watch for 20 seconds</button>
        <span class="dim" id="fix-watch-status"></span>
      </div>
    </div>
    <div id="watch-result"></div>

    <div class="finding">
      <div class="top"><span class="sev good">step 2</span>
        <span class="ttl">Read Apple Music's own logs</span></div>
      <div class="det">
        Apple Music writes ETL traces of every playback attempt. This decodes
        the newest ones and pulls out the actual error codes, which is the
        only way to tell a refused store request apart from a local DRM
        problem. Takes up to a minute.
      </div>
      <div class="acts">
        <button class="btn primary" id="fix-logs">Analyse logs</button>
        <span class="dim" id="fix-logs-status"></span>
      </div>
    </div>
    <div id="logs-result"></div>

    <h2>What's on this machine</h2>
    <div class="det dim" style="margin-bottom:10px">
      Apple Music ${esc(r.version || "?")} · ${r.running ? "running" : "not running"}
    </div>
    ${r.findings.map(findingHtml).join("")}
  `;

  $("#fix-watch").onclick = runWatch;
  $("#fix-logs").onclick = runLogs;
  $$("#fix-body [data-remedy]").forEach((b) => {
    b.onclick = () => applyRemedy(b.dataset.remedy, b.textContent.trim());
  });
}

async function runLogs() {
  const btn = $("#fix-logs");
  const status = $("#fix-logs-status");
  btn.disabled = true;
  status.textContent = "Decoding traces…";

  let r;
  try {
    r = await API.fix_logs();
  } finally {
    btn.disabled = false;
    status.textContent = "";
  }

  if (!r.available) {
    $("#logs-result").innerHTML =
      `<div class="verdict"><b>No logs to read</b>${esc(r.reason || "")}</div>`;
    return;
  }

  const rows = r.findings.length
    ? r.findings.map((f) => `<div class="finding">
        <div class="top"><span class="sev error">${f.count}×</span>
          <span class="ttl">${esc(f.label)}</span></div>
        <div class="note">${esc(f.explanation)}</div>
        ${f.sample ? `<div class="det"><code>${esc(f.sample)}</code></div>` : ""}
      </div>`).join("")
    : `<p class="dim">No known error codes in the traces scanned.</p>`;

  const remedy =
    r.verdict === "store_request" || r.verdict === "identity"
      ? `<div class="acts">
           <button class="btn primary" data-remedy="reset_identity">
             Reset device identity + FairPlay</button>
           <button class="btn" data-remedy="restart_app">
             Restart Apple Music</button>
         </div>`
      : "";

  $("#logs-result").innerHTML = `<div class="verdict">
      <b>What the logs say</b>${esc(r.summary)}
      <div class="det" style="margin-top:8px">
        Scanned: ${esc((r.scanned || []).join(", ") || "nothing")}
      </div>
      ${remedy}
    </div>${rows}`;

  $$("#logs-result [data-remedy]").forEach((b) => {
    b.onclick = () => applyRemedy(b.dataset.remedy, b.textContent.trim());
  });
}

async function runWatch() {
  const btn = $("#fix-watch");
  const status = $("#fix-watch-status");
  btn.disabled = true;
  let left = 20;
  status.textContent = `Watching… ${left}s — make sure Apple Music is playing`;
  const iv = setInterval(() => {
    left -= 1;
    status.textContent = `Watching… ${Math.max(0, left)}s`;
  }, 1000);

  const r = await API.fix_watch(20);
  clearInterval(iv);
  btn.disabled = false;
  status.textContent = "";

  const titles = {
    internal: "Apple Music is dropping the tracks itself",
    external_software: "An application is pressing next",
    external_hardware: "A hardware device is pressing next",
    no_data: "Nothing to measure",
  };
  const remedy =
    r.verdict === "internal"
      ? `<div class="acts">
           <button class="btn primary" data-remedy="reset_playready">
             Reset PlayReady DRM store</button>
           <button class="btn" data-remedy="restart_app">Restart Apple Music</button>
         </div>`
      : "";

  $("#watch-result").innerHTML = `<div class="verdict">
    <b>${esc(titles[r.verdict] || r.verdict)}</b>
    ${esc(r.advice)}
    <div class="det" style="margin-top:9px">
      ${r.track_changes} track change(s) · ${r.next_key_events} next-key event(s)
      · ${r.changes_preceded_by_key} change(s) directly after a key
      ${r.median_gap_seconds != null
        ? ` · median gap ${r.median_gap_seconds}s` : ""}
      ${r.injected_keys ? ` · ${r.injected_keys} software-injected` : ""}
    </div>
    ${remedy}
  </div>`;

  $$("#watch-result [data-remedy]").forEach((b) => {
    b.onclick = () => applyRemedy(b.dataset.remedy, b.textContent.trim());
  });
}

const REMEDY_WARNINGS = {
  reset_identity:
    "This closes Apple Music and iTunes, then clears the shared Apple " +
    "device-identity (ADI) and FairPlay (SC Info) stores.\n\n" +
    "Timestamped backups are taken first. On next launch Apple Music " +
    "re-provisions with Apple; you may be asked to sign in again.\n\n" +
    "This is the fix for store-request error 7505 — not the PlayReady " +
    "cache.\n\nContinue?",
  install_apple_root:
    "This only shows you the instructions — Cadence will not install a " +
    "root certificate for you.\n\nContinue?",
  start_timesync:
    "Set the Windows Time service to start automatically and resync the " +
    "clock now? This may require administrator rights.",
  reset_playready:
    "This closes Apple Music and deletes its PlayReady DRM licence store.\n\n" +
    "A timestamped backup is taken first. Windows rebuilds the store the " +
    "next time you play a protected track; you may need to sign in again.\n\n" +
    "Continue?",
  clear_cache:
    "This closes Apple Music and clears its local cache.\n\n" +
    "A timestamped backup is taken first. The app rebuilds the cache on " +
    "next launch and will likely ask you to sign in again.\n\nContinue?",
  restart_app: "Close and reopen Apple Music now?",
  reregister:
    "This re-registers the Apple Music app package with Windows.\n\n" +
    "It does not delete your library. Continue?",
};

async function applyRemedy(remedy, label) {
  if (!confirm(REMEDY_WARNINGS[remedy] || `Run "${label}"?`)) return;
  toast("Working…", 8000);
  const r = await API.fix_apply(remedy, true);
  if (r.ok) {
    toast(r.message || "Done");
  } else {
    toast("Failed: " + (r.error || "unknown error"), 6000);
  }
  loadFix();
}

/* ---------------- settings ---------------- */

function fieldSwitch(label, desc, path, dflt) {
  const on = cfgGet(path, dflt) ? "on" : "";
  return `<div class="field">
    <div class="lbl"><b>${esc(label)}</b><p>${esc(desc)}</p></div>
    <div class="ctlbox">
      <button class="switch ${on}" data-toggle="${esc(path)}"></button>
    </div>
  </div>`;
}

function fieldRange(label, desc, path, min, max, step, dflt, fmt) {
  const v = cfgGet(path, dflt);
  return `<div class="field">
    <div class="lbl"><b>${esc(label)}</b><p>${esc(desc)}</p></div>
    <div class="ctlbox">
      <input type="range" data-range="${esc(path)}" min="${min}" max="${max}"
             step="${step}" value="${v}">
      <span class="val" data-valfor="${esc(path)}">${esc(
        fmt ? fmt(v) : v)}</span>
    </div>
  </div>`;
}

function fieldSelect(label, desc, path, options, dflt) {
  const v = cfgGet(path, dflt);
  return `<div class="field">
    <div class="lbl"><b>${esc(label)}</b><p>${esc(desc)}</p></div>
    <div class="ctlbox">
      <select data-select="${esc(path)}">
        ${options.map(([val, lbl]) =>
          `<option value="${esc(val)}"${val === v ? " selected" : ""}>${
            esc(lbl)}</option>`).join("")}
      </select>
    </div>
  </div>`;
}

function renderSettings() {
  const body = $("#settings-body");
  const themeName = cfgGet("theme.name", "midnight");

  const swatches = Object.entries(THEMES)
    .map(([key, t]) => `<div class="sw ${key === themeName ? "on" : ""}"
        data-theme="${key}">
        <div class="chips" style="background:${t.bg}">
          <i style="background:${t.bg2}"></i>
          <i style="background:${t.accent}"></i>
          <i style="background:${t.fg}"></i>
        </div>
        <div class="nm">${esc(t.label)}</div>
      </div>`)
    .join("");

  const hkRows = Object.entries(cfgGet("hotkeys.bindings", {}))
    .map(([action, combo]) => `<div class="field">
      <div class="lbl"><b>${esc(action.replace(/_/g, " "))}</b></div>
      <div class="ctlbox">
        <input class="hk-input" data-hk="${esc(action)}" readonly
               value="${esc(combo || "unset")}">
        <button class="btn" data-hk-clear="${esc(action)}">✕</button>
      </div>
    </div>`)
    .join("");

  const sources = (STATE.available_sources || [])
    .map((s) => [s.aumid, `${s.label} (${s.status})`]);

  body.innerHTML = `
    <h2>Theme</h2>
    <div class="theme-swatches">${swatches}</div>
    ${fieldSwitch("Adapt to artwork",
      "Blend the theme toward colours sampled from the current album art.",
      "theme.adaptive", true)}
    ${fieldRange("Adaptation strength", "How far to push toward the artwork.",
      "theme.adaptive_strength", 0, 1, 0.05, 0.85,
      (v) => Math.round(v * 100) + "%")}
    <div class="field">
      <div class="lbl"><b>Accent override</b>
        <p>Force one accent colour regardless of theme or artwork.</p></div>
      <div class="ctlbox">
        <input type="color" data-color="theme.accent_override"
               value="${esc(cfgGet("theme.accent_override", "") || "#fa2d48")}">
        <button class="btn" id="clear-accent">Clear</button>
      </div>
    </div>
    ${fieldSelect("Font", "Typeface for the whole interface.", "theme.font", [
      ["system", "System"], ["inter", "Inter"], ["rounded", "Rounded"],
      ["serif", "Serif"], ["mono", "Monospace"]], "system")}
    ${fieldRange("Text size", "Scales all type.", "theme.font_scale",
      0.75, 1.5, 0.05, 1, (v) => Math.round(v * 100) + "%")}
    ${fieldSwitch("Blurred artwork backdrop",
      "Show the album art behind the player.", "theme.background_art", true)}
    ${fieldRange("Backdrop blur", "", "theme.background_art_blur",
      0, 120, 2, 42, (v) => v + "px")}
    ${fieldRange("Backdrop opacity", "", "theme.background_art_opacity",
      0, 1, 0.05, 0.5, (v) => Math.round(v * 100) + "%")}

    <h2>Window</h2>
    ${fieldSelect("Layout", "Cycle at any time with the layout hotkey.",
      "window.layout", [["card", "Card"], ["bar", "Bar"],
      ["compact", "Compact"], ["art", "Artwork only"]], "card")}
    ${fieldSwitch("Always on top", "Keep the player above other windows.",
      "window.always_on_top", true)}
    ${fieldSwitch("Click-through",
      "Mouse clicks pass through to whatever is behind. Use the hotkey to " +
      "turn this back off — the window stops accepting clicks.",
      "window.click_through", false)}
    ${fieldRange("Opacity", "", "window.opacity", 0.25, 1, 0.05, 1,
      (v) => Math.round(v * 100) + "%")}
    ${fieldRange("Scale", "Resizes the player.", "window.scale",
      0.7, 1.6, 0.05, 1, (v) => Math.round(v * 100) + "%")}
    ${fieldRange("Corner radius", "", "window.corner_radius", 0, 48, 1, 18,
      (v) => v + "px")}
    ${fieldSwitch("Mica backdrop",
      "Windows 11 translucent material behind the player.",
      "window.transparent", false)}

    <h2>Shown in the player</h2>
    ${fieldSwitch("Artwork", "", "display.show_artwork", true)}
    ${fieldSwitch("Progress bar", "", "display.show_progress", true)}
    ${fieldSwitch("Transport controls", "", "display.show_controls", true)}
    ${fieldSwitch("Album / genre / year line", "", "display.show_meta", true)}
    ${fieldSwitch("Lyrics", "", "display.show_lyrics", true)}
    ${fieldSwitch("Source badge", "", "display.show_source_badge", true)}
    ${fieldSwitch("Scroll long titles", "", "display.marquee_long_titles", true)}
    ${fieldSelect("Time display", "", "display.time_style",
      [["elapsed", "Elapsed / total"], ["remaining", "Elapsed / remaining"]],
      "elapsed")}

    <h2>Source</h2>
    ${fieldSelect("Follow", "Which player Cadence controls.", "source.mode",
      [["apple", "Apple Music only"], ["any", "Whatever is playing"],
       ["pinned", "A specific app"]], "apple")}
    ${sources.length ? fieldSelect("Pinned app",
      "Used when Follow is set to a specific app.", "source.pinned_aumid",
      [["", "— choose —"], ...sources], "") : ""}
    ${fieldRange("Poll interval", "Lower is snappier, slightly more CPU.",
      "source.poll_ms", 80, 1000, 10, 200, (v) => v + "ms")}

    <h2>Lyrics</h2>
    ${fieldSwitch("Fetch lyrics", "From LRCLIB. No account needed.",
      "lyrics.enabled", true)}
    ${fieldRange("Timing offset", "Nudge synced lyrics earlier or later.",
      "lyrics.offset_ms", -3000, 3000, 50, 0, (v) => v + "ms")}

    <h2>Metadata</h2>
    ${fieldSwitch("Enrich from Apple's catalogue",
      "High-resolution artwork, genre and year via the public iTunes " +
      "Search API. No login required.", "enrich.enabled", true)}
    ${fieldRange("Artwork resolution", "", "enrich.artwork_size",
      200, 2000, 100, 1000, (v) => v + "px")}
    <div class="field">
      <div class="lbl"><b>Store region</b>
        <p>Two-letter country code; affects which catalogue is searched.</p></div>
      <div class="ctlbox">
        <input type="text" data-text="enrich.country" maxlength="2" size="4"
               value="${esc(cfgGet("enrich.country", "us"))}">
      </div>
    </div>

    <h2>History</h2>
    ${fieldSwitch("Log plays", "Stored locally in SQLite. Never uploaded.",
      "history.enabled", true)}
    ${fieldRange("Count a play after", "", "history.min_seconds",
      5, 300, 5, 60, (v) => v + "s")}
    ${fieldRange("…or this fraction of the track", "", "history.min_fraction",
      0.05, 1, 0.05, 0.5, (v) => Math.round(v * 100) + "%")}

    <h2>Play from your phone</h2>
    <p class="dim" style="margin-bottom:10px">
      Turns this PC into a Bluetooth speaker. Play Apple Music on your phone,
      pick this PC as the output, and the sound comes out here — the track
      and artwork show up in the player automatically.
    </p>
    <div id="phone-body"><p class="dim">Loading…</p></div>

    <h2>Hotkeys</h2>
    ${fieldSwitch("Enable global hotkeys", "", "hotkeys.enabled", true)}
    <div id="hk-warn"></div>
    ${hkRows}

    <h2>Custom CSS</h2>
    <p class="dim" style="margin-bottom:8px">
      Injected after everything else. Variables available:
      <code>--bg --bg2 --fg --muted --accent --radius</code>
    </p>
    <textarea id="custom-css" spellcheck="false"
      placeholder="#title { letter-spacing: -0.03em; }">${
        esc(cfgGet("theme.custom_css", ""))}</textarea>
    <div style="margin-top:8px"><button class="btn primary" id="save-css">
      Apply CSS</button></div>
  `;

  wireSettings();
  loadPhoneAudio();
  API.get_hotkey_status().then((s) => {
    const fails = Object.entries(s.failures || {});
    $("#hk-warn").innerHTML = fails.length
      ? `<div class="finding"><div class="top">
           <span class="sev warn">conflict</span>
           <span class="ttl">Some hotkeys could not be registered</span></div>
         <div class="det">${fails.map(([a, m]) =>
           `${esc(a)}: ${esc(m)}`).join("<br>")}</div></div>`
      : "";
  });
}

async function loadPhoneAudio() {
  const box = $("#phone-body");
  if (!box) return;
  let r;
  try {
    r = await API.phone_devices();
  } catch (e) {
    box.innerHTML = `<p class="dim">Bluetooth audio is unavailable here.</p>`;
    return;
  }

  const st = r.status || {};
  const devices = r.devices || [];

  const connected = st.connected
    ? `<div class="verdict"><b>Connected</b>
         ${esc(st.device_name || "device")} is playing through this PC.
         <div class="acts"><button class="btn" id="phone-disconnect">
           Disconnect</button></div>
       </div>`
    : "";

  const list = devices.length
    ? devices.map((d) => `<div class="field">
        <div class="lbl"><b>${esc(d.name)}</b>
          <p>${d.enabled ? "Ready" : "Paired but not reachable right now"}</p>
        </div>
        <div class="ctlbox">
          <button class="btn${d.enabled ? " primary" : ""}"
            data-phone-connect="${esc(d.id)}" data-phone-name="${esc(d.name)}"
            ${d.enabled ? "" : "disabled"}>Connect</button>
        </div>
      </div>`).join("")
    : `<div class="finding">
         <div class="top"><span class="sev warn">no devices</span>
           <span class="ttl">No phone is paired with this PC yet</span></div>
         <div class="det">
           1. On this PC: Settings &rsaquo; Bluetooth &amp; devices &rsaquo;
              Add device &rsaquo; Bluetooth.<br>
           2. On your phone: Settings &rsaquo; Bluetooth, and pair with this PC.<br>
           3. Come back here and press Refresh — your phone will be listed.
         </div>
       </div>`;

  const err = st.error
    ? `<p class="dim" style="margin-top:8px">${esc(st.error)}</p>` : "";

  box.innerHTML = connected + list + err +
    `<div style="margin-top:10px">
       <button class="btn" id="phone-refresh">Refresh device list</button>
     </div>`;

  $("#phone-refresh").onclick = loadPhoneAudio;
  const dis = $("#phone-disconnect");
  if (dis) {
    dis.onclick = async () => {
      await API.phone_disconnect();
      toast("Disconnected");
      loadPhoneAudio();
    };
  }
  $$("#phone-body [data-phone-connect]").forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      b.textContent = "Connecting…";
      const res = await API.phone_connect(b.dataset.phoneConnect,
                                          b.dataset.phoneName);
      toast(res.message || (res.ok ? "Connected" : "Could not connect"), 6000);
      loadPhoneAudio();
    };
  });
}

function wireSettings() {
  $$("[data-toggle]").forEach((el) => {
    el.onclick = async () => {
      const path = el.dataset.toggle;
      const next = !cfgGet(path, false);
      el.classList.toggle("on", next);
      await saveCfg(path, next);
      if (path === "window.click_through" && next) {
        toast("Click-through on — press the hotkey to turn it off", 4200);
      }
    };
  });

  $$("[data-range]").forEach((el) => {
    const path = el.dataset.range;
    el.oninput = () => {
      const out = $(`[data-valfor="${CSS.escape(path)}"]`);
      if (out) {
        const v = Number(el.value);
        out.textContent = /opacity|strength|fraction|scale/.test(path)
          ? Math.round(v * 100) + "%"
          : /blur|radius/.test(path) ? v + "px"
          : /_ms$/.test(path) ? v + "ms"
          : /seconds/.test(path) ? v + "s"
          : /size/.test(path) ? v + "px"
          : v;
      }
    };
    el.onchange = () => saveCfg(path, Number(el.value));
  });

  $$("[data-select]").forEach((el) => {
    el.onchange = () => saveCfg(el.dataset.select, el.value);
  });

  $$("[data-text]").forEach((el) => {
    el.onchange = () => saveCfg(el.dataset.text, el.value.trim().toLowerCase());
  });

  $$("[data-color]").forEach((el) => {
    el.onchange = () => saveCfg(el.dataset.color, el.value);
  });

  $$("[data-theme]").forEach((el) => {
    el.onclick = async () => {
      $$("[data-theme]").forEach((x) => x.classList.remove("on"));
      el.classList.add("on");
      await saveCfg("theme.name", el.dataset.theme);
      lastTheme = "";
      refreshTheme();
    };
  });

  const clearAccent = $("#clear-accent");
  if (clearAccent) {
    clearAccent.onclick = async () => {
      await saveCfg("theme.accent_override", "");
      lastTheme = "";
      refreshTheme();
      toast("Accent follows the theme again");
    };
  }

  const saveCss = $("#save-css");
  if (saveCss) {
    saveCss.onclick = async () => {
      await saveCfg("theme.custom_css", $("#custom-css").value);
      toast("CSS applied");
    };
  }

  $$("[data-hk]").forEach((el) => captureHotkey(el));
  $$("[data-hk-clear]").forEach((el) => {
    el.onclick = async () => {
      const action = el.dataset.hkClear;
      await saveCfg(`hotkeys.bindings.${action}`, "");
      renderSettings();
    };
  });
}

function captureHotkey(input) {
  input.onclick = () => {
    input.classList.add("capturing");
    input.value = "press keys…";

    const onKey = async (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "Escape") return stop(null);

      const mods = [];
      if (e.ctrlKey) mods.push("Ctrl");
      if (e.altKey) mods.push("Alt");
      if (e.shiftKey) mods.push("Shift");
      if (e.metaKey) mods.push("Win");
      if (["Control", "Alt", "Shift", "Meta"].includes(e.key)) return;
      if (!mods.length) {
        input.value = "needs a modifier";
        return;
      }
      const key = e.key.length === 1 ? e.key.toUpperCase()
        : e.key.replace("Arrow", "");
      stop([...mods, key].join("+"));
    };

    const stop = async (combo) => {
      window.removeEventListener("keydown", onKey, true);
      input.classList.remove("capturing");
      if (combo) {
        await saveCfg(`hotkeys.bindings.${input.dataset.hk}`, combo);
        input.value = combo;
        const s = await API.get_hotkey_status();
        if (s.failures && s.failures[input.dataset.hk]) {
          toast(s.failures[input.dataset.hk], 4000);
        } else {
          toast("Hotkey set");
        }
      } else {
        input.value = cfgGet(`hotkeys.bindings.${input.dataset.hk}`, "") || "unset";
      }
    };

    window.addEventListener("keydown", onKey, true);
  };
}

/* ---------------- wiring ---------------- */

function wireStatic() {
  $$("[data-nav]").forEach((b) => (b.onclick = () => setView(b.dataset.nav)));
  $$("[data-win]").forEach((b) => (b.onclick = () => API.window_action(b.dataset.win)));

  $$("[data-ctl]").forEach((b) => {
    b.onclick = async () => {
      const a = b.dataset.ctl;
      if (a === "shuffle") await API.control("shuffle", !STATE.shuffle);
      else if (a === "repeat") {
        const order = ["none", "list", "track"];
        const next = order[(order.indexOf(STATE.repeat || "none") + 1) % 3];
        await API.control("repeat", next);
      } else {
        const r = await API.control(a);
        if (!r.ok && a !== "play_pause") toast("Apple Music refused that command");
      }
      poll();
    };
  });

  $("#stats-range").onclick = (e) => {
    const b = e.target.closest("[data-days]");
    if (!b) return;
    $$("#stats-range button").forEach((x) => x.classList.remove("on"));
    b.classList.add("on");
    statsDays = Number(b.dataset.days);
    loadStats();
  };

  $("#fix-rescan").onclick = loadFix;
  $("#settings-reset").onclick = async () => {
    if (!confirm("Reset every setting to its default?")) return;
    CFG = await API.reset_settings();
    applyConfigToDom();
    lastTheme = "";
    refreshTheme();
    renderSettings();
    toast("Settings reset");
  };
  $("#settings-open-file").onclick = () => {
    API.open_settings_folder && API.open_settings_folder();
  };

  // scrubbing
  const scrub = $("#scrub");
  const posFromEvent = (e) => {
    const r = scrub.getBoundingClientRect();
    const p = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    return p * (Number(STATE.duration) || 0);
  };
  scrub.addEventListener("pointerdown", (e) => {
    if (!STATE.can_seek || !STATE.duration) return;
    scrubbing = true;
    scrubValue = posFromEvent(e);
    scrub.classList.add("dragging");
    scrub.setPointerCapture(e.pointerId);
    renderPlayer(STATE);
  });
  scrub.addEventListener("pointermove", (e) => {
    if (!scrubbing) return;
    scrubValue = posFromEvent(e);
    renderPlayer(STATE);
  });
  scrub.addEventListener("pointerup", async (e) => {
    if (!scrubbing) return;
    scrubbing = false;
    scrub.classList.remove("dragging");
    const r = await API.control("seek", scrubValue);
    if (!r.ok) toast("This player does not allow seeking");
    poll();
  });

  document.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
    if (e.key === " ") { e.preventDefault(); API.control("play_pause").then(poll); }
    if (e.key === "ArrowRight" && e.ctrlKey) API.control("next").then(poll);
    if (e.key === "ArrowLeft" && e.ctrlKey) API.control("previous").then(poll);
    if (e.key === "Escape" && app.dataset.view !== "player") setView("player");
  });
}

/* ---------------- boot ---------------- */

async function boot() {
  API = window.pywebview.api;
  CFG = await API.get_settings();
  applyConfigToDom();
  wireStatic();
  await poll();
  refreshTheme();

  // The backend decides the starting view (--view on the command line);
  // the markup always begins on "player", so adopt whatever it says.
  await setView(STATE.view || "player");

  setInterval(poll, 250);
}

if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot);
