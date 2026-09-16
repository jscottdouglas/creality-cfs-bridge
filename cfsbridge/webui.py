"""The page Orca shows in its Device tab.

Plain HTML, plain CSS, vanilla JS. No build step, no CDN, no framework: Orca
renders this inside an embedded browser and the whole point of the bridge is
that the user never has to install or run anything.

Everything on the page comes from one endpoint, `GET /cfs/state`, polled every
five seconds. The three buttons post to `/cfs/start`, `/cfs/discard` and
`/cfs/undo`. Nothing else is served.

`setup_page()` at the bottom is the other page this module holds: the first-run
page a fresh install lands on, which is the only place the printer address gets
typed. It is deliberately self-contained and tiny, because it has to work
before anything about the printer is known.
"""
from __future__ import annotations

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CFS bridge</title>
<style>
:root {
  --bg: #f6f7f9; --card: #ffffff; --ink: #14171a; --dim: #5c6670;
  --line: #dfe3e8; --accent: #1769c4; --good: #1f8a4c; --warn: #b4690e;
  --bad: #c0392b; --chip: #eef1f5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16191d; --card: #1e2227; --ink: #e8ecf1; --dim: #9aa5b1;
    --line: #2d333b; --accent: #5aa9f0; --good: #3fb968; --warn: #e0a33c;
    --bad: #ef6f60; --chip: #262c33;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 16px; background: var(--bg); color: var(--ink);
  font: 14px/1.45 "Segoe UI", system-ui, -apple-system, Arial, sans-serif;
}
h1 { font-size: 18px; margin: 0; }
h2 { font-size: 14px; margin: 0 0 10px; text-transform: uppercase;
     letter-spacing: .06em; color: var(--dim); }
a { color: var(--accent); }
header { display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
         margin-bottom: 14px; }
header .sub { color: var(--dim); font-size: 12px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
        padding: 14px 16px; margin-bottom: 14px; }
.chip { display: inline-block; padding: 1px 8px; border-radius: 10px;
        background: var(--chip); color: var(--dim); font-size: 12px; }
.chip.live { background: rgba(31,138,76,.16); color: var(--good); }
.chip.dry  { background: rgba(180,105,14,.16); color: var(--warn); }
.chip.down { background: rgba(192,57,43,.16); color: var(--bad); }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--line);
         font-variant-numeric: tabular-nums; }
th { color: var(--dim); font-weight: 600; font-size: 12px; }
tr:last-child td { border-bottom: none; }
td.slot { font-weight: 600; width: 46px; }
.sw { display: inline-block; width: 13px; height: 13px; border-radius: 3px;
      border: 1px solid rgba(128,128,128,.55); vertical-align: -2px;
      margin-right: 6px; }
.sw.none { background: repeating-linear-gradient(45deg, transparent, transparent 3px,
           var(--line) 3px, var(--line) 6px); }
.units { display: grid; gap: 12px;
         grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
.unit h3 { margin: 0 0 6px; font-size: 13px; }
.unit .off { color: var(--bad); font-weight: normal; font-size: 12px; }
.empty td { color: var(--dim); }
.bar { height: 8px; background: var(--chip); border-radius: 4px; overflow: hidden;
       margin: 8px 0 4px; }
.bar > i { display: block; height: 100%; background: var(--accent); }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 2px 14px; }
.kv dt { color: var(--dim); }
.kv dd { margin: 0; word-break: break-all; }
button { font: inherit; padding: 6px 14px; border-radius: 6px; cursor: pointer;
         border: 1px solid var(--line); background: var(--card); color: var(--ink); }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button[disabled] { opacity: .5; cursor: not-allowed; }
select { font: inherit; padding: 4px 6px; border-radius: 5px; max-width: 100%;
         border: 1px solid var(--line); background: var(--card); color: var(--ink); }
.pend { border: 1px solid var(--line); border-radius: 7px; padding: 12px;
        margin-bottom: 10px; }
.pend .why { color: var(--warn); margin: 4px 0 10px; }
.pend .row { display: flex; align-items: center; gap: 10px; margin-bottom: 6px;
             flex-wrap: wrap; }
.pend .row label { min-width: 128px; color: var(--dim); }
.actions { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.msg { margin-top: 8px; padding: 8px 10px; border-radius: 6px; background: var(--chip); }
.msg.bad { color: var(--bad); }
.msg.good { color: var(--good); }
.banner { border-left: 4px solid var(--good); }
.banner.undone { border-left-color: var(--dim); }
.note { color: var(--dim); font-size: 12px; }
iframe { width: 100%; height: 420px; border: 1px solid var(--line); border-radius: 6px;
         background: #000; }
footer { color: var(--dim); font-size: 12px; margin-top: 4px; }
code { background: var(--chip); padding: 1px 5px; border-radius: 4px; }
</style>
</head>
<body>

<header>
  <h1>CFS bridge</h1>
  <span id="mode" class="chip">...</span>
  <span id="conn" class="chip">connecting</span>
  <span class="sub" id="ident"></span>
</header>

<div id="banner"></div>

<section class="card">
  <h2>Current job</h2>
  <div id="job">reading the printer...</div>
</section>

<section class="card">
  <h2>Waiting for a slot choice</h2>
  <div id="pending">...</div>
</section>

<section class="card">
  <h2>CFS slots</h2>
  <div id="units">...</div>
  <div id="mapnote" class="note" style="margin-top:10px"></div>
</section>

<section class="card">
  <h2>Printer web interface and camera</h2>
  <div id="camera"></div>
</section>

<footer id="foot"></footer>

<script>
(function () {
  "use strict";

  var state = null;
  var choices = {};          // pendingId -> { tool -> slot label }
  var pendSig = "";          // rebuild the pending cards only when they change
  var busy = false;          // pause polling while a button is working
  var timer = null;

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined && text !== null) { n.textContent = String(text); }
    return n;
  }
  function swatch(colour) {
    var s = el("span", colour ? "sw" : "sw none");
    if (colour) { s.style.background = colour; }
    return s;
  }
  function dash(v) { return (v === null || v === undefined || v === "") ? "-" : v; }
  function secs(n) {
    n = Math.max(0, Math.round(n || 0));
    var h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60);
    if (h) { return h + "h " + m + "m"; }
    if (m) { return m + "m"; }
    return n + "s";
  }

  function post(url, body) {
    busy = true;
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, message: "HTTP " + r.status }; });
    }).catch(function (e) {
      return { ok: false, message: String(e) };
    }).then(function (out) {
      busy = false;
      pendSig = "";                 // force a redraw so the message shows
      refresh();
      return out;
    });
  }

  // -- current job --------------------------------------------------------

  function drawJob(p) {
    var box = $("job");
    box.innerHTML = "";
    if (!p || !p.reachable) {
      box.appendChild(el("div", "msg bad",
        "The printer at " + (state.host || "?") + " is not answering" +
        (p && p.error ? " (" + p.error + ")" : "") +
        ". The bridge keeps retrying; nothing is lost."));
      return;
    }
    var dl = el("dl", "kv");
    function row(k, v) {
      dl.appendChild(el("dt", null, k));
      dl.appendChild(el("dd", null, v));
    }
    row("State", p.state_text + (p.idle ? " (accepting jobs)" : ""));
    row("File", dash(p.file_name));
    if (p.total_layer) { row("Layer", p.layer + " of " + p.total_layer); }
    row("Nozzle", p.nozzle + " C of " + p.nozzle_target + " C");
    row("Bed", p.bed + " C of " + p.bed_target + " C");
    if (p.box_temp !== null) { row("CFS chamber", p.box_temp + " C"); }
    if (p.left_time) { row("Remaining", secs(p.left_time)); }
    if (p.job_time) { row("Elapsed", secs(p.job_time)); }
    if (p.error_code) { row("Printer error", "code " + p.error_code); }
    box.appendChild(dl);

    var bar = el("div", "bar");
    var fill = el("i");
    fill.style.width = Math.max(0, Math.min(100, p.progress || 0)) + "%";
    bar.appendChild(fill);
    box.appendChild(bar);
    box.appendChild(el("div", "note", (p.progress || 0) + "% complete"));

    var m = state.active_map || [];
    var h = el("div");
    h.style.marginTop = "12px";
    h.appendChild(el("h2", null, "Active slot mapping"));
    if (!m.length) {
      h.appendChild(el("div", "note",
        "The printer holds no mapping, so a bare T0 in the G-code loads slot 1A."));
    } else {
      var t = el("table");
      var head = el("tr");
      ["G-code tool", "Loads slot", "Material"].forEach(function (c) {
        head.appendChild(el("th", null, c));
      });
      t.appendChild(head);
      m.forEach(function (e) {
        var tr = el("tr");
        tr.appendChild(el("td", null, e.tool + "  (a bare T" + e.tool_index + ")"));
        tr.appendChild(el("td", "slot", e.slot));
        var td = el("td");
        td.appendChild(swatch(e.colour));
        td.appendChild(document.createTextNode(dash(e.type) + "  " + dash(e.vendor)));
        tr.appendChild(td);
        t.appendChild(tr);
      });
      h.appendChild(t);
    }
    box.appendChild(h);
  }

  // -- slots --------------------------------------------------------------

  function drawUnits() {
    var box = $("units");
    box.innerHTML = "";
    var grid = el("div", "units");
    (state.units || []).forEach(function (u) {
      var card = el("div", "unit");
      var title = el("h3", null, "CFS " + u.unit + " ");
      if (!u.connected) { title.appendChild(el("span", "off", "not connected")); }
      card.appendChild(title);
      var t = el("table");
      var head = el("tr");
      ["", "Type", "Vendor", "Left", ""].forEach(function (c) {
        head.appendChild(el("th", null, c));
      });
      t.appendChild(head);
      u.slots.forEach(function (s) {
        var tr = el("tr", s.present ? null : "empty");
        var c0 = el("td", "slot");
        c0.appendChild(swatch(s.colour));
        c0.appendChild(document.createTextNode(s.label));
        tr.appendChild(c0);
        tr.appendChild(el("td", null, dash(s.type)));
        tr.appendChild(el("td", null, dash(s.vendor)));
        tr.appendChild(el("td", null,
          (!s.present || s.remain_len_m === null || s.remain_len_m <= 0)
            ? "-" : s.remain_len_m.toFixed(1) + " m"));
        var flags = [];
        if (s.loaded) { flags.push("LOADED"); }
        if (s.mapped) { flags.push("mapped"); }
        if (s.manually_set) { flags.push("manual"); }
        if (!s.present) { flags = ["empty"]; }
        tr.appendChild(el("td", null, flags.join(", ")));
        t.appendChild(tr);
      });
      card.appendChild(t);
      grid.appendChild(card);
    });
    box.appendChild(grid);
    var redirected = !state.map_is_identity || (state.active_map || []).length > 0;
    $("mapnote").textContent =
      "A bare T<n> in G-code means slot index n, so T0 is 1A, T4 is 2A, T5 is 2B, T15 is 4D. " +
      (redirected
        ? "The printer is currently redirecting at least one tool; see Active slot mapping above."
        : "The printer holds no redirection, so every T<n> loads its own slot.");
  }

  // -- pending uploads ----------------------------------------------------

  function pendingSignature() {
    return JSON.stringify((state.pending || []).map(function (p) {
      return [p.id, p.status, p.message, p.tools.map(function (t) {
        return [t.tool, t.options.map(function (o) { return o.label; })];
      })];
    }));
  }

  function drawPending(force) {
    var box = $("pending");
    var list = state.pending || [];
    var sig = pendingSignature();
    if (!force && sig === pendSig) { return; }
    pendSig = sig;
    box.innerHTML = "";
    if (!list.length) {
      box.appendChild(el("div", "note",
        "Nothing waiting. When Orca sends a job the bridge cannot map on its own, " +
        "it lands here with a slot chooser instead of failing."));
      return;
    }
    list.forEach(function (p) {
      var card = el("div", "pend");
      var head = el("div");
      head.appendChild(el("strong", null, p.name));
      head.appendChild(el("span", "note", "   " + p.size_text + ", received " +
        secs(p.age_s) + " ago"));
      card.appendChild(head);
      card.appendChild(el("div", "why", p.reason));

      if (!choices[p.id]) { choices[p.id] = {}; }
      p.tools.forEach(function (t) {
        var row = el("div", "row");
        var label = "T" + t.tool + "  " + dash(t.type);
        row.appendChild(el("label", null, label));
        row.appendChild(swatch(t.colour));
        var sel = el("select");
        sel.setAttribute("data-pend", p.id);
        sel.setAttribute("data-tool", String(t.tool));
        t.options.forEach(function (o) {
          var opt = document.createElement("option");
          opt.value = o.label;
          opt.textContent = o.text;
          sel.appendChild(opt);
        });
        var chosen = choices[p.id][t.tool] || t.selected;
        if (chosen) { sel.value = chosen; }
        choices[p.id][t.tool] = sel.value;
        sel.addEventListener("change", function () {
          choices[p.id][t.tool] = sel.value;
        });
        if (!t.options.length) {
          sel.disabled = true;
          var opt = document.createElement("option");
          opt.textContent = "no slot holds any filament";
          sel.appendChild(opt);
        }
        row.appendChild(sel);
        card.appendChild(row);
      });

      var acts = el("div", "actions");
      var start = el("button", "primary", "Start this print");
      start.addEventListener("click", function () {
        start.disabled = true;
        post("/cfs/start", { id: p.id, map: choices[p.id] });
      });
      var drop = el("button", null, "Discard");
      drop.addEventListener("click", function () {
        drop.disabled = true;
        post("/cfs/discard", { id: p.id });
      });
      acts.appendChild(start);
      acts.appendChild(drop);
      card.appendChild(acts);

      if (p.message) {
        card.appendChild(el("div", "msg " + (p.ok ? "good" : "bad"), p.message));
      }
      box.appendChild(card);
    });
  }

  // -- the 60 second banner ----------------------------------------------

  function drawBanner() {
    var box = $("banner");
    var r = state.recent;
    box.innerHTML = "";
    if (!r) { return; }
    var card = el("div", "card banner" + (r.undone ? " undone" : ""));
    card.appendChild(el("strong", null,
      r.undone ? "Cancelled: " + r.name : "Started: " + r.name));
    var t = el("table");
    r.mapping.forEach(function (m) {
      var tr = el("tr");
      tr.appendChild(el("td", null, "T" + m.tool));
      var td = el("td");
      td.appendChild(swatch(m.colour));
      td.appendChild(document.createTextNode(m.slot + "  " + dash(m.type) + "  " + m.reason));
      tr.appendChild(td);
      t.appendChild(tr);
    });
    card.appendChild(t);
    if (r.undo_available) {
      var acts = el("div", "actions");
      var undo = el("button", null, "Undo (cancel this job)");
      undo.addEventListener("click", function () {
        undo.disabled = true;
        post("/cfs/undo", {});
      });
      acts.appendChild(undo);
      acts.appendChild(el("span", "note",
        "Undo is offered for " + Math.max(0, Math.round(r.undo_left_s)) +
        " more seconds, and only while the job has not started heating."));
      card.appendChild(acts);
    } else if (!r.undone) {
      card.appendChild(el("div", "note", r.undo_reason));
    }
    if (r.message) {
      card.appendChild(el("div", "msg " + (r.ok ? "good" : "bad"), r.message));
    }
    box.appendChild(card);
  }

  // -- camera -------------------------------------------------------------

  var cameraDrawn = false;
  function drawCamera() {
    if (cameraDrawn) { return; }
    cameraDrawn = true;
    var box = $("camera");
    box.innerHTML = "";
    var c = state.camera || {};
    var p = el("p");
    var a = el("a", null, "Open Fluidd on the printer");
    a.href = state.fluidd_url;
    a.target = "_blank";
    a.rel = "noopener";
    p.appendChild(a);
    p.appendChild(document.createTextNode("  (" + state.fluidd_url + ")"));
    box.appendChild(p);
    box.appendChild(el("div", "note", c.note || ""));
    if (c.embeddable) {
      var acts = el("div", "actions");
      var show = el("button", null, "Show the camera");
      acts.appendChild(show);
      box.appendChild(acts);
      var holder = el("div");
      holder.style.marginTop = "10px";
      box.appendChild(holder);
      show.addEventListener("click", function () {
        if (holder.firstChild) {
          holder.innerHTML = "";
          show.textContent = "Show the camera";
          return;
        }
        var f = document.createElement("iframe");
        f.src = c.embed_url;
        f.setAttribute("allow", "autoplay");
        holder.appendChild(f);
        show.textContent = "Hide the camera";
      });
    }
  }

  // -- refresh ------------------------------------------------------------

  function drawChrome() {
    var mode = $("mode");
    mode.textContent = state.live ? "live" : "dry run";
    mode.className = "chip " + (state.live ? "live" : "dry");
    var conn = $("conn");
    var p = state.printer || {};
    if (p.reachable) {
      conn.textContent = "printer " + state.host + " ok";
      conn.className = "chip live";
    } else {
      conn.textContent = "printer " + state.host + " unreachable";
      conn.className = "chip down";
    }
    $("ident").textContent = (p.hostname ? p.hostname + ", " : "") +
      "cfsbridge " + state.version + ", mapping method " + state.method;
    $("foot").textContent =
      "Log: " + (state.log_path || "(console only)") +
      ".  Spool: " + state.spool_dir +
      ".  To point Orca back at the printer directly see docs/ORCA_PRESET.md.";
  }

  function refresh() {
    if (busy) { return; }
    fetch("/cfs/state", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state = data;
        drawChrome();
        drawBanner();
        drawJob(state.printer);
        drawPending(false);
        drawUnits();
        drawCamera();
      })
      .catch(function (e) {
        var conn = $("conn");
        conn.textContent = "bridge not answering";
        conn.className = "chip down";
      });
  }

  refresh();
  timer = setInterval(refresh, 5000);
})();
</script>
</body>
</html>
"""


# -- the first-run page ----------------------------------------------------

SETUP_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>CFS bridge setup</title>
<style>body{font:15px system-ui;margin:40px auto;max-width:560px;color:#e6e9ef;background:#14171c}
input{font-size:16px;padding:8px;width:100%;box-sizing:border-box;background:#1e232b;color:#e6e9ef;border:1px solid #2d3441;border-radius:6px}
button{font-size:16px;padding:10px 16px;margin-top:12px;border-radius:8px;border:1px solid #2f6fb3;background:#1f4b7a;color:#fff;cursor:pointer}
.ok{color:#3fb950}.bad{color:#f85149}.muted{color:#8a94a6}</style></head><body>
<h1>Creality CFS bridge</h1>
<p>Enter your K2's printer address. Find it on the printer: Settings, Network. The PC and the printer must be on the same network.</p>
<label>Printer address</label><input id="host" placeholder="192.168.1.50">
<div><button id="test">Test</button> <button id="save">Save</button></div>
<p id="msg" class="muted"></p>
<p class="muted">After saving, the slicer's Device tab shows Fluidd with the CFS card, and the camera appears once the stream connects.</p>
<script>
var $=function(i){return document.getElementById(i)};
fetch('/api/setup').then(r=>r.json()).then(j=>{ if(j.host) $('host').value=j.host; });
function post(p,b){return fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());}
$('test').onclick=function(){ $('msg').textContent='testing...'; post('/api/setup/test',{host:$('host').value}).then(j=>{
  $('msg').className=j.ok?'ok':'bad'; $('msg').textContent=j.ok?('Found '+j.model+' ('+j.hostname+'), '+j.cfs_units+' CFS unit(s)'):('Not reachable: '+j.error);});};
$('save').onclick=function(){ post('/api/setup',{host:$('host').value}).then(j=>{
  $('msg').className=j.ok?'ok':'bad'; $('msg').textContent=j.ok?'Saved. Opening the bridge page...':('Refused: '+j.error);
  if(j.ok) setTimeout(function(){location.href='/';},1200);});};
</script></body></html>"""


def setup_page() -> str:
    """`GET /setup`: the one page that works before a printer is configured.

    Its own style rather than the shared one above, because it is the page a
    first-run user sees and it must not depend on anything the bridge has not
    read from a printer yet.
    """
    return SETUP_PAGE
