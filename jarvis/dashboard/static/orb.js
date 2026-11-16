// The orb: a sphere of particles drawn on a canvas.
// States: standby, listening, thinking, speaking, alert (plus offline).
// It only draws. It reads nothing from the page and writes no HTML.
(function () {
  "use strict";

  var STATES = {
    standby:   { color: [83, 212, 255],  spin: 0.10, pulse: 0.015, pulseRate: 0.6, jitter: 0.000, glow: 0.35 },
    listening: { color: [73, 224, 164],  spin: 0.18, pulse: 0.050, pulseRate: 1.1, jitter: 0.004, glow: 0.55 },
    thinking:  { color: [183, 155, 255], spin: 0.75, pulse: 0.025, pulseRate: 2.2, jitter: 0.012, glow: 0.60 },
    speaking:  { color: [233, 251, 255], spin: 0.30, pulse: 0.110, pulseRate: 5.5, jitter: 0.008, glow: 0.85 },
    alert:     { color: [255, 84, 102],  spin: 0.22, pulse: 0.080, pulseRate: 3.2, jitter: 0.020, glow: 0.80 },
    offline:   { color: [110, 126, 140], spin: 0.03, pulse: 0.005, pulseRate: 0.3, jitter: 0.000, glow: 0.15 }
  };
  var KEYS = ["spin", "pulse", "pulseRate", "jitter", "glow"];
  var COUNT = 900;

  var canvas = document.getElementById("orb");
  if (!canvas || !canvas.getContext) { return; }
  var ctx = canvas.getContext("2d");
  var reduceMotion = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Evenly spread points on a sphere (Fibonacci lattice).
  var points = [];
  var golden = Math.PI * (3 - Math.sqrt(5));
  for (var i = 0; i < COUNT; i++) {
    var y = 1 - (i / (COUNT - 1)) * 2;
    var r = Math.sqrt(1 - y * y);
    var theta = golden * i;
    points.push({ x: Math.cos(theta) * r, y: y, z: Math.sin(theta) * r, seed: (i * 0.618) % 1 });
  }

  var target = STATES.standby;
  var now = { color: target.color.slice() };
  KEYS.forEach(function (k) { now[k] = target[k]; });

  var size = 0;
  function resize() {
    var ratio = Math.min(window.devicePixelRatio || 1, 2);
    var box = canvas.getBoundingClientRect();
    size = Math.max(120, Math.floor(box.width));
    canvas.width = size * ratio;
    canvas.height = size * ratio;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  }
  window.addEventListener("resize", resize);
  resize();

  var angle = 0;
  var last = performance.now();

  function frame(t) {
    var dt = Math.min(0.05, (t - last) / 1000);
    last = t;
    var ease = 1 - Math.pow(0.02, dt); // glide towards the target state
    KEYS.forEach(function (k) { now[k] += (target[k] - now[k]) * ease; });
    for (var c = 0; c < 3; c++) { now.color[c] += (target.color[c] - now.color[c]) * ease; }

    var motion = reduceMotion ? 0.15 : 1;
    angle += now.spin * dt * motion;
    var seconds = t / 1000;
    var breath = 1 + Math.sin(seconds * now.pulseRate * motion) * now.pulse;
    var cx = size / 2, cy = size / 2;
    var radius = size * 0.34 * breath;
    var rgb = Math.round(now.color[0]) + "," + Math.round(now.color[1]) + "," + Math.round(now.color[2]);

    ctx.clearRect(0, 0, size, size);

    var halo = ctx.createRadialGradient(cx, cy, radius * 0.2, cx, cy, radius * 1.45);
    halo.addColorStop(0, "rgba(" + rgb + "," + (0.16 * now.glow).toFixed(3) + ")");
    halo.addColorStop(1, "rgba(" + rgb + ",0)");
    ctx.fillStyle = halo;
    ctx.fillRect(0, 0, size, size);

    var cosA = Math.cos(angle), sinA = Math.sin(angle);
    var tilt = 0.38, cosT = Math.cos(tilt), sinT = Math.sin(tilt);
    for (var i = 0; i < COUNT; i++) {
      var p = points[i];
      var wobble = 1 + Math.sin(seconds * 3.1 * motion + p.seed * 40) * now.jitter * 6;
      var x = (p.x * cosA + p.z * sinA) * wobble;
      var z = (p.z * cosA - p.x * sinA) * wobble;
      var y = p.y * wobble;
      var y2 = y * cosT - z * sinT;
      var z2 = z * cosT + y * sinT;
      var depth = (z2 + 1) / 2; // 0 at the back, 1 at the front
      var alpha = (0.12 + depth * 0.78) * (0.55 + now.glow * 0.45);
      var dot = 0.6 + depth * 1.5;
      ctx.fillStyle = "rgba(" + rgb + "," + alpha.toFixed(3) + ")";
      ctx.beginPath();
      ctx.arc(cx + x * radius, cy + y2 * radius, dot, 0, Math.PI * 2);
      ctx.fill();
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  window.JarvisOrb = {
    setState: function (name) {
      target = Object.prototype.hasOwnProperty.call(STATES, name) ? STATES[name] : STATES.standby;
    }
  };
})();
