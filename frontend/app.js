// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// 1. Sign in with Cognito managed login (authorization code flow + PKCE).
// 2. POST /api/session with the Cognito access token. The session Lambda finds
//    or launches this user's own MicroVM and returns a one-time launch ticket.
// 3. Navigate to <IDE host>/launch?t=<ticket>. The IDE host redeems the ticket,
//    sets two HttpOnly cookies there (MicroVM endpoint and auth token) and
//    opens /ide/. CloudFront routes every /ide/ request (page, assets,
//    WebSocket) to the user's MicroVM and adds the auth header
//    (infra/ide_router.js). This script never sees the MicroVM token.
// 4. When the MicroVM token expires, CloudFront redirects back here with
//    ?session=expired; this page renews the session and returns to the IDE.
//
// Cognito tokens live in sessionStorage only (cleared when the tab closes).
// The IDE runs on a different hostname, so nothing served from a MicroVM can
// read them.

const cfg = window.APP_CONFIG;
const $ = (id) => document.getElementById(id);
const store = sessionStorage;

// ---- UI helpers -------------------------------------------------------------
function show(id) {
  for (const el of document.querySelectorAll("[data-view]")) el.hidden = el.id !== id;
}
function status(text) { $("status").textContent = text; }

// ---- Sign-in (OAuth 2.0 authorization code + PKCE) --------------------------
function base64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function signIn() {
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = base64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const oauthState = base64url(crypto.getRandomValues(new Uint8Array(16)));
  store.setItem("pkce_verifier", verifier);
  store.setItem("oauth_state", oauthState);
  const params = new URLSearchParams({
    response_type: "code",
    client_id: cfg.clientId,
    redirect_uri: cfg.redirectUri,
    scope: "openid email",
    code_challenge_method: "S256",
    code_challenge: challenge,
    state: oauthState,
  });
  location.assign(`${cfg.cognitoDomain}/oauth2/authorize?${params}`);
}

async function completeSignIn(code, returnedState) {
  const verifier = store.getItem("pkce_verifier");
  const expected = store.getItem("oauth_state");
  store.removeItem("pkce_verifier");
  store.removeItem("oauth_state");
  history.replaceState(null, "", location.pathname); // drop ?code= from the URL
  if (!verifier || returnedState !== expected) throw new Error("sign-in state mismatch");

  const resp = await fetch(`${cfg.cognitoDomain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: cfg.clientId,
      code,
      redirect_uri: cfg.redirectUri,
      code_verifier: verifier,
    }),
  });
  if (!resp.ok) throw new Error(`token exchange failed (${resp.status})`);
  const tokens = await resp.json();
  store.setItem("access_token", tokens.access_token);
  store.setItem("id_token", tokens.id_token);
}

function signOut() {
  store.clear();
  const params = new URLSearchParams({ client_id: cfg.clientId, logout_uri: cfg.redirectUri });
  location.assign(`${cfg.cognitoDomain}/logout?${params}`);
}

function userEmail() {
  const idToken = store.getItem("id_token");
  if (!idToken) return "";
  const payload = JSON.parse(atob(idToken.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
  return payload.email || "";
}

// ---- Session API ------------------------------------------------------------
async function api(method) {
  const resp = await fetch("/api/session", {
    method,
    headers: { Authorization: `Bearer ${store.getItem("access_token")}` },
  });
  if (resp.status === 401) { store.clear(); show("signed-out"); throw new Error("session expired, sign in again"); }
  return { status: resp.status, body: await resp.json() };
}

async function getSession() {
  for (let attempt = 0; attempt < 15; attempt++) {
    const { status: code, body } = await api("POST");
    if (code === 200) return body;
    if (code !== 202) throw new Error(`session API returned ${code}: ${JSON.stringify(body)}`);
    status("your MicroVM is starting…");
    await new Promise((r) => setTimeout(r, (body.retryAfterSeconds || 2) * 1000));
  }
  throw new Error("MicroVM did not become ready in time");
}

// ---- VS Code ----------------------------------------------------------------
async function openIde() {
  status("requesting your MicroVM…");
  const { ticket } = await getSession();
  status("opening VS Code…");
  location.assign(`${cfg.ideUrl}/launch?t=${encodeURIComponent(ticket)}`);
}

async function stopVm() {
  status("terminating your MicroVM…");
  await api("DELETE");
  status("MicroVM terminated. Open VS Code again to get a fresh one.");
}

function fail(err) { status(`error: ${err.message}`); console.error(err); }

// ---- Wire up ----------------------------------------------------------------
$("sign-in").onclick = () => signIn().catch(fail);
$("sign-out").onclick = () => signOut();
$("start").onclick = () => openIde().catch(fail);
$("stop").onclick = () => stopVm().catch(fail);

(async () => {
  const q = new URLSearchParams(location.search);
  try {
    if (q.get("code")) await completeSignIn(q.get("code"), q.get("state"));
  } catch (err) { fail(err); }
  if (store.getItem("access_token")) {
    $("user").textContent = userEmail();
    show("signed-in");
    // Coming back from an expired IDE session: renew it and go straight back.
    if (q.get("session") === "expired") {
      history.replaceState(null, "", location.pathname);
      openIde().catch(fail);
    }
  } else {
    show("signed-out");
  }
})();
