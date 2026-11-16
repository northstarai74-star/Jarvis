// Fetches /api/state and shows it. Every value is written with textContent,
// so text that came from a lead, a reply or a booking can never run as code.
(function () {
  "use strict";

  var POLL_MS = 3000;
  var KNOWN_STATES = ["standby", "listening", "thinking", "speaking", "alert"];

  function el(id) { return document.getElementById(id); }

  function node(tag, text, className) {
    var n = document.createElement(tag);
    if (text !== undefined && text !== null) { n.textContent = String(text); }
    if (className) { n.className = className; }
    return n;
  }

  function clear(parent) {
    while (parent.firstChild) { parent.removeChild(parent.firstChild); }
  }

  function when(isoText) {
    var d = new Date(isoText);
    if (!isoText || isNaN(d.getTime())) { return String(isoText || ""); }
    return d.toLocaleString("en-GB", {
      timeZone: "Europe/London", weekday: "short", day: "2-digit", month: "short",
      hour: "2-digit", minute: "2-digit"
    });
  }

  function fill(listId, items, emptyText, build) {
    var list = el(listId);
    clear(list);
    if (!items || !items.length) {
      list.appendChild(node("li", emptyText, "empty"));
      return;
    }
    items.forEach(function (item) { list.appendChild(build(item)); });
  }

  function row(main, meta, className) {
    var li = node("li", null, className);
    li.appendChild(node("span", main));
    li.appendChild(node("span", meta, "meta"));
    return li;
  }

  function stat(parent, label, value) {
    var box = node("div");
    box.appendChild(node("dt", label));
    box.appendChild(node("dd", value));
    parent.appendChild(box);
  }

  function activityClass(text) {
    if (/: (refused|error)/.test(text) || /bad_signature|lockout|tampered/.test(text)) { return "bad"; }
    if (/: pending/.test(text)) { return "wait"; }
    return "";
  }

  function render(data) {
    var state = KNOWN_STATES.indexOf(data.orb) >= 0 ? data.orb : "standby";
    var stateEl = el("state");
    stateEl.textContent = state;
    stateEl.setAttribute("data-state", state);
    el("reason").textContent = data.orb_reason || "";
    el("clock").textContent = when(data.now);
    if (window.JarvisOrb) { window.JarvisOrb.setState(state); }

    var s = data.stats || {};
    var stats = el("stats");
    clear(stats);
    stat(stats, "Sending", s.stopped ? "Stopped" : (s.live ? "Live" : "Not live"));
    stat(stats, "Sent today", (s.sent_today || 0) + " / " + (s.cap || 0));
    stat(stats, "Approvals", s.pending || 0);
    stat(stats, "Firms", s.firms || 0);
    stat(stats, "Leads ready", s.leads_ready || 0);
    stat(stats, "Replies", s.replies || 0);

    fill("activity", data.activity, "Nothing has happened yet.", function (a) {
      return row(a.text, when(a.ts) + " · " + a.actor, activityClass(a.text));
    });
    fill("approvals", data.approvals, "Nothing is waiting.", function (a) {
      return row(a.summary, "id " + a.id + " · expires " + when(a.expires), "wait");
    });
    fill("meetings", data.meetings, "No meetings booked.", function (m) {
      var who = m.firm ? m.name + " (" + m.firm + ")" : m.name;
      return row(who || "Unknown", when(m.start) + " · " + m.status,
                 m.status === "booked" ? "good" : "");
    });
    fill("calls", data.calls, "No calls yet.", function (c) {
      return row(c.reason, when(c.when) + " · " + c.status);
    });
  }

  function offline() {
    var stateEl = el("state");
    stateEl.textContent = "offline";
    stateEl.setAttribute("data-state", "offline");
    el("reason").textContent = "Cannot reach Jarvis. Is the dashboard still running?";
    if (window.JarvisOrb) { window.JarvisOrb.setState("offline"); }
  }

  function poll() {
    fetch("/api/state", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) { throw new Error("bad status"); }
        return response.json();
      })
      .then(render)
      .catch(offline)
      .then(function () { setTimeout(poll, POLL_MS); });
  }

  poll();
})();
