// Shared page behaviour: theme toggle, relative times, polling. No framework, no build step.
(function () {
  "use strict";

  document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("theme", next); } catch (e) {}
      document.dispatchEvent(new CustomEvent("themechange"));
    });
  });

  // <time datetime="..."> gets a relative label; the absolute time stays in the tooltip.
  function relative(iso) {
    var seconds = (Date.now() - new Date(iso).getTime()) / 1000;
    var future = seconds < 0;
    seconds = Math.abs(seconds);
    var text;
    if (seconds < 45) text = "just now";
    else if (seconds < 3600) text = Math.round(seconds / 60) + "m";
    else if (seconds < 86400) text = Math.round(seconds / 3600) + "h";
    else text = Math.round(seconds / 86400) + "d";
    if (text === "just now") return text;
    return future ? "in " + text : text + " ago";
  }
  function refreshTimes() {
    document.querySelectorAll("time[datetime]:not([data-clock])").forEach(function (el) {
      if (!el.dataset.absolute) el.dataset.absolute = el.textContent;
      el.textContent = relative(el.getAttribute("datetime"));
    });
  }
  refreshTimes();
  setInterval(refreshTimes, 30000);

  // Pages that show live state reload themselves, but only while the tab is visible.
  var reloadAfter = Number(document.body.dataset.autoreload || 0);
  if (reloadAfter > 0) {
    setInterval(function () { if (!document.hidden) location.reload(); }, reloadAfter * 1000);
  }

  // A busy incident polls its JSON and reloads once the state moves on.
  var poll = document.querySelector("[data-poll-incident]");
  if (poll) {
    var id = poll.dataset.pollIncident;
    var state = poll.dataset.state;
    setInterval(function () {
      if (document.hidden) return;
      fetch("/api/incidents/" + encodeURIComponent(id), { headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) { if (data && data.state !== state) location.reload(); })
        .catch(function () {});
    }, 3000);
  }
})();
