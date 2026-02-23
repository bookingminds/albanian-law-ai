"""Anti-abuse for free trial: disposable email blocklist and helpers."""

# Curated list of common disposable/temporary email domains (subset for MVP)
DISPOSABLE_EMAIL_DOMAINS = frozenset({
    "10minutemail.com", "10minutemail.net", "guerrillamail.com", "guerrillamail.net",
    "guerrillamail.org", "mailinator.com", "mailinator.net", "tempmail.com",
    "tempmail.net", "throwaway.email", "yopmail.com", "yopmail.fr", "fakeinbox.com",
    "trashmail.com", "getnada.com", "temp-mail.org", "sharklasers.com",
    "grr.la", "guerrillamail.biz", "guerrillamail.de", "guerrillamail.info",
    "guerrillamail.nl", "guerrillamailblock.com", "spam4.me", "dispostable.com",
    "maildrop.cc", "tmpeml.com", "tempail.com", "mohmal.com", "emailondeck.com",
    "33mail.com", "inboxkitten.com", "mailnesia.com", "mintemail.com",
    "mytemp.email", "mytempmail.com", "anonymousemail.me", "disposablemail.com",
    "minuteinbox.com", "tempinbox.com", "fakeinbox.info", "mail-temp.com",
    "temp-mail.io", "burnermail.io", "getairmail.com", "tmailor.com",
    "dropmail.me", "temp-mail.live", "tempmailo.com", "emailfake.com",
    "nospammail.net", "mail-temporaire.com", "throwawaymail.com", "tempail.org",
    "mailsac.com", "mailinator2.com", "mailcatch.com", "inboxalias.com",
    "crazymailing.com", "mytrashmail.com", "trashmail.ws", "discard.email",
    "discardmail.com", "discardmail.de", "emailondeck.com", "jetable.org",
    "mailnull.com", "spamgourmet.com", "mailnesia.com", "mailfa.tk",
    "harakirimail.com", "incognitomail.com", "mailinator.com", "maildrop.cc",
})


def is_disposable_email(email: str) -> bool:
    """Return True if the email domain is a known disposable/temp domain."""
    if not email or "@" not in email:
        return False
    domain = email.strip().lower().split("@")[-1]
    if domain in DISPOSABLE_EMAIL_DOMAINS:
        return True
    parts = domain.split(".")
    if len(parts) > 2:
        parent = ".".join(parts[-2:])
        return parent in DISPOSABLE_EMAIL_DOMAINS
    return False


def get_client_ip(request) -> str:
    """Get client IP from request, respecting X-Forwarded-For when behind a proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
