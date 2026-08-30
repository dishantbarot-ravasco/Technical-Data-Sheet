/**
 * auth.js - Session management, authentication helpers, and UI utilities
 * for the INDUS TDS Automation App.
 *
 * 2FA flow: device-trust + email OTP (Instagram-style "new device" check),
 * implemented by login() + verifyDevice() below (backed by
 * django_backend/apps/api/routers/device_views.py). A separate TOTP
 * (Google Authenticator) design was scaffolded earlier but never wired up
 * and has since been removed from the backend entirely — this file's
 * device-trust + email-OTP flow is the only 2FA this app implements.
 *
 * What's actually true today:
 *   - login() returns an intermediate status object; on a trusted device it
 *     already contains the full session, on a new device it's
 *     { status: 'device_verify' } and the caller must call verifyDevice(code).
 *   - setSession() stores { user_id, role, full_name, email, token }. `token`
 *     is kept for backward compatibility with sessions created before the
 *     httpOnly-cookie approach; getAuthHeaders() only sends a Bearer header
 *     when an old-style token is present, otherwise the cookie does the work.
 *   - requireAuth() checks for user_id (new) or token (old sessions, backward compat).
 *     If neither is in sessionStorage (fresh tab/browser restart), it falls back to
 *     asking the server via /api/auth/me (+ a silent /api/auth/token/refresh retry) —
 *     the tds_access/tds_refresh httpOnly cookies can keep a trusted device signed in
 *     for up to 30 days without a password, so a missing sessionStorage entry alone
 *     no longer forces a re-login.
 *   - logout() is async: calls /api/auth/logout to clear the httpOnly cookie
 *     before clearing sessionStorage and redirecting.
 *
 * Exports:
 *   getSession, setSession, clearSession
 *   getAuthHeaders
 *   requireAuth
 *   recoverSession       - cookie-only session recovery, used by index.html and requireAuth()
 *   logout
 *   login
 *   verifyDevice          - completes the live device-trust + email-OTP 2FA step
 *   showToast
 *   openChangePasswordModal
 *   openAuthChangePasswordModal
 *   populateNavUser
 *
 * No imports - this module has no dependencies so it can be loaded first.
 *
 * Also has a side effect on import: injects the mobile hamburger nav toggle
 * (see _initMobileNav() at the bottom) into any page with a .topnav — no
 * per-page wiring needed since every page importing this module gets it.
 */

// Key used to store the session object in sessionStorage.
const SESSION_KEY = 'tds_session';

// Backend base URL - relative so it works regardless of hostname/port.
const API_BASE    = '/api';

/**
 * Escape a value for safe insertion into an innerHTML template string.
 * Used by showToast() below, since toast messages are often server-supplied
 * text (error details) rather than a fixed literal string.
 */
function _escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* ══════════════════════════════════════════════════════════
   SECTION: Session Read / Write / Clear
   New session shape (Phase 5): { user_id, role, full_name?, email? }
   Old session shape (backward compat): { token, user_id, role, full_name?, email? }
   The JWT token now lives in the httpOnly cookie - not in sessionStorage.
══════════════════════════════════════════════════════════ */

/**
 * Read the current session from sessionStorage.
 * Returns null if nothing is stored or if the stored value is not valid JSON.
 * @returns {{ user_id: number, role: string, full_name?: string, email?: string, token?: string } | null}
 */
export function getSession() {
  try { return JSON.parse(sessionStorage.getItem(SESSION_KEY)); } catch { return null; }
}

/**
 * Persist a session object in sessionStorage.
 * @param {{ user_id: number, role: string, full_name?: string, email?: string }} data
 */
export function setSession(data) {
  sessionStorage.setItem(SESSION_KEY, JSON.stringify(data));
}

/**
 * Remove the session from sessionStorage (client-side logout).
 * The httpOnly cookie is cleared separately via /api/auth/logout.
 */
export function clearSession() { sessionStorage.removeItem(SESSION_KEY); }

/* ══════════════════════════════════════════════════════════
   SECTION: Auth Headers
   Phase 5: new sessions use httpOnly cookie auth - no Authorization header.
   Old sessions (with a stored token) still send the Bearer header for compat.
══════════════════════════════════════════════════════════ */

/**
 * Return request headers for authenticated API calls.
 *
 * New sessions (Phase 5): returns {} - the httpOnly cookie is sent automatically
 *   by the browser on every same-origin request.
 * Old sessions (backward compat): returns { Authorization: 'Bearer <token>' }
 *   so existing sessions keep working after upgrade.
 *
 * @returns {{ Authorization?: string }}
 */
export function getAuthHeaders() {
  const s = getSession();
  // Old sessions still have a token in sessionStorage - keep them working
  if (s?.token) return { 'Authorization': `Bearer ${s.token}` };
  // New sessions: httpOnly cookie handles authentication automatically
  return {};
}

/* ══════════════════════════════════════════════════════════
   SECTION: Require Auth Guard
══════════════════════════════════════════════════════════ */

/**
 * Enforce that the current page requires a logged-in session.
 * Checks for user_id (new sessions) or token (old sessions, backward compat).
 * If neither is found, redirects to the login page.
 *
 * @param {string} [redirectTo='index.html']
 * @returns {{ user_id: number, role: string } | null}
 */
export async function requireAuth(redirectTo = 'index.html') {
  const s = getSession();
  // Fast path: sessionStorage already has a session for this tab.
  if (s?.user_id || s?.token) return s;

  // No sessionStorage session — this happens on every fresh tab/browser
  // restart, since sessionStorage doesn't survive those. That used to mean
  // an automatic redirect to login even when the httpOnly tds_access (or
  // tds_refresh) cookie was still perfectly valid. Before giving up, ask the
  // server whether this browser is still authenticated (and silently
  // refresh the access token via the cookie-backed refresh flow if it had
  // merely expired) — only redirect to login if that also fails.
  const recovered = await recoverSession();
  if (recovered) { setSession(recovered); return recovered; }

  window.location.href = redirectTo;
  return null;
}

/**
 * Attempt to recover a session purely from cookies (no sessionStorage),
 * refreshing the access token once via the httpOnly tds_refresh cookie if
 * needed. Returns a session-shaped object on success, or null.
 *
 * Exported (not just used internally by requireAuth()) because index.html's
 * "already logged in?" check needs the same cookie-based recovery — without
 * it, a fresh tab/browser restart would show the login form even with a
 * fully valid 30-day tds_refresh cookie, since sessionStorage alone can't
 * tell it otherwise.
 */
export async function recoverSession() {
  try {
    let res = await fetch(`${API_BASE}/auth/me`, { credentials: 'include' });
    if (res.status === 401) {
      const r = await fetch(`${API_BASE}/auth/token/refresh`, {
        method:      'POST',
        credentials: 'include',
        headers:     { 'Content-Type': 'application/json' },
        body:        '{}',
      });
      if (!r.ok) return null;
      res = await fetch(`${API_BASE}/auth/me`, { credentials: 'include' });
    }
    if (!res.ok) return null;
    const data = await res.json();
    return { user_id: data.user_id, role: data.role, full_name: data.full_name, email: data.email };
  } catch {
    return null;
  }
}

/* ══════════════════════════════════════════════════════════
   SECTION: Logout
══════════════════════════════════════════════════════════ */

/**
 * Log the user out:
 *   1. Call /api/auth/logout to clear the httpOnly cookie server-side.
 *   2. Clear sessionStorage.
 *   3. Redirect to the login page.
 *
 * Made async so the cookie is cleared before navigation. Event listeners
 * handle async functions automatically (the click handler awaits the promise).
 */
export async function logout() {
  try {
    await fetch(`${API_BASE}/auth/logout`, { method: 'POST' });
  } catch (_) { /* ignore network errors - session is cleared locally anyway */ }
  clearSession();
  window.location.href = 'index.html';
}

/* ══════════════════════════════════════════════════════════
   SECTION: Login - device-aware 2FA
   POST /api/auth/login returns one of two shapes:
     Trusted device → { status:'ok', access_token, user_id, role, ... }
     New device     → { status:'device_verify' }
══════════════════════════════════════════════════════════ */

/**
 * Authenticate with email + password.
 *
 * Returns one of:
 *   { status: 'ok', access_token, user_id, role, full_name, email }
 *     → session is set automatically; caller should redirect to home.html.
 *   { status: 'device_verify' }
 *     → a 6-digit OTP was emailed; caller should show the device-verify step.
 *
 * @param {string} email
 * @param {string} password
 * @returns {Promise<{ status: string, access_token?: string, user_id?: number, role?: string }>}
 * @throws {Error} With a user-friendly message on any failure
 */
export async function login(email, password) {
  const res = await fetch(`${API_BASE}/auth/login`, {
    method:      'POST',
    credentials: 'include',                               // send/receive session cookie
    headers:     { 'Content-Type': 'application/json' },
    body:        JSON.stringify({ email: email.trim(), password }),
  });

  if (res.status === 401) throw new Error('Incorrect email or password.');
  if (res.status === 403) throw new Error('This account has been deactivated. Contact your administrator.');
  if (res.status === 429) throw new Error('Too many login attempts. Please wait a minute and try again.');
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || 'Cannot reach the server. Is the backend running?');
  }

  const data = await res.json();
  if (data.detail) throw new Error(data.detail);

  // Trusted device - set session immediately so the caller can redirect
  if (data.status === 'ok') {
    // SECURITY: TDSLoginView (auth_views.py) now sets the httpOnly tds_access
    // cookie on this exact response, and TDSCookieJWTAuthentication already
    // reads that cookie first on every request — so the JWT no longer needs
    // to be duplicated into sessionStorage, which is readable by any script
    // on the page (including via an XSS bug). We deliberately do NOT store
    // data.access_token here anymore. getAuthHeaders() still supports a
    // `token` field for any pre-existing session created before this change
    // that may still be sitting in a returning user's sessionStorage.
    const session = {
      user_id:   data.user_id,
      role:      data.role,
      full_name: data.full_name || '',
      email:     data.email     || '',
    };
    setSession(session);
  }

  return data;   // caller checks data.status === 'device_verify' to show OTP step
}

/* ══════════════════════════════════════════════════════════
   SECTION: Device Verification
   Called when login() returns { status: 'device_verify' }
══════════════════════════════════════════════════════════ */

/**
 * Verify the 6-digit OTP that was emailed for a new-device login.
 *
 * On success:
 *   - Server registers the device (sets httpOnly tds_device cookie - 1 year).
 *   - Returns a full JWT; this function stores it in sessionStorage.
 *   - On future logins from this browser, device is trusted automatically.
 *
 * @param {string} code  - 6-digit OTP from the verification email
 * @returns {Promise<{ user_id: number, role: string, full_name: string, email: string }>}
 * @throws {Error} With a user-friendly message on failure
 */
export async function verifyDevice(code) {
  const res = await fetch(`${API_BASE}/auth/device-verify`, {
    method:      'POST',
    credentials: 'include',                               // must include session cookie
    headers:     { 'Content-Type': 'application/json' },
    body:        JSON.stringify({ code: code.trim() }),
  });

  if (res.status === 401) throw new Error('Session expired. Please sign in again.');
  if (res.status === 429) throw new Error('Too many attempts. Please wait a minute.');
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || 'Invalid or expired code. Please try again.');
  }

  const data = await res.json();
  // SECURITY: same reasoning as login() above — device_verify (device_views.py)
  // now sets the httpOnly tds_access cookie on this response too, so the JWT
  // is deliberately not duplicated into sessionStorage here either.
  const session = {
    user_id:   data.user_id,
    role:      data.role,
    full_name: data.full_name || '',
    email:     data.email     || '',
  };
  setSession(session);
  return session;
}

/* ══════════════════════════════════════════════════════════
   SECTION: Toast Notifications
══════════════════════════════════════════════════════════ */

/**
 * Show a temporary toast notification in the bottom-right corner.
 * @param {string} message
 * @param {'info'|'success'|'error'|'warning'} [type='info']
 * @param {number} [duration=3500]
 */
export function showToast(message, type = 'info', duration = 3500) {
  let c = document.getElementById('toast-container');
  if (!c) { c = document.createElement('div'); c.id = 'toast-container'; document.body.appendChild(c); }
  // ACCESSIBILITY: toasts are the app's most-used dynamic-message mechanism
  // (every create/update/delete/error across the whole app goes through
  // here) but the container had no aria-live region, so a screen reader
  // never announced any of them — set once, idempotently, since 8 pages
  // declare this div statically in HTML and none of them had it either.
  if (!c.hasAttribute('aria-live')) {
    c.setAttribute('aria-live', 'polite');
    c.setAttribute('aria-atomic', 'true');
  }

  const icons = { success: '✓', error: '✕', info: 'ℹ', warning: '⚠' };
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  // SECURITY (fixed): `message` is frequently a server error string (e.g.
  // err.message / err.detail from a validation failure) that can echo back
  // caller-supplied text -- escape it before inserting into innerHTML so a
  // crafted value can't inject markup into every page that calls showToast().
  t.innerHTML = `<span>${icons[type]||'•'}</span><span>${_escapeHtml(message)}</span>`;
  c.appendChild(t);

  setTimeout(() => {
    t.style.cssText += 'opacity:0;transform:translateY(8px);transition:all .3s;';
    setTimeout(() => t.remove(), 350);
  }, duration);
}

/* ══════════════════════════════════════════════════════════
   SECTION: Change Password Modal
══════════════════════════════════════════════════════════ */

function _injectChangePasswordModal() {
  if (document.getElementById('change-pw-modal')) return;

  const div = document.createElement('div');
  div.innerHTML = `
  <div id="change-pw-modal" style="display:none;position:fixed;inset:0;z-index:9000;
       background:rgba(0,0,0,.55);backdrop-filter:blur(3px);
       align-items:center;justify-content:center;">
    <div style="background:#fff;border:1px solid #E2E8F0;border-radius:12px;
                width:420px;max-width:95vw;overflow:hidden;
                box-shadow:0 24px 64px rgba(0,0,0,.18);">
      <div style="background:#1A2535;padding:20px 24px;border-bottom:3px solid #D4940A;
                  display:flex;align-items:center;justify-content:space-between;">
        <div>
          <h3 style="margin:0;font-family:Montserrat,sans-serif;font-size:14px;font-weight:800;
                     letter-spacing:.06em;color:#fff;">Change Password</h3>
          <p id="cpw-subtitle" style="margin:4px 0 0;font-size:11px;color:rgba(255,255,255,.55);">
            A one-time code will be sent to your email</p>
        </div>
        <button id="cpw-close" style="background:none;border:none;color:rgba(255,255,255,.6);
                font-size:18px;cursor:pointer;padding:4px 8px;border-radius:4px;">✕</button>
      </div>
      <!-- Step 1: Enter email to receive OTP -->
      <div id="cpw-step1" style="padding:28px 24px;">
        <div style="margin-bottom:18px;">
          <label style="display:block;font-size:11px;font-weight:600;letter-spacing:.06em;
                        text-transform:uppercase;color:#4A5568;margin-bottom:6px;">Your Account Email</label>
          <input id="cpw-email" type="email" placeholder="you@company.com" style="
            width:100%;padding:10px 13px;border:1px solid #CBD5E0;border-radius:6px;
            font-size:13px;outline:none;box-sizing:border-box;"
            onfocus="this.style.borderColor='#C17F0A'" onblur="this.style.borderColor='#CBD5E0'" />
        </div>
        <p style="font-size:11px;color:#5B6472;margin:0 0 20px;line-height:1.5;">
          We'll send a 6-digit code to this address. Valid for 10 minutes.</p>
        <div id="cpw-err1" style="display:none;padding:10px 12px;background:#FFF5F5;
             border:1px solid #FED7D7;border-radius:6px;font-size:12px;color:#C53030;margin-bottom:16px;"></div>
        <button id="cpw-btn-send" style="width:100%;padding:12px;background:#C17F0A;border:none;
          border-radius:6px;font-family:Montserrat,sans-serif;font-size:11px;font-weight:800;
          letter-spacing:.1em;text-transform:uppercase;color:#1A2535;cursor:pointer;">Send OTP Code</button>
      </div>
      <!-- Step 2: Enter OTP + new password -->
      <div id="cpw-step2" style="display:none;padding:28px 24px;">
        <div style="background:#F0FFF4;border:1px solid #C6F6D5;border-radius:6px;
                    padding:10px 14px;margin-bottom:20px;font-size:12px;color:#1A7A4A;">
          ✓ OTP sent to <strong id="cpw-sent-to"></strong>
        </div>
        <div style="margin-bottom:16px;">
          <label style="display:block;font-size:11px;font-weight:600;letter-spacing:.06em;
                        text-transform:uppercase;color:#4A5568;margin-bottom:6px;">OTP Code</label>
          <input id="cpw-otp" type="text" placeholder="123456" maxlength="6" style="
            width:100%;padding:10px 13px;border:1px solid #CBD5E0;border-radius:6px;
            font-size:22px;letter-spacing:.3em;font-family:Courier New,monospace;font-weight:700;
            text-align:center;outline:none;box-sizing:border-box;"
            onfocus="this.style.borderColor='#C17F0A'" onblur="this.style.borderColor='#CBD5E0'" />
        </div>
        <div style="margin-bottom:16px;">
          <label style="display:block;font-size:11px;font-weight:600;letter-spacing:.06em;
                        text-transform:uppercase;color:#4A5568;margin-bottom:6px;">New Password</label>
          <div style="position:relative;">
            <input id="cpw-pw1" type="password" placeholder="Min. 8 characters" style="
              width:100%;padding:10px 40px 10px 13px;border:1px solid #CBD5E0;border-radius:6px;
              font-size:13px;outline:none;box-sizing:border-box;"
              onfocus="this.style.borderColor='#C17F0A'" onblur="this.style.borderColor='#CBD5E0'" />
            <button type="button" id="cpw-toggle-pw1" aria-label="Toggle password visibility" style="
              position:absolute;right:10px;top:50%;transform:translateY(-50%);
              background:none;border:none;color:#5B6472;cursor:pointer;font-size:14px;
              padding:2px;line-height:1;">👁</button>
          </div>
        </div>
        <div style="margin-bottom:20px;">
          <label style="display:block;font-size:11px;font-weight:600;letter-spacing:.06em;
                        text-transform:uppercase;color:#4A5568;margin-bottom:6px;">Confirm Password</label>
          <div style="position:relative;">
            <input id="cpw-pw2" type="password" placeholder="Repeat password" style="
              width:100%;padding:10px 40px 10px 13px;border:1px solid #CBD5E0;border-radius:6px;
              font-size:13px;outline:none;box-sizing:border-box;"
              onfocus="this.style.borderColor='#C17F0A'" onblur="this.style.borderColor='#CBD5E0'" />
            <button type="button" id="cpw-toggle-pw2" aria-label="Toggle password visibility" style="
              position:absolute;right:10px;top:50%;transform:translateY(-50%);
              background:none;border:none;color:#5B6472;cursor:pointer;font-size:14px;
              padding:2px;line-height:1;">👁</button>
          </div>
        </div>
        <div id="cpw-err2" style="display:none;padding:10px 12px;background:#FFF5F5;
             border:1px solid #FED7D7;border-radius:6px;font-size:12px;color:#C53030;margin-bottom:16px;"></div>
        <div style="display:flex;gap:10px;">
          <button id="cpw-btn-back" style="flex:0;padding:12px 16px;background:transparent;
            border:1px solid #CBD5E0;border-radius:6px;font-size:11px;font-weight:700;
            color:#4A5568;cursor:pointer;">← Back</button>
          <button id="cpw-btn-verify" style="flex:1;padding:12px;background:#C17F0A;border:none;
            border-radius:6px;font-family:Montserrat,sans-serif;font-size:11px;font-weight:800;
            letter-spacing:.1em;text-transform:uppercase;color:#1A2535;cursor:pointer;">Change Password</button>
        </div>
        <div style="text-align:center;margin-top:12px;">
          <button id="cpw-btn-resend" style="background:none;border:none;font-size:11px;
            color:#C17F0A;cursor:pointer;text-decoration:underline;">Resend OTP</button>
        </div>
      </div>
    </div>
  </div>`;

  document.body.appendChild(div.firstElementChild);

  const modal = document.getElementById('change-pw-modal');
  let otpEmail = '';

  const show  = (id) => { document.getElementById(id).style.display='block'; };
  const hide  = (id) => { document.getElementById(id).style.display='none'; };
  const setErr = (id,msg) => { const e=document.getElementById(id); e.textContent=msg; e.style.display='block'; };
  const clrErr = (id) => { document.getElementById(id).style.display='none'; };

  document.getElementById('cpw-close').addEventListener('click', () => { modal.style.display='none'; });
  modal.addEventListener('click', (e) => { if(e.target===modal) modal.style.display='none'; });

  // ── Show/hide toggles for the New Password / Confirm Password fields ──────
  ['cpw-pw1', 'cpw-pw2'].forEach((inputId) => {
    const toggleBtn = document.getElementById(`${inputId.replace('cpw-', 'cpw-toggle-')}`);
    const input     = document.getElementById(inputId);
    toggleBtn.addEventListener('click', () => {
      input.type = input.type === 'password' ? 'text' : 'password';
    });
  });

  async function doSend() {
    clrErr('cpw-err1');
    const email = document.getElementById('cpw-email').value.trim();
    if (!email) { setErr('cpw-err1','Please enter your email address.'); return; }

    const btn = document.getElementById('cpw-btn-send');
    btn.disabled=true; btn.textContent='Sending…';
    try {
      const res = await fetch(`${API_BASE}/auth/request-otp`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({email}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Request failed');

      otpEmail = email;
      document.getElementById('cpw-sent-to').textContent = email;
      document.getElementById('cpw-subtitle').textContent = 'Enter the OTP sent to your email';
      hide('cpw-step1'); show('cpw-step2');
      document.getElementById('cpw-otp').focus();
    } catch(err) { setErr('cpw-err1', err.message); }
    finally { btn.disabled=false; btn.textContent='Send OTP Code'; }
  }

  document.getElementById('cpw-btn-send').addEventListener('click', doSend);
  document.getElementById('cpw-email').addEventListener('keydown', (e)=>{ if(e.key==='Enter') doSend(); });

  document.getElementById('cpw-btn-verify').addEventListener('click', async () => {
    clrErr('cpw-err2');
    const otp = document.getElementById('cpw-otp').value.trim();
    const pw1 = document.getElementById('cpw-pw1').value;
    const pw2 = document.getElementById('cpw-pw2').value;

    if (!otp || otp.length!==6) { setErr('cpw-err2','Enter the 6-digit OTP code.'); return; }
    if (!pw1 || pw1.length<8)   { setErr('cpw-err2','Password must be at least 8 characters.'); return; }
    if (pw1!==pw2)               { setErr('cpw-err2','Passwords do not match.'); return; }

    const btn = document.getElementById('cpw-btn-verify');
    btn.disabled=true; btn.textContent='Verifying…';
    try {
      const res = await fetch(`${API_BASE}/auth/verify-otp`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({email:otpEmail, otp, new_password:pw1}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Verification failed');

      // Clear any "Remember me" credentials saved on the login page (see
      // index.html's SAVED_CREDS_KEY) - otherwise the OLD password would keep
      // getting silently pre-filled into the login form after this change,
      // even though it no longer works. The key name is duplicated here
      // (rather than imported) since index.html defines it inline and this
      // modal can be opened from any page, not just index.html.
      try { localStorage.removeItem('tds_saved_credentials'); } catch (_) {}

      modal.style.display='none';
      showToast('Password changed. Please sign in again.','success',5000);
      setTimeout(logout, 2500);
    } catch(err) { setErr('cpw-err2', err.message); }
    finally { btn.disabled=false; btn.textContent='Change Password'; }
  });

  document.getElementById('cpw-btn-back').addEventListener('click', () => {
    hide('cpw-step2'); show('cpw-step1'); clrErr('cpw-err2');
    document.getElementById('cpw-subtitle').textContent='A one-time code will be sent to your email';
  });

  document.getElementById('cpw-btn-resend').addEventListener('click', () => {
    hide('cpw-step2'); show('cpw-step1'); clrErr('cpw-err1');
  });
}

/**
 * Open the change-password modal.
 * @param {string} [prefillEmail='']
 */
export function openChangePasswordModal(prefillEmail='') {
  _injectChangePasswordModal();

  const modal = document.getElementById('change-pw-modal');
  document.getElementById('cpw-step1').style.display='block';
  document.getElementById('cpw-step2').style.display='none';
  document.getElementById('cpw-err1').style.display='none';
  document.getElementById('cpw-err2').style.display='none';
  document.getElementById('cpw-otp').value='';
  document.getElementById('cpw-pw1').value='';
  document.getElementById('cpw-pw2').value='';
  document.getElementById('cpw-subtitle').textContent='A one-time code will be sent to your email';

  if (prefillEmail) document.getElementById('cpw-email').value=prefillEmail;
  modal.style.display='flex';
  document.getElementById('cpw-email').focus();
}

/* ══════════════════════════════════════════════════════════
   SECTION: Authenticated Change Password Modal (logged-in users)
   Uses POST /api/auth/change-password — requires current password.
   Does NOT trigger logout on success (session remains valid).
══════════════════════════════════════════════════════════ */

function _injectAuthChangePasswordModal() {
  if (document.getElementById('auth-cpw-modal')) return;

  const div = document.createElement('div');
  div.innerHTML = `
  <div id="auth-cpw-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);
       z-index:9999;align-items:center;justify-content:center;">
    <div style="background:#fff;border-radius:12px;width:100%;max-width:400px;
                margin:16px;box-shadow:0 24px 64px rgba(0,0,0,.18);overflow:hidden;">
      <!-- Header -->
      <div style="background:linear-gradient(135deg,#1A2535,#2D3A50);padding:20px 24px;
                  display:flex;align-items:center;justify-content:space-between;">
        <div>
          <h3 style="margin:0;font-family:Montserrat,sans-serif;font-size:14px;font-weight:800;
                     letter-spacing:.06em;color:#fff;">Change Password</h3>
          <p style="margin:4px 0 0;font-size:11px;color:rgba(255,255,255,.55);">
            Enter your current password then choose a new one</p>
        </div>
        <button id="auth-cpw-close" style="background:none;border:none;color:rgba(255,255,255,.6);
                font-size:18px;cursor:pointer;padding:4px 8px;border-radius:4px;">✕</button>
      </div>
      <!-- Form -->
      <div style="padding:28px 24px;">
        <div style="margin-bottom:16px;">
          <label style="display:block;font-size:11px;font-weight:600;letter-spacing:.06em;
                        text-transform:uppercase;color:#4A5568;margin-bottom:6px;">Current Password</label>
          <div style="position:relative;">
            <input id="auth-cpw-current" type="password" placeholder="Your current password" style="
              width:100%;padding:10px 40px 10px 13px;border:1px solid #CBD5E0;border-radius:6px;
              font-size:13px;outline:none;box-sizing:border-box;"
              onfocus="this.style.borderColor='#C17F0A'" onblur="this.style.borderColor='#CBD5E0'" />
            <button type="button" id="auth-cpw-toggle-current" aria-label="Toggle visibility"
              style="position:absolute;right:10px;top:50%;transform:translateY(-50%);
                     background:none;border:none;color:#5B6472;cursor:pointer;font-size:14px;">👁</button>
          </div>
        </div>
        <div style="margin-bottom:16px;">
          <label style="display:block;font-size:11px;font-weight:600;letter-spacing:.06em;
                        text-transform:uppercase;color:#4A5568;margin-bottom:6px;">New Password</label>
          <div style="position:relative;">
            <input id="auth-cpw-new" type="password" placeholder="Min. 8 characters" style="
              width:100%;padding:10px 40px 10px 13px;border:1px solid #CBD5E0;border-radius:6px;
              font-size:13px;outline:none;box-sizing:border-box;"
              onfocus="this.style.borderColor='#C17F0A'" onblur="this.style.borderColor='#CBD5E0'" />
            <button type="button" id="auth-cpw-toggle-new" aria-label="Toggle visibility"
              style="position:absolute;right:10px;top:50%;transform:translateY(-50%);
                     background:none;border:none;color:#5B6472;cursor:pointer;font-size:14px;">👁</button>
          </div>
        </div>
        <div style="margin-bottom:16px;">
          <label style="display:block;font-size:11px;font-weight:600;letter-spacing:.06em;
                        text-transform:uppercase;color:#4A5568;margin-bottom:6px;">Confirm New Password</label>
          <div style="position:relative;">
            <input id="auth-cpw-confirm" type="password" placeholder="Repeat new password" style="
              width:100%;padding:10px 40px 10px 13px;border:1px solid #CBD5E0;border-radius:6px;
              font-size:13px;outline:none;box-sizing:border-box;"
              onfocus="this.style.borderColor='#C17F0A'" onblur="this.style.borderColor='#CBD5E0'" />
            <button type="button" id="auth-cpw-toggle-confirm" aria-label="Toggle visibility"
              style="position:absolute;right:10px;top:50%;transform:translateY(-50%);
                     background:none;border:none;color:#5B6472;cursor:pointer;font-size:14px;">👁</button>
          </div>
        </div>
        <div id="auth-cpw-err" style="display:none;padding:10px 12px;background:#FFF5F5;
             border:1px solid #FED7D7;border-radius:6px;font-size:12px;color:#C53030;margin-bottom:16px;"></div>
        <button id="auth-cpw-btn-submit" style="width:100%;padding:12px;background:#C17F0A;border:none;
          border-radius:6px;font-family:Montserrat,sans-serif;font-size:11px;font-weight:800;
          letter-spacing:.1em;text-transform:uppercase;color:#1A2535;cursor:pointer;">Update Password</button>
      </div>
    </div>
  </div>`;
  document.body.appendChild(div);

  const modal  = document.getElementById('auth-cpw-modal');
  const setErr = (msg) => { const e=document.getElementById('auth-cpw-err'); e.textContent=msg; e.style.display='block'; };
  const clrErr = ()    => { document.getElementById('auth-cpw-err').style.display='none'; };

  document.getElementById('auth-cpw-close').addEventListener('click', () => { modal.style.display='none'; });
  modal.addEventListener('click', (e) => { if (e.target===modal) modal.style.display='none'; });

  // Toggle visibility buttons
  ['current','new','confirm'].forEach(key => {
    const input  = document.getElementById(`auth-cpw-${key}`);
    const toggle = document.getElementById(`auth-cpw-toggle-${key}`);
    toggle.addEventListener('click', () => { input.type = input.type==='password' ? 'text' : 'password'; });
  });

  async function doSubmit() {
    clrErr();
    const currentPw = document.getElementById('auth-cpw-current').value;
    const newPw     = document.getElementById('auth-cpw-new').value;
    const confirmPw = document.getElementById('auth-cpw-confirm').value;

    if (!currentPw)              { setErr('Please enter your current password.'); return; }
    if (!newPw || newPw.length<8){ setErr('New password must be at least 8 characters.'); return; }
    if (newPw !== confirmPw)     { setErr('New passwords do not match.'); return; }
    if (newPw === currentPw)     { setErr('New password must be different from the current one.'); return; }

    const btn = document.getElementById('auth-cpw-btn-submit');
    btn.disabled=true; btn.textContent='Updating…';
    try {
      const res = await fetch(`${API_BASE}/auth/change-password`, {
        method: 'POST',
        headers: { 'Content-Type':'application/json', ...getAuthHeaders() },
        body: JSON.stringify({ current_password: currentPw, new_password: newPw }),
      });
      const data = await res.json().catch(()=>({}));
      if (!res.ok) throw new Error(data.detail || 'Failed to change password.');

      modal.style.display='none';
      showToast('Password changed successfully.', 'success', 4000);
    } catch(err) { setErr(err.message); }
    finally { btn.disabled=false; btn.textContent='Update Password'; }
  }

  document.getElementById('auth-cpw-btn-submit').addEventListener('click', doSubmit);
  document.getElementById('auth-cpw-confirm').addEventListener('keydown', (e) => { if(e.key==='Enter') doSubmit(); });
}

/**
 * Open the authenticated change-password modal (for already-logged-in users).
 * Requires current password — calls POST /api/auth/change-password.
 * Session stays active after success (no logout).
 */
export function openAuthChangePasswordModal() {
  _injectAuthChangePasswordModal();
  const modal = document.getElementById('auth-cpw-modal');
  // Reset fields
  ['auth-cpw-current','auth-cpw-new','auth-cpw-confirm'].forEach(id => {
    document.getElementById(id).value = '';
    document.getElementById(id).type  = 'password';
  });
  document.getElementById('auth-cpw-err').style.display = 'none';
  modal.style.display = 'flex';
  document.getElementById('auth-cpw-current').focus();
}

/* ══════════════════════════════════════════════════════════
   SECTION: Nav User Dropdown
══════════════════════════════════════════════════════════ */

function _buildDropdown(user) {
  const roleLabel = (user.role||'').replace(/_/g,' ');
  const isPriv    = user.role==='admin';
  const initials  = (user.full_name||user.email||'?').split(' ').map(w=>w[0]).join('').slice(0,2).toUpperCase();

  document.getElementById('nav-user-dropdown-wrap')?.remove();

  const wrap = document.createElement('div');
  wrap.id = 'nav-user-dropdown-wrap';
  wrap.style.cssText = 'position:relative;display:flex;align-items:center;';

  // Three distinct role badge colors: admin=gold, user=green, viewer=blue.
  const roleColor = user.role==='admin' ? '#C17F0A' : user.role==='viewer' ? '#2B6CB0' : '#1A7A4A';
  const roleBg    = user.role==='admin' ? '#FEF3C7' : user.role==='viewer' ? '#EBF8FF' : '#F0FFF4';
  const roleBdr   = user.role==='admin' ? '#F0B429' : user.role==='viewer' ? '#BEE3F8' : '#C6F6D5';

  wrap.innerHTML = `
    <button id="nav-user-trigger" aria-haspopup="true" aria-expanded="false"
      style="display:flex;align-items:center;gap:10px;background:none;border:none;
             cursor:pointer;padding:6px 10px 6px 6px;border-radius:8px;transition:background .15s;
             border:1px solid transparent;"
      onmouseenter="this.style.background='rgba(193,127,10,.08)';this.style.borderColor='rgba(193,127,10,.2)'"
      onmouseleave="this.style.background='transparent';this.style.borderColor='transparent'">
      <div style="width:34px;height:34px;border-radius:50%;
                  background:linear-gradient(135deg,#C17F0A 0%,#1A2535 100%);
                  display:flex;align-items:center;justify-content:center;
                  font-family:Montserrat,sans-serif;font-size:12px;font-weight:800;
                  color:#fff;letter-spacing:.04em;flex-shrink:0;">${initials}</div>
      <div style="text-align:left;line-height:1.25;">
        <div style="font-size:12px;font-weight:600;color:#1A202C;white-space:nowrap;
                    max-width:130px;overflow:hidden;text-overflow:ellipsis;">
          ${user.full_name||user.email.split('@')[0]}</div>
        <div style="font-size:12px;color:#5F6B78;text-transform:capitalize;letter-spacing:.04em;">${roleLabel}</div>
      </div>
      <svg width="11" height="11" viewBox="0 0 12 12" fill="none" style="opacity:.45;flex-shrink:0;">
        <path d="M2 4l4 4 4-4" stroke="#718096" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>

    <div id="nav-user-menu" style="display:none;position:absolute;top:calc(100% + 8px);right:0;
         background:#fff;border:1px solid #E2E8F0;border-radius:10px;
         box-shadow:0 12px 32px rgba(0,0,0,.12),0 4px 8px rgba(0,0,0,.05);
         min-width:220px;overflow:hidden;z-index:500;">
      <div style="padding:14px 16px;background:linear-gradient(135deg,#F7F8FA,#fff);
                  border-bottom:1px solid #E2E8F0;">
        <div style="font-size:12px;font-weight:600;color:#1A202C;">${user.full_name||'-'}</div>
        <div style="font-size:11px;color:#5B6472;margin-top:1px;">${user.email}</div>
        <div style="margin-top:8px;">
          <span style="display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;
                       font-weight:700;letter-spacing:.1em;text-transform:uppercase;
                       background:${roleBg};color:${roleColor};border:1px solid ${roleBdr};">
            ${roleLabel}</span>
        </div>
      </div>
      <div style="padding:6px 0;">
        ${isPriv ? `<a href="admin.html" id="nav-menu-admin" style="display:flex;align-items:center;
            gap:10px;padding:9px 16px;font-size:12px;color:#1A202C;text-decoration:none;"
            onmouseenter="this.style.background='#F7F8FA'" onmouseleave="this.style.background='transparent'">
            <span style="width:20px;text-align:center;font-size:14px;">⚙️</span>
            <span>Admin Panel</span>
          </a>` : ''}
        <button id="nav-menu-change-pw" style="width:100%;display:flex;align-items:center;
            gap:10px;padding:9px 16px;font-size:12px;color:#2D3748;background:none;
            border:none;cursor:pointer;text-align:left;"
            onmouseenter="this.style.background='#F7F8FA'" onmouseleave="this.style.background='transparent'">
          <span style="width:20px;text-align:center;font-size:14px;">🔑</span>
          <span>Change Password</span>
        </button>
        <button id="nav-menu-logout" style="width:100%;display:flex;align-items:center;
            gap:10px;padding:9px 16px;font-size:12px;color:#C53030;background:none;
            border:none;cursor:pointer;text-align:left;"
            onmouseenter="this.style.background='#FFF5F5'" onmouseleave="this.style.background='transparent'">
          <span style="width:20px;text-align:center;font-size:14px;">🚪</span>
          <span>Sign Out</span>
        </button>
      </div>
    </div>`;

  const trigger = wrap.querySelector('#nav-user-trigger');
  const menu    = wrap.querySelector('#nav-user-menu');
  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = menu.style.display==='block';
    menu.style.display = open ? 'none' : 'block';
    trigger.setAttribute('aria-expanded', String(!open));
  });

  document.addEventListener('click', () => { menu.style.display='none'; });

  wrap.querySelector('#nav-menu-change-pw').addEventListener('click', () => {
    menu.style.display = 'none';
    openAuthChangePasswordModal();
  });
  wrap.querySelector('#nav-menu-logout').addEventListener('click', logout);

  return wrap;
}

/**
 * Fetch the current user's profile from /auth/me and populate the nav bar.
 * Called at the top of every protected page right after requireAuth().
 * @returns {Promise<void>}
 */
export async function populateNavUser() {
  const session = getSession();
  if (!session) return;

  try {
    const res = await fetch(`${API_BASE}/auth/me`, { headers: getAuthHeaders() });
    if (!res.ok) { logout(); return; }

    const user = await res.json();

    const stored = getSession();
    if (stored) { stored.full_name=user.full_name; stored.email=user.email; setSession(stored); }

    const navUser = document.querySelector('.nav-user');
    if (navUser) { navUser.innerHTML=''; navUser.appendChild(_buildDropdown(user)); }

    const nameEl = document.getElementById('nav-user-name');
    const roleEl = document.getElementById('nav-user-role');
    if (nameEl) nameEl.textContent = user.full_name || user.email.split('@')[0];
    if (roleEl) roleEl.textContent = (user.role||'').replace('_',' ');

    const heroName = document.getElementById('hero-name');
    if (heroName) heroName.textContent = (user.full_name||'').split(' ')[0]||'';

    const adminLink = document.getElementById('nav-admin-link');
    if (adminLink) adminLink.style.display = (user.role==='admin') ? 'block' : 'none';

  } catch { /* non-fatal */ }

  const logoutBtn = document.getElementById('btn-logout');
  if (logoutBtn) logoutBtn.addEventListener('click', logout);
}

/* ══════════════════════════════════════════════════════════
   SECTION: Mobile Nav Toggle
   Below 900px, css/style.css turns .nav-links into a closed-by-default
   dropdown panel (see its ".nav-links"/".nav-links.open" rules) instead of
   the old behavior of just hiding it with no way to get it back. This
   function injects the hamburger button that opens/closes it.
   Runs automatically for every page that imports this module — the
   .topnav/.nav-links markup is duplicated identically across all pages
   that have a nav bar, so wiring it once here avoids editing each page's
   HTML individually. Pages with no topnav (e.g. index.html) just no-op.
══════════════════════════════════════════════════════════ */
function _initMobileNav() {
  const nav   = document.querySelector('.topnav');
  const links = document.querySelector('.nav-links');
  if (!nav || !links) return;
  if (document.getElementById('nav-toggle')) return; // already wired (shouldn't happen, but idempotent)

  links.id = links.id || 'nav-links';

  const btn = document.createElement('button');
  btn.id   = 'nav-toggle';
  btn.type = 'button';
  btn.className = 'nav-toggle';
  btn.setAttribute('aria-label', 'Toggle navigation menu');
  btn.setAttribute('aria-expanded', 'false');
  btn.setAttribute('aria-controls', links.id);
  btn.innerHTML = '&#9776;'; // ☰
  nav.insertBefore(btn, links);

  const setOpen = (open) => {
    links.classList.toggle('open', open);
    btn.setAttribute('aria-expanded', String(open));
    btn.innerHTML = open ? '&#10005;' : '&#9776;'; // ✕ when open, ☰ when closed
  };

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    setOpen(!links.classList.contains('open'));
  });
  // Close after choosing a link, or on an outside click.
  links.addEventListener('click', (e) => { if (e.target.closest('a')) setOpen(false); });
  document.addEventListener('click', (e) => {
    if (links.classList.contains('open') && !nav.contains(e.target)) setOpen(false);
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _initMobileNav);
} else {
  _initMobileNav();
}
