/* The Gallery — broadcast surface.
   Server ticks at 10 Hz; render runs at display rate and interpolates between
   frames so the map moves like television rather than a status page. */
(function () {
  "use strict";

  var reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  var $ = function (id) { return document.getElementById(id); };

  var el = {
    circuit: $("circuit"), lap: $("lap"),
    chipSource: $("chipSource"), chipModel: $("chipModel"), chipGrafana: $("chipGrafana"),
    tower: $("tower"), towerCount: $("towerCount"), selNote: $("selNote"),
    mapName: $("mapName"), mapSrc: $("mapSrc"), flash: $("flash"),
    onair: $("onair"), oaNum: $("oaNum"), oaShot: $("oaShot"), oaTier: $("oaTier"),
    oaAgainst: $("oaAgainst"), oaLine: $("oaLine"), oaHold: $("oaHold"),
    oaConf: $("oaConf"), oaGap: $("oaGap"), oaScore: $("oaScore"),
    battles: $("battles"), log: $("log"), logHint: $("logHint"),
    fLatency: $("fLatency"), fScored: $("fScored"), fPushed: $("fPushed"),
    sheet: $("sheet"), sheetX: $("sheetX"), sheetEyebrow: $("sheetEyebrow"),
    sheetTitle: $("sheetTitle"), sheetBody: $("sheetBody")
  };

  /* ───────── state ───────── */
  var outline = [], corners = [], drsZones = [], bounds = [0, 0, 1, 1];
  var prev = null, next = null, tPrev = 0, tNext = 0;
  var trails = {}, lastCutKey = "", latest = null;
  var selected = null;      // driver number the operator is tracking
  var circuitRev = -1;
  var logFrozen = false;    // hover-freeze on the decision log

  /* ───────── socket ───────── */
  function connect() {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var sock = new WebSocket(proto + "://" + location.host + "/ws");
    sock.onmessage = function (ev) {
      var msg = JSON.parse(ev.data);
      if (msg.type === "circuit") {
        outline = msg.outline || [];
        corners = msg.corners || [];
        drsZones = msg.drs_zones || [];
        if (msg.bounds && msg.bounds.length === 4) bounds = msg.bounds;
        el.mapName.textContent = msg.name || "—";
        return;
      }
      if (msg.type !== "state") return;
      prev = next; tPrev = tNext;
      next = msg; tNext = performance.now();
      if (!prev) { prev = next; tPrev = tNext - 100; }
      latest = msg;
      paint(msg);
    };
    sock.onclose = function () { setTimeout(connect, 1200); };
    sock.onerror = function () { sock.close(); };
  }

  /* ───────── helpers ───────── */
  function chip(node, text, cls) {
    node.className = "chip " + cls;
    node.querySelector("span").textContent = text;
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function isModelTier(t) { return t === "adk" || t === "genai"; }

  function select(num) {
    selected = (selected === num) ? null : num;
    // Only speaks when there is something to say. The idle prompt was
    // permanent furniture telling you what you had already worked out.
    el.selNote.hidden = !selected;
    el.selNote.textContent = selected ? "tracking car " + selected + " — click again to clear" : "";
    el.selNote.classList.toggle("active", !!selected);
    if (latest) paint(latest);
  }

  /* ───────── panels ───────── */
  function paint(s) {
    el.circuit.textContent = s.circuit || "—";
    el.lap.textContent = s.lap + " / " + (s.total_laps || "—");
    el.mapSrc.textContent = s.source === "fastf1" ? "real telemetry · fastf1" : "synthetic replay";
    el.towerCount.textContent = s.cars.length + " cars";
    el.fScored.textContent = (s.pairings_scored || 0).toLocaleString();
    el.fLatency.textContent = s.director.latency_ms ? s.director.latency_ms + " ms" : "—";
    el.fPushed.textContent = (s.grafana.pushed || 0).toLocaleString();

    chip(el.chipSource, s.source === "fastf1" ? "fastf1 · real" : "synthetic",
         s.source === "fastf1" ? "on" : "off");

    // Only adk/genai are model decisions. Everything else is deterministic and
    // must read as degraded rather than borrowing the model's colour.
    var d = s.director, model = isModelTier(d.tier);
    var label = d.blocked ? "quota · backing off"
      : model ? d.tier + " · " + d.model
      : d.tier === "timeout" ? "fallback · too slow"
      : (d.configured ? "standby" : "no credentials");
    chip(el.chipModel, label, d.blocked ? "warn" : model ? "ai" : "warn");

    chip(el.chipGrafana,
         s.grafana.live ? "grafana live" : (s.grafana.enabled ? "grafana unreachable" : "grafana off"),
         s.grafana.live ? "on" : "warn");

    paintTower(s); paintOnAir(s); paintBattles(s); paintMetrics(s); paintViews(s); paintFocus(s); paintTransport(s); refreshCircuit(s);
    if (!logFrozen) paintLog(s);
  }

  /* Rows are built once and updated in place, never re-created.
     Rebuilding the tower with innerHTML ten times a second destroyed the node
     between mousedown and mouseup, so clicks were swallowed at random and a
     driver would only select after several tries. Order is expressed with the
     CSS order property so nothing moves in the DOM either. */
  var rowEls = {};

  function buildRow(num) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "row";
    b.setAttribute("data-car", num);
    b.innerHTML = '<span class="p"></span><span class="bar"></span>'
      + '<span class="code"></span><span class="tyre"></span>'
      + '<span class="gap"></span>'
      + '<span class="delta"><span class="dbar"><i></i></span><span class="arw"></span></span>';
    b._p = b.querySelector(".p"); b._bar = b.querySelector(".bar");
    b._code = b.querySelector(".code"); b._tyre = b.querySelector(".tyre");
    b._gap = b.querySelector(".gap"); b._dbar = b.querySelector(".dbar i");
    b._arw = b.querySelector(".arw");
    el.tower.appendChild(b);
    rowEls[num] = b;
    return b;
  }

  function paintTower(s) {
    var live = s.on_air ? s.on_air.num : null;
    var fighting = {};
    (s.battles || []).forEach(function (b) {
      if (b.score >= 0.4) { fighting[b.ahead_num] = 1; fighting[b.behind_num] = 1; }
    });

    var seen = {};
    for (var i = 0; i < s.cars.length; i++) {
      var c = s.cars[i];
      seen[c.num] = 1;
      var r = rowEls[c.num] || buildRow(c.num);
      r.style.order = c.pos;

      var lead = c.pos === 1;
      var gapTxt = lead ? "LEADER" : "+" + c.gap.toFixed(3);
      var gapCls = "gap" + (lead ? "" : c.gap <= 1.0 ? " drs" : c.gap <= 2.2 ? " close" : "");
      var mag = lead ? 0 : Math.max(0, Math.min(1, 1 - c.gap / 3));
      var magCol = c.gap <= 1.0 ? "var(--green)" : c.gap <= 2.2 ? "var(--amber)" : "var(--ink-3)";
      var arw = "flat", gly = "\u2022";
      if (!lead && c.closing > 0.006) { arw = "closing"; gly = "\u25B2"; }
      else if (!lead && c.closing < -0.006) { arw = "widening"; gly = "\u25BC"; }

      if (r._p.textContent !== String(c.pos)) r._p.textContent = c.pos;
      if (r._bar.style.background !== c.color) r._bar.style.background = c.color;
      if (r._code.textContent !== c.code) r._code.textContent = c.code;
      var tyre = '<b class="' + c.tyre + '">' + (c.tyre || "\u2014").slice(0, 1) + "</b>" + c.age + "L";
      if (r._tyre.innerHTML !== tyre) r._tyre.innerHTML = tyre;
      if (r._gap.textContent !== gapTxt) r._gap.textContent = gapTxt;
      if (r._gap.className !== gapCls) r._gap.className = gapCls;
      r._dbar.style.width = Math.round(mag * 100) + "%";
      r._dbar.style.background = magCol;
      if (r._arw.textContent !== gly) r._arw.textContent = gly;
      var arwCls = "arw " + arw;
      if (r._arw.className !== arwCls) r._arw.className = arwCls;

      var cls = "row"
        + (c.num === live ? " live" : "")
        + (fighting[c.num] ? " fight" : "")
        + (selected === c.num ? " sel" : "")
        + (selected && selected !== c.num ? " dim" : "");
      if (r.className !== cls) r.className = cls;
    }

    Object.keys(rowEls).forEach(function (num) {
      if (!seen[num]) { rowEls[num].remove(); delete rowEls[num]; }
    });
    el.towerCount.textContent = s.cars.length + " cars";
  }

  function paintOnAir(s) {
    var a = s.on_air;
    if (!a) return;
    var car = (s.cars || []).filter(function (c) { return c.num === a.num; })[0];
    var key = a.num + "|" + a.line;
    if (key !== lastCutKey) {
      lastCutKey = key;
      if (!reduced) { el.flash.classList.remove("go"); void el.flash.offsetWidth; el.flash.classList.add("go"); }
    }
    var model = isModelTier(a.tier);
    el.onair.classList.toggle("hot", !!a.hot && model);
    el.onair.classList.toggle("fallback", !model);
    el.oaNum.textContent = a.num;
    el.oaNum.style.background = car ? car.color : "var(--ink-3)";
    el.oaShot.textContent = a.shot || "ONBOARD";
    el.oaTier.textContent = model ? a.tier : (a.tier === "timeout" ? "deterministic · timeout" : "deterministic");
    el.oaTier.classList.toggle("fallback", !model);
    el.oaAgainst.textContent = "P" + a.position + " · " + a.code + " defending from " + a.against;
    el.oaLine.textContent = a.line || "—";
    el.oaHold.textContent = (s.hold || 0).toFixed(1) + "s";
    el.oaConf.textContent = a.confidence ? Math.round(a.confidence * 100) + "%" : "—";
    el.oaGap.textContent = a.gap.toFixed(2) + "s";
    el.oaScore.textContent = a.score.toFixed(2);
  }

  function paintBattles(s) {
    var b = s.battles || [];
    if (!b.length) { el.battles.innerHTML = '<div class="battle-why">no position within attack range</div>'; return; }
    var live = s.on_air ? s.on_air.num : null;
    el.battles.innerHTML = b.slice(0, 5).map(function (x) {
      var hot = x.score >= 0.55;
      var col = x.ahead_num === live ? "var(--purple)" : (hot ? "var(--yellow)" : "var(--ink-3)");
      var sel = selected === x.ahead_num || selected === x.behind_num;
      return '<button type="button" class="battle' + (sel ? " sel" : "") + '" data-car="' + esc(x.ahead_num) + '">'
        + '<div class="battle-top"><span class="pos">P' + x.position + "</span>"
        + '<span class="vs">' + esc(x.ahead) + " ◂ " + esc(x.behind) + "</span>"
        + '<span class="sc' + (hot ? " hot" : "") + '">' + x.score.toFixed(2) + "</span></div>"
        + '<div class="battle-bar"><i style="width:' + Math.round(Math.min(1, x.score) * 100) + "%;background:" + col + '"></i></div>'
        + '<div class="battle-why">' + esc(x.why) + "</div></button>";
    }).join("");
  }

  function clock(t) {
    var m = Math.floor(t / 60), sec = Math.floor(t % 60);
    return String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0");
  }

  function paintLog(s) {
    var entries = (s.log || []).slice(0, 40);
    // Only dim non-matching entries when the selected car actually appears
    // somewhere in the list. Muting all of them reads as a broken panel.
    var anyMatch = selected && entries.some(function (e) {
      return e.text.indexOf("car " + selected) !== -1;
    });
    el.log.innerHTML = entries.map(function (e) {
      var kind = e.kind, tier = e.tier;
      var cls = tier ? (isModelTier(tier) ? tier : tier === "timeout" ? "timeout" : "det")
                     : (kind === "hold" ? "hold" : kind === "release" ? "release" : "sys");
      var text = tier ? (isModelTier(tier) ? tier.toUpperCase() : tier === "timeout" ? "TIMEOUT" : "DET")
                      : (kind === "hold" ? "HOLD" : kind === "release" ? "REL" : "SYS");
      // When tracking a driver, dim entries that do not mention them.
      var muted = anyMatch && e.text.indexOf("car " + selected) === -1;
      return '<li class="' + (muted ? "muted" : "") + '">'
        + '<span class="t">' + clock(e.t) + "</span>"
        + '<span class="badge ' + cls + '">' + text + "</span>"
        + '<span class="txt ' + esc(kind) + '">' + esc(e.text) + "</span></li>";
    }).join("");
  }

  /* ───────── interactions ───────── */
  el.tower.addEventListener("click", function (e) {
    var row = e.target.closest("[data-car]");
    if (row) select(row.getAttribute("data-car"));
  });
  el.battles.addEventListener("click", function (e) {
    var b = e.target.closest("[data-car]");
    if (b) select(b.getAttribute("data-car"));
  });

  // Freeze the log while the pointer is inside it, so a decision can be read
  // without the list reordering underneath the cursor.
  el.log.addEventListener("mouseenter", function () {
    logFrozen = true; el.logHint.textContent = "paused"; el.logHint.classList.add("frozen");
  });
  el.log.addEventListener("mouseleave", function () {
    logFrozen = false; el.logHint.textContent = "live"; el.logHint.classList.remove("frozen");
    if (latest) paintLog(latest);
  });

  /* ───────── transport ───────── */
  var seeking = false;

  function control(action, value) {
    var q = value === undefined ? "" : "?value=" + encodeURIComponent(value);
    return fetch("/api/control/" + action + q, { method: "POST" })
      .then(function (r) { return r.json(); })
      .catch(function () { return { ok: false }; });
  }

  $("tPlay").addEventListener("click", function () {
    var paused = latest && latest.transport && latest.transport.paused;
    control(paused ? "resume" : "pause");
  });

  [].forEach.call(document.querySelectorAll("[data-speed]"), function (b) {
    b.addEventListener("click", function () {
      control("speed", b.getAttribute("data-speed"));
    });
  });

  var tpPop = $("tpPop"), tpToggle = $("tpToggle");
  function tpOpen(open) {
    tpPop.hidden = !open;
    tpToggle.setAttribute("aria-expanded", open ? "true" : "false");
  }
  tpToggle.addEventListener("click", function (e) {
    e.stopPropagation();
    tpOpen(tpPop.hidden);
  });
  tpPop.addEventListener("click", function (e) { e.stopPropagation(); });
  document.addEventListener("click", function () { if (!tpPop.hidden) tpOpen(false); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") tpOpen(false); });

  var raceEl = $("tRace");
  raceEl.addEventListener("change", function () {
    raceEl.disabled = true;
    // Selection belongs to the old field; carrying it over rings a car that
    // may not be in this race.
    if (selected) select(selected);
    trails = {};
    fetch("/api/control/race/" + encodeURIComponent(raceEl.value), { method: "POST" })
      .catch(function () {})
      .then(function () { raceEl.disabled = false; });
  });

  function paintRaces(s) {
    if (!s.races || raceEl.options.length === s.races.length) return;
    raceEl.innerHTML = s.races.map(function (r) {
      return '<option value="' + r.id + '">' + esc(r.label) + " — " + esc(r.note) + "</option>";
    }).join("");
  }

  function refreshCircuit(s) {
    if (s.circuit_rev === circuitRev) return;
    circuitRev = s.circuit_rev;
    fetch("/api/circuit").then(function (r) { return r.json(); }).then(function (d) {
      outline = d.outline || [];
      corners = d.corners || [];
      drsZones = d.drs_zones || [];
      if (d.bounds && d.bounds.length === 4) bounds = d.bounds;
      trails = {};
      el.mapName.textContent = d.name || "—";
    }).catch(function () {});
  }

  var lapEl = $("tLap");
  lapEl.addEventListener("input", function () {
    seeking = true;
    $("tLapV").textContent = lapEl.value;
  });
  lapEl.addEventListener("change", function () {
    control("seek", lapEl.value).then(function () { seeking = false; });
  });

  function paintTransport(s) {
    var tr = s.transport || { paused: false, speed: 3 };
    var b = $("tPlay");
    b.textContent = tr.paused ? "▶ Resume" : "❚❚ Pause";
    b.classList.toggle("paused", !!tr.paused);
    // Surface a non-default state on the closed button, so a paused replay is
    // never a mystery hidden inside a popover nobody opened.
    tpToggle.textContent = tr.paused ? "▶ Paused" : (tr.speed !== 3 ? "⚙ " + tr.speed + "×" : "⚙ Replay");
    tpToggle.classList.toggle("paused", !!tr.paused);
    [].forEach.call(document.querySelectorAll("[data-speed]"), function (x) {
      x.classList.toggle("on", Number(x.getAttribute("data-speed")) === tr.speed);
    });
    paintRaces(s);
    if (tr.race_id && raceEl.value !== tr.race_id) raceEl.value = tr.race_id;
    if (!seeking) {
      lapEl.max = s.total_laps || 51;
      lapEl.value = s.lap;
      $("tLapV").textContent = s.lap;
    }
  }

  /* ───────── driver focus card ───────── */
  function paintFocus(s) {
    var box = $("focus");
    if (!selected) { box.hidden = true; return; }
    var c = (s.cars || []).filter(function (x) { return x.num === selected; })[0];
    if (!c) { box.hidden = true; return; }
    box.hidden = false;

    $("fNum").textContent = c.num;
    $("fNum").style.background = c.color;
    $("fCode").textContent = c.code;
    $("fTeam").textContent = c.team || "—";
    $("fPos").textContent = "P" + c.pos;
    $("fSpeed").textContent = c.speed ? Math.round(c.speed) + " km/h" : "—";

    var ahead = $("fAhead");
    ahead.textContent = c.pos === 1 ? "LEADER" : "+" + c.gap.toFixed(3);
    ahead.className = "fv " + (c.pos === 1 ? "" : c.gap <= 1 ? "good" : c.gap <= 2.2 ? "warn" : "");

    var behind = $("fBehind");
    behind.textContent = c.gap_behind ? "−" + c.gap_behind.toFixed(3) : "—";
    behind.className = "fv " + (c.gap_behind && c.gap_behind <= 1 ? "warn" : "");

    $("fTyre").textContent = (c.tyre || "—").slice(0, 1) + " · " + c.age + "L";

    var cl = $("fClosing");
    if (c.pos === 1) { cl.textContent = "—"; cl.className = "fv"; }
    else if (c.closing > 0.006) { cl.textContent = "▲ " + c.closing.toFixed(3); cl.className = "fv good"; }
    else if (c.closing < -0.006) { cl.textContent = "▼ " + Math.abs(c.closing).toFixed(3); cl.className = "fv warn"; }
    else { cl.textContent = "• holding"; cl.className = "fv"; }

    var onAir = s.on_air && s.on_air.num === selected;
    var inBattle = (s.battles || []).filter(function (b) {
      return b.ahead_num === selected || b.behind_num === selected;
    })[0];
    var st = $("fState");
    st.classList.toggle("onair", !!onAir);
    st.textContent = onAir ? "◉ on air — " + (s.on_air.shot || "onboard")
      : inBattle ? "in a fight for P" + inBattle.position + " · score " + inBattle.score.toFixed(2)
      : "clear air";

    var mine = (s.log || []).filter(function (e) {
      return e.text.indexOf("car " + selected) !== -1 || e.text.indexOf("(" + c.code + ")") !== -1;
    }).slice(0, 4);
    $("fLog").innerHTML = mine.length
      ? mine.map(function (e) { return "<div>" + clock(e.t) + " · " + esc(e.text) + "</div>"; }).join("")
      : "<div>no decisions involving this car yet</div>";
  }

  $("fClose").addEventListener("click", function () { if (selected) select(selected); });

  /* ───────── view tabs ───────── */
  document.getElementById("tabs").addEventListener("click", function (e) {
    var b = e.target.closest("[data-view]");
    if (!b) return;
    [].forEach.call(document.querySelectorAll(".tab"), function (x) { x.classList.remove("on"); });
    b.classList.add("on");
    document.body.setAttribute("data-view", b.getAttribute("data-view"));
    if (latest) paintViews(latest);
  });
  document.body.setAttribute("data-view", "control");

  function paintViews(s) {
    var link = $("dashLink");
    if (link) {
      if (s.grafana.url) { link.href = s.grafana.url; link.textContent = "Open the dashboard →"; }
      else { link.removeAttribute("href"); link.textContent = "dashboard not provisioned"; }
    }
    var kv = $("monKv");
    if (kv) {
      kv.innerHTML = [
        ["grafana reachable", s.grafana.live ? "yes" : "no"],
        ["series pushed", (s.grafana.pushed || 0).toLocaleString()],
        ["push error", s.grafana.push_error || "none"],
        ["pairings scored", (s.pairings_scored || 0).toLocaleString()],
        ["director tier", s.director.tier],
        ["director latency", s.director.latency_ms + " ms"],
        ["race source", s.source],
        ["lap", s.lap + " / " + s.total_laps]
      ].map(function (r) { return "<dt>" + r[0] + "</dt><dd>" + esc(r[1]) + "</dd>"; }).join("");
    }
  }

  /* ───────── metric strip ───────── */
  function paintMetrics(s) {
    var d = s.director, model = isModelTier(d.tier);
    var q = $("mQuota"), qs = $("mQuotaSub");
    if (d.blocked) { q.textContent = "BACKING OFF"; q.className = "m-v warn"; qs.textContent = d.blocked; }
    else { q.textContent = "OK"; q.className = "m-v good"; qs.textContent = "within quota"; }

    var lat = $("mLat");
    lat.textContent = d.latency_ms ? (d.latency_ms / 1000).toFixed(2) + "s" : "—";
    lat.className = "m-v " + (!d.latency_ms ? "" : d.latency_ms < 2500 ? "good" : "warn");

    var tier = $("mTier"), ts = $("mTierSub");
    tier.textContent = model ? "GEMINI" : "DETERMINISTIC";
    tier.className = "m-v " + (model ? "live" : "warn");
    ts.textContent = model ? d.model : (d.tier === "timeout" ? "model exceeded deadline" : "fallback active");

    $("mPushed").textContent = (s.grafana.pushed || 0).toLocaleString();
  }

  /* ───────── explain sheet ───────── */
  var EXPLAIN = {
    source: { eyebrow: "Data source", title: "Where the race comes from",
      body: "<p>Position and timing telemetry for a real Grand Prix, loaded through <code>FastF1</code>. Car coordinates, lap numbers, tyre compound and age are all as recorded on the day.</p><p>The circuit outline is built from one clean lap of position data, and every gap in the timing tower is derived from arc-length along that line.</p><p>If the telemetry cannot be reached the app falls back to a deterministic synthetic race and says so here rather than pretending.</p>" },
    director: { eyebrow: "Agent", title: "The director",
      body: "<p>A Gemini agent running on the Google Agent Development Kit. It is given the ranked shortlist of contested positions and asked the one thing the arithmetic cannot answer: which of these is worth putting on air, and how to justify it on commentary.</p><p>It calls a <code>recent_airtime</code> tool before deciding, so coverage spreads across the field instead of pooling on the leaders.</p><p>Tiers: <code>adk</code> and <code>genai</code> are model decisions. <code>timeout</code> and <code>heuristic</code> are deterministic fallbacks, shown in amber so a fallback is never mistaken for the model choosing.</p>" },
    tension: { eyebrow: "Scoring", title: "Tension scorer",
      body: "<p>Plain Python, ten times a second, across all nineteen adjacent pairings. No model in this loop — it must be cheap, stable and explainable.</p><p>Each pairing scores on proximity, closing rate, DRS availability and tyre-age delta, weighted by which position is being contested. Closing rate is normalised by 0.015 s/s, measured from a full replay rather than guessed.</p>" },
    cutguard: { eyebrow: "Constraint", title: "Cut guard",
      body: "<p>Broadcast grammar, applied deterministically. A director that chases the highest score every tick produces unwatchable television.</p><p>It enforces a minimum hold so a shot can be read, forces a change when one goes stale, and requires a clear margin before abandoning a developing story for a marginally better one.</p>" },
    feed: { eyebrow: "Output", title: "World feed",
      body: "<p>The single shot going out. One camera, one global audience — a pass that happens off screen is lost.</p><p>The card shows which car is live, who they are defending from, the gap, the score that won the cut, and the commentary line the director wrote for it.</p>" },
    fastf1: { eyebrow: "Data source", title: "FastF1",
      body: "<p>Open library for Formula 1 timing and telemetry. Supplies position samples, lap and sector times, tyre compound and age, and the circuit geometry used for the corner markers.</p><p>DRS zones are not published anywhere, so they are recovered from the DRS channel in car telemetry and mapped back onto the track.</p>" },
    quota: { eyebrow: "Capacity", title: "Model quota",
      body: "<p>Vertex serves Gemini from dynamic shared quota — regional capacity rather than a fixed per-minute ceiling. A 429 arrives with no number and no retry delay attached.</p><p>When one lands, the director backs off 6-10 seconds with jitter and every decision in that window is taken deterministically and labelled as such. A fixed thirty-second backoff, which is the obvious default, throws away most of a race for a transient blip.</p>" },
    grafana: { eyebrow: "Observability", title: "Grafana",
      body: "<p>The app provisions its own dashboard over the Grafana HTTP API on startup, installs a battle-imminent alert rule, and writes every cut onto the race timeline as an annotation.</p><p>Metrics go up by Prometheus <code>remote_write</code> straight from this process — a hosted Grafana has no route to scrape it, and pushing removes the need for anything to sit in between.</p>" }
  };

  function openSheet(key) {
    var e = EXPLAIN[key];
    if (!e) return;
    el.sheetEyebrow.textContent = e.eyebrow;
    el.sheetTitle.textContent = e.title;
    var extra = "";
    if (latest) {
      if (key === "director") {
        extra = '<dl class="kv"><dt>tier</dt><dd>' + esc(latest.director.tier) + "</dd>"
          + "<dt>model</dt><dd>" + esc(latest.director.model) + "</dd>"
          + "<dt>latency</dt><dd>" + latest.director.latency_ms + " ms</dd></dl>";
      } else if (key === "tension") {
        extra = '<dl class="kv"><dt>pairings scored</dt><dd>' + latest.pairings_scored.toLocaleString() + "</dd>"
          + "<dt>in range now</dt><dd>" + (latest.battles || []).length + "</dd></dl>";
      } else if (key === "grafana") {
        extra = '<dl class="kv"><dt>reachable</dt><dd>' + (latest.grafana.live ? "yes" : "no") + "</dd>"
          + "<dt>series pushed</dt><dd>" + (latest.grafana.pushed || 0).toLocaleString() + "</dd></dl>";
      } else if (key === "source" || key === "fastf1") {
        extra = '<dl class="kv"><dt>source</dt><dd>' + esc(latest.source) + "</dd>"
          + "<dt>circuit</dt><dd>" + esc(latest.circuit) + "</dd>"
          + "<dt>corners</dt><dd>" + corners.length + "</dd>"
          + "<dt>DRS zones</dt><dd>" + drsZones.length + "</dd></dl>";
      }
    }
    el.sheetBody.innerHTML = e.body + extra;
    el.sheet.hidden = false;
  }
  function closeSheet() { el.sheet.hidden = true; }

  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-explain]");
    if (t) { openSheet(t.getAttribute("data-explain")); return; }
    if (e.target === el.sheet) closeSheet();
  });
  el.sheetX.addEventListener("click", closeSheet);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { if (!el.sheet.hidden) closeSheet(); else if (selected) select(selected); }
  });

  /* ───────── map ───────── */
  var cv = $("map"), ctx = cv.getContext("2d"), W = 0, H = 0;
  function size() {
    var r = cv.getBoundingClientRect(), d = window.devicePixelRatio || 1;
    cv.width = Math.max(1, Math.round(r.width * d));
    cv.height = Math.max(1, Math.round(r.height * d));
    ctx.setTransform(d, 0, 0, d, 0, 0);
    W = r.width; H = r.height;
  }
  size(); addEventListener("resize", size);

  /* Canvas is drawn in device-independent pixels, so anything sized as a
     constant stays physically tiny as the display grows. Everything on the map
     scales off the fitted track size instead. */
  var mapScale = 1;

  /* Camera. Tracking a driver closes in on them rather than just ringing a dot
     three pixels wide — at race pace the interesting thing is the car alongside,
     and at full-circuit zoom you cannot see it. Eased so the move reads as a
     camera push rather than a jump cut. */
  var cam = { z: 1, x: 0.5, y: 0.5 };
  var camT = { z: 1, x: 0.5, y: 0.5 };
  var FOLLOW_ZOOM = 3.2;

  function stepCamera(cars) {
    var cx = (bounds[0] + bounds[2]) / 2, cy = (bounds[1] + bounds[3]) / 2;
    if (selected) {
      var me = cars && cars.filter(function (c) { return c.num === selected; })[0];
      if (me) { camT.z = FOLLOW_ZOOM; camT.x = me.x; camT.y = me.y; }
    } else {
      camT.z = 1; camT.x = cx; camT.y = cy;
    }
    var k = reduced ? 1 : 0.12;
    cam.z += (camT.z - cam.z) * k;
    // Don't ease across the start/finish wrap or the camera swings the long way.
    cam.x += Math.abs(camT.x - cam.x) > 0.4 ? (camT.x - cam.x) : (camT.x - cam.x) * k;
    cam.y += Math.abs(camT.y - cam.y) > 0.4 ? (camT.y - cam.y) : (camT.y - cam.y) * k;
  }

  function fitter() {
    var pad = Math.max(28, Math.min(W, H) * 0.04);
    var availW = W - pad * 2, availH = H - pad * 2;

    /* Fit the box the circuit actually occupies rather than a square. Monza is
       2:1 after rotation, so square-fitting spent half the stage on margin. */
    var bw = Math.max(0.02, bounds[2] - bounds[0]);
    var bh = Math.max(0.02, bounds[3] - bounds[1]);
    var scale = Math.min(availW / bw, availH / bh);
    mapScale = Math.max(1, Math.min(2.6, scale / 1100)) * Math.min(1.7, cam.z);

    return function (x, y) {
      return [W / 2 + (x - cam.x) * cam.z * scale,
              H / 2 + (y - cam.y) * cam.z * scale];
    };
  }

  function px(n) { return n * mapScale; }
  function lerp(a, b, k) { return a + (b - a) * k; }

  function interpolated() {
    if (!next) return null;
    var span = Math.max(16, tNext - tPrev);
    var k = reduced ? 1 : Math.max(0, Math.min(1.35, (performance.now() - tNext) / span + 1));
    var byNum = {};
    (prev.cars || []).forEach(function (c) { byNum[c.num] = c; });
    return (next.cars || []).map(function (c) {
      var p = byNum[c.num] || c, dx = c.x - p.x, dy = c.y - p.y;
      var jump = Math.abs(dx) > 0.25 || Math.abs(dy) > 0.25;   // start/finish wrap
      return { num: c.num, code: c.code, color: c.color, pos: c.pos, gap: c.gap,
               x: jump ? c.x : lerp(p.x, c.x, k), y: jump ? c.y : lerp(p.y, c.y, k) };
    });
  }

  function drawZones(to) {
    if (!drsZones.length || !outline.length) return;
    var n = outline.length;
    drsZones.forEach(function (z) {
      var a = Math.floor(z[0] * n), b = Math.floor(z[1] * n);
      ctx.beginPath();
      for (var i = a; i <= b; i++) {
        var q = to(outline[i % n][0], outline[i % n][1]);
        i === a ? ctx.moveTo(q[0], q[1]) : ctx.lineTo(q[0], q[1]);
      }
      ctx.strokeStyle = "rgba(0,230,118,.32)";
      ctx.lineWidth = px(15); ctx.lineCap = "round"; ctx.stroke();
      ctx.lineCap = "butt";
    });
  }

  function drawCorners(to) {
    if (!corners.length) return;
    ctx.font = Math.round(px(9)) + "px " + getComputedStyle(document.body).getPropertyValue("--mono");
    ctx.textAlign = "center"; ctx.fillStyle = "#4A5570";
    corners.forEach(function (c) {
      var q = to(c.x, c.y);
      ctx.beginPath(); ctx.arc(q[0], q[1], px(1.6), 0, 6.2832); ctx.fill();
      ctx.fillText("T" + c.n, q[0], q[1] - px(7));
    });
  }

  /* Labels collide badly in a tight pack. Place each one, and if it overlaps a
     label already placed, push it further out and draw a leader line. */
  function placeLabels(items) {
    var placed = [];
    items.forEach(function (it) {
      var lift = px(13), tries = 0;
      while (tries < 7) {
        var ly = it.y - lift;
        var clash = placed.some(function (p) {
          return Math.abs(p.x - it.x) < px(26) && Math.abs(p.y - ly) < px(12);
        });
        if (!clash) break;
        lift += px(12); tries++;
      }
      it.lx = it.x; it.ly = it.y - lift; it.lift = lift;
      placed.push({ x: it.lx, y: it.ly });
    });
    return items;
  }

  function draw() {
    requestAnimationFrame(draw);
    ctx.clearRect(0, 0, W, H);
    if (!outline.length) return;
    var s = latest, cars = interpolated();
    if (!cars || !s) return;
    stepCamera(cars);
    var to = fitter();

    drawZones(to);

    ctx.beginPath();
    for (var i = 0; i < outline.length; i++) {
      var q = to(outline[i][0], outline[i][1]);
      i === 0 ? ctx.moveTo(q[0], q[1]) : ctx.lineTo(q[0], q[1]);
    }
    ctx.closePath();
    ctx.strokeStyle = "#1E222D"; ctx.lineWidth = px(13); ctx.lineJoin = "round"; ctx.stroke();
    ctx.strokeStyle = "#2A3040"; ctx.lineWidth = px(1.2); ctx.stroke();

    drawCorners(to);

    var pos = {};
    cars.forEach(function (c) { pos[c.num] = to(c.x, c.y); });
    var live = s.on_air ? s.on_air.num : null;

    (s.battles || []).forEach(function (b) {
      var a = pos[b.ahead_num], d = pos[b.behind_num];
      if (!a || !d) return;
      if (Math.hypot(a[0] - d[0], a[1] - d[1]) > Math.min(W, H) * 0.42) return;
      var k = Math.max(0, Math.min(1, b.score / 0.8));
      var onAir = b.ahead_num === live;
      var isSel = selected && (b.ahead_num === selected || b.behind_num === selected);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(d[0], d[1]);
      if (onAir) {
        ctx.strokeStyle = "rgba(255,87,34," + (0.45 + k * 0.5).toFixed(3) + ")";
        ctx.lineWidth = px(1.4 + k * 2.6); ctx.shadowColor = "#FF5722"; ctx.shadowBlur = px(12);
      } else if (isSel) {
        ctx.strokeStyle = "rgba(255,255,255,.8)"; ctx.lineWidth = px(2);
      } else if (b.drs) {
        ctx.strokeStyle = "rgba(0,230,118," + (0.22 + k * 0.38).toFixed(3) + ")";
        ctx.lineWidth = px(1 + k * 1.4);
      } else {
        ctx.strokeStyle = "rgba(143,155,186," + (0.1 + k * 0.3).toFixed(3) + ")";
        ctx.lineWidth = px(0.9);
      }
      ctx.stroke(); ctx.shadowBlur = 0;
    });

    var wrapJump = Math.min(W, H) * 0.18;
    cars.forEach(function (c) {
      var p = pos[c.num], tr = trails[c.num] || (trails[c.num] = []);
      var last = tr[tr.length - 1];
      if (last && Math.hypot(p[0] - last[0], p[1] - last[1]) > wrapJump) { tr.length = 0; tr.push([p[0], p[1]]); }
      else if (!last || Math.hypot(p[0] - last[0], p[1] - last[1]) > px(1.4)) tr.push([p[0], p[1]]);
      if (tr.length > 14) tr.shift();
      if (tr.length < 2) return;
      var fade = selected && selected !== c.num ? 0.08 : 0.3;
      for (var j = 1; j < tr.length; j++) {
        ctx.beginPath(); ctx.moveTo(tr[j - 1][0], tr[j - 1][1]); ctx.lineTo(tr[j][0], tr[j][1]);
        ctx.strokeStyle = c.color; ctx.globalAlpha = (j / tr.length) * fade;
        ctx.lineWidth = px(2.2); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    });

    if (live && pos[live]) {
      var f = pos[live], r = px(15 + Math.sin(performance.now() / 320) * 3);
      ctx.beginPath(); ctx.arc(f[0], f[1], r, 0, 6.2832);
      ctx.strokeStyle = "rgba(255,87,34,.9)"; ctx.lineWidth = px(1.8);
      ctx.shadowColor = "#FF5722"; ctx.shadowBlur = px(16); ctx.stroke(); ctx.shadowBlur = 0;
      ctx.beginPath(); ctx.arc(f[0], f[1], r + px(9), 0, 6.2832);
      ctx.strokeStyle = "rgba(255,87,34,.22)"; ctx.lineWidth = px(1); ctx.stroke();
    }
    if (selected && pos[selected]) {
      var q2 = pos[selected];
      ctx.beginPath(); ctx.arc(q2[0], q2[1], px(19), 0, 6.2832);
      ctx.strokeStyle = "rgba(255,255,255,.9)"; ctx.lineWidth = px(1.6);
      ctx.setLineDash([px(4), px(4)]); ctx.stroke(); ctx.setLineDash([]);
    }

    var labelled = [];
    cars.forEach(function (c) {
      var p = pos[c.num], on = c.num === live, sel = c.num === selected;
      var dim = selected && !sel;
      ctx.globalAlpha = dim ? 0.3 : 1;
      ctx.beginPath(); ctx.arc(p[0], p[1], px(on || sel ? 5.2 : 3.6), 0, 6.2832);
      ctx.fillStyle = c.color;
      if (on) { ctx.shadowColor = c.color; ctx.shadowBlur = px(10); }
      ctx.fill(); ctx.shadowBlur = 0; ctx.globalAlpha = 1;
      if (on || sel || c.pos <= 3) labelled.push({ x: p[0], y: p[1], code: c.code, on: on, sel: sel });
    });

    placeLabels(labelled).forEach(function (it) {
      if (it.lift > px(15)) {
        ctx.beginPath(); ctx.moveTo(it.x, it.y - px(6)); ctx.lineTo(it.lx, it.ly + px(3));
        ctx.strokeStyle = "rgba(143,155,186,.4)"; ctx.lineWidth = px(0.8); ctx.stroke();
      }
      ctx.font = "600 " + Math.round(px(10)) + "px " + getComputedStyle(document.body).getPropertyValue("--mono");
      ctx.textAlign = "center";
      ctx.fillStyle = it.on ? "#FFFFFF" : it.sel ? "#FFFFFF" : "#8F9BBA";
      ctx.fillText(it.code, it.lx, it.ly);
    });
  }

  draw();
  connect();
})();
