/* Talis Desk — Eval Dashboard JS overlay.
 *
 * The base SPA-ish JS (tab toggle + /api/veto wiring) lives in
 * `render.py::_DASHBOARD_JS` so the renderer can ship a single self-contained
 * HTML document. This file is reserved for future reviewer-side enhancements
 * (e.g. inline charts) and is loaded from `/static/dashboard.js` only when a
 * caller embeds it explicitly. Empty by default.
 */
