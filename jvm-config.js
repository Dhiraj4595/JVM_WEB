/* JVM Shyamali - frontend/backend connection settings.
 *
 * NORMAL SETUP (recommended): leave JVM_API_BASE empty. The website pages and the Python server
 * are one service on one address, so no CORS and no other setup are needed.
 *
 * SPLIT HOSTING (pages on one address, app_server.py on another): put the backend's full https
 * address here, e.g. "https://api.yourschool.in" (no trailing slash), AND set the environment
 * variable JVM_ALLOWED_ORIGINS=https://www.yourschool.in on the backend. See HOSTING.md.
 */
window.JVM_API_BASE = "";

(function () {
  var B = String(window.JVM_API_BASE || "").replace(/\/+$/, "");
  if (!B) return;                                   // same-origin mode: do nothing
  var isApi = function (u) { return /^\/(api\/|healthz)/.test(u); };
  var nativeFetch = window.fetch;
  window.fetch = function (input, init) {
    if (typeof input === "string" && isApi(input)) {
      input = B + input;
      init = Object.assign({}, init, { credentials: "include" });   // send the login cookie cross-origin
    }
    return nativeFetch.call(this, input, init);
  };
  // Links such as fee receipts (/api/fees/receipt?...) must also point at the backend.
  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest && e.target.closest("a[href]");
    if (a && isApi(a.getAttribute("href"))) a.setAttribute("href", B + a.getAttribute("href"));
  }, true);
})();
