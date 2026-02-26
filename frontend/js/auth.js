/**
 * Shared auth module: Supabase SDK session management + fallback to localStorage.
 *
 * When Supabase is configured, the SDK handles token storage, automatic refresh,
 * and session persistence via onAuthStateChange. A synced copy of the access_token
 * is kept in localStorage so existing getToken() callers work unchanged.
 *
 * When Supabase is NOT configured, falls back to plain localStorage tokens
 * issued by the backend.
 */

const AUTH_TOKEN_KEY = 'albanian_law_ai_token';

let _supabaseClient = null;
let _supabaseReady = false;
let _supabaseConfigured = false;
let _initPromise = null;

// ── Supabase SDK initialization ──────────────────────────────

async function initSupabaseAuth() {
    if (_initPromise) return _initPromise;
    _initPromise = _doInitSupabaseAuth();
    return _initPromise;
}

async function _doInitSupabaseAuth() {
    try {
        var res = await fetch('/api/auth/config');
        if (!res.ok) { _supabaseReady = true; return; }
        var cfg = await res.json();
        if (!cfg.supabase_configured || !cfg.supabase_url || !cfg.supabase_anon_key) {
            _supabaseReady = true;
            return;
        }

        if (typeof supabase === 'undefined' || !supabase.createClient) {
            console.warn('Supabase JS SDK not loaded');
            _supabaseReady = true;
            return;
        }

        _supabaseClient = supabase.createClient(cfg.supabase_url, cfg.supabase_anon_key, {
            auth: {
                persistSession: true,
                autoRefreshToken: true,
                detectSessionInUrl: true,
            }
        });
        _supabaseConfigured = true;

        var { data: { session } } = await _supabaseClient.auth.getSession();
        if (session && session.access_token) {
            localStorage.setItem(AUTH_TOKEN_KEY, session.access_token);
        }

        _supabaseClient.auth.onAuthStateChange(function(event, session) {
            if (session && session.access_token) {
                localStorage.setItem(AUTH_TOKEN_KEY, session.access_token);
            } else {
                localStorage.removeItem(AUTH_TOKEN_KEY);
            }
        });
    } catch (e) {
        console.warn('Supabase auth init failed, using fallback:', e.message);
    }
    _supabaseReady = true;
}

// ── Token accessors (synchronous, backward-compatible) ───────

function getToken() {
    return localStorage.getItem(AUTH_TOKEN_KEY);
}

function setToken(token) {
    if (token) localStorage.setItem(AUTH_TOKEN_KEY, token);
    else localStorage.removeItem(AUTH_TOKEN_KEY);
}

function clearToken() {
    localStorage.removeItem(AUTH_TOKEN_KEY);
}

// ── Auth helpers ─────────────────────────────────────────────

function authHeaders() {
    var token = getToken();
    var h = { 'Content-Type': 'application/json' };
    if (token) h['Authorization'] = 'Bearer ' + token;
    return h;
}

function authHeadersForm() {
    var token = getToken();
    var h = {};
    if (token) h['Authorization'] = 'Bearer ' + token;
    return h;
}

function redirectToLogin() {
    clearToken();
    window.location.href = '/login?redirect=' + encodeURIComponent(window.location.pathname || '/');
}

// ── Supabase sign-in / sign-up / sign-out ────────────────────

async function supabaseSignIn(email, password) {
    if (!_supabaseClient) {
        throw new Error('Supabase not configured');
    }
    var result = await _supabaseClient.auth.signInWithPassword({
        email: email,
        password: password,
    });
    if (result.error) throw result.error;
    if (result.data.session) {
        localStorage.setItem(AUTH_TOKEN_KEY, result.data.session.access_token);
    }
    return result.data;
}

async function supabaseSignUp(email, password) {
    var res = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email, password: password }),
    });
    var data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Regjistrimi dështoi');

    if (_supabaseClient) {
        await supabaseSignIn(email, password);
    } else if (data.token) {
        setToken(data.token);
    }
    return data;
}

async function supabaseSignOut() {
    if (_supabaseClient) {
        try { await _supabaseClient.auth.signOut(); } catch (_) {}
    }
    localStorage.removeItem(AUTH_TOKEN_KEY);
}

function isSupabaseConfigured() {
    return _supabaseConfigured;
}
