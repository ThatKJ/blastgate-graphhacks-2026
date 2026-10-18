// Minimal vanilla script for BlastGate marketing pages (CSP compliant)
document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.getElementById("mobile-toggle");
  const nav = document.getElementById("site-nav");
  if (toggle && nav) {
    toggle.addEventListener("click", () => {
      const open = nav.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", String(open));
    });
  }
});
