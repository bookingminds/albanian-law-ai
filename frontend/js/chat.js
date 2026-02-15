/**
 * Albanian Law AI - Chat Interface JavaScript
 */

const API = '';
let sessionId = generateSessionId();
let isLoading = false;

function getAuthHeaders() {
    const token = typeof getToken === 'function' ? getToken() : null;
    const h = { 'Content-Type': 'application/json' };
    if (token) h['Authorization'] = 'Bearer ' + token;
    return h;
}

// ── Session Management ───────────────────────────────────

function generateSessionId() {
    return 'session_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
}

function newSession() {
    sessionId = generateSessionId();
    const messages = document.getElementById('chatMessages');
    const t = typeof __t !== 'undefined' ? __t : (k) => k;
    messages.innerHTML = `
        <div class="message message-assistant">
            <div class="message-bubble">
                <p><strong>${t('chat.welcome_title')}</strong></p>
                <p>${t('chat.welcome_body')}</p>
                <p style="color:var(--text-muted); font-size:.85rem; margin-top:.5rem;">${t('chat.welcome_note')}</p>
            </div>
        </div>
    `;
}

// ── Send Message ─────────────────────────────────────────

async function sendMessage() {
    const input = document.getElementById('chatInput');
    const question = input.value.trim();

    if (!question || isLoading) return;

    if (typeof window._canSend !== 'undefined' && !window._canSend) {
        var token = typeof getToken === 'function' ? getToken() : null;
        if (token) {
            if (typeof window.showPaywallOverlay === 'function') window.showPaywallOverlay();
            else {
                var el = document.getElementById('paywallOverlay');
                if (el) el.style.display = 'flex';
            }
        } else {
            if (typeof openAuthModal === 'function') openAuthModal();
        }
        return;
    }

    isLoading = true;
    const sendBtn = document.getElementById('sendBtn');
    sendBtn.disabled = true;

    // Add user message to chat
    addMessage('user', question);
    input.value = '';
    autoResize(input);

    // Show typing indicator
    showTypingIndicator();

    try {
        const res = await fetch(`${API}/api/chat`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                question: question,
                session_id: sessionId,
            }),
        });

        removeTypingIndicator();

        if (res.status === 401 && typeof redirectToLogin === 'function') {
            redirectToLogin();
            return;
        }
        if (res.status === 403) {
            window._canSend = false;
            if (typeof window.showPaywallOverlay === 'function') window.showPaywallOverlay();
            else {
                var pw = document.getElementById('paywallOverlay');
                if (pw) pw.style.display = 'flex';
            }
            showToast(typeof __t !== 'undefined' ? __t('chat.error_subscription') : 'Active subscription required. Please subscribe to use the chat.', 'error');
            return;
        }
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            const fallback = typeof __t !== 'undefined' ? __t('chat.error_generic') : 'Failed to get response';
            throw new Error(err.detail || fallback);
        }

        const data = await res.json();
        sessionId = data.session_id;

        // Add assistant message
        addMessage('assistant', data.answer, data.sources);

    } catch (err) {
        removeTypingIndicator();
        const errText = typeof __t !== 'undefined' ? __t('chat.error_generic') : 'Error: Please check that the server is running and documents are uploaded.';
        addMessage('assistant', err.message || errText);
        showToast(err.message || errText, 'error');
    } finally {
        isLoading = false;
        sendBtn.disabled = false;
        input.focus();
    }
}

// ── Message Rendering ────────────────────────────────────

function addMessage(role, content, sources = []) {
    const messages = document.getElementById('chatMessages');

    const msgDiv = document.createElement('div');
    msgDiv.className = `message message-${role}`;

    let bubbleContent = '';
    if (role === 'assistant') {
        // Format the answer with basic markdown-like rendering
        bubbleContent = formatAnswer(content);
    } else {
        bubbleContent = `<p>${escapeHtml(content)}</p>`;
    }

    let sourcesHtml = '';
    if (sources && sources.length > 0) {
        const sourceItems = sources.map(s => {
            const parts = [];
            if (s.title) parts.push(`<strong>${escapeHtml(s.title)}</strong>`);
            if (s.law_number) parts.push(`Ligji Nr. ${escapeHtml(s.law_number)}`);
            if (s.law_date) parts.push(`datë ${escapeHtml(s.law_date)}`);
            if (s.article) parts.push(`Neni ${escapeHtml(s.article)}`);
            if (s.pages) parts.push(`Faqe ${escapeHtml(s.pages)}`);
            return `<div class="source-item">${parts.join(' | ')}</div>`;
        }).join('');

        const sourcesTitle = typeof __t !== 'undefined' ? __t('sources.title') : 'Burimet';
        sourcesHtml = `
            <div class="sources">
                <div class="sources-title">${sourcesTitle}</div>
                ${sourceItems}
            </div>
        `;
    }

    msgDiv.innerHTML = `
        <div class="message-bubble">
            ${bubbleContent}
        </div>
        ${sourcesHtml}
    `;

    messages.appendChild(msgDiv);
    scrollToBottom();
}

function formatAnswer(text) {
    // Basic formatting: paragraphs, bold, lists
    let html = escapeHtml(text);

    // Bold text **text**
    html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');

    // Italic text *text*
    html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');

    // Horizontal rule ---
    html = html.replace(/^---$/gm, '<hr style="margin:.75rem 0; border:none; border-top:1px solid var(--border);">');

    // Convert line breaks to paragraphs
    const paragraphs = html.split(/\n\n+/);
    html = paragraphs.map(p => {
        // Check if it's a list
        const lines = p.split('\n');
        const isList = lines.every(l => l.match(/^[-•]\s/) || l.trim() === '');
        if (isList && lines.some(l => l.match(/^[-•]\s/))) {
            const items = lines
                .filter(l => l.match(/^[-•]\s/))
                .map(l => `<li>${l.replace(/^[-•]\s/, '')}</li>`)
                .join('');
            return `<ul style="margin:.5rem 0; padding-left:1.5rem;">${items}</ul>`;
        }
        return `<p>${p.replace(/\n/g, '<br>')}</p>`;
    }).join('');

    return html;
}

// ── Typing Indicator ─────────────────────────────────────

function showTypingIndicator() {
    const messages = document.getElementById('chatMessages');
    const indicator = document.createElement('div');
    indicator.id = 'typingIndicator';
    indicator.className = 'typing-indicator';
    indicator.innerHTML = '<span></span><span></span><span></span>';
    messages.appendChild(indicator);
    scrollToBottom();
}

function removeTypingIndicator() {
    const indicator = document.getElementById('typingIndicator');
    if (indicator) indicator.remove();
}

// ── Input Handling ───────────────────────────────────────

function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
}

function autoResize(textarea) {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 150) + 'px';
}

// ── Helpers ──────────────────────────────────────────────

function scrollToBottom() {
    const messages = document.getElementById('chatMessages');
    messages.scrollTop = messages.scrollHeight;
}

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(50px)';
        toast.style.transition = 'all .3s';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

// ── i18n: placeholder and send button title ───────────────
(function() {
    if (typeof __t === 'undefined') return;
    var inp = document.getElementById('chatInput');
    if (inp) inp.placeholder = __t('chat.placeholder');
    var btn = document.getElementById('sendBtn');
    if (btn) btn.title = __t('chat.send_title');
})();

// ── Focus input on load ──────────────────────────────────
document.getElementById('chatInput').focus();
