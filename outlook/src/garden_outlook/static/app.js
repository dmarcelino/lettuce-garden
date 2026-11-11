// Handles the bookmarklet handover: when the Outlook page is opened with the
// session material in the URL fragment (#connect=...), decode it, clear the
// fragment so the token is not left in the address bar or history, and POST it to
// /outlook/connect behind Access. No token is ever put in a query string or a log.
(function () {
  "use strict";
  var base = (document.querySelector('meta[name="base"]') || {}).content || "/outlook";
  var csrf = document.body.getAttribute("data-csrf") || "";
  var status = document.getElementById("connect-status");
  function say(text) {
    if (status) status.textContent = text;
  }
  var match = (location.hash || "").match(/#connect=(.+)$/);
  if (!match) return;

  var payload;
  try {
    payload = JSON.parse(decodeURIComponent(escape(atob(decodeURIComponent(match[1])))));
  } catch (e) {
    history.replaceState(null, "", base + "/");
    say("Could not read the connect data. Run the bookmarklet again.");
    return;
  }
  // Drop the fragment before doing anything else.
  history.replaceState(null, "", base + "/");
  say("Connecting…");

  fetch(base + "/connect", {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json", "x-csrf-token": csrf },
    body: JSON.stringify(payload)
  })
    .then(function (response) {
      return response.json().then(function (body) {
        return { ok: response.ok, body: body };
      });
    })
    .then(function (result) {
      if (result.ok) {
        say("Connected as " + (result.body.account || "") + ". Reloading…");
        location.reload();
      } else {
        say("Could not connect: " + (result.body.error || "error"));
      }
    })
    .catch(function () {
      say("Could not reach the server. Try again.");
    });
})();
