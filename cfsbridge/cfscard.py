"""The CFS control card: `GET /cfscard`.

A page with no furniture, meant to be framed. `GET /fluidd` floats it over
Fluidd next to the camera card, the same way and with the same title strip, so
the CFS can be managed from Orca's Device tab instead of from the touchscreen.

Everything it draws comes from `GET /cfs/card`, polled every three seconds.
Every button posts to one of `/cfs/edit`, `/cfs/config`, `/cfs/feed`,
`/cfs/refresh` and `/cfs/dry` and shows what came back in the status line,
including a dry run's exact JSON. The page enforces nothing: every rule about
what may be written while a job is on the bed lives in `serve.py` and
`cfs.py`, and the page only reflects the answer. A button greyed out here is a
convenience, not a guard.

Plain HTML, plain CSS, vanilla JS. No build step, no CDN, nothing fetched from
anywhere but this bridge.
"""
from __future__ import annotations

CFS_CARD_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CFS</title>
<style>
  :root { color-scheme: dark;
          --bg: #1a1a1a; --line: #2f2f2f; --ink: #d8d8d8; --dim: #8d949c;
          --accent: #3f5f7f; --good: #3fb968; --warn: #e0a33c; --bad: #ef6f60;
          --chip: #262626; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; background: var(--bg);
               color: var(--ink);
               font: 12px/1.45 "Segoe UI", system-ui, -apple-system, sans-serif; }
  body { padding: 8px 10px 10px; overflow-y: auto; }
  h1 { font-size: 13px; margin: 0 0 8px; }
  h1[hidden] { display: none; }
  .dim { color: var(--dim); }
  .row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .unit { border: 1px solid var(--line); border-radius: 5px; margin-bottom: 8px; }
  .unit > header { display: flex; align-items: baseline; gap: 8px;
                   padding: 4px 8px; background: #202020;
                   border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  .unit > header b { font-size: 12px; }
  .unit.off > header b { color: var(--bad); }
  table { border-collapse: collapse; width: 100%; }
  td, th { padding: 3px 6px; border-bottom: 1px solid var(--line);
           text-align: left; font-variant-numeric: tabular-nums;
           vertical-align: middle; }
  th { color: var(--dim); font-weight: 600; font-size: 11px; }
  tr:last-child td { border-bottom: none; }
  tr.slot { cursor: pointer; }
  tr.slot:hover { background: #222; }
  tr.empty td { color: var(--dim); }
  td.lab { width: 34px; font-weight: 600; white-space: nowrap; }
  td.mat { max-width: 0; overflow: hidden; text-overflow: ellipsis;
           white-space: nowrap; }
  td.pct { width: 42px; text-align: right; }
  td.act { width: 1%; white-space: nowrap; }
  .sw { display: inline-block; width: 11px; height: 11px; border-radius: 2px;
        border: 1px solid rgba(148,148,148,.6); vertical-align: -1px;
        margin-right: 5px; }
  .sw.none { background: repeating-linear-gradient(45deg, transparent,
             transparent 3px, var(--line) 3px, var(--line) 6px); }
  .tag { display: inline-block; padding: 0 5px; border-radius: 8px;
         background: var(--chip); color: var(--dim); font-size: 10px;
         margin-left: 4px; }
  .tag.load { background: rgba(63,185,104,.2); color: var(--good); }
  .tag.map { background: rgba(224,163,60,.2); color: var(--warn); }
  button { padding: 1px 7px; border: 1px solid #3d3d3d; border-radius: 3px;
           background: var(--chip); color: var(--ink); cursor: pointer;
           font: 11px/1.5 "Segoe UI", system-ui, sans-serif; }
  button:hover:not([disabled]) { background: #383838; }
  button[disabled] { opacity: .42; cursor: not-allowed; }
  button.on { background: var(--accent); border-color: #567f9f; color: #fff; }
  button.primary { background: var(--accent); border-color: #567f9f; color: #fff; }
  input, select { font: inherit; padding: 2px 4px; border-radius: 3px;
                  border: 1px solid #3d3d3d; background: #232323;
                  color: var(--ink); max-width: 100%; }
  input[type=color] { padding: 0; width: 34px; height: 22px; }
  input[type=number] { width: 62px; }
  .edit { border: 1px solid var(--accent); border-radius: 5px; padding: 8px;
          margin: 6px 0 8px; background: #1d2126; }
  .edit .line { display: grid; grid-template-columns: 74px 1fr; gap: 4px 8px;
                align-items: center; margin-bottom: 5px; }
  .edit .line > label { color: var(--dim); }
  .status { border-left: 3px solid var(--line); padding: 5px 8px; margin: 8px 0 0;
            background: #202020; border-radius: 0 4px 4px 0;
            white-space: pre-wrap; word-break: break-word; }
  .status.good { border-left-color: var(--good); }
  .status.bad { border-left-color: var(--bad); }
  .status.dry { border-left-color: var(--warn); }
  .opts { border: 1px solid var(--line); border-radius: 5px; padding: 6px 8px;
          margin-bottom: 8px; }
  .opts .row { margin-bottom: 4px; }
  .banner { padding: 4px 8px; border-radius: 4px; margin-bottom: 8px;
            background: rgba(224,163,60,.14); color: var(--warn); }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<h1__BARE__>CFS</h1>
<div id="banner" class="banner" hidden></div>
<div id="units" class="dim">Reading the CFS...</div>
<div id="opts" class="opts" hidden></div>
<div id="log"></div>
<div id="status" class="status dim">Idle.</div>
<script>
(function () {
  "use strict";

  var LETTERS = "ABCD";
  var state = null;          // the last /cfs/card payload
  var editing = null;        // the slot label whose form is open
  var form = {};             // the open form's current values
  var busy = false;
  var lastSignature = "";

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }
  function dash(value) {
    return (value === null || value === undefined || value === "") ? "-" : value;
  }
  function swatch(colour) {
    var node = el("span", colour ? "sw" : "sw none");
    if (colour) { node.style.background = colour; }
    return node;
  }
  function say(text, kind) {
    var box = $("status");
    box.textContent = text;
    box.className = "status " + (kind || "dim");
  }

  function post(url, body) {
    busy = true;
    say("Sending...", "dim");
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, message: "The bridge answered HTTP " + r.status };
      });
    }).catch(function (e) {
      return { ok: false, message: "The bridge could not be reached: " + e };
    }).then(function (out) {
      busy = false;
      var kind = out.dry_run ? "dry" : (out.ok ? "good" : "bad");
      say((out.dry_run ? "DRY RUN. " : "") + (out.message || ""), kind);
      lastSignature = "";
      refresh();
      return out;
    });
  }

  // -- the units ---------------------------------------------------------

  function slotRow(unit, slot) {
    var tr = el("tr", "slot" + (slot.present ? "" : " empty"));
    tr.addEventListener("click", function (event) {
      if (event.target.closest && event.target.closest("button")) { return; }
      editing = (editing === slot.label) ? null : slot.label;
      form = {
        vendor: slot.vendor || "", type: slot.type || "",
        name: slot.name || "", colour: slot.colour || "#808080",
        min_temp: slot.min_temp, max_temp: slot.max_temp
      };
      lastSignature = "";
      draw();
    });

    var lab = el("td", "lab");
    lab.appendChild(swatch(slot.colour));
    lab.appendChild(document.createTextNode(slot.label));
    tr.appendChild(lab);

    var mat = el("td", "mat");
    mat.appendChild(document.createTextNode(
      slot.present ? (dash(slot.type) + "  " + dash(slot.name || slot.vendor))
                   : "empty"));
    if (slot.loaded) { mat.appendChild(el("span", "tag load", "LOADED")); }
    if (slot.mapped) { mat.appendChild(el("span", "tag map", "mapped")); }
    if (slot.manually_set) { mat.appendChild(el("span", "tag", "manual")); }
    mat.title = [slot.label, slot.type, slot.vendor, slot.name,
                 slot.colour, slot.rfid].filter(Boolean).join("  ");
    tr.appendChild(mat);

    var pct = el("td", "pct");
    if (slot.remain_len_m !== null && slot.remain_len_m !== undefined
        && slot.remain_len_m > 0) {
      pct.textContent = slot.remain_len_m.toFixed(0) + " m";
    } else {
      pct.textContent = slot.present ? (dash(slot.percent) + "%") : "-";
    }
    tr.appendChild(pct);

    var act = el("td", "act");
    act.appendChild(action("Load", "Load this slot to the toolhead",
      !slot.present || slot.loaded, function () {
        post("/cfs/feed", { unit: unit.unit, slot: slot.material_id, feed: true });
      }));
    act.appendChild(action("Unload", "Unload this slot", !slot.loaded, function () {
      post("/cfs/feed", { unit: unit.unit, slot: slot.material_id, feed: false });
    }));
    act.appendChild(action("Tag", "Re-read this slot's RFID tag",
      !slot.present, function () {
        post("/cfs/refresh", { unit: unit.unit, slot: slot.material_id });
      }));
    tr.appendChild(act);
    return tr;
  }

  function action(text, title, disabled, handler) {
    var button = el("button", null, text);
    button.title = title;
    button.disabled = !!disabled;
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      handler();
    });
    return button;
  }

  function unitBlock(unit) {
    var box = el("div", "unit" + (unit.present ? "" : " off"));
    var head = el("header");
    head.appendChild(el("b", null, "T" + unit.unit + " " + unit.model_name));
    if (!unit.present) { head.appendChild(el("span", "dim", "not on the bus")); }
    if (unit.serial_short) {
      head.appendChild(el("span", "dim", "sn ..." + unit.serial_short));
    }
    if (unit.temp !== null && unit.temp !== undefined) {
      head.appendChild(el("span", "dim", unit.temp.toFixed(0) + " C"));
    }
    if (unit.humidity !== null && unit.humidity !== undefined) {
      head.appendChild(el("span", "dim", unit.humidity.toFixed(0) + "% RH"));
    }
    var dry = el("span", "row");
    dry.style.marginLeft = "auto";
    if (unit.can_dry) {
      dry.appendChild(action("Dry", "Start drying bin 1", false, function () {
        post("/cfs/dry", { unit: unit.unit, action: "start", bin: 1 });
      }));
      dry.appendChild(action("Stop", "Stop drying bin 1", false, function () {
        post("/cfs/dry", { unit: unit.unit, action: "stop", bin: 1 });
      }));
    } else {
      var off = el("span", "dim", "no dryer");
      off.title = "Drying needs a CFS-Pro or CFS-C cabinet. " +
                  "This unit reports " + (unit.model || "no model id") + ".";
      dry.appendChild(off);
    }
    head.appendChild(dry);
    box.appendChild(head);

    var table = el("table");
    unit.slots.forEach(function (slot) {
      table.appendChild(slotRow(unit, slot));
      if (editing === slot.label) {
        var tr = el("tr");
        var td = el("td");
        td.colSpan = 4;
        td.appendChild(editForm(unit, slot));
        tr.appendChild(td);
        table.appendChild(tr);
      }
    });
    box.appendChild(table);
    return box;
  }

  // -- the edit form ------------------------------------------------------

  function line(parent, label, node) {
    var row = el("div", "line");
    row.appendChild(el("label", null, label));
    row.appendChild(node);
    parent.appendChild(row);
    return node;
  }

  function select(values, chosen, onChange) {
    var node = el("select");
    values.forEach(function (value) {
      var option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      node.appendChild(option);
    });
    if (values.indexOf(chosen) >= 0) { node.value = chosen; }
    node.addEventListener("change", function () { onChange(node.value); });
    return node;
  }

  // An empty slot arrives with no vendor, type or name at all, and three
  // empty dropdowns are no use. Fill in only what is blank, so a slot that
  // already holds something the database has never heard of keeps its own
  // values and gets them prepended to the lists instead.
  function fixForm(db) {
    if (!db.vendors.length) { return; }
    if (!form.vendor) { form.vendor = db.vendors[0]; }
    var types = db.types[form.vendor] || [];
    if (!form.type) { form.type = types[0] || ""; }
    var names = (db.names[form.vendor] || {})[form.type] || [];
    if (!form.name) { form.name = names[0] || ""; }
    if (!form.min_temp && !form.max_temp) { applyEntry(db); }
  }

  function editForm(unit, slot) {
    var db = (state.cfs.materials) || { vendors: [], types: {}, names: {} };
    fixForm(db);
    var box = el("div", "edit");

    if (db.vendors.length) {
      var vendors = db.vendors.slice();
      if (form.vendor && vendors.indexOf(form.vendor) < 0) {
        vendors.unshift(form.vendor);
      }
      line(box, "Vendor", select(vendors, form.vendor, function (value) {
        form.vendor = value;
        var types = (db.types[value] || []);
        if (types.indexOf(form.type) < 0) { form.type = types[0] || ""; }
        var names = ((db.names[value] || {})[form.type] || []);
        if (names.indexOf(form.name) < 0) { form.name = names[0] || ""; }
        applyEntry(db);
        lastSignature = "";
        draw();
      }));
      var types = (db.types[form.vendor] || []).slice();
      if (form.type && types.indexOf(form.type) < 0) { types.unshift(form.type); }
      line(box, "Type", select(types, form.type, function (value) {
        form.type = value;
        var names = ((db.names[form.vendor] || {})[value] || []);
        if (names.indexOf(form.name) < 0) { form.name = names[0] || ""; }
        applyEntry(db);
        lastSignature = "";
        draw();
      }));
      var names = ((db.names[form.vendor] || {})[form.type] || []).slice();
      if (form.name && names.indexOf(form.name) < 0) { names.unshift(form.name); }
      line(box, "Name", select(names, form.name, function (value) {
        form.name = value;
        applyEntry(db);
        lastSignature = "";
        draw();
      }));
    } else {
      // No filament database: the printer did not answer reqMaterials. Free
      // text is worse than the dropdowns but it is not a dead end.
      line(box, "Vendor", text("vendor"));
      line(box, "Type", text("type"));
      line(box, "Name", text("name"));
      box.appendChild(el("div", "dim",
        "The printer's filament list could not be read, so these are free text."));
    }

    var colours = el("span", "row");
    var picker = el("input");
    picker.type = "color";
    picker.value = /^#[0-9a-fA-F]{6}$/.test(form.colour) ? form.colour : "#808080";
    var hex = el("input");
    hex.type = "text";
    hex.size = 8;
    hex.value = form.colour || "";
    picker.addEventListener("input", function () {
      form.colour = picker.value.toUpperCase();
      hex.value = form.colour;
    });
    hex.addEventListener("input", function () {
      form.colour = hex.value.trim();
      if (/^#[0-9a-fA-F]{6}$/.test(form.colour)) { picker.value = form.colour; }
    });
    colours.appendChild(picker);
    colours.appendChild(hex);
    line(box, "Colour", colours);

    var temps = el("span", "row");
    temps.appendChild(number("min_temp"));
    temps.appendChild(el("span", "dim", "to"));
    temps.appendChild(number("max_temp"));
    temps.appendChild(el("span", "dim", "C"));
    line(box, "Temperature", temps);

    var actions = el("div", "row");
    actions.style.marginTop = "6px";
    var save = el("button", "primary", "Write to slot " + slot.label);
    save.addEventListener("click", function () {
      post("/cfs/edit", {
        unit: unit.unit, slot: slot.material_id,
        vendor: form.vendor, type: form.type, name: form.name,
        colour: form.colour, min_temp: form.min_temp, max_temp: form.max_temp
      });
    });
    actions.appendChild(save);
    var clear = el("button", null, "Clear slot");
    clear.title = "Write the empty material, the way the touchscreen's reset does";
    clear.addEventListener("click", function () {
      post("/cfs/edit", { unit: unit.unit, slot: slot.material_id, clear: true });
    });
    actions.appendChild(clear);
    var close = el("button", null, "Cancel");
    close.addEventListener("click", function () {
      editing = null;
      lastSignature = "";
      draw();
    });
    actions.appendChild(close);
    box.appendChild(actions);

    if (slot.loaded) {
      box.appendChild(el("div", "dim",
        "This slot is loaded to the toolhead. The bridge refuses to edit it " +
        "while a job is printing or paused."));
    }
    return box;
  }

  function applyEntry(db) {
    var match = null;
    (db.entries || []).forEach(function (entry) {
      if (entry.vendor === form.vendor && entry.type === form.type
          && entry.name === form.name) { match = entry; }
    });
    if (match) {
      form.min_temp = match.min_temp;
      form.max_temp = match.max_temp;
    }
  }

  function text(key) {
    var node = el("input");
    node.type = "text";
    node.value = form[key] || "";
    node.addEventListener("input", function () { form[key] = node.value; });
    return node;
  }

  function number(key) {
    var node = el("input");
    node.type = "number";
    node.value = (form[key] === null || form[key] === undefined) ? "" : form[key];
    node.addEventListener("input", function () { form[key] = node.value; });
    return node;
  }

  // -- the printer-wide options ------------------------------------------

  function drawOptions() {
    var box = $("opts");
    var config = state.cfs.box_config || {};
    box.hidden = false;
    box.innerHTML = "";
    box.appendChild(toggle("Auto refill", "auto_refill", config.auto_refill,
      "Switch to another slot holding the same material when one runs out."));
    box.appendChild(toggle("Auto feed", "auto_feed", config.auto_feed,
      "Feed a slot automatically when filament is inserted."));
    box.appendChild(toggle("Self test", "self_test", config.self_test,
      "Run the printer's calibration before a job."));
    var read = el("div", "row dim");
    read.appendChild(document.createTextNode(
      "Auto update filament: " + (config.auto_update_filament ? "on" : "off") +
      " (read only)"));
    read.title = "Creality's own client never writes this one, so neither does " +
                 "the bridge. See docs/PROTOCOL.md section 6.3.";
    box.appendChild(read);
    var note = el("div", "dim", config.note || "");
    note.style.marginTop = "4px";
    box.appendChild(note);
  }

  function toggle(label, key, value, title) {
    var row = el("div", "row");
    row.title = title;
    row.appendChild(el("span", null, label));
    var button = el("button", value ? "on" : null, value ? "on" : "off");
    button.style.marginLeft = "auto";
    button.addEventListener("click", function () {
      var body = {};
      body[key] = !value;
      post("/cfs/config", body);
    });
    row.appendChild(button);
    return row;
  }

  // -- the write log ------------------------------------------------------

  function drawLog() {
    var box = $("log");
    box.innerHTML = "";
    var writes = (state.cfs.writes || []).slice(0, 4);
    if (!writes.length) { return; }
    writes.forEach(function (write) {
      var row = el("div", "row dim");
      row.style.borderTop = "1px solid var(--line)";
      row.style.padding = "3px 0";
      row.appendChild(el("span", null,
        (write.sent ? "" : "dry run: ") + write.description));
      var right = el("span", "row");
      right.style.marginLeft = "auto";
      if (write.undoable) {
        right.appendChild(action("Undo", "Write the previous values back", false,
          function () { post("/cfs/undo_write", { id: write.id }); }));
      } else if (write.undo_reason) {
        var why = el("span", null, "no undo");
        why.title = write.undo_reason;
        right.appendChild(why);
      }
      row.appendChild(right);
      box.appendChild(row);
    });
  }

  // -- drawing ------------------------------------------------------------

  function signature() {
    return JSON.stringify([editing, state.cfs.print_state, state.cfs.live,
      state.cfs.box_config, state.cfs.writes.length,
      (state.cfs.units || []).map(function (u) {
        return [u.unit, u.present, u.temp, u.humidity, u.serial_short,
                u.slots.map(function (s) {
                  return [s.label, s.type, s.vendor, s.name, s.colour, s.present,
                          s.loaded, s.mapped, s.percent, s.remain_len_m];
                })];
      })]);
  }

  function draw() {
    var sig = signature();
    if (sig === lastSignature) { return; }
    lastSignature = sig;

    var banner = $("banner");
    var messages = [];
    if (!state.cfs.live) {
      messages.push("The bridge is running without --live, so every button " +
                    "below is a dry run and sends nothing.");
    }
    if (state.cfs.busy) {
      messages.push("The printer is " + state.cfs.print_state + ". Writes are " +
                    "refused, except editing a slot that is neither loaded nor " +
                    "in the running job's map.");
    }
    banner.textContent = messages.join("  ");
    banner.hidden = !messages.length;

    var box = $("units");
    box.innerHTML = "";
    box.className = "";
    var units = state.cfs.units || [];
    if (!units.length) {
      box.className = "dim";
      box.textContent = "No CFS unit is reported. The printer may not be " +
                        "answering; check the bridge's own page.";
      return;
    }
    units.forEach(function (unit) { box.appendChild(unitBlock(unit)); });
    drawOptions();
    drawLog();
  }

  function refresh() {
    if (busy) { return; }
    fetch("/cfs/card", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) { state = data; draw(); })
      .catch(function (e) {
        $("units").className = "dim";
        $("units").textContent = "The bridge is not answering (" + e + ").";
      });
  }

  refresh();
  setInterval(refresh, 3000);
})();
</script>
</body>
</html>
"""


def cfs_card_page(bare: bool = False) -> str:
    """The `GET /cfscard` body.

    `bare` drops the page's own heading, for the case where the frame around it
    already has a title strip. That is exactly how `GET /fluidd` embeds it.
    """
    return CFS_CARD_PAGE.replace("__BARE__", " hidden" if bare else "")
