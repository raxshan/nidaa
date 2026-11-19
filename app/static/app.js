// Nidaa dashboard: small progressive enhancements. Every page works without this file.
(function () {
  "use strict";

  // Confirm dialogs on forms marked data-confirm.
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Live percentage next to volume sliders.
  document.querySelectorAll("input[type=range][data-out]").forEach(function (input) {
    var out = input.parentElement.querySelector("output");
    var update = function () { if (out) out.textContent = input.value + "%"; };
    input.addEventListener("input", update);
    update();
  });

  // "Stop after" fields: warn while the value is too short to be heard.
  document.querySelectorAll("input[data-short-stop]").forEach(function (input) {
    var hint = input.parentElement.querySelector("[data-short-stop-hint]");
    var limit = parseFloat(input.getAttribute("data-short-stop")) || 10;
    var update = function () {
      var v = parseFloat(input.value);
      if (hint) hint.hidden = !(v > 0 && v < limit);
    };
    input.addEventListener("input", update);
    update();
  });

  // Event form: show only the fields for the chosen trigger type.
  var typeSelect = document.getElementById("trigger-type");
  if (typeSelect) {
    var sync = function () {
      document.querySelectorAll("[data-show]").forEach(function (el) {
        var on = el.getAttribute("data-show").split(" ").indexOf(typeSelect.value) !== -1;
        el.hidden = !on;
      });
    };
    typeSelect.addEventListener("change", sync);
    sync();
  }

  // Countdown to the next event.
  var countdowns = document.querySelectorAll("[data-countdown]");
  function tick() {
    var now = Date.now();
    countdowns.forEach(function (el) {
      var ms = new Date(el.getAttribute("data-countdown")).getTime() - now;
      if (ms <= 0) { el.textContent = "now"; return; }
      var s = Math.floor(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
      el.textContent = "in " + (h ? h + " h " : "") + m + " min" + (h ? "" : " " + (s % 60) + " s");
    });
  }
  if (countdowns.length) { tick(); setInterval(tick, 1000); }

  // Today page: refresh the scheduler + device badges every 15 s.
  var badge = document.getElementById("sched-badge");
  if (badge) {
    setInterval(function () {
      fetch("/api/status", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(function (st) {
          badge.className = "badge " + (st.alive ? "good" : "bad");
          badge.textContent = st.alive ? "Scheduler running" : "Scheduler not responding";
          document.querySelectorAll("[data-device]").forEach(function (el) {
            if (el.classList.contains("muted")) return;
            var d = (st.devices || {})[el.getAttribute("data-device")];
            el.className = "badge " + (!d ? "unknown" : d.online ? "good" : "bad");
            el.textContent = el.getAttribute("data-device") + (!d ? " · ?" : d.online ? "" : " · offline");
          });
        })
        .catch(function () {
          badge.className = "badge bad";
          badge.textContent = "Dashboard can't read status";
        });
    }, 15000);
  }

  // Play page: poll the result of a queued command.
  var result = document.getElementById("cmd-result");
  if (result) {
    var id = result.getAttribute("data-cmd"), tries = 0, label = result.querySelector(".cmd-status");
    var poll = function () {
      tries++;
      fetch("/api/commands/" + encodeURIComponent(id), { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (c) {
          if (c.pending || !c.result) {
            label.textContent = "waiting for the scheduler…";
            if (tries < 60) setTimeout(poll, 2000);
            return;
          }
          var res = c.result;
          label.textContent = res.status + (res.message ? ": " + res.message : "");
          result.classList.add(res.status === "started" ? "ok" : "error");
        })
        .catch(function () { if (tries < 60) setTimeout(poll, 3000); });
    };
    poll();
  }
})();
