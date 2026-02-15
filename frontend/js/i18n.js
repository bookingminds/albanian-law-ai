/**
 * i18n: Albanian as default. Structure ready for multiple languages (e.g. en).
 * Usage: t('key') or t('key', { count: 3 }) for simple interpolation.
 */
(function (global) {
    var translations = {
        al: {
            // App name & nav
            "app.name": "Albanian Law AI",
            "nav.chat": "Bisedë",
            "nav.admin": "Paneli i administrimit",
            "nav.login": "Hyrje",
            "nav.logout": "Dil",

            // Subscription & trial
            "trial.badge": "Provë falas",
            "trial.days_left": "Provë falas – kanë mbetur {{n}} ditë",
            "trial.days_left_one": "Provë falas – ka mbetur 1 ditë",
            "trial.last_day": "Provë falas – dita e fundit. Aktivizo abonimin për të vazhduar.",
            "trial.warning_24h": "Prova juaj falas po përfundon së shpejti",
            "trial.on_trial": "Jeni në provën falas.",
            "sub.badge": "Abonuar",
            "sub.activate": "Aktivizo abonimin",
            "sub.price_month": "€9.99 / muaj",
            "sub.stripe": "Aktivizo me Stripe",
            "sub.paypal": "Paguaj me PayPal",
            "sub.unlimited": "Përdorim i pakufizuar (zbatohet politika e përdorimit të drejtë)",

            // Paywall
            "paywall.title": "Aktivizo abonimin për të përdorur Albanian Law AI",
            "paywall.trial_ended": "Provë falas e përfunduar",
            "paywall.trial_ended_short": "Prova falas ka përfunduar",
            "paywall.activate_to_continue": "Aktivizo abonimin për të vazhduar",
            "paywall.subtitle": "Përdorim i pakufizuar i asistentit juridik për një çmim fiks.",
            "paywall.subtitle_trial_ended": "Aktivizo abonimin tani për të vazhduar të bësh pyetje për ligjet shqiptare.",
            "paywall.login_prompt": "Hyni në llogari për të aktivizuar abonimin dhe përdorur bisedën.",
            "paywall.goto_login": "Shko te Hyrja",

            // Chat
            "chat.title": "Asistent juridik",
            "chat.new_chat": "Bisedë e re",
            "chat.placeholder": "Shkruaj pyetjen tënde për ligjet shqiptare…",
            "chat.send_title": "Dërgo",
            "chat.disclaimer": "Përgjigjet bazohen vetëm në dokumentet e ngarkuara. Kontrollo gjithmonë me burime zyrtare.",
            "chat.welcome_title": "Mirë se vini në Albanian Law AI!",
            "chat.welcome_body": "Mund t’i përgjigjem pyetjeve bazuar në dokumentet juridike të ngarkuara. Më pyet çdo gjë për ligjet, rregulloret ose procedurat juridike shqiptare.",
            "chat.welcome_note": "Përgjigjem vetëm në bazë të dokumenteve të ngarkuara. Nëse nuk kam informacion të mjaftueshëm, do ta them.",
            "chat.error_subscription": "Nevitet abonim aktiv. Aktivizo abonimin për të përdorur bisedën.",
            "chat.error_generic": "Gabim. Kontrollo që shërbyesi të jetë aktiv dhe të ketë dokumente të ngarkuara.",
            "sources.title": "Burimet",

            // Login / Register
            "auth.welcome_back": "Mirë se u ktheve",
            "auth.signin_subtitle": "Hyr për të përdorur Albanian Law AI",
            "auth.create_account": "Krijo llogari",
            "auth.register_subtitle": "Regjistrohu për një provë falas 3-ditore, pastaj €9.99/muaj.",
            "auth.email": "Email",
            "auth.password": "Fjalëkalimi",
            "auth.password_hint": "Fjalëkalimi (të paktën 8 karaktere)",
            "auth.sign_in": "Hyr",
            "auth.create_account_btn": "Krijo llogari",
            "auth.placeholder_email": "ju@shembull.com",
            "auth.placeholder_pass": "••••••••",
            "auth.signup_to_start": "Regjistrohu falas për 3 ditë provë dhe fillo të bisedosh.",
            "auth.login_to_start": "Hyni në llogari për të vazhduar.",

            // Admin
            "admin.title": "Menaxhimi i dokumenteve",
            "admin.subtitle": "Ngarko dokumente juridike shqiptare (PDF, DOCX, TXT) dhe shiko statusin e përpunimit.",
            "admin.upload_title": "Ngarko dokument të ri",
            "admin.drop_zone": "Kliko ose tërhiq një skedar këtu",
            "admin.formats": "Formatet e mbështetura: PDF, DOCX, DOC, TXT",
            "admin.remove": "Hiq",
            "admin.doc_title": "Titulli i dokumentit (opsional)",
            "admin.law_number": "Nr. i ligjit (opsional)",
            "admin.date": "Data (opsional)",
            "admin.upload_btn": "Ngarko dhe përpunoj",
            "admin.uploading": "Duke ngarkuar…",
            "admin.documents_list": "Dokumentet e ngarkuara",
            "admin.refresh": "Rifresko",
            "admin.empty": "Ende nuk ka dokumente të ngarkuara.",
            "admin.delete": "Fshi",
            "admin.admin_required": "Nevitet akses administratori.",
            "admin.back_chat": "Kthehu te Biseda",
            "admin.unsupported_file": "Lloji i skedarit nuk mbështetet",
            "admin.upload_success": "U ngarkua me sukses. Përpunimi filloi.",
            "admin.delete_confirm": "Je i sigurt që do ta fshish \"{{name}}\"? Do të hiqet edhe nga indeksi i kërkimit.",
            "admin.deleted": "U fshi.",
            "admin.table_doc": "Dokumenti",
            "admin.table_law": "Nr. i ligjit",
            "admin.table_date": "Data",
            "admin.table_status": "Statusi",
            "admin.table_actions": "Veprime",

            // Status badges
            "status.uploaded": "Ngarkuar",
            "status.processing": "Duke përpunuar",
            "status.processed": "Përpunuar",
            "status.error": "Gabim",

            // Toasts / errors (also from API)
            "error.login_failed": "Hyrja dështoi",
            "error.register_failed": "Regjistrimi dështoi",
            "error.generic": "Gabim"
        }
        // Future: en: { ... }
    };

    var currentLocale = 'al';

    function t(key, opts) {
        var str = translations[currentLocale] && translations[currentLocale][key];
        if (str === undefined) str = translations.al[key];
        if (str === undefined) return key;
        if (opts) {
            if (typeof opts.n !== 'undefined') str = str.replace(/\{\{n\}\}/g, String(opts.n));
            if (typeof opts.name !== 'undefined') str = str.replace(/\{\{name\}\}/g, String(opts.name));
        }
        return str;
    }

    /** Format date as dd/mm/yyyy (Albanian format) */
    function formatDate(isoOrStr) {
        if (!isoOrStr) return '';
        var d = new Date(isoOrStr);
        if (isNaN(d.getTime())) return isoOrStr;
        var day = ('0' + d.getDate()).slice(-2);
        var month = ('0' + (d.getMonth() + 1)).slice(-2);
        var year = d.getFullYear();
        return day + '/' + month + '/' + year;
    }

    /** Apply translations to elements with data-i18n or data-i18n-placeholder */
    function applyI18n() {
        if (typeof document === 'undefined') return;
        document.querySelectorAll('[data-i18n]').forEach(function (el) {
            var key = el.getAttribute('data-i18n');
            if (key) el.textContent = t(key);
        });
        document.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
            var key = el.getAttribute('data-i18n-placeholder');
            if (key) el.placeholder = t(key);
        });
    }

    global.__locale = currentLocale;
    global.__t = t;
    global.__formatDate = formatDate;
    global.__applyI18n = applyI18n;
    if (typeof document !== 'undefined' && document.addEventListener) {
        document.addEventListener('DOMContentLoaded', applyI18n);
    }
})(typeof window !== 'undefined' ? window : this);
