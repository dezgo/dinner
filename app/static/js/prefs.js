// Applied before first paint: remembered theme and text size (per device).
(function () {
  try {
    var t = localStorage.getItem("dt_theme");
    if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
    var f = parseInt(localStorage.getItem("dt_fs") || "", 10);
    if (f >= 14 && f <= 26) document.documentElement.style.setProperty("--fs", f + "px");
  } catch (e) { /* storage blocked: defaults are fine */ }
})();
