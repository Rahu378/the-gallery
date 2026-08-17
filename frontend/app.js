/* The Gallery — broadcast surface.
   Server ticks at 10 Hz; we render at display rate and interpolate between
   frames so the map moves like television rather than a status page. */
(function () {
  "use strict";

  var reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  var el = {
    circuit: document.getElementById("circuit"),
    lap: document.getElementById("lap"),
    chipSource: document.getElementById("chipSource"),
    chipModel: document.getElementById("chipModel"),
    chipGrafana: document.getElementById("chipGrafana"),
    tower: document.getElementById("tower"),
    towerCount: document.getElementById("towerCount"),
    scored: document.getElementById("scored"),
    mapName: document.getElementById("mapName"),
    mapSrc: document.getElementById("mapSrc"),
    flash: document.getElementById("flash"),
    onair: document.getElementById("onair"),
    oaNum: document.getElementById("oaNum"),
    oaShot: document.getElementById("oaShot"),
    oaAgainst: document.getElementById("oaAgainst"),
    oaLine: document.getElementById("oaLine"),
    oaHold: document.getElementById("oaHold"),
    oaConf: document.getElementById("oaConf"),
    oaTier: document.getElementById("oaTier"),
    battles: document.getElementById("battles"),
    log: document.getElementById("log"),
    fLatency: document.getElementById("fLatency"),
    fScored: document.getElementById("fScored")
  };

  /* ───────────────────────── state ───────────────────────── */
  var outline = [];
  var prev = null, next = null, tPrev = 0, tNext = 0;
  var trails = {};              // car number -> recent [x,y] points
  var lastCutKey = "";
  var latest = null;

  /* ───────────────────────── socket ──────────────────────── */
  function connect() {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var sock = new WebSocket(proto + "://" + location.host + "/ws");

    sock.onmessage = function (ev) {
      var msg = JSON.parse(ev.data);
      if (msg.type === "circuit") {
        outline = msg.outline || [];
        el.mapName.textContent = msg.name || "—";
        return;
      }
      if (msg.type !== "state") return;
      prev = next; tPrev = tNext;
      next = msg; tNext = performance.now();
      if (!prev) prev = next, tPrev = tNext - 100;
      latest = msg;
      paintPanels(msg);
    };

    sock.onclose = function () { setTimeout(connect, 1200); };
    sock.onerror = function () { sock.close(); };
  }

  /* ───────────────────────── panels ──────────────────────── */
  function chip(node, on, text, cls) {
    node.className = "chip " + (cls || (on ? "on" : "off"));
    node.querySelector("span").textContent = text;
  }

  function paintPanels(s) {
    el.circuit.textContent = s.circuit || "—";
    el.lap.textContent = s.lap + " / " + (s.total_laps || "—");
    el.mapSrc.textContent = s.source === "fastf1"
      ? "real telemetry · fastf1" : "synthetic replay";
    el.towerCount.textContent = s.cars.length + " cars";
    el.scored.textContent = (s.cars.length - 1) + " pairings / tick";
    el.fScored.textContent = (s.pairings_scored || 0).toLocaleString();
    el.fLatency.textContent = s.director.latency_ms ? s.director.latency_ms + " ms" : "—";

    chip(el.chipSource, true, s.source === "fastf1" ? "fastf1 · real" : "synthetic",
         s.source === "fastf1" ? "on" : "off");
    var d = s.director;
    chip(el.chipModel, true,
         d.blocked ? d.blocked
           : (d.tier === "heuristic" ? (d.configured ? "standby" : "no key") : d.tier)
             + " · " + d.model,
         d.blocked ? "off" : (d.tier === "heuristic" ? "off" : "ai"));
    chip(el.chipGrafana, s.grafana.live, s.grafana.live ? "grafana live"
         : (s.grafana.enabled ? "grafana unreachable" : "grafana offline"),
         s.grafana.live ? "on" : "off");

    paintTower(s);
    paintOnAir(s);
    paintBattles(s);
    paintLog(s);
  }

  function paintTower(s) {
    var live = s.on_air ? s.on_air.num : null;
    var fighting = {};
    (s.battles || []).forEach(function (b) {
      if (b.score >= 0.4) { fighting[b.ahead_num] = 1; fighting[b.behind_num] = 1; }
    });

    var html = "";
    for (var i = 0; i < s.cars.length; i++) {
      var c = s.cars[i];
      var gapTxt = c.pos === 1 ? "LEADER" : "+" + c.gap.toFixed(3);
      var gapCls = c.pos === 1 ? "" : (c.gap <= 1.0 ? " drs" : (c.gap <= 2.2 ? " close" : ""));
      var cls = "row" + (c.num === live ? " live" : "") + (fighting[c.num] ? " fight" : "");
      html +=
        '<div class="' + cls + '">' +
          '<span class="p">' + c.pos + "</span>" +
          '<span class="bar" style="background:' + c.color + '"></span>' +
          '<span class="code">' + c.code + "</span>" +
          '<span class="tyre"><b class="' + c.tyre + '">' + (c.tyre || "—").slice(0, 1) +
            "</b>" + c.age + "L</span>" +
          '<span class="gap' + gapCls + '">' + gapTxt + "</span>" +
        "</div>";
    }
    el.tower.innerHTML = html;
  }

  function paintOnAir(s) {
    var a = s.on_air;
    if (!a) return;
    var car = (s.cars || []).filter(function (c) { return c.num === a.num; })[0];
    var color = car ? car.color : "#5E5E74";

    var key = a.num + "|" + a.line;
    if (key !== lastCutKey) {
      lastCutKey = key;
      if (!reduced) {
        el.flash.classList.remove("go");
        void el.flash.offsetWidth;         // restart the animation
        el.flash.classList.add("go");
      }
    }

    el.onair.classList.toggle("hot", !!a.hot);
    el.oaNum.textContent = a.num;
    el.oaNum.style.background = color;
    el.oaShot.textContent = a.shot || "ONBOARD";
    el.oaAgainst.textContent = "P" + a.position + " · " + a.code +
      " defending from " + a.against + " · " + a.gap.toFixed(2) + "s";
    el.oaLine.textContent = a.line || "—";
    el.oaHold.textContent = (s.hold || 0).toFixed(1) + "s";
    el.oaConf.textContent = a.confidence ? Math.round(a.confidence * 100) + "%" : "—";
    el.oaTier.textContent = a.tier === "heuristic" ? "deterministic" : a.tier;
  }

  function paintBattles(s) {
    var b = s.battles || [];
    if (!b.length) {
      el.battles.innerHTML = '<div class="battle-why">no position within attack range</div>';
      return;
    }
    var live = s.on_air ? s.on_air.num : null;
    el.battles.innerHTML = b.slice(0, 5).map(function (x) {
      var hot = x.score >= 0.55;
      var col = x.ahead_num === live ? "var(--purple)" : (hot ? "var(--yellow)" : "var(--ink-3)");
      return '<div class="battle">' +
        '<div class="battle-top">' +
          '<span class="pos">P' + x.position + "</span>" +
          '<span class="vs">' + x.ahead + " ◂ " + x.behind + "</span>" +
          '<span class="sc' + (hot ? " hot" : "") + '">' + x.score.toFixed(2) + "</span>" +
        "</div>" +
        '<div class="battle-bar"><i style="width:' +
          Math.round(Math.min(1, x.score) * 100) + "%;background:" + col + '"></i></div>' +
        '<div class="battle-why">' + x.why + "</div>" +
      "</div>";
    }).join("");
  }

  function clock(t) {
    var m = Math.floor(t / 60), sec = Math.floor(t % 60);
    return String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0");
  }

  function paintLog(s) {
    el.log.innerHTML = (s.log || []).slice(0, 12).map(function (e) {
      var badge = e.tier
        ? '<span class="badge ' + (e.tier === "heuristic" ? "heuristic" : "") + '">' +
          (e.tier === "heuristic" ? "DET" : e.tier.toUpperCase()) + "</span>"
        : "";
      return '<li><span class="t">' + clock(e.t) + "</span>" + badge +
             '<span class="' + e.kind + '">' + e.text + "</span></li>";
    }).join("");
  }

  /* ───────────────────────── map ─────────────────────────── */
  var cv = document.getElementById("map");
  var ctx = cv.getContext("2d");
  var W = 0, H = 0;

  function size() {
    var r = cv.getBoundingClientRect(), d = window.devicePixelRatio || 1;
    cv.width = Math.max(1, Math.round(r.width * d));
    cv.height = Math.max(1, Math.round(r.height * d));
    ctx.setTransform(d, 0, 0, d, 0, 0);
    W = r.width; H = r.height;
  }
  size();
  addEventListener("resize", size);

  function fitter() {
    var pad = 54;
    var side = Math.min(W - pad * 2, H - pad * 2);
    var ox = (W - side) / 2, oy = (H - side) / 2;
    return function (x, y) { return [ox + x * side, oy + y * side]; };
  }

  function lerp(a, b, k) { return a + (b - a) * k; }

  function interpolated() {
    if (!next) return null;
    var span = Math.max(16, tNext - tPrev);
    var k = reduced ? 1 : Math.max(0, Math.min(1.35, (performance.now() - tNext) / span + 1));
    var byNum = {};
    (prev.cars || []).forEach(function (c) { byNum[c.num] = c; });
    return (next.cars || []).map(function (c) {
      var p = byNum[c.num] || c;
      var dx = c.x - p.x, dy = c.y - p.y;
      // Don't interpolate across the start/finish wrap.
      var jump = Math.abs(dx) > 0.25 || Math.abs(dy) > 0.25;
      return {
        num: c.num, code: c.code, color: c.color, pos: c.pos, gap: c.gap,
        x: jump ? c.x : lerp(p.x, c.x, k),
        y: jump ? c.y : lerp(p.y, c.y, k)
      };
    });
  }

  function draw() {
    requestAnimationFrame(draw);
    ctx.clearRect(0, 0, W, H);
    if (!outline.length) return;

    var to = fitter();
    var s = latest;
    var cars = interpolated();
    if (!cars) return;

    /* ---- circuit ---- */
    ctx.beginPath();
    for (var i = 0; i < outline.length; i++) {
      var q = to(outline[i][0], outline[i][1]);
      i === 0 ? ctx.moveTo(q[0], q[1]) : ctx.lineTo(q[0], q[1]);
    }
    ctx.closePath();
    ctx.strokeStyle = "#1C1C26"; ctx.lineWidth = 13; ctx.lineJoin = "round"; ctx.stroke();
    ctx.strokeStyle = "#2C2C3A"; ctx.lineWidth = 1.2; ctx.stroke();

    var pos = {};
    cars.forEach(function (c) { pos[c.num] = to(c.x, c.y); });

    /* ---- tension lines ---- */
    var live = s.on_air ? s.on_air.num : null;
    (s.battles || []).forEach(function (b) {
      var a = pos[b.ahead_num], d = pos[b.behind_num];
      if (!a || !d) return;
      var dist = Math.hypot(a[0] - d[0], a[1] - d[1]);
      if (dist > Math.min(W, H) * 0.42) return;      // wrapped around the lap
      var k = Math.max(0, Math.min(1, b.score / 0.8));
      var onAir = b.ahead_num === live;
      ctx.beginPath();
      ctx.moveTo(a[0], a[1]); ctx.lineTo(d[0], d[1]);
      if (onAir) {
        ctx.strokeStyle = "rgba(162,75,255," + (0.45 + k * 0.5).toFixed(3) + ")";
        ctx.lineWidth = 1.4 + k * 2.6;
        ctx.shadowColor = "#A24BFF"; ctx.shadowBlur = 12;
      } else if (b.drs) {
        ctx.strokeStyle = "rgba(0,230,118," + (0.25 + k * 0.4).toFixed(3) + ")";
        ctx.lineWidth = 1 + k * 1.4;
      } else {
        ctx.strokeStyle = "rgba(148,148,170," + (0.10 + k * 0.32).toFixed(3) + ")";
        ctx.lineWidth = 0.9;
      }
      ctx.stroke();
      ctx.shadowBlur = 0;
    });

    /* ---- trails ---- */
    // A car crossing the start/finish line jumps from one end of the outline to
    // the other. Without this the trail draws that jump as a line straight
    // across the circuit, once per car per lap.
    var wrapJump = Math.min(W, H) * 0.18;
    cars.forEach(function (c) {
      var p = pos[c.num];
      var tr = trails[c.num] || (trails[c.num] = []);
      var last = tr[tr.length - 1];
      if (last && Math.hypot(p[0] - last[0], p[1] - last[1]) > wrapJump) {
        tr.length = 0;                       // wrapped — start a fresh trail
        tr.push([p[0], p[1]]);
      } else if (!last || Math.hypot(p[0] - last[0], p[1] - last[1]) > 1.4) {
        tr.push([p[0], p[1]]);
      }
      if (tr.length > 14) tr.shift();
      if (tr.length < 2) return;
      for (var j = 1; j < tr.length; j++) {
        ctx.beginPath();
        ctx.moveTo(tr[j - 1][0], tr[j - 1][1]);
        ctx.lineTo(tr[j][0], tr[j][1]);
        ctx.strokeStyle = c.color;
        ctx.globalAlpha = (j / tr.length) * 0.30;
        ctx.lineWidth = 2.2;
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    });

    /* ---- focus ring ---- */
    if (live && pos[live]) {
      var f = pos[live];
      var t = performance.now() / 1000;
      var r = 15 + Math.sin(t * 3.1) * 3;
      ctx.beginPath(); ctx.arc(f[0], f[1], r, 0, 6.2832);
      ctx.strokeStyle = "rgba(162,75,255,.85)"; ctx.lineWidth = 1.6;
      ctx.shadowColor = "#A24BFF"; ctx.shadowBlur = 16; ctx.stroke();
      ctx.shadowBlur = 0;
      ctx.beginPath(); ctx.arc(f[0], f[1], r + 9, 0, 6.2832);
      ctx.strokeStyle = "rgba(162,75,255,.18)"; ctx.lineWidth = 1; ctx.stroke();
    }

    /* ---- cars ---- */
    cars.forEach(function (c) {
      var p = pos[c.num], on = c.num === live;
      ctx.beginPath(); ctx.arc(p[0], p[1], on ? 5.2 : 3.6, 0, 6.2832);
      ctx.fillStyle = c.color;
      if (on) { ctx.shadowColor = c.color; ctx.shadowBlur = 10; }
      ctx.fill();
      ctx.shadowBlur = 0;
      if (on || c.pos <= 3) {
        ctx.font = "600 10px " + getComputedStyle(document.body).getPropertyValue("--mono");
        ctx.fillStyle = on ? "#EDEDF4" : "#7A7A92";
        ctx.textAlign = "center";
        ctx.fillText(c.code, p[0], p[1] - 12);
      }
    });
  }

  draw();
  connect();
})();
