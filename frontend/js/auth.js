/**
 * Shared auth helpers: token storage and API headers.
 */

const AUTH_TOKEN_KEY = 'albanian_law_ai_token';

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

function authHeaders() {
    const token = getToken();
    const h = { 'Content-Type': 'application/json' };
    if (token) h['Authorization'] = `Bearer ${token}`;
    return h;
}

function authHeadersForm() {
    const token = getToken();
    const h = {};
    if (token) h['Authorization'] = `Bearer ${token}`;
    return h;
}

function redirectToLogin() {
    clearToken();
    window.location.href = '/login?redirect=' + encodeURIComponent(window.location.pathname || '/');
}
