"""The connect bookmarklet, built for one stack's garden origin.

The person runs it while signed in on https://outlook.office.com. It reads what the
Outlook web app already cached for them (the access token, the refresh token, the
account and the authority) and opens the stack's Outlook page with that material in
the URL *fragment* — not a cross-origin fetch, which Outlook's CSP would block, and
not a query string, which would reach a server log. The Outlook page's own script
(static/app.js) reads the fragment and POSTs it to /outlook/connect behind Access.

This is best-effort by nature: it depends on how the Outlook web app stores its
session, which Microsoft can change. It is plain, readable JavaScript so the person
can see exactly what it does before adding it to their bookmarks bar; the readable
source is shown on the page beside the draggable link.

`READABLE` is the source; `href(origin)` is the `javascript:` URL for the link.
"""

from __future__ import annotations

# The readable source. `__ORIGIN__` is replaced with the stack's garden origin.
READABLE = r"""
(function () {
  var ORIGIN = "__ORIGIN__";
  var AUD = "outlook.office.com";
  function entries() {
    var out = [];
    for (var i = 0; i < localStorage.length; i++) {
      var k = localStorage.key(i);
      try { out.push([k, JSON.parse(localStorage.getItem(k))]); } catch (e) {}
    }
    return out;
  }
  function pick(kind, wantAud) {
    var best = null;
    entries().forEach(function (e) {
      var k = e[0].toLowerCase(), v = e[1];
      if (!v || typeof v !== "object" || !v.secret) return;
      if (k.indexOf(kind) === -1) return;
      if (wantAud) {
        var t = (v.target || "") + " " + k;
        if (t.toLowerCase().indexOf(AUD) === -1) return;
      }
      var exp = Number(v.expiresOn || 0);
      if (!best || exp > best.exp) best = { v: v, exp: exp };
    });
    return best ? best.v : null;
  }
  function account() {
    var found = null;
    entries().forEach(function (e) {
      var v = e[1];
      if (v && typeof v === "object" && (v.username || v.home_account_id) && e[0].toLowerCase().indexOf("account") !== -1) {
        found = v;
      }
    });
    return found || {};
  }
  var at = pick("accesstoken", true);
  var rt = pick("refreshtoken", false);
  var acc = account();
  if (!at || !at.secret) {
    alert("Could not find an Outlook access token. Open https://outlook.office.com/mail/, wait for it to load, then run this again.");
    return;
  }
  var tenant = at.realm || acc.realm || acc.tenantId || "organizations";
  var payload = {
    accessToken: at.secret,
    refreshToken: rt ? rt.secret : null,
    expiresOn: Number(at.expiresOn || 0),
    account: (acc.username || "").toLowerCase(),
    clientId: at.clientId || rt && rt.clientId || "",
    tokenEndpoint: "https://login.microsoftonline.com/" + tenant + "/oauth2/v2.0/token",
    scopes: (at.target || "").split(" ").filter(Boolean)
  };
  var frag = encodeURIComponent(btoa(unescape(encodeURIComponent(JSON.stringify(payload)))));
  window.open(ORIGIN + "/outlook/#connect=" + frag, "_blank");
})();
"""


def readable(origin: str) -> str:
    """The readable source with the origin filled in (shown on the page)."""
    return READABLE.replace("__ORIGIN__", origin).strip()


def href(origin: str) -> str:
    """The `javascript:` URL for the draggable link: the source on one line."""
    source = readable(origin)
    # One line, so it survives being dragged to the bookmarks bar.
    one_line = " ".join(line.strip() for line in source.splitlines() if line.strip())
    return "javascript:" + one_line
