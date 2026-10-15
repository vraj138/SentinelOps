// Time-series panels and sparklines, drawn with uPlot from /api/panels/<id>.
// The server owns the PromQL; this file only fetches, formats and draws.
(function () {
  "use strict";

  var MiB = 1024 * 1024;
  var SYNC = uPlot.sync("sentinelops");

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function format(unit, x) {
    if (x == null || !isFinite(x)) return "-";
    if (unit === "reqps") return (x >= 100 ? x.toFixed(0) : x >= 10 ? x.toFixed(1) : x.toFixed(2)) + " req/s";
    if (unit === "ratio") return (x * 100).toFixed(x !== 0 && x < 0.1 ? 2 : 1) + "%";
    if (unit === "seconds") return x < 1 ? (x * 1000).toFixed(0) + " ms" : x.toFixed(2) + " s";
    if (unit === "bytes") return (x / MiB).toFixed(x / MiB < 10 ? 1 : 0) + " MiB";
    return Number.isInteger(x) ? String(x) : x.toFixed(1);
  }

  // Tick labels: precision follows the spacing between ticks, so 0.25% never shows as 0.3%.
  function decimalsFor(step) {
    if (!(step > 0)) return 0;
    for (var d = 0; d < 3; d++) {
      var scaled = step * Math.pow(10, d);
      if (Math.abs(Math.round(scaled) - scaled) < 1e-6) return d;
    }
    return 3;
  }

  // Thresholds read as round numbers: "5%", "1 s", "100 MiB".
  function compact(unit, x) {
    var trim = function (v) { return String(Number(v.toFixed(2))); };
    if (unit === "ratio") return trim(x * 100) + "%";
    if (unit === "seconds") return x < 1 ? trim(x * 1000) + " ms" : trim(x) + " s";
    if (unit === "bytes") return trim(x / MiB) + " MiB";
    return trim(x);
  }

  function axisLabels(unit) {
    return function (u, splits) {
      var step = splits.length > 1 ? Math.abs(splits[1] - splits[0]) : 0;
      return splits.map(function (x) {
        if (unit === "ratio") return (x * 100).toFixed(decimalsFor(step * 100)) + "%";
        if (unit === "seconds") return x < 1 ? (x * 1000).toFixed(0) + "ms" : x.toFixed(decimalsFor(step)) + "s";
        if (unit === "bytes") return (x / MiB).toFixed(0) + " MiB";
        return x.toFixed(decimalsFor(step));
      });
    };
  }

  // Counts tick at whole numbers and memory at powers of two MiB.
  var INCRS = {
    count: [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000],
    bytes: [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024].map(function (m) { return m * MiB; }),
  };

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  function timeLabels(u, splits) {
    var span = u.scales.x.max - u.scales.x.min;
    return splits.map(function (ts, i) {
      var d = new Date(ts * 1000);
      var hm = pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
      if (span > 43200 && (i === 0 || (d.getUTCHours() === 0 && d.getUTCMinutes() === 0))) {
        return pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) + " " + hm;
      }
      return hm;
    });
  }

  function alpha(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return "rgba(" + (n >> 16) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }

  function lastValue(values) {
    for (var i = values.length - 1; i >= 0; i--) if (values[i] != null) return values[i];
    return null;
  }

  function dashed(u, draw) {
    var ctx = u.ctx, dpr = devicePixelRatio || 1;
    ctx.save();
    ctx.lineWidth = dpr;
    ctx.setLineDash([4 * dpr, 4 * dpr]);
    draw(ctx, dpr);
    ctx.restore();
  }

  // Alert threshold as a dashed horizontal line, when it's inside the visible range.
  function thresholdPlugin(threshold) {
    return { hooks: { draw: function (u) {
      if (threshold == null) return;
      var y = u.valToPos(threshold, "y", true);
      if (y < u.bbox.top || y > u.bbox.top + u.bbox.height) return;
      dashed(u, function (ctx) {
        ctx.strokeStyle = cssVar("--bad");
        ctx.beginPath();
        ctx.moveTo(u.bbox.left, y);
        ctx.lineTo(u.bbox.left + u.bbox.width, y);
        ctx.stroke();
      });
    } } };
  }

  // Incidents as dashed vertical lines with a short label.
  function markersPlugin(markers) {
    return { hooks: { draw: function (u) {
      markers.forEach(function (m) {
        if (m.t < u.scales.x.min || m.t > u.scales.x.max) return;
        var x = u.valToPos(m.t, "x", true);
        dashed(u, function (ctx, dpr) {
          ctx.strokeStyle = cssVar("--warn");
          ctx.beginPath();
          ctx.moveTo(x, u.bbox.top);
          ctx.lineTo(x, u.bbox.top + u.bbox.height);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = cssVar("--warn");
          ctx.font = 10 * dpr + "px " + cssVar("--mono");
          ctx.fillText(m.label, x + 4 * dpr, u.bbox.top + 10 * dpr);
        });
      });
    } } };
  }

  function legendPlugin(el, unit) {
    function render(u, idx) {
      el.innerHTML = "";
      u.series.forEach(function (s, i) {
        if (i === 0) return;
        var values = u.data[i];
        var v = idx == null ? lastValue(values) : values[idx];
        var key = document.createElement("span");
        key.className = "key";
        var sw = document.createElement("span");
        sw.className = "swatch";
        sw.style.background = s._stroke;
        var label = document.createElement("span");
        label.textContent = s.label;
        var val = document.createElement("span");
        val.className = "val";
        val.textContent = format(unit, v);
        key.append(sw, label, val);
        el.appendChild(key);
      });
    }
    return { hooks: {
      ready: function (u) { render(u, null); },
      setData: function (u) { render(u, null); },
      setCursor: function (u) { render(u, u.cursor.idx); },
    } };
  }

  function chartOptions(box, data, colors, markers, legendEl) {
    var text = cssVar("--text-dim"), grid = cssVar("--grid"), font = "11px " + cssVar("--sans");
    var axis = { stroke: text, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid, width: 1, size: 4 }, font: font };
    var series = [{}].concat(data.series.map(function (s) {
      var color = colors[s.label] || "#8c95a3";
      return { label: s.label, stroke: color, _stroke: color, width: 1.5, fill: alpha(color, 0.07), points: { show: false } };
    }));
    var threshold = data.threshold;
    return {
      width: box.clientWidth,
      height: box.clientHeight,
      cursor: { sync: { key: SYNC.key }, points: { size: 6 }, drag: { x: false, y: false } },
      legend: { show: false },
      scales: { y: { range: function (u, min, max) {
        var top = max == null ? 1 : max;
        if (threshold != null && top >= threshold * 0.4) top = Math.max(top, threshold * 1.15);
        if (top <= 0) top = data.unit === "ratio" ? 0.01 : 1;
        return [0, top * 1.08];
      } } },
      axes: [
        Object.assign({}, axis, { values: timeLabels, space: 60 }),
        Object.assign({}, axis, { size: 58, values: axisLabels(data.unit), incrs: INCRS[data.unit] }),
      ],
      series: series,
      plugins: [thresholdPlugin(threshold), markersPlugin(markers), legendPlugin(legendEl, data.unit)],
    };
  }

  function toColumns(data) {
    return [data.x].concat(data.series.map(function (s) { return s.values; }));
  }

  function showState(box, text, isError) {
    var el = box.querySelector(".chart-state");
    if (!el) {
      el = document.createElement("div");
      box.appendChild(el);
    }
    el.className = "chart-state" + (isError ? " error" : "");
    el.textContent = text;
    el.hidden = !text;
  }

  function fetchPanel(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) throw new Error(body.detail || body.error || r.status + " " + r.statusText);
        return body;
      });
    });
  }

  // One dashboard: a set of panels sharing a range, colours and incident markers.
  function Dashboard(root) {
    var colors = JSON.parse(root.dataset.colors || "{}");
    var markers = JSON.parse(root.dataset.markers || "[]");
    var panels = Array.prototype.slice.call(root.querySelectorAll("[data-panel]"));
    var charts = {};
    var lastData = {};

    function query(panel) {
      var params = new URLSearchParams({ services: panel.dataset.services });
      if (root.dataset.start) {
        params.set("start", root.dataset.start);
        params.set("end", root.dataset.end);
      } else {
        params.set("range", root.dataset.range || "1h");
      }
      return "/api/panels/" + encodeURIComponent(panel.dataset.panel) + "?" + params;
    }

    function updateStat(id, data) {
      var el = document.querySelector('[data-stat="' + id + '"]');
      if (!el) return;
      var latest = data.series.map(function (s) { return lastValue(s.values); }).filter(function (v) { return v != null; });
      if (!latest.length) { el.textContent = "-"; return; }
      var value = id === "requests"
        ? latest.reduce(function (a, b) { return a + b; }, 0)
        : Math.max.apply(null, latest);
      el.textContent = format(data.unit, value);
      el.classList.toggle("bad", data.threshold != null && value > data.threshold);
    }

    function draw(panel, data) {
      var box = panel.querySelector(".chart");
      var legendEl = panel.querySelector(".legend");
      var id = panel.dataset.panel;
      lastData[id] = data;
      updateStat(id, data);
      var note = panel.querySelector(".threshold-note");
      if (note && data.threshold != null && !note.dataset.done) {
        note.textContent = data.alert + " above " + compact(data.unit, data.threshold);
        note.dataset.done = "1";
      }
      var empty = data.series.every(function (s) { return s.empty; });
      if (empty) {
        if (panel.hasAttribute("data-optional")) { panel.hidden = true; return; }
        if (charts[id]) { charts[id].destroy(); delete charts[id]; }
        legendEl.innerHTML = "";
        showState(box, "No data in this range", false);
        return;
      }
      panel.hidden = false;
      showState(box, "", false);
      if (charts[id]) {
        charts[id].setData(toColumns(data));
        return;
      }
      charts[id] = new uPlot(chartOptions(box, data, colors, markers, legendEl), toColumns(data), box);
    }

    function load(panel) {
      return fetchPanel(query(panel)).then(function (data) { draw(panel, data); }).catch(function (err) {
        var id = panel.dataset.panel;
        if (charts[id]) { charts[id].destroy(); delete charts[id]; }
        panel.querySelector(".legend").innerHTML = "";
        showState(panel.querySelector(".chart"), "Query failed: " + err.message, true);
      });
    }

    function loadAll() { panels.forEach(load); }

    function rebuild() {
      Object.keys(charts).forEach(function (id) { charts[id].destroy(); delete charts[id]; });
      panels.forEach(function (panel) {
        var data = lastData[panel.dataset.panel];
        if (data) draw(panel, data);
      });
    }

    var resize = new ResizeObserver(function () {
      panels.forEach(function (panel) {
        var chart = charts[panel.dataset.panel];
        var box = panel.querySelector(".chart");
        if (chart && box.clientWidth) chart.setSize({ width: box.clientWidth, height: box.clientHeight });
      });
    });
    panels.forEach(function (panel) { resize.observe(panel.querySelector(".chart")); });
    document.addEventListener("themechange", rebuild);

    loadAll();
    var every = Number(root.dataset.refresh || 0);
    if (every > 0) setInterval(function () { if (!document.hidden) loadAll(); }, every * 1000);
  }

  // Sparklines: total request rate over the last hour, one query per product.
  function sparkline(el) {
    var params = new URLSearchParams({ services: el.dataset.services, range: "1h" });
    fetchPanel("/api/panels/requests?" + params).then(function (data) {
      var total = data.x.map(function (_, i) {
        var sum = null;
        data.series.forEach(function (s) { if (s.values[i] != null) sum = (sum || 0) + s.values[i]; });
        return sum;
      });
      if (total.every(function (v) { return v == null; })) { el.textContent = "no data"; el.className += " faint"; return; }
      var color = cssVar("--accent");
      new uPlot({
        width: el.clientWidth, height: el.clientHeight,
        cursor: { show: false }, legend: { show: false }, select: { show: false },
        axes: [{ show: false }, { show: false }],
        scales: { x: { time: false }, y: { range: function (u, min, max) { return [0, (max || 1) * 1.1]; } } },
        series: [{}, { stroke: color, width: 1.25, fill: alpha(color.length === 7 ? color : "#5794f2", 0.12), points: { show: false } }],
      }, [data.x, total], el);
    }).catch(function (err) {
      el.textContent = "unavailable";
      el.title = err.message;
      el.className += " bad";
    });
  }

  document.querySelectorAll("[data-dashboard]").forEach(function (root) { Dashboard(root); });
  document.querySelectorAll("[data-spark]").forEach(sparkline);
})();
