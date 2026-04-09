---
name: DashCast JavaScript compatibility
description: DashCast uses old Chromium that doesn't support modern JS (fetch, async/await, const/let, template literals, nullish coalescing)
type: feedback
---

JavaScript in pages loaded by DashCast on Chromecast must use ES5 only.

**Why:** DashCast runs on an old Chromium engine that silently fails on modern JS features. When `fetch()`, `async/await`, `const`/`let`, template literals, or `??` are used, the script doesn't execute at all — no errors visible, just a black screen. This caused the display page polling to never start, so no frames were shown.

**How to apply:** Any JavaScript in `/cast/display`, `/cast/startup-check`, or any page loaded via DashCast must use:
- `var` instead of `const`/`let`
- `XMLHttpRequest` instead of `fetch()`
- String concatenation instead of template literals
- `||` instead of `??` (nullish coalescing)
- Regular functions instead of `async/await`
- No arrow functions
