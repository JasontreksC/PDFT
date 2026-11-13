// PDFT home — the scroll-driven translation flow. No dependencies.
//
// The section (#flow) is a tall scroll track with a sticky stage. The scroll position IS the
// playback position: this module only measures it into --p/--s1..--s5 on #flow (plus data-stage),
// and style.css turns those into transform/opacity. Scrubbing back replays everything in reverse.
// Nothing runs off the home view, near the section only, and never under reduced motion
// (there the stylesheet alone shows the steps with the finished sheet).

const flow = document.getElementById("flow");
const hint = document.getElementById("flow-hint");
const reduce = matchMedia("(prefers-reduced-motion: reduce)");

if (hint && flow) {
  hint.addEventListener("click", () => flow.scrollIntoView({ behavior: reduce.matches ? "auto" : "smooth" }));
}

if (flow && !reduce.matches) {
  const chips = [...flow.querySelectorAll(".flow-chip")];
  const steps = [...flow.querySelectorAll(".flow-step")];
  // stages 2-5 over the whole progress: each 0.22 long, starting 0.23 apart (slight overlap);
  // stage 5 ends at p=0.92 and the rest holds the finished sheet. --s1 instead follows the
  // section entering the viewport (see update), so the paper is fully up once the stage pins.
  const STARTS = [0.03, 0.26, 0.49, 0.7];
  const LEN = 0.22;
  const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
  const atHome = () => document.body.classList.contains("at-home");

  // --dx/--dy per chip: the vector from its resting place in the tray back to its text on the
  // paper. Both ends are measured at rest (transforms off); the vector itself never depends on
  // the scroll, so measuring at load/resize is enough.
  let measured = false;
  function measure() {
    if (!atHome() || flow.getBoundingClientRect().height === 0) return; // hidden: retry when seen
    flow.classList.add("flow-measuring");
    for (const chip of chips) {
      const origin = document.getElementById(chip.dataset.origin || "");
      if (!origin) continue;
      const c = chip.getBoundingClientRect();
      const o = origin.getBoundingClientRect();
      if (c.width === 0 || o.width === 0) continue;
      chip.style.setProperty("--dx", `${(o.left + o.width / 2 - (c.left + c.width / 2)).toFixed(1)}px`);
      chip.style.setProperty("--dy", `${(o.top + o.height / 2 - (c.top + c.height / 2)).toFixed(1)}px`);
    }
    flow.classList.remove("flow-measuring");
    measured = true;
  }

  let stage = 0;
  let last = "";
  function update() {
    if (!measured) measure();
    if (!atHome()) return;
    const total = flow.offsetHeight - innerHeight;
    if (total <= 0) return;
    const rect = flow.getBoundingClientRect();
    const p = clamp01(-rect.top / total);
    const s = [clamp01((innerHeight * 0.9 - rect.top) / (innerHeight * 0.6)), ...STARTS.map((st) => clamp01((p - st) / LEN))];
    const key = [p, ...s].map((v) => v.toFixed(4)).join(",");
    if (key !== last) {
      last = key;
      flow.style.setProperty("--p", p.toFixed(4));
      s.forEach((v, i) => flow.style.setProperty(`--s${i + 1}`, v.toFixed(4)));
    }
    const next = 1 + STARTS.filter((st) => p >= st).length;
    if (next !== stage) {
      stage = next;
      flow.dataset.stage = String(next);
      for (const li of steps) {
        if (Number(li.dataset.step) === next) li.setAttribute("aria-current", "step");
        else li.removeAttribute("aria-current");
      }
    }
  }

  // scroll/resize share one rAF; the observer keeps us idle away from the section
  let queued = false;
  let near = true;
  function schedule() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      if (near) update();
    });
  }
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(([e]) => {
      near = e.isIntersecting;
      if (near) schedule();
    }, { rootMargin: "60% 0px" }).observe(flow);
  }
  addEventListener("scroll", schedule, { passive: true });
  addEventListener("resize", () => {
    measured = false; // layout (and both flight ends) moved
    schedule();
  });
  if (document.fonts?.ready) document.fonts.ready.then(() => {
    measured = false;
    schedule();
  });
  schedule();
}
