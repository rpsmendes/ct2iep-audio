"use strict";
// Header buttons shared with the other ct2iep-* apps: keep the screen on and full screen
// (same behaviour as ct2iep-amp).

const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
};

// ---------- keep the screen on ----------
// Screen Wake Lock (needs HTTPS); fallback: a tiny muted looping video (needs a first tap).
const wake = { want: false, lock: null, video: null };
async function wakeAcquire() {
  if ("wakeLock" in navigator && window.isSecureContext) {
    try {
      wake.lock = await navigator.wakeLock.request("screen");
      wake.lock.addEventListener("release", () => { wake.lock = null; });
      return "lock";
    } catch (e) { /* fall back to the video */ }
  }
  if (!wake.video) {
    const v = document.createElement("video");
    v.muted = true; v.loop = true; v.setAttribute("playsinline", ""); v.setAttribute("aria-hidden", "true");
    v.style.cssText = "position:fixed;width:1px;height:1px;opacity:0.01;pointer-events:none;bottom:0;left:0";
    for (const [src, type] of [["/static/nosleep.webm", "video/webm"], ["/static/nosleep.mp4", "video/mp4"]]) {
      const so = document.createElement("source"); so.src = src; so.type = type; v.appendChild(so);
    }
    document.body.appendChild(v);
    wake.video = v;
  }
  try { await wake.video.play(); return "video"; } catch (e) { return null; }
}
function wakeRelease() {
  if (wake.lock) { wake.lock.release().catch(() => {}); wake.lock = null; }
  if (wake.video) wake.video.pause();
}
async function setWake(on, userClick = false) {
  wake.want = on;
  store.set("ct2iep.wake", on ? "1" : "0");
  const btn = $("#btnWake");
  btn.setAttribute("aria-pressed", String(on));
  if (!on) { wakeRelease(); btn.title = "Keep the screen on while this page is open"; return; }
  const how = await wakeAcquire();
  btn.title = how === "lock" ? "Screen kept on (browser wake lock)"
    : how === "video" ? "Trying the video method: recent Chrome / Brave on Android ignore it; open the app over HTTPS"
    : "Could not keep the screen on: tap the button again";
  btn.classList.toggle("weak", how !== "lock");
  if (!how) btn.setAttribute("aria-pressed", "false");
  if (how !== "lock" && userClick) {
    toast(window.isSecureContext
      ? "This browser refused to keep the screen on."
      : "Keep screen on needs HTTPS on Android: open the app through ct2iep-station (port 7080).", 6000, true);
  }
}
$("#btnWake").addEventListener("click", () => setWake(!wake.want, true));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && wake.want && !wake.lock) wakeAcquire();
});
if (store.get("ct2iep.wake") === "1") {
  setWake(true);
  document.addEventListener("pointerdown", () => { if (wake.want && !wake.lock) wakeAcquire(); }, { once: true });
}

// ---------- full screen ----------
const fsBtn = $("#btnFull");
if (!document.fullscreenEnabled) fsBtn.hidden = true;
fsBtn.addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else document.documentElement.requestFullscreen({ navigationUI: "hide" }).catch(() => {});
});
document.addEventListener("fullscreenchange", () => fsBtn.setAttribute("aria-pressed", String(!!document.fullscreenElement)));
