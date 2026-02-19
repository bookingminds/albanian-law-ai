"""Accuracy-first multi-query expansion — 10-18 intent-preserving variants.

Generates search variants that maximize retrieval recall while
strictly preserving the user's original intent and logic.

Key feature: domain-specific legal taxonomy ensures every uploaded
document (28 codes + laws) is reachable from relevant user queries
by injecting domain keywords when a legal domain is detected.

Variant types:
  1) original query (as-is)
  2) normalized (ë/ç stripped, legal term roots)
  3) simplified (plain language)
  4) domain-specific keywords (from detected legal domain)
  5) keyword-heavy (entities, numbers, dates, articles)
  6-12) LLM variants (synonyms, broader, narrower, formal, etc.)
  13-18) diacritical + entity + fallback variants
"""

import json
import logging
import re
from openai import OpenAI
from backend.config import settings
from backend.text_normalizer import (
    normalize_query, normalize_legal_query,
    expand_diacritical_variants,
)

logger = logging.getLogger("rag.expander")

_client = OpenAI(api_key=settings.OPENAI_API_KEY)


# ═══════════════════════════════════════════════════════════════
# DOMAIN TAXONOMY — maps legal domains to trigger words, search
# keywords, and related terms.  Every document in the corpus
# must be reachable from at least one domain.
# ═══════════════════════════════════════════════════════════════

_LEGAL_DOMAINS: list[dict] = [
    # ── Constitutional ──────────────────────────────────
    {
        "id": "constitutional",
        "triggers": [
            "kushtetut", "kushtetues", "te drejtat themelore",
            "liri", "lirite", "barazi", "demokraci",
            "te drejtat e njeriut", "referendum", "president",
            "gjykate kushtetuese", "amendament",
        ],
        "search_terms": [
            "Kushtetuta e Republikes se Shqiperise",
            "te drejtat dhe lirite themelore kushtetuta",
            "parimet kushtetuese",
        ],
    },
    # ── Civil ───────────────────────────────────────────
    {
        "id": "civil",
        "triggers": [
            "civil", "pronesi", "pronesise", "zoterim",
            "kontrat", "detyr", "detyrimi", "demshperblim",
            "trashegim", "trashegimia", "testament",
            "servitut", "barrë", "hipoteke", "peng",
            "send", "shitje", "qera", "huadheni",
        ],
        "search_terms": [
            "Kodi Civil detyrimet kontrata pronesia",
            "e drejta civile detyrimet",
            "trashegimia kontrata Kodi Civil",
        ],
    },
    # ── Civil Procedure ─────────────────────────────────
    {
        "id": "civil_procedure",
        "triggers": [
            "procedur", "procedura civile", "gjykim civil",
            "padi", "paditës", "paditur", "gjykim",
            "apel", "ankim", "ekzekutim", "urdhër ekzekutimi",
            "prove", "deshmitar", "seancë", "vendim gjyqësor",
            "gjykate", "arbitrazh",
        ],
        "search_terms": [
            "Kodi Procedures Civile procedura gjyqesore",
            "padia gjykimi civil ankimi apeli",
            "procedura civile gjykata",
        ],
    },
    # ── Criminal ────────────────────────────────────────
    {
        "id": "criminal",
        "triggers": [
            "penal", "krim", "veper penale", "vepra penale",
            "denim", "burg", "gjobe", "ndeshkim",
            "vjedhje", "vrasje", "mashtr", "korrupsion",
            "droge", "trafik", "falsifik", "armë",
        ],
        "search_terms": [
            "Kodi Penal vepra penale denimet",
            "krim veper penale sanksion",
            "Kodi Penal burg gjobe",
        ],
    },
    # ── Criminal Procedure ──────────────────────────────
    {
        "id": "criminal_procedure",
        "triggers": [
            "procedura penale", "hetim", "prokurori",
            "arrestim", "mase siguri", "paraburgim",
            "gjykim penal", "akuz", "ndjekje penale",
            "deshmitar", "ekspert", "gjykate penale",
        ],
        "search_terms": [
            "Kodi Procedures Penale hetimi gjykimi penal",
            "procedura penale arrestimi prokuroria",
            "ndjekja penale masa sigurimit",
        ],
    },
    # ── Juvenile Justice ────────────────────────────────
    {
        "id": "juvenile",
        "triggers": [
            "mitur", "te mitur", "femij", "femije",
            "drejtesia per te mitur", "i mitur", "te miturit",
            "kujdes", "mbrojtje femijeve", "adoleshent",
        ],
        "search_terms": [
            "Kodi Drejtesise Penale per te Mitur",
            "drejtesia per te mitur femije",
            "te miturit vepra penale",
            "mbrojtja e femijeve ne procesin penal",
        ],
    },
    # ── Military Criminal ───────────────────────────────
    {
        "id": "military",
        "triggers": [
            "ushtarak", "ushtar", "ushtri", "forcave te armatosura",
            "penal ushtarak", "dezertim", "komandat",
        ],
        "search_terms": [
            "Kodi Penal Ushtarak",
            "vepra penale ushtarake",
            "krime ushtarake forcat e armatosura",
        ],
    },
    # ── Electoral ───────────────────────────────────────
    {
        "id": "electoral",
        "triggers": [
            "zgjedh", "zgjedhje", "zgjedhor", "votim",
            "vot", "kandidat", "fushat", "parti",
            "komision zgjedhor", "kqz", "lista",
        ],
        "search_terms": [
            "Kodi Zgjedhor zgjedhjet votimi",
            "procedura zgjedhore kandidatet partite",
            "komisioni qendror zgjedhjeve",
        ],
    },
    # ── Administrative Procedure ────────────────────────
    {
        "id": "administrative",
        "triggers": [
            "administrat", "akt administrativ", "procedur administrative",
            "organ publik", "administrate publike",
            "ankimi administrativ", "sherbim publik",
            "vendim administrativ", "leje", "licenc",
        ],
        "search_terms": [
            "Kodi Procedurave Administrative",
            "procedura administrative akti administrativ",
            "ankimi administrativ organi publik",
            "vendimi administrativ ligji administrates",
        ],
    },
    # ── Family ──────────────────────────────────────────
    {
        "id": "family",
        "triggers": [
            "familj", "martese", "martes", "divorc", "shkurorezim",
            "femij", "biresim", "kujdestar", "alimenta",
            "bashkeshort", "prindi", "atesi", "amesi",
            "paternitet", "biresi",
        ],
        "search_terms": [
            "Kodi Familjes martesa divorci",
            "e drejta familjare kujdestaria biresia",
            "alimentat prindi femija",
        ],
    },
    # ── Labor ───────────────────────────────────────────
    {
        "id": "labor",
        "triggers": [
            "pun", "pune", "punesim", "punonjes", "punedhenes",
            "kontrat pune", "pagë", "paga", "pushim",
            "sindikat", "grev", "largim", "shkark",
            "sigurim shoqeror", "sigurime",
        ],
        "search_terms": [
            "Kodi Punes marredheniet e punes",
            "kontrata e punes punesimi punedhenes",
            "e drejta e punes paga pushimet",
        ],
    },
    # ── Road Traffic ────────────────────────────────────
    {
        "id": "road",
        "triggers": [
            "rrugor", "trafik", "automjet", "makine", "shofer",
            "patent", "sinjal", "rruge", "shpejtesi",
            "aksident", "siguri rrugore", "polici rrugor",
        ],
        "search_terms": [
            "Kodi Rrugor rregullat e qarkullimit",
            "qarkullimi rrugor automjete patenta",
            "siguria rrugore aksidente",
        ],
    },
    # ── Customs ─────────────────────────────────────────
    {
        "id": "customs",
        "triggers": [
            "doganor", "doganë", "dogane", "import", "eksport",
            "tarif", "mallra", "tregti", "kufir",
            "procedura doganore", "deklarate doganore",
        ],
        "search_terms": [
            "Kodi Doganor procedura doganore",
            "importi eksporti tarifat doganore",
            "deklarata doganore mallra kufiri",
            "tregti nderkombetare doganat",
        ],
    },
    # ── Railway ─────────────────────────────────────────
    {
        "id": "railway",
        "triggers": [
            "hekurudh", "hekurudhor", "tren", "transporti hekurudhor",
            "linja hekurudhore", "stacion",
        ],
        "search_terms": [
            "Kodi Hekurudhor transporti hekurudhor",
            "linjat hekurudhore treni",
        ],
    },
    # ── Aviation ────────────────────────────────────────
    {
        "id": "aviation",
        "triggers": [
            "ajror", "aviacion", "fluturim", "aeroplan",
            "aeroport", "pilot", "hapesira ajrore",
        ],
        "search_terms": [
            "Kodi Ajror hapesira ajrore aviacion",
            "fluturimi transporti ajror aeroporte",
        ],
    },
    # ── Maritime ────────────────────────────────────────
    {
        "id": "maritime",
        "triggers": [
            "detar", "det", "anije", "port", "lundr",
            "transport detar", "ngarkes", "trageti",
        ],
        "search_terms": [
            "Kodi Detar transporti detar",
            "anijet portet lundrimi detar",
        ],
    },
    # ── Construction ────────────────────────────────────
    {
        "id": "construction",
        "triggers": [
            "ndert", "ndërt", "ndertim", "ndertimi",
            "leje ndertimi", "ndertese", "objekt",
            "konstruksion", "kantijer", "pallat",
        ],
        "search_terms": [
            "legjislacioni per ndertimet leje ndertimi",
            "ndertim ndertesa objekte ndertimore",
            "rregullat e ndertimit",
        ],
    },
    # ── Cadastre ────────────────────────────────────────
    {
        "id": "cadastre",
        "triggers": [
            "kadastr", "kadaster", "kadastral", "regjist",
            "regjistrim", "pasuri", "pasurite", "toke",
            "ngastre", "certifikat", "hipoteke",
        ],
        "search_terms": [
            "legjislacioni per kadastren regjistrimi",
            "kadaster pasurite e paluajtshme",
            "regjistrimi i pasurive te paluajtshme",
        ],
    },
    # ── Notary ──────────────────────────────────────────
    {
        "id": "notary",
        "triggers": [
            "noter", "noteri", "noterial", "akt noterial",
            "vertetim", "legalizim akti", "prokure",
        ],
        "search_terms": [
            "legjislacioni per noterine",
            "akti noterial noter vertetimi",
            "sherbimi noterial dokumentet",
        ],
    },
    # ── Urban Planning ──────────────────────────────────
    {
        "id": "urban",
        "triggers": [
            "urbanist", "urbanistik", "plan rregullues",
            "plan zhvillimi", "territori", "planifik",
            "zhvillim urban", "harte", "zona",
        ],
        "search_terms": [
            "legjislacioni per urbanistiken",
            "planifikimi i territorit urbanistika",
            "plani rregullues zhvillimi urban",
        ],
    },
    # ── Domestic Violence ───────────────────────────────
    {
        "id": "domestic_violence",
        "triggers": [
            "dhun", "dhuna", "familje dhune", "mbrojtje",
            "urdher mbrojtje", "viktim", "abuzim",
            "dhuna ne familje", "dhuna ndaj grave",
            "parandalim dhune",
        ],
        "search_terms": [
            "parandalimi mbrojtja dhuna ndaj grave dhuna ne familje",
            "urdhri i mbrojtjes dhuna familjare",
            "ligji kunder dhunes ne familje viktima",
            "masat mbrojtese dhuna familjare",
        ],
    },
    # ── Legalization ────────────────────────────────────
    {
        "id": "legalization",
        "triggers": [
            "legaliz", "legalizim", "informal",
            "ndertim informal", "ndertim pa leje",
        ],
        "search_terms": [
            "legjislacioni per legalizimet",
            "legalizimi i ndertimeve informale",
            "ndertim pa leje legalizim",
        ],
    },
    # ── Immovable Property ──────────────────────────────
    {
        "id": "property",
        "triggers": [
            "pasuri", "paluajtshm", "prone", "toke",
            "apartament", "shitblerje", "qera",
            "titull pronesi", "certifikat pronesie",
            "kalim pronesi",
        ],
        "search_terms": [
            "legjislacioni per pasurite e paluajtshme",
            "pronesia e pasurive te paluajtshme",
            "titull pronesie kalimi i pronesise",
        ],
    },
    # ── Civil Service ───────────────────────────────────
    {
        "id": "civil_service",
        "triggers": [
            "nepunes", "nepunesi", "nepunesine",
            "administrat publik", "sherbyes civil",
            "funksionar", "nenpunes", "prurje",
        ],
        "search_terms": [
            "ligji per nepunesine civile",
            "nepunesi civile administrata publike",
            "statusi i nepunesit civil",
            "marredheniet e punes ne sherbimin civil",
        ],
    },
    # ── Concessions / PPP ───────────────────────────────
    {
        "id": "concessions",
        "triggers": [
            "koncesion", "partneritet", "publik privat",
            "ppp", "kontrat koncesioni",
        ],
        "search_terms": [
            "ligji per koncesionet partneritetin publik privat",
            "kontrata e koncesionit PPP",
        ],
    },
    # ── Hunting ─────────────────────────────────────────
    {
        "id": "hunting",
        "triggers": [
            "gjueti", "gjah", "gjuajt", "kafshe",
            "gjueti e egra", "arme gjuetie",
        ],
        "search_terms": [
            "ligji per gjuetine",
            "gjuetia rregullat e gjuetise",
            "kafshet e egra gjuetia",
        ],
    },
    # ── Gender Equality ─────────────────────────────────
    {
        "id": "gender",
        "triggers": [
            "gjinor", "barazi gjinore", "diskriminim",
            "grua", "burrë", "gjini", "femër", "mashkull",
        ],
        "search_terms": [
            "ligji per barazine gjinore",
            "barazia gjinore diskriminimi",
            "te drejtat e grave barazia",
        ],
    },
    # ── Co-ownership / Building Management ──────────────
    {
        "id": "co_ownership",
        "triggers": [
            "bashkepronesi", "bashkepronar",
            "ndertesa", "pallat", "apartament",
            "administrim ndertese", "tarrace",
            "rregullore ndertese", "keste",
        ],
        "search_terms": [
            "ligji per administrimin bashkepronesise ndertesa",
            "bashkepronaresia ne ndertesa pallat",
            "administrimi i nderteses bashkepronar",
            "rregullat e bashkepronesise ndertesa",
        ],
    },
]


def _detect_domains(question: str) -> list[dict]:
    """Detect which legal domains match the user's query.

    Returns matched domains sorted by trigger-hit count (best first).
    """
    q_lower = normalize_query(question)
    q_words = set(re.findall(r'\b\w{3,}\b', q_lower))

    scored: list[tuple[int, dict]] = []
    for domain in _LEGAL_DOMAINS:
        hits = 0
        for trigger in domain["triggers"]:
            t_lower = trigger.lower()
            # Exact substring match in the query
            if t_lower in q_lower:
                hits += 2  # strong signal
            # Partial word overlap
            elif any(t_lower.startswith(w) or w.startswith(t_lower)
                     for w in q_words if len(w) >= 3):
                hits += 1
        if hits > 0:
            scored.append((hits, domain))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored]


# ─── LLM expansion prompt ──────────────────────────────────

EXPANSION_PROMPT = """\
Ti je një specialist i kërkimit juridik shqiptar.

Detyra: Gjenero variante kërkimi për pyetjen e përdoruesit.
Çdo variant DUHET të ruajë qëllimin e njëjtë — mos e ndrysho pyetjen.

Pyetja origjinale:
"{question}"

{domain_hint}

Gjenero saktësisht këto variante (në shqip):
1. original — pyetja siç është
2. simplified — version i thjeshtëzuar, gjuhë e lehtë
3. synonyms — zëvendëso fjalë kyçe me sinonime shqip
4. keywords — VETËM fjalë kyçe: emra, numra, data, nene, ligje
5. broader — e njëjta temë por më e gjerë
6. narrower — e njëjta temë por më specifike
7. legal_formal — terminologji juridike formale
8. reformulation — reformulo me të njëjtin kuptim
9. imperative — formuloji si kërkesë direkte
10. definition_seeking — formuloji si pyetje përkufizimi
11. context_query — pyet për kontekstin / historikun
12. comparison — formuloji si krahasim nëse ka kuptim

Përgjigju VETËM me JSON: {{"variants": ["v1", "v2", ...]}}
"""


# ─── Entity / keyword extraction ───────────────────────────

_NENI_RE = re.compile(r'[Nn]eni\s+\d+', re.UNICODE)
_LIGJ_RE = re.compile(r'[Ll]igj(?:i|in|it)?\s+[Nn]r\.?\s*[\d/.]+', re.UNICODE)
_DATE_RE = re.compile(r'\d{1,2}[./]\d{1,2}[./]\d{2,4}', re.UNICODE)
_NUMBER_RE = re.compile(r'\b\d{2,}\b')
_KOD_RE = re.compile(r'[Kk]od(?:i|it|in)?\s+\w+', re.UNICODE)


def _extract_entities(question: str) -> list[str]:
    """Extract legal entities (articles, law numbers, dates, codes)."""
    entities = []
    entities.extend(_NENI_RE.findall(question))
    entities.extend(_LIGJ_RE.findall(question))
    entities.extend(_KOD_RE.findall(question))
    entities.extend(_DATE_RE.findall(question))
    entities.extend(_NUMBER_RE.findall(question))
    return list(set(entities))


def _extract_keywords(question: str) -> list[str]:
    """Extract significant keywords (3+ chars, no stopwords)."""
    stopwords = frozenset(
        'dhe ose per nga nje tek te ne me se ka si do jane eshte nuk qe i e '
        'ky kjo keto ato por nese edhe mund duhet cfare cilat cili si eshte '
        'jane kane ka nje'.split()
    )
    words = re.findall(r'\b\w{3,}\b', question.lower())
    return [w for w in words if w not in stopwords]


# ─── Main expansion function ──────────────────────────────

async def expand_query(question: str) -> list[str]:
    """Generate 10-18 intent-preserving search variants.

    Pipeline:
    1. Deterministic: normalization, entity extraction, keywords
    2. Domain detection: inject domain-specific search terms
    3. LLM: intent-preserving reformulations
    4. Diacritical expansion
    5. Deduplicate + cap at 18
    """
    question = question.strip()
    if not question:
        return [question]

    variants = [question]

    # ── 1. Deterministic normalization ────────────────────

    norm = normalize_query(question)
    if norm != question.lower():
        variants.append(norm)

    legal_norm = normalize_legal_query(question)
    if legal_norm != norm:
        variants.append(legal_norm)

    # Entity-focused
    entities = _extract_entities(question)
    if entities:
        variants.append(" ".join(entities))

    # Keyword-only
    keywords = _extract_keywords(question)
    if len(keywords) >= 2:
        variants.append(" ".join(keywords))

    # ── 2. Domain-specific expansion ──────────────────────

    detected_domains = _detect_domains(question)
    domain_hint = ""

    if detected_domains:
        domain_names = [d["id"] for d in detected_domains[:3]]
        logger.info(
            f"Domain detection: '{question[:50]}' -> "
            f"domains={domain_names}"
        )

        # Inject domain-specific search terms (top 2 domains)
        for domain in detected_domains[:2]:
            for term in domain["search_terms"]:
                variants.append(term)

        # Build domain hint for LLM prompt
        top_domain = detected_domains[0]
        domain_hint = (
            f"Fusha ligjore e zbuluar: {top_domain['id']}. "
            f"Terma kyç të fushës: {', '.join(top_domain['triggers'][:8])}. "
            f"Sigurohu që disa variante përdorin terminologjinë e kësaj fushe."
        )
    else:
        logger.info(f"Domain detection: '{question[:50]}' -> no specific domain")

    # ── 3. LLM variants ──────────────────────────────────

    try:
        response = _client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system",
                 "content": "Gjenero variante kërkimi në format JSON."},
                {"role": "user",
                 "content": EXPANSION_PROMPT.format(
                     question=question,
                     domain_hint=domain_hint,
                 )},
            ],
            temperature=0.4,
            max_tokens=1000,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()

        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            for key in ("variants", "queries", "results", "data"):
                if key in parsed and isinstance(parsed[key], list):
                    parsed = parsed[key]
                    break
            else:
                vals = list(parsed.values())
                if vals and isinstance(vals[0], list):
                    parsed = vals[0]

        if isinstance(parsed, list):
            for v in parsed:
                if isinstance(v, str) and v.strip():
                    variants.append(v.strip())

        logger.info(
            f"Query expansion: '{question[:60]}' -> "
            f"{len(variants)} raw variants (LLM OK)"
        )

    except Exception as e:
        logger.warning(f"LLM query expansion failed: {e}. Using deterministic only.")
        variants.extend(_fallback_variants(question))

    # ── 4. Diacritical expansion ──────────────────────────

    for kw in keywords[:4]:
        diac_variants = expand_diacritical_variants(kw)
        for dv in diac_variants:
            if dv != kw:
                kw_query = question.lower().replace(kw, dv)
                if kw_query not in [v.lower() for v in variants]:
                    variants.append(kw_query)
                    break

    # ── 5. Deduplicate + cap ──────────────────────────────

    seen = set()
    unique = []
    for v in variants:
        key = v.lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(v)

    cap = 18
    logger.info(
        f"Query expansion final: '{question[:50]}' -> "
        f"{len(unique[:cap])} unique variants "
        f"(domains={[d['id'] for d in detected_domains[:2]]})"
    )

    return unique[:cap]


def _fallback_variants(question: str) -> list[str]:
    """Generate additional deterministic variants when LLM fails."""
    variants = []

    clean = question.rstrip("?!.").strip()
    if clean != question:
        variants.append(clean)

    words = _extract_keywords(question)
    if len(words) >= 3:
        variants.append(" ".join(words[:5]))
        variants.append(" ".join(reversed(words[:4])))

    # Domain fallback — still inject domain terms even without LLM
    detected = _detect_domains(question)
    for domain in detected[:2]:
        for term in domain["search_terms"][:2]:
            variants.append(term)

    return variants
