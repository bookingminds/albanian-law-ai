/**
 * Albanian Law AI - Chat Interface JavaScript
 * Multi-document QA with per-user isolation
 */

const API = '';
let sessionId = generateSessionId();
let isLoading = false;
let selectedDocId = null;
let userDocuments = [];
let docPollTimer = null;
let debugMode = false;  // developer toggle

function getAuthHeaders() {
    const token = typeof getToken === 'function' ? getToken() : null;
    const h = { 'Content-Type': 'application/json' };
    if (token) h['Authorization'] = 'Bearer ' + token;
    return h;
}

function getAuthHeadersRaw() {
    const token = typeof getToken === 'function' ? getToken() : null;
    const h = {};
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

// ── Document Management ─────────────────────────────────

async function loadUserDocuments() {
    const token = typeof getToken === 'function' ? getToken() : null;
    if (!token) return;
    // Only admin can list documents
    if (!window._isAdmin) {
        updateDocToolbar();
        return;
    }

    try {
        const res = await fetch(API + '/api/user/documents', {
            headers: getAuthHeadersRaw(),
        });
        if (!res.ok) return;
        const data = await res.json();
        userDocuments = data.documents || [];
        updateDocToolbar();
        updateDocPanel();

        // If any docs are processing, poll for updates
        const hasProcessing = userDocuments.some(d => d.status === 'processing');
        if (hasProcessing && !docPollTimer) {
            docPollTimer = setInterval(loadUserDocuments, 5000);
        } else if (!hasProcessing && docPollTimer) {
            clearInterval(docPollTimer);
            docPollTimer = null;
        }
    } catch (e) {
        // Silently fail
    }
}

function updateDocToolbar() {
    const toolbar = document.getElementById('docToolbar');
    const token = typeof getToken === 'function' ? getToken() : null;
    if (!token || !window._isAdmin) {
        toolbar.style.display = 'none';
        return;
    }
    toolbar.style.display = 'flex';

    // Update dropdown
    const select = document.getElementById('docFilter');
    const currentVal = select.value;
    select.innerHTML = '<option value="">Të gjitha dokumentet</option>';

    const readyDocs = userDocuments.filter(d => d.status === 'ready');
    readyDocs.forEach(d => {
        const opt = document.createElement('option');
        opt.value = d.id;
        opt.textContent = d.title || d.original_filename || 'Dokument #' + d.id;
        select.appendChild(opt);
    });

    // Restore selection if still valid
    if (currentVal && readyDocs.some(d => String(d.id) === currentVal)) {
        select.value = currentVal;
    } else {
        select.value = '';
        selectedDocId = null;
    }

    // Status indicator
    const indicator = document.getElementById('docStatusIndicator');
    const processing = userDocuments.filter(d => d.status === 'processing');
    if (processing.length > 0) {
        indicator.innerHTML =
            '<span class="processing-dot"></span>' +
            processing.length + ' duke u përpunuar';
    } else if (readyDocs.length > 0) {
        indicator.textContent = readyDocs.length + ' dokument' + (readyDocs.length > 1 ? 'e' : '');
    } else {
        indicator.textContent = '';
    }
}

function updateDocPanel() {
    const body = document.getElementById('docPanelBody');
    if (!userDocuments.length) {
        body.innerHTML = '<div class="empty-state" style="padding:1.5rem;"><p style="font-size:.85rem;">Nuk keni dokumente ende. Klikoni "Ngarko" për të shtuar.</p></div>';
        return;
    }

    body.innerHTML = userDocuments.map(d => {
        const name = d.title || d.original_filename || 'Dokument';
        const statusLabel = d.status === 'ready' ? 'Gati' : d.status === 'processing' ? 'Duke u përpunuar...' : 'Dështoi';
        const statusClass = d.status;

        let actions = '';
        if (d.status === 'failed') {
            actions += `<button class="btn-retry-mini" onclick="retryDocument(${d.id})">Riprovo</button>`;
        }
        actions += `<button class="btn-delete-mini" onclick="deleteDocument(${d.id})" title="Fshi">&times;</button>`;

        return `
            <div class="doc-panel-item">
                <span class="doc-item-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
                <span class="doc-item-status ${statusClass}">${statusLabel}</span>
                ${actions}
            </div>
        `;
    }).join('');
}

function onDocFilterChange() {
    const val = document.getElementById('docFilter').value;
    selectedDocId = val ? parseInt(val) : null;
}

function toggleDocPanel() {
    document.getElementById('docPanel').classList.toggle('open');
}

async function handleFileUpload(input) {
    if (!input.files || !input.files[0]) return;

    const file = input.files[0];
    const maxSize = 50 * 1024 * 1024;
    if (file.size > maxSize) {
        showToast('Skedari është shumë i madh. Maksimumi: 50MB', 'error');
        input.value = '';
        return;
    }

    const formData = new FormData();
    formData.append('file', file);

    try {
        showToast('Duke ngarkuar "' + file.name + '"...', 'info');

        const res = await fetch(API + '/api/user/documents/upload', {
            method: 'POST',
            headers: getAuthHeadersRaw(),
            body: formData,
        });

        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || 'Ngarkimi dështoi');
        }

        showToast('Dokumenti u ngarkua! Përpunimi ka filluar.', 'success');
        loadUserDocuments();
    } catch (err) {
        showToast(err.message || 'Gabim gjatë ngarkimit', 'error');
    }

    input.value = '';
}

async function retryDocument(docId) {
    try {
        const res = await fetch(API + '/api/user/documents/' + docId + '/retry', {
            method: 'POST',
            headers: getAuthHeadersRaw(),
        });
        if (!res.ok) {
            const d = await res.json().catch(() => ({}));
            throw new Error(d.detail || 'Dështoi');
        }
        showToast('Ripërpunimi ka filluar', 'info');
        loadUserDocuments();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function deleteDocument(docId) {
    if (!confirm('Jeni të sigurt që dëshironi ta fshini këtë dokument?')) return;
    try {
        const res = await fetch(API + '/api/user/documents/' + docId, {
            method: 'DELETE',
            headers: getAuthHeadersRaw(),
        });
        if (!res.ok) {
            const d = await res.json().catch(() => ({}));
            throw new Error(d.detail || 'Fshirja dështoi');
        }
        showToast('Dokumenti u fshi', 'success');
        loadUserDocuments();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// ── Send Message ─────────────────────────────────────────

async function sendMessage() {
    const input = document.getElementById('chatInput');
    // Prefer the pending suggestion (set by useSuggestion) over raw input value
    // to avoid stale state issues on mobile WebViews
    const question = (_pendingSuggestion || input.value).trim();
    _pendingSuggestion = null;

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

    hideSuggestPanel();

    addMessage('user', question);
    input.value = '';
    autoResize(input);

    showTypingIndicator();

    try {
        const payload = {
            question: question,
            session_id: sessionId,
            stream: true,
            debug: debugMode,
        };
        // Only admin can filter by document
        if (selectedDocId && window._isAdmin) {
            payload.document_id = selectedDocId;
        }

        const res = await fetch(`${API}/api/chat`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify(payload),
        });

        removeTypingIndicator();

        if (res.status === 401 && typeof redirectToLogin === 'function') {
            redirectToLogin();
            return;
        }
        if (res.status === 403) {
            window._canSend = false;
            if (typeof window.showPaywallOverlay === 'function') window.showPaywallOverlay();
            else { var pw = document.getElementById('paywallOverlay'); if (pw) pw.style.display = 'flex'; }
            showToast('Active subscription required.', 'error');
            return;
        }
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to get response');
        }

        // Handle SSE streaming response
        if (res.headers.get('content-type')?.includes('text/event-stream')) {
            await handleStreamResponse(res);
        } else {
            // Fallback: non-streaming response
            const data = await res.json();
            sessionId = data.session_id || sessionId;
            addMessage('assistant', data.answer, data.sources, data);
        }

    } catch (err) {
        removeTypingIndicator();
        addMessage('assistant', err.message || 'Error');
        showToast(err.message || 'Error', 'error');
    } finally {
        isLoading = false;
        sendBtn.disabled = false;
        input.focus();
    }
}

// ── Streaming Handler ─────────────────────────────────────

async function handleStreamResponse(res) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let fullText = '';
    let sources = [];
    let allSources = [];
    let metrics = {};

    // Create a streaming message bubble
    const messages = document.getElementById('chatMessages');
    const msgDiv = document.createElement('div');
    msgDiv.className = 'message message-assistant';
    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    bubble.innerHTML = '<p class="streaming-text"></p>';
    msgDiv.appendChild(bubble);
    messages.appendChild(msgDiv);
    const streamEl = bubble.querySelector('.streaming-text');

    // Status indicator (shows pipeline progress before answer starts)
    let statusEl = null;

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
                const data = JSON.parse(line.substring(6));
                if (data.type === 'status') {
                    // Show pipeline progress message
                    if (!statusEl) {
                        statusEl = document.createElement('span');
                        statusEl.className = 'stream-status';
                        streamEl.appendChild(statusEl);
                    }
                    statusEl.textContent = data.text;
                    scrollToBottom();
                } else if (data.type === 'chunk') {
                    // First chunk arrived — remove status indicator
                    if (statusEl) {
                        statusEl.remove();
                        statusEl = null;
                    }
                    fullText += data.text;
                    streamEl.innerHTML = formatAnswer(fullText);
                    scrollToBottom();
                } else if (data.type === 'sources') {
                    sources = data.sources || [];
                    allSources = data.all_sources || [];
                } else if (data.type === 'done') {
                    metrics = data;
                }
            } catch (e) {}
        }
    }

    // Finalize: replace streaming bubble with proper rendered message
    bubble.innerHTML = formatAnswer(fullText);

    // Sources are already included in the answer text under "**Burimet:**"
    // so we do NOT render a separate sources card to avoid duplication.

    // Add debug info if enabled
    if (debugMode && metrics) {
        const debugHtml = renderDebugInfo(metrics);
        msgDiv.insertAdjacentHTML('beforeend', debugHtml);
    }

    scrollToBottom();
}


// ── Message Rendering ────────────────────────────────────

function addMessage(role, content, sources = [], meta = {}) {
    const messages = document.getElementById('chatMessages');

    const msgDiv = document.createElement('div');
    msgDiv.className = `message message-${role}`;

    let bubbleContent = '';
    if (role === 'assistant') {
        bubbleContent = formatAnswer(content);
    } else {
        bubbleContent = `<p>${escapeHtml(content)}</p>`;
    }

    // Sources are already part of the answer text under "**Burimet:**"
    // so no separate sources card is rendered.

    let debugHtml = '';
    if (debugMode && meta && (meta.chunks_used || meta.top_similarity || meta.debug)) {
        debugHtml = renderDebugInfo(meta);
    }

    msgDiv.innerHTML = `
        <div class="message-bubble">
            ${bubbleContent}
        </div>
        ${debugHtml}
    `;

    messages.appendChild(msgDiv);
    scrollToBottom();
}

function formatAnswer(text) {
    let html = escapeHtml(text);
    html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
    html = html.replace(/^---$/gm, '<hr style="margin:.75rem 0; border:none; border-top:1px solid var(--border);">');

    const paragraphs = html.split(/\n\n+/);
    html = paragraphs.map(p => {
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

// ── Source & Debug Rendering ──────────────────────────────

function renderSources(sources, allSources) {
    if (!sources || !sources.length) return '';

    const primaryItems = sources.map(s => {
        const docTitle = s.title || 'Dokument';
        const lawNum = s.law_number || '';
        const lawDate = s.law_date || '';
        const articles = s.articles || [];
        const pagesList = s.pages_list || [];
        const chunkCount = s.chunk_count || 1;

        // Full document identity line
        let titleLine = `<strong>${escapeHtml(docTitle)}</strong>`;
        if (lawNum) titleLine += ` <span class="source-law-num">${escapeHtml(lawNum)}</span>`;
        if (lawDate) titleLine += ` <span class="source-law-date">(${escapeHtml(lawDate)})</span>`;

        // Articles and pages
        let details = '';
        if (articles.length) {
            details += `Neni ${articles.map(a => escapeHtml(a)).join(', ')}`;
        }
        if (pagesList.length) {
            if (details) details += ' | ';
            details += `Faqe ${pagesList.join(', ')}`;
        }
        const detailsHtml = details ? `<div class="source-details">${details}</div>` : '';

        // View PDF button (admin only — they have access to PDF files)
        let viewBtn = '';
        const page = s.page || (pagesList.length ? pagesList[0] : '');
        if (s.document_id && page && window._isAdmin) {
            viewBtn = `<button class="btn-view-source" onclick="window.open('/api/user/documents/${s.document_id}/pdf?token='+getToken()+'#page=${parseInt(page)||1}','_blank')">Shiko PDF</button>`;
        }

        const countHint = chunkCount > 1
            ? `<span class="source-chunk-count">${chunkCount} fragmente</span>`
            : '';

        return `<div class="source-item-clean">
            <div class="source-title-line">${titleLine} ${countHint} ${viewBtn}</div>
            ${detailsHtml}
        </div>`;
    }).join('');

    // "Show more" with full per-chunk source list
    let expandSection = '';
    if (allSources && allSources.length > sources.length) {
        const uid = 'src_' + Math.random().toString(36).substr(2, 6);
        const extraItems = allSources.map(s => {
            const parts = [];
            if (s.title) parts.push(escapeHtml(s.title));
            if (s.article) parts.push('Neni ' + escapeHtml(s.article));
            const pg = s.page || s.pages || '';
            if (pg) parts.push('Faqe ' + escapeHtml(String(pg)));
            const sim = s.similarity || 0;
            const simPct = sim ? ` (${(sim * 100).toFixed(0)}%)` : '';
            return `<div class="source-extra-item">${parts.join(' | ')}${simPct}</div>`;
        }).join('');

        expandSection = `
            <div class="source-expand-wrap">
                <button class="btn-source-expand" onclick="var el=document.getElementById('${uid}');el.style.display=el.style.display==='none'?'block':'none';this.textContent=el.style.display==='none'?'Shfaq më shumë burime (${allSources.length})':'Fshih burimet'">Shfaq më shumë burime (${allSources.length})</button>
                <div id="${uid}" class="source-extra-list" style="display:none;">${extraItems}</div>
            </div>`;
    }

    return `<div class="sources-clean">
        <div class="sources-label">Burimet:</div>
        ${primaryItems}
        ${expandSection}
    </div>`;
}


function renderDebugInfo(meta) {
    if (!meta || !window._isAdmin) return '';
    let html = '<div class="debug-panel">';
    html += '<div class="debug-title" onclick="this.parentElement.classList.toggle(\'open\')">&#128736; Debug info &#9660;</div>';
    html += '<div class="debug-body">';
    if (meta.chunks_used !== undefined) html += `<p><strong>Chunks used:</strong> ${meta.chunks_used}</p>`;
    if (meta.queries_used) html += `<p><strong>Query variants:</strong> ${meta.queries_used}</p>`;
    if (meta.top_similarity !== undefined) html += `<p><strong>Top similarity:</strong> ${(meta.top_similarity * 100).toFixed(1)}%</p>`;
    if (meta.confidence_blocked) html += `<p style="color:var(--error);"><strong>Confidence gate BLOCKED</strong></p>`;

    // Timing breakdown
    let timeParts = [];
    if (meta.expand_time_ms) timeParts.push(`expand: ${meta.expand_time_ms}ms`);
    if (meta.search_time_ms !== undefined) timeParts.push(`search: ${meta.search_time_ms}ms`);
    if (meta.stitch_time_ms) timeParts.push(`stitch: ${meta.stitch_time_ms}ms`);
    if (meta.generation_time_ms !== undefined) timeParts.push(`gen: ${meta.generation_time_ms}ms`);
    if (meta.coverage_check_ms) timeParts.push(`coverage: ${meta.coverage_check_ms}ms (${meta.coverage_passes || 1} passes)`);
    if (timeParts.length) html += `<p><strong>Timing:</strong> ${timeParts.join(' | ')}</p>`;

    // Query variants
    if (meta.debug && meta.debug.query_variants) {
        html += '<h4>Query variants:</h4><ol style="font-size:.75rem;margin:.3rem 0 .5rem 1.2rem;">';
        meta.debug.query_variants.forEach(q => {
            html += `<li>${escapeHtml(q.substring(0, 100))}</li>`;
        });
        html += '</ol>';
    }

    // Per-query results
    if (meta.debug && meta.debug.per_query) {
        html += '<h4>Per-query results:</h4><table class="debug-table"><tr><th>#</th><th>Chunks</th><th>Time</th><th>Query</th></tr>';
        meta.debug.per_query.forEach((pq, i) => {
            html += `<tr><td>${i + 1}</td><td>${pq.chunks || 0}</td><td>${pq.search_time_ms || '-'}ms</td><td class="debug-preview">${escapeHtml((pq.query || '').substring(0, 60))}</td></tr>`;
        });
        html += '</table>';
    }

    // Final chunk ranking
    if (meta.debug && meta.debug.final_ranking) {
        html += '<h4>Chunk ranking:</h4><table class="debug-table"><tr><th>#</th><th>Score</th><th>Sim</th><th>Hits</th><th>Sources</th><th>Preview</th></tr>';
        meta.debug.final_ranking.forEach((c, i) => {
            html += `<tr><td>${i + 1}</td><td>${(c.final_score || 0).toFixed(5)}</td><td>${((c.similarity || 0) * 100).toFixed(1)}%</td><td>${c.query_hits || 1}</td><td>${(c.sources || []).join(',')}</td><td class="debug-preview">${escapeHtml((c.text_preview || '').substring(0, 60))}</td></tr>`;
        });
        html += '</table>';
    }

    // Coverage passes
    if (meta.debug && meta.debug.coverage_passes) {
        html += '<h4>Coverage self-check:</h4>';
        meta.debug.coverage_passes.forEach(cp => {
            const icon = cp.status === 'COMPLETE' ? '&#9989;' : '&#9888;';
            html += `<p style="font-size:.75rem;">${icon} Pass ${cp.pass}: ${cp.status} (${cp.coverage_pct || '?'}% covered, +${cp.extra_chunks || 0} chunks, ${cp.time_ms}ms)</p>`;
        });
    } else if (meta.coverage_passes !== undefined) {
        html += `<p><strong>Coverage passes:</strong> ${meta.coverage_passes}</p>`;
    }

    html += '</div></div>';
    return html;
}




function toggleDebugMode() {
    debugMode = !debugMode;
    const btn = document.getElementById('debugToggle');
    if (btn) { btn.classList.toggle('active', debugMode); btn.title = debugMode ? 'Debug mode ON' : 'Debug mode OFF'; }
    showToast(debugMode ? 'Debug mode ON' : 'Debug mode OFF', 'info');
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

// ── Ultra-Fast Smart Question Helper ─────────────────────

var _sgTimer = null;
var _sgAbort = null;
var _sgLastQuery = '';
var _pendingSuggestion = null;
var _sgCache = {};
var _sgTopics = [];            // precomputed topic index from server
var _sgTopicsReady = false;
var _sgLastFetchTs = 0;        // rate-limit: max 1 fetch per second
var _SG_CACHE_TTL = 600000;    // 10 min
var _SG_DEBOUNCE = 450;        // ms
var _SG_MIN_LEN = 8;           // min characters
var _SG_RATE_MS = 1000;        // max 1 request per second

// ── Cache helpers ────────────────────────────────────────

function _sgNorm(t) {
    return t.toLowerCase().trim().replace(/\s+/g, ' ');
}

function _sgCacheGet(text) {
    var key = _sgNorm(text);
    var e = _sgCache[key];
    if (e && (Date.now() - e.ts) < _SG_CACHE_TTL) return e.d;
    for (var n = key.length - 1; n >= _SG_MIN_LEN; n--) {
        e = _sgCache[key.substring(0, n)];
        if (e && (Date.now() - e.ts) < _SG_CACHE_TTL) return e.d;
    }
    return null;
}

function _sgCacheSet(text, data) {
    var key = _sgNorm(text);
    var keys = Object.keys(_sgCache);
    if (keys.length > 150) {
        keys.sort(function(a, b) { return _sgCache[a].ts - _sgCache[b].ts; });
        for (var i = 0; i < 30; i++) delete _sgCache[keys[i]];
    }
    _sgCache[key] = { d: data, ts: Date.now() };
}

// ── Precomputed topic index (loaded once) ────────────────

function _sgLoadTopics() {
    var token = typeof getToken === 'function' ? getToken() : null;
    if (!token) { setTimeout(_sgLoadTopics, 2000); return; }
    fetch(API + '/api/suggest-topics', { headers: getAuthHeaders() })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(d) {
            if (d && d.topics) {
                _sgTopics = d.topics;
                _sgTopicsReady = true;
            }
        })
        .catch(function() {});
}

var _SG_STOP = new Set(
    'dhe ose per nga nje tek te ne me se ka si do jane eshte nuk qe i e u ' +
    'ky kjo keto ato por nese edhe mund duhet cfare cilat cili kane ' +
    'neni ligj ligji ligje kodi kodit nr date sipas mos'.split(' ')
);

function _sgNormAlb(w) {
    return w.replace(/ë/g,'e').replace(/ç/g,'c').replace(/Ë/g,'E').replace(/Ç/g,'C').toLowerCase();
}

function _sgLocalMatch(text) {
    if (!_sgTopics.length) return null;
    var words = text.toLowerCase().match(/\b\w{3,}\b/g) || [];
    words = words.filter(function(w) { return !_SG_STOP.has(w); });
    if (!words.length) return null;

    var wordsNorm = words.map(_sgNormAlb);

    var matches = [];
    for (var i = 0; i < _sgTopics.length && matches.length < 12; i++) {
        var kw = _sgTopics[i].kw;
        var kwN = _sgNormAlb(kw);
        for (var j = 0; j < words.length; j++) {
            var wN = wordsNorm[j];
            if (kwN.indexOf(wN) === 0 || wN.indexOf(kwN) === 0 ||
                kw.indexOf(words[j]) === 0 || words[j].indexOf(kw) === 0) {
                matches.push(_sgTopics[i]);
                break;
            }
        }
    }
    if (!matches.length) return null;

    var suggestions = [], related = [], seen = {};
    for (var k = 0; k < matches.length; k++) {
        var m = matches[k];
        if (!seen[m.suggestion] && suggestions.length < 3) {
            suggestions.push(m.suggestion);
            seen[m.suggestion] = 1;
        }
        var label = m.article ? ('Neni ' + m.article + ' — ' + m.title) : m.title;
        if (!seen[label] && related.length < 3) {
            related.push(label);
            seen[label] = 1;
        }
    }
    return suggestions.length ? { suggestions: suggestions, related: related, local: true } : null;
}

// ── Generic template fallback (shown if server takes >700ms) ──

function _sgGenericFallback(text) {
    var clean = text.replace(/[?!.]+$/, '').trim();
    return {
        suggestions: [
            'Çfarë thotë ligji për ' + clean.toLowerCase() + '?',
            'Si rregullohet ' + clean.toLowerCase() + ' sipas ligjit?',
            'Cilat janë dispozitat për ' + clean.toLowerCase() + '?',
        ],
        related: [],
        generic: true
    };
}

// ── Main input handler ───────────────────────────────────

function onInputChange(textarea) {
    var text = textarea.value.trim();
    _pendingSuggestion = null;

    if (_sgTimer) clearTimeout(_sgTimer);
    if (_sgAbort) { _sgAbort.abort(); _sgAbort = null; }

    if (text.length < _SG_MIN_LEN || isLoading) {
        hideSuggestPanel();
        return;
    }

    if (text === _sgLastQuery) return;

    // 1. Client cache → instant
    var cached = _sgCacheGet(text);
    if (cached) {
        _sgLastQuery = text;
        renderSuggestPanel(cached, text);
        return;
    }

    // 2. Local topic match → instant (0ms)
    var local = _sgLocalMatch(text);
    if (local) {
        _sgLastQuery = text;
        renderSuggestPanel(local, text);
    }

    // 3. Debounce → fetch grounded suggestions (replaces local on arrival)
    _sgTimer = setTimeout(function() { _sgFetch(text, !!local); }, _SG_DEBOUNCE);
}

async function _sgFetch(partial, hasLocal) {
    var now = Date.now();
    // Rate-limit: max 1 request per second
    if (now - _sgLastFetchTs < _SG_RATE_MS) {
        _sgTimer = setTimeout(function() { _sgFetch(partial, hasLocal); },
                              _SG_RATE_MS - (now - _sgLastFetchTs));
        return;
    }

    var token = typeof getToken === 'function' ? getToken() : null;
    if (!token) return;

    _sgAbort = new AbortController();
    _sgLastQuery = partial;
    _sgLastFetchTs = Date.now();

    // Show generic fallback after 700ms if nothing shown yet
    var fallbackTimer = null;
    if (!hasLocal) {
        fallbackTimer = setTimeout(function() {
            renderSuggestPanel(_sgGenericFallback(partial), partial);
        }, 700);
    }

    try {
        var res = await fetch(API + '/api/suggest-questions', {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({ partial: partial }),
            signal: _sgAbort.signal,
        });

        if (fallbackTimer) clearTimeout(fallbackTimer);

        if (!res.ok) { if (!hasLocal) hideSuggestPanel(); return; }
        var data = await res.json();
        _sgCacheSet(partial, data);
        // Only render if this is still the current query
        if (partial === _sgLastQuery) {
            renderSuggestPanel(data, partial);
        }
    } catch (e) {
        if (fallbackTimer) clearTimeout(fallbackTimer);
        if (e.name !== 'AbortError' && !hasLocal) hideSuggestPanel();
    }
}

// ── Render ───────────────────────────────────────────────

function renderSuggestPanel(data, originalText) {
    var panel = document.getElementById('suggestPanel');
    if (!panel) return;

    var suggestions = data.suggestions || [];
    var related = data.related || [];
    if (!suggestions.length && !related.length) {
        hideSuggestPanel();
        return;
    }

    var html = '';

    if (suggestions.length) {
        html += '<div class="suggest-chips-row">';
        suggestions.forEach(function(s) {
            html += '<button class="suggest-chip-btn" onclick="useSuggestion(this)" data-text="'
                + escapeHtml(s) + '">' + escapeHtml(s)
                + '<span class="sg-use">Përdor</span></button>';
        });
        html += '</div>';
    }

    if (related.length) {
        html += '<div class="suggest-topics-row">';
        related.forEach(function(t) {
            html += '<button class="suggest-topic-chip" onclick="useSuggestion(this)" data-text="'
                + escapeHtml(t) + '">' + escapeHtml(t) + '</button>';
        });
        html += '</div>';
    }

    html += '<button class="suggest-close" onclick="hideSuggestPanel()" title="Mbyll">&times;</button>';
    panel.innerHTML = html;
    panel.style.display = 'block';
}

function useSuggestion(btn) {
    var text = btn.getAttribute('data-text');
    if (!text) return;
    var input = document.getElementById('chatInput');
    _pendingSuggestion = text;
    input.value = text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    autoResize(input);
    hideSuggestPanel();
    input.focus();
    _sgLastQuery = text;
}

function hideSuggestPanel() {
    var panel = document.getElementById('suggestPanel');
    if (panel) {
        panel.style.display = 'none';
        panel.innerHTML = '';
    }
    _sgLastQuery = '';
}


// ── i18n: placeholder and send button title ───────────────
(function() {
    if (typeof __t === 'undefined') return;
    var inp = document.getElementById('chatInput');
    if (inp) inp.placeholder = __t('chat.placeholder');
    var btn = document.getElementById('sendBtn');
    if (btn) btn.title = __t('chat.send_title');
})();

// ── Initialize: load documents on page load ──────────────
document.getElementById('chatInput').focus();

// Load user docs and precomputed topics after a short delay
setTimeout(function() {
    loadUserDocuments();
    _sgLoadTopics();
}, 500);
