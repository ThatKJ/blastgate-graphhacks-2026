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

  // Demo Video Modal Handlers
  const modal = document.getElementById("video-modal");
  const openTriggers = document.querySelectorAll("[data-open-video]");
  const closeTriggers = document.querySelectorAll("[data-close-video]");
  const videoPlayer = document.getElementById("modal-video-player");

  function openVideoModal() {
    if (!modal) return;
    modal.classList.add("is-active");
    document.body.style.overflow = "hidden";
    if (videoPlayer && typeof videoPlayer.play === "function") {
      videoPlayer.play().catch(() => {});
    }
  }

  function closeVideoModal() {
    if (!modal) return;
    modal.classList.remove("is-active");
    document.body.style.overflow = "";
    if (videoPlayer && typeof videoPlayer.pause === "function") {
      videoPlayer.pause();
    }
  }

  openTriggers.forEach((trigger) => {
    trigger.addEventListener("click", (e) => {
      e.preventDefault();
      openVideoModal();
    });
  });

  closeTriggers.forEach((trigger) => {
    trigger.addEventListener("click", (e) => {
      e.preventDefault();
      closeVideoModal();
    });
  });

  if (modal) {
    modal.addEventListener("click", (e) => {
      if (e.target === modal) {
        closeVideoModal();
      }
    });
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modal && modal.classList.contains("is-active")) {
      closeVideoModal();
    }
  });
});
