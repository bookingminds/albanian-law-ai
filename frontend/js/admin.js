/**
 * Albanian Law AI - Admin Panel JavaScript
 */

const API = '';

function getAuthHeaders() {
    const token = typeof getToken === 'function' ? getToken() : null;
    const h = {};
    if (token) h['Authorization'] = 'Bearer ' + token;
    return h;
}

function getAuthHeadersJson() {
    const h = getAuthHeaders();
    h['Content-Type'] = 'application/json';
    return h;
}

// ── File Upload ──────────────────────────────────────────

const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const uploadForm = document.getElementById('uploadForm');
const uploadBtn = document.getElementById('uploadBtn');
let selectedFile = null;

// Click to browse
dropZone.addEventListener('click', () => fileInput.click());

// File selected via input
fileInput.addEventListener('change', (e) => {
    if (e.target.files.length > 0) {
        selectFile(e.target.files[0]);
    }
});

// Drag & Drop
dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('dragover');
});

dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('dragover');
});

dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    if (e.dataTransfer.files.length > 0) {
        selectFile(e.dataTransfer.files[0]);
    }
});

function selectFile(file) {
    const allowed = ['pdf', 'docx', 'doc', 'txt'];
    const ext = file.name.split('.').pop().toLowerCase();
    if (!allowed.includes(ext)) {
        const msg = typeof __t !== 'undefined' ? __t('admin.unsupported_file') + ': .' + ext : 'Unsupported file type: .' + ext;
        showToast(msg, 'error');
        return;
    }
    selectedFile = file;
    document.getElementById('selectedFile').style.display = 'block';
    document.getElementById('selectedFileName').textContent =
        `${file.name} (${formatSize(file.size)})`;
    uploadBtn.disabled = false;
}

function clearFile() {
    selectedFile = null;
    fileInput.value = '';
    document.getElementById('selectedFile').style.display = 'none';
    uploadBtn.disabled = true;
}

function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
}

// Upload form submit
uploadForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!selectedFile) return;

    uploadBtn.disabled = true;
    const uploadingText = typeof __t !== 'undefined' ? __t('admin.uploading') : 'Uploading...';
    uploadBtn.innerHTML = '<span class="spinner"></span> ' + uploadingText;

    const formData = new FormData();
    formData.append('file', selectedFile);

    const title = document.getElementById('docTitle').value.trim();
    const lawNum = document.getElementById('lawNumber').value.trim();
    const lawDate = document.getElementById('lawDate').value.trim();

    if (title) formData.append('title', title);
    if (lawNum) formData.append('law_number', lawNum);
    if (lawDate) formData.append('law_date', lawDate);

    try {
        const headers = getAuthHeaders();
        const res = await fetch(`${API}/api/documents/upload`, {
            method: 'POST',
            headers,
            body: formData,
        });
        if (res.status === 401) { redirectToLogin(); return; }
        if (res.status === 403) {
            showToast(typeof __t !== 'undefined' ? __t('admin.admin_required') : 'Admin access required', 'error');
            return;
        }

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || (typeof __t !== 'undefined' ? __t('error.generic') : 'Upload failed'));
        }

        const data = await res.json();
        const successMsg = typeof __t !== 'undefined' ? __t('admin.upload_success') : '"' + data.filename + '" uploaded successfully! Processing started.';
        showToast(successMsg, 'success');

        // Reset form
        clearFile();
        document.getElementById('docTitle').value = '';
        document.getElementById('lawNumber').value = '';
        document.getElementById('lawDate').value = '';

        // Refresh list
        loadDocuments();

        // Auto-refresh while processing
        startPolling();

    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        uploadBtn.disabled = false;
        uploadBtn.textContent = typeof __t !== 'undefined' ? __t('admin.upload_btn') : 'Upload & Process';
    }
});


// ── Documents List ───────────────────────────────────────

let pollInterval = null;

async function loadDocuments() {
    try {
        const res = await fetch(`${API}/api/documents`, { headers: getAuthHeaders() });
        if (res.status === 401) { redirectToLogin(); return; }
        if (res.status === 403) {
            showToast(typeof __t !== 'undefined' ? __t('admin.admin_required') : 'Admin access required', 'error');
            return;
        }
        const data = await res.json();
        renderDocuments(data.documents);

        // Check if any are still processing
        const processing = data.documents.some(d =>
            d.status === 'uploaded' || d.status === 'processing'
        );
        if (processing && !pollInterval) {
            startPolling();
        } else if (!processing && pollInterval) {
            stopPolling();
        }
    } catch (err) {
        console.error('Failed to load documents:', err);
    }
}

function startPolling() {
    if (pollInterval) return;
    pollInterval = setInterval(loadDocuments, 3000);
}

function stopPolling() {
    if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
    }
}

function renderDocuments(docs) {
    const container = document.getElementById('documentsContainer');

    if (!docs || docs.length === 0) {
        const emptyText = typeof __t !== 'undefined' ? __t('admin.empty') : 'No documents uploaded yet.';
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">&#128218;</div>
                <p>${emptyText}</p>
            </div>
        `;
        return;
    }

    const t = typeof __t !== 'undefined' ? __t : function(k) { return k; };
    const thDoc = t('admin.table_doc');
    const thLaw = t('admin.table_law');
    const thDate = t('admin.table_date');
    const thStatus = t('admin.table_status');
    const thActions = t('admin.table_actions');
    const btnDelete = t('admin.delete');

    const rows = docs.map(doc => {
        const statusClass = `badge-${doc.status}`;
        const statusIcon = {
            'uploaded': '&#9202;',
            'processing': '&#9881;',
            'processed': '&#9989;',
            'error': '&#10060;',
        }[doc.status] || '';
        const statusLabel = t('status.' + doc.status) || doc.status;

        const meta = doc.metadata_json ? (() => {
            try { return JSON.parse(doc.metadata_json); } catch { return {}; }
        })() : {};

        const title = doc.title || meta.title || doc.original_filename;
        const lawNum = doc.law_number || meta.law_number || '-';
        const date = doc.law_date || meta.law_date || '-';
        const filenameAttr = escapeHtml(doc.original_filename).replace(/"/g, '&quot;');

        return `
            <tr>
                <td>
                    <div class="doc-name">${escapeHtml(title)}</div>
                    <div class="doc-meta">${escapeHtml(doc.original_filename)} &middot; ${formatSize(doc.file_size)}</div>
                </td>
                <td>${escapeHtml(lawNum)}</td>
                <td>${escapeHtml(date)}</td>
                <td>
                    <span class="badge ${statusClass}">
                        ${statusIcon} ${statusLabel}
                    </span>
                    ${doc.status === 'processed' ? `<div class="doc-meta">${doc.total_chunks} chunks</div>` : ''}
                    ${doc.status === 'error' ? `<div class="doc-meta" style="color:var(--error)">${escapeHtml(doc.error_message || '')}</div>` : ''}
                </td>
                <td>
                    <button class="btn btn-sm btn-danger" data-doc-id="${doc.id}" data-doc-filename="${filenameAttr}" onclick="deleteDoc(this)">
                        ${btnDelete}
                    </button>
                </td>
            </tr>
        `;
    }).join('');

    container.innerHTML = `
        <table class="doc-table">
            <thead>
                <tr>
                    <th>${thDoc}</th>
                    <th>${thLaw}</th>
                    <th>${thDate}</th>
                    <th>${thStatus}</th>
                    <th>${thActions}</th>
                </tr>
            </thead>
            <tbody>
                ${rows}
            </tbody>
        </table>
    `;
}

async function deleteDoc(buttonOrId, nameOrNothing) {
    var id, name;
    if (buttonOrId && buttonOrId.getAttribute && buttonOrId.getAttribute('data-doc-id')) {
        id = parseInt(buttonOrId.getAttribute('data-doc-id'), 10);
        name = buttonOrId.getAttribute('data-doc-filename') || '';
    } else {
        id = buttonOrId;
        name = nameOrNothing || '';
    }
    const msg = typeof __t !== 'undefined' ? __t('admin.delete_confirm', { name: name }) : 'Are you sure you want to delete "' + name + '"? This will also remove it from the search index.';
    if (!confirm(msg)) {
        return;
    }

    try {
        const res = await fetch(`${API}/api/documents/${id}`, { method: 'DELETE', headers: getAuthHeaders() });
        if (res.status === 401) { redirectToLogin(); return; }
        if (res.status === 403) {
            showToast(typeof __t !== 'undefined' ? __t('admin.admin_required') : 'Admin access required', 'error');
            return;
        }
        if (!res.ok) throw new Error(typeof __t !== 'undefined' ? __t('error.generic') : 'Delete failed');
        showToast(typeof __t !== 'undefined' ? __t('admin.deleted') : '"' + name + '" deleted.', 'info');
        loadDocuments();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// ── Toast Notifications ──────────────────────────────────

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

// ── Helpers ──────────────────────────────────────────────

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ── Init ─────────────────────────────────────────────────

loadDocuments();
