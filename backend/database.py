"""PostgreSQL database for document metadata, chunks tracking, chat history, users.

Uses asyncpg with a connection pool.  Requires DATABASE_URL to be set.
"""

import asyncpg
import json
import logging
import re
from datetime import datetime
from backend.config import settings

logger = logging.getLogger("rag.database")


def _parse_ts(val) -> datetime | None:
    """Convert a string/datetime to a datetime object, or None."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        url = settings.DATABASE_URL
        if not url:
            raise RuntimeError("DATABASE_URL is not set")
        _pool = await asyncpg.create_pool(url, min_size=2, max_size=10)
        logger.info("PostgreSQL connection pool created")
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def init_db():
    """Create tables, indexes, and seed data."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # ── Users ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT DEFAULT '',
                is_admin BOOLEAN DEFAULT FALSE,
                supabase_uid TEXT UNIQUE,
                trial_ends_at TIMESTAMPTZ,
                trial_used_at TIMESTAMPTZ,
                signup_ip TEXT,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                is_premium BOOLEAN DEFAULT FALSE,
                subscription_status TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        for col, coltype, default in [
            ("stripe_customer_id", "TEXT", "NULL"),
            ("stripe_subscription_id", "TEXT", "NULL"),
            ("is_premium", "BOOLEAN", "FALSE"),
            ("subscription_status", "TEXT", "''"),
        ]:
            try:
                await conn.execute(
                    f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {coltype} DEFAULT {default}"
                )
            except Exception:
                pass

        # ── Documents ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                filename TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                title TEXT,
                law_number TEXT,
                law_date TEXT,
                status TEXT DEFAULT 'processing',
                total_chunks INTEGER DEFAULT 0,
                page_count INTEGER DEFAULT 0,
                error_message TEXT,
                metadata_json TEXT DEFAULT '{}',
                storage_bucket TEXT DEFAULT 'Ligje',
                storage_path TEXT,
                uploaded_at TIMESTAMPTZ DEFAULT NOW(),
                processed_at TIMESTAMPTZ
            )
        """)
        # Add storage columns if table already exists without them
        for col, default in [("storage_bucket", "'Ligje'"), ("storage_path", "NULL")]:
            try:
                await conn.execute(
                    f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS {col} TEXT DEFAULT {default}"
                )
            except Exception:
                pass

        # ── Document Chunks ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS document_chunks (
                id SERIAL PRIMARY KEY,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                article TEXT,
                section_title TEXT DEFAULT '',
                pages TEXT,
                page_start INTEGER,
                page_end INTEGER,
                char_count INTEGER DEFAULT 0
            )
        """)
        try:
            await conn.execute(
                "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS section_title TEXT DEFAULT ''"
            )
        except Exception:
            pass

        # ── Chat Messages ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sources_json TEXT DEFAULT '[]',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # ── Subscriptions ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                purchase_token TEXT,
                product_id TEXT,
                platform TEXT DEFAULT 'google_play',
                status TEXT NOT NULL,
                current_period_end TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # ── Suggested Questions ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS suggested_questions (
                id SERIAL PRIMARY KEY,
                category TEXT NOT NULL,
                question TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(category, question)
            )
        """)

        # ── Indexes ──
        index_statements = [
            "CREATE INDEX IF NOT EXISTS idx_documents_user_id ON documents(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status)",
            "CREATE INDEX IF NOT EXISTS idx_documents_user_status ON documents(user_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON document_chunks(document_id)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_user_id ON document_chunks(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_user_doc ON document_chunks(user_id, document_id)",
            "CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id)",
            "CREATE INDEX IF NOT EXISTS idx_sq_active ON suggested_questions(is_active)",
        ]
        for stmt in index_statements:
            await conn.execute(stmt)

        # GIN index for full-text search on chunk content
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_fts
            ON document_chunks USING GIN (to_tsvector('simple', content))
        """)

        # ── Seed suggested questions ──
        count = await conn.fetchval("SELECT COUNT(*) FROM suggested_questions")
        if count == 0:
            seed_questions = [
                # ── Kodi i Procedurës Penale ──
                ("Parimet themelore", "Cili është qëllimi i procedimit penal?", 1),
                ("Parimet themelore", "Çfarë do të thotë prezumimi i pafajësisë?", 2),
                ("Parimet themelore", "A lejohet gjykimi i një personi dy herë për të njëjtën vepër?", 3),
                ("Gjykatat dhe juridiksioni", "Cilat janë gjykatat që shqyrtojnë çështjet penale?", 1),
                ("Gjykatat dhe juridiksioni", "Si përcaktohet kompetenca territoriale e gjykatës?", 2),
                ("Gjykatat dhe juridiksioni", "Kur gjykon një gjyqtar i vetëm?", 3),
                ("Gjykatat dhe juridiksioni", "Kur kërkohet trup gjykues me tre gjyqtarë?", 4),
                ("Gjykatat dhe juridiksioni", "Kur një gjyqtar është i papajtueshëm për të gjykuar?", 5),
                ("Prokurori dhe policia gjyqësore", "Cili është roli i prokurorit në procedim penal?", 1),
                ("Prokurori dhe policia gjyqësore", "Çfarë funksioni ka policia gjyqësore?", 2),
                ("Prokurori dhe policia gjyqësore", "Kur prokurori mund të pushojë çështjen?", 3),
                ("Prokurori dhe policia gjyqësore", "Si kontrollon prokurori veprimet e policisë gjyqësore?", 4),
                ("Prokurori dhe policia gjyqësore", "Kur transferohet çështja në një prokurori tjetër?", 5),
                ("I pandehuri dhe të drejtat", "Kur një person merr statusin e të pandehurit?", 1),
                ("I pandehuri dhe të drejtat", "Cilat janë të drejtat themelore të të pandehurit?", 2),
                ("I pandehuri dhe të drejtat", "A ka të drejtë i pandehuri të mos flasë?", 3),
                ("I pandehuri dhe të drejtat", "Kur është e detyrueshme mbrojtja me avokat?", 4),
                ("I pandehuri dhe të drejtat", "Çfarë të drejtash ka një person i arrestuar?", 5),
                ("Provat dhe procedurat", "Si mblidhen provat në procedimin penal?", 1),
                ("Provat dhe procedurat", "A mund të përdoren prova të paligjshme?", 2),
                ("Provat dhe procedurat", "Çfarë janë provat në favor të të pandehurit?", 3),
                ("Provat dhe procedurat", "Si bëhet marrja në pyetje e të pandehurit?", 4),
                ("Provat dhe procedurat", "A lejohet përdorimi i dhunës për të marrë deklarime?", 5),
                ("Masat e sigurimit", "Çfarë janë masat e sigurimit personal?", 1),
                ("Masat e sigurimit", "Kur vendoset arresti me burg?", 2),
                ("Masat e sigurimit", "Kur vendoset arresti në shtëpi?", 3),
                ("Masat e sigurimit", "Cilat janë kushtet për ndalimin e personit?", 4),
                ("Masat e sigurimit", "Si kontrollohet ligjshmëria e masës së sigurimit?", 5),
                ("Hetimi paraprak", "Kur fillon hetimi penal?", 1),
                ("Hetimi paraprak", "Çfarë roli ka prokurori në hetim?", 2),
                ("Hetimi paraprak", "Sa zgjat hetimi paraprak?", 3),
                ("Hetimi paraprak", "Kur pushohet hetimi?", 4),
                ("Hetimi paraprak", "Kur çështja kalon për gjykim?", 5),
                ("Gjykimi", "Cilat janë fazat e gjykimit?", 1),
                ("Gjykimi", "Si paraqiten provat në gjykatë?", 2),
                ("Gjykimi", "Kur jepet vendimi?", 3),
                ("Gjykimi", "Çfarë përmban vendimi penal?", 4),
                ("Mjetet e ankimit", "Çfarë është ankimi në apel?", 1),
                ("Mjetet e ankimit", "Kur mund të bëhet rekurs në Gjykatën e Lartë?", 2),
                ("Mjetet e ankimit", "Kush ka të drejtë të ankimojë vendimin?", 3),
                ("Mjetet e ankimit", "Brenda çfarë afati bëhet ankimi?", 4),
                ("Mjetet e ankimit", "Çfarë ndodh pas pranimit të ankimit?", 5),
                ("Ekzekutimi i vendimit", "Si ekzekutohet një vendim penal?", 1),
                ("Ekzekutimi i vendimit", "Kur fillon dënimi?", 2),
                ("Ekzekutimi i vendimit", "A mund të pezullohet ekzekutimi i vendimit?", 3),
                # ── Ligji për Nëpunësin Civil ──
                ("Dispozita të Përgjithshme", "Cili është qëllimi i ligjit për nëpunësin civil?", 1),
                ("Dispozita të Përgjithshme", "Çfarë rregullon marrëdhënia e shërbimit civil?", 2),
                ("Dispozita të Përgjithshme", "Për kë zbatohet ky ligj në administratën publike?", 3),
                ("Dispozita të Përgjithshme", "Cilat kategori përjashtohen nga zbatimi i ligjit?", 4),
                ("Dispozita të Përgjithshme", "Çfarë kuptimi ka termi 'nëpunës civil'?", 5),
                ("Administrimi i Shërbimit Civil", "Cilat janë parimet e administrimit të shërbimit civil?", 1),
                ("Administrimi i Shërbimit Civil", "Çfarë roli ka Departamenti i Administratës Publike?", 2),
                ("Administrimi i Shërbimit Civil", "Çfarë funksioni ka ASPA?", 3),
                ("Administrimi i Shërbimit Civil", "Si organizohet njësia e burimeve njerëzore?", 4),
                ("Administrimi i Shërbimit Civil", "Cilat janë detyrat e Komisionerit të Shërbimit Civil?", 5),
                ("Dosjet dhe Planifikimi", "Çfarë përmban dosja individuale e nëpunësit civil?", 1),
                ("Dosjet dhe Planifikimi", "Çfarë është regjistri qendror i personelit?", 2),
                ("Dosjet dhe Planifikimi", "Si bëhet planifikimi i rekrutimit në shërbimin civil?", 3),
                ("Klasifikimi i Pozicioneve", "Si klasifikohen pozicionet në shërbimin civil?", 1),
                ("Klasifikimi i Pozicioneve", "Cilat janë kategoritë e nëpunësve civilë?", 2),
                ("Klasifikimi i Pozicioneve", "Çfarë përfshin kategoria e lartë drejtuese?", 3),
                ("Pranimi në Shërbimin Civil", "Cilat janë kërkesat për t'u bërë nëpunës civil?", 1),
                ("Pranimi në Shërbimin Civil", "Si zhvillohet konkursi për nëpunës civil?", 2),
                ("Pranimi në Shërbimin Civil", "Si bëhet vlerësimi i kandidatëve?", 3),
                ("Pranimi në Shërbimin Civil", "Sa zgjat lista e fituesve?", 4),
                ("Pranimi në Shërbimin Civil", "Çfarë është periudha e provës?", 5),
                ("Lëvizja dhe Ngritja në Detyrë", "Çfarë është lëvizja paralele?", 1),
                ("Lëvizja dhe Ngritja në Detyrë", "Si bëhet ngritja në detyrë?", 2),
                ("Lëvizja dhe Ngritja në Detyrë", "Si plotësohen vendet e lira në administratë?", 3),
                ("Të Drejtat e Nëpunësit Civil", "Cilat janë të drejtat kryesore të nëpunësit civil?", 1),
                ("Të Drejtat e Nëpunësit Civil", "Si përbëhet paga e nëpunësit civil?", 2),
                ("Të Drejtat e Nëpunësit Civil", "A ka të drejtë nëpunësi civil të bëjë grevë?", 3),
                ("Të Drejtat e Nëpunësit Civil", "A ka të drejtë nëpunësi civil të marrë pjesë në politikë?", 4),
                ("Detyrimet", "Cilat janë detyrimet e nëpunësit civil në punë?", 1),
                # ── Kodi i Punës ──
                ("Bazat dhe fusha e zbatimit", "Çfarë rregullon Kodi i Punës i Shqipërisë?", 1),
                ("Bazat dhe fusha e zbatimit", "Në çfarë bazash ligjore mbështetet Kodi i Punës?", 2),
                ("Bazat dhe fusha e zbatimit", "Në cilat raste zbatohet ligji shqiptar për kontratat e punës?", 3),
                ("Bazat dhe fusha e zbatimit", "Si përcaktohet ligji që zbatohet për punëmarrës që punon në disa shtete?", 4),
                ("Bazat dhe fusha e zbatimit", "Kur mund të zgjidhet një ligj tjetër për kontratën e punës?", 5),
                ("Bazat dhe fusha e zbatimit", "Cilat kategori punësimi përjashtohen nga Kodi i Punës?", 6),
                ("Bazat dhe fusha e zbatimit", "Si zbatohet Kodi për kontratat e lidhura para hyrjes në fuqi?", 7),
                ("Bazat dhe fusha e zbatimit", "Çfarë është kompetenca territoriale në çështjet e punës?", 8),
                ("Të drejtat themelore në punë", "Çfarë konsiderohet punë e detyruar?", 1),
                ("Të drejtat themelore në punë", "Në cilat raste puna nuk konsiderohet e detyruar?", 2),
                ("Të drejtat themelore në punë", "Çfarë ndalon ligji për diskriminimin në punë?", 3),
                ("Të drejtat themelore në punë", "Çfarë konsiderohet diskriminim sipas Kodit të Punës?", 4),
                ("Të drejtat themelore në punë", "Kur lejohet trajtim i ndryshëm pa u konsideruar diskriminim?", 5),
                ("Të drejtat themelore në punë", "Çfarë detyrimesh ka punëdhënësi për barazinë në punë?", 6),
                ("Të drejtat themelore në punë", "Çfarë të drejtash kanë punonjësit në lidhje me sindikatat?", 7),
                ("Të drejtat themelore në punë", "Si mbrohen punonjësit që raportojnë shkelje ose korrupsion?", 8),
                ("Burimet e marrëdhënies së punës", "Cilat janë burimet kryesore që rregullojnë marrëdhënien e punës?", 1),
                ("Burimet e marrëdhënies së punës", "Çfarë ndodh kur një dispozitë bie ndesh me një ligj më të lartë?", 2),
                ("Burimet e marrëdhënies së punës", "A mund të heqë dorë punëmarrësi nga të drejtat e tij?", 3),
                ("Burimet e marrëdhënies së punës", "Çfarë roli kanë kontratat kolektive dhe individuale?", 4),
                ("Kontrata e punës", "Çfarë është kontrata e punës?", 1),
                ("Kontrata e punës", "Cilat janë elementet që duhet të përmbajë kontrata e punës?", 2),
                ("Kontrata e punës", "Kur konsiderohet e lidhur një kontratë pune?", 3),
                ("Kontrata e punës", "A është e detyrueshme forma e shkruar e kontratës?", 4),
                ("Kontrata e punës", "Çfarë ndodh nëse kontrata nuk është lidhur me shkrim?", 5),
                ("Kontrata e punës", "Çfarë është kontrata me kohë të pjesshme?", 6),
                ("Kontrata e punës", "Çfarë të drejtash ka punonjësi me kohë të pjesshme?", 7),
                ("Kontrata e punës", "Çfarë është telepuna dhe puna nga shtëpia?", 8),
                ("Kontrata e punës", "Çfarë është kontrata e mësimit të profesionit?", 9),
                ("Kontrata e punës", "Çfarë është kontrata e agjentit tregtar?", 10),
                ("Punësimi i përkohshëm dhe agjencitë", "Çfarë është Agjencia e Punësimit të Përkohshëm?", 1),
                ("Punësimi i përkohshëm dhe agjencitë", "Cilat janë të drejtat e punonjësve të punësuar nga agjencitë?", 2),
                ("Punësimi i përkohshëm dhe agjencitë", "Sa mund të zgjasë një punësim i përkohshëm?", 3),
                ("Punësimi i përkohshëm dhe agjencitë", "Kush paguan pagën e punonjësit të agjencisë?", 4),
                ("Punësimi i përkohshëm dhe agjencitë", "Çfarë detyrimesh ka ndërmarrja pritëse?", 5),
                ("Punësimi i përkohshëm dhe agjencitë", "Kur ndalohet përdorimi i punësimit të përkohshëm?", 6),
                ("Detyrimet e punëmarrësit", "Çfarë detyrimi ka punëmarrësi për kryerjen e punës?", 1),
                ("Detyrimet e punëmarrësit", "A duhet të zbatojë punëmarrësi çdo urdhër të punëdhënësit?", 2),
                ("Detyrimet e punëmarrësit", "Kur punëmarrësi ka të drejtë të refuzojë urdhra?", 3),
                ("Detyrimet e punëmarrësit", "Çfarë detyrimi ka punëmarrësi për kujdesin në punë?", 4),
                ("Detyrimet e punëmarrësit", "Çfarë është detyrimi i besnikërisë ndaj punëdhënësit?", 5),
                ("Detyrimet e punëmarrësit", "A lejohet punonjësi të punojë për konkurentë?", 6),
                ("Detyrimet e punëmarrësit", "Çfarë përgjegjësie ka punëmarrësi për dëmet?", 7),
                ("Ndalimi i konkurrencës", "Kur mund të ndalohet konkurrenca pas largimit nga puna?", 1),
                ("Ndalimi i konkurrencës", "Sa mund të zgjasë ndalimi i konkurrencës?", 2),
                ("Ndalimi i konkurrencës", "Çfarë kompensimi duhet të marrë punonjësi gjatë këtij ndalimi?", 3),
                ("Ndalimi i konkurrencës", "Kur përfundon ndalimi i konkurrencës?", 4),
                ("Ndalimi i konkurrencës", "Çfarë ndodh nëse punonjësi shkel marrëveshjen e konkurrencës?", 5),
                ("Detyrimet e punëdhënësit", "Çfarë detyrimesh ka punëdhënësi për mbrojtjen e punonjësit?", 1),
                ("Detyrimet e punëdhënësit", "Si mbrohen të dhënat personale të punëmarrësit?", 2),
                # ── Kodi i Familjes ──
                ("Parime të përgjithshme dhe të drejtat e fëmijës", "Cilat janë parimet bazë mbi të cilat mbështetet martesa dhe familja?", 1),
                ("Parime të përgjithshme dhe të drejtat e fëmijës", "Çfarë nënkupton 'interesi më i lartë i fëmijës' dhe kur zbatohet?", 2),
                ("Parime të përgjithshme dhe të drejtat e fëmijës", "Cilat janë detyrimet kryesore të prindërve ndaj fëmijëve?", 3),
                ("Parime të përgjithshme dhe të drejtat e fëmijës", "A kanë fëmijët e lindur jashtë martese të njëjtat të drejta si ata të lindur nga martesa?", 4),
                ("Parime të përgjithshme dhe të drejtat e fëmijës", "Çfarë të drejte ka i mituri për t'u dëgjuar në procedurat që e prekin?", 5),
                ("Kushtet thelbësore për lidhjen e martesës", "Cila është mosha minimale për lidhjen e martesës?", 1),
                ("Kushtet thelbësore për lidhjen e martesës", "Në cilat raste gjykata mund të lejojë martesë para moshës minimale?", 2),
                ("Kushtet thelbësore për lidhjen e martesës", "Si kërkohet dhe vërtetohet pëlqimi i lirë i bashkëshortëve?", 3),
                ("Kushtet thelbësore për lidhjen e martesës", "Kur ndalohet lidhja e një martese të re për shkak të një martese të mëparshme?", 4),
                ("Kushtet thelbësore për lidhjen e martesës", "Cilat janë ndalimet e martesës për shkak të lidhjeve familjare (gjakësore)?", 5),
                ("Ndalime të tjera për lidhjen e martesës", "A lejohet martesa midis vjehrrit dhe nuses apo vjehrrës dhe dhëndrit?", 1),
                ("Ndalime të tjera për lidhjen e martesës", "A lejohet martesa midis njerkut dhe thjeshtrës?", 2),
                ("Ndalime të tjera për lidhjen e martesës", "Kur ndalohet martesa për shkak të gjendjes mendore/psikike?", 3),
                ("Ndalime të tjera për lidhjen e martesës", "Kur ndalohet martesa midis kujdestarit dhe personit në kujdestari?", 4),
                ("Ndalime të tjera për lidhjen e martesës", "Si trajtohet martesa në rastet e birësimit (birësues/birësuar)?", 5),
                ("Shpallja dhe procedura e lidhjes së martesës", "Çfarë është shpallja e martesës dhe pse bëhet?", 1),
                ("Shpallja dhe procedura e lidhjes së martesës", "Ku bëhet shpallja e martesës kur bashkëshortët kanë vendbanime të ndryshme?", 2),
                ("Shpallja dhe procedura e lidhjes së martesës", "Sa ditë duhet të kalojnë pas shpalljes përpara lidhjes së martesës?", 3),
                ("Shpallja dhe procedura e lidhjes së martesës", "Çfarë dokumentesh kërkohen për shpalljen e martesës?", 4),
                ("Shpallja dhe procedura e lidhjes së martesës", "Kur duhet bërë shpallje e re (p.sh. pas kalimit të afateve)?", 5),
                ("Kundërshtimi i lidhjes së martesës", "Kush ka të drejtë të kundërshtojë lidhjen e martesës?", 1),
                ("Kundërshtimi i lidhjes së martesës", "Si bëhet kundërshtimi dhe ku paraqitet?", 2),
                ("Kundërshtimi i lidhjes së martesës", "Çfarë duhet të përmbajë akti i kundërshtimit?", 3),
                ("Kundërshtimi i lidhjes së martesës", "Çfarë ndodh kur nëpunësi i gjendjes civile e pranon kundërshtimin si të rregullt?", 4),
                ("Kundërshtimi i lidhjes së martesës", "Brenda çfarë afatesh vendos gjykata për heqjen ose jo të kundërshtimit?", 5),
                ("Lidhja e martesës dhe refuzimi", "Si zhvillohet lidhja e martesës para nëpunësit të gjendjes civile?", 1),
                ("Lidhja e martesës dhe refuzimi", "Çfarë roli kanë dëshmitarët në lidhjen e martesës?", 2),
                ("Lidhja e martesës dhe refuzimi", "A mund të lidhet martesa pa shpallje? Në cilat raste?", 3),
                ("Lidhja e martesës dhe refuzimi", "Në cilat raste nëpunësi i gjendjes civile mund të refuzojë lidhjen e martesës?", 4),
                ("Lidhja e martesës dhe refuzimi", "Si ankimohet refuzimi i lidhjes së martesës?", 5),
                ("Pavlefshmëria e martesës – shkaqet", "Kur konsiderohet martesa e pavlefshme për mungesë pëlqimi të lirë?", 1),
                ("Pavlefshmëria e martesës – shkaqet", "Çfarë është 'lajthimi' në martesë dhe kur sjell pavlefshmëri?", 2),
                ("Pavlefshmëria e martesës – shkaqet", "Kur pavlefshmëria lidhet me kanosje/kërcënim?", 3),
                ("Pavlefshmëria e martesës – shkaqet", "Kur martesa është e pavlefshme për shkak të moshës?", 4),
                ("Pavlefshmëria e martesës – shkaqet", "Kur martesa është e pavlefshme për shkak të ndalimeve (martesa e dytë, lidhjet, etj.)?", 5),
                ("Pavlefshmëria – afatet dhe e drejta e padisë", "Kush ka të drejtë të ngrejë padi për pavlefshmërinë e martesës?", 1),
                ("Pavlefshmëria – afatet dhe e drejta e padisë", "Cilat raste kanë afat parashkrimi dhe sa është afati?", 2),
                ("Pavlefshmëria – afatet dhe e drejta e padisë", "A mund të ngrihet padi për pavlefshmëri edhe pas zgjidhjes së martesës?", 3),
                ("Pavlefshmëria – afatet dhe e drejta e padisë", "A u kalon trashëgimtarëve e drejta e padisë për pavlefshmëri?", 4),
                ("Pavlefshmëria – afatet dhe e drejta e padisë", "Cilat janë pasojat juridike kur martesa shpallet e pavlefshme?", 5),
                ("Të drejtat dhe detyrimet reciproke të bashkëshortëve", "Cilat janë detyrimet reciproke të bashkëshortëve (besnikëri, ndihmë, bashkëpunim)?", 1),
                ("Të drejtat dhe detyrimet reciproke të bashkëshortëve", "Si zgjidhet mbiemri i përbashkët i bashkëshortëve?", 2),
                ("Të drejtat dhe detyrimet reciproke të bashkëshortëve", "Si përcaktohet mbiemri i fëmijëve kur prindërit kanë mbiemra të ndryshëm?", 3),
                ("Të drejtat dhe detyrimet reciproke të bashkëshortëve", "Si përcaktohet vendbanimi i familjes kur ka mosmarrëveshje?", 4),
                ("Të drejtat dhe detyrimet reciproke të bashkëshortëve", "Çfarë ndodh kur një bashkëshort largohet pa shkak nga vendbanimi familjar?", 5),
                ("Banesa bashkëshortore, autorizime dhe masa urgjente", "A mund të disponohet banesa bashkëshortore pa pëlqimin e tjetrit?", 1),
                ("Banesa bashkëshortore, autorizime dhe masa urgjente", "Në çfarë rrethanash gjykata mund të autorizojë një bashkëshort për veprime juridike?", 2),
                ("Banesa bashkëshortore, autorizime dhe masa urgjente", "Kur lejohet përfaqësimi i bashkëshortit me autorizim gjyqësor?", 3),
                ("Banesa bashkëshortore, autorizime dhe masa urgjente", "Çfarë masash urgjente mund të vendosë gjykata kur cenohen interesat e familjes?", 4),
                ("Banesa bashkëshortore, autorizime dhe masa urgjente", "Çfarë mase urgjente parashikon Kodi në rast dhune në familje (largimi nga banesa)?", 5),
            ]
            await conn.executemany(
                "INSERT INTO suggested_questions (category, question, sort_order) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                seed_questions,
            )

    logger.info("Database initialized successfully")


# ── Document CRUD ──────────────────────────────────────────────

async def create_document(user_id: int, filename: str, original_filename: str,
                          file_type: str, file_size: int,
                          title: str = None, law_number: str = None,
                          law_date: str = None,
                          storage_bucket: str = "Ligje",
                          storage_path: str = None) -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO documents
               (user_id, filename, original_filename, file_type, file_size,
                title, law_number, law_date, storage_bucket, storage_path, status)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'processing')
               RETURNING id""",
            user_id, filename, original_filename, file_type, file_size,
            title, law_number, law_date, storage_bucket, storage_path,
        )
        return row["id"]


async def update_document_status(doc_id: int, status: str,
                                  total_chunks: int = None,
                                  error_message: str = None,
                                  metadata: dict = None):
    pool = await _get_pool()
    parts = ["status = $1"]
    values: list = [status]
    idx = 2

    if total_chunks is not None:
        parts.append(f"total_chunks = ${idx}"); values.append(total_chunks); idx += 1
    if error_message is not None:
        parts.append(f"error_message = ${idx}"); values.append(error_message); idx += 1
    if metadata is not None:
        parts.append(f"metadata_json = ${idx}"); values.append(json.dumps(metadata)); idx += 1
        if metadata.get("title"):
            parts.append(f"title = COALESCE(NULLIF(title, ''), ${idx})")
            values.append(metadata["title"]); idx += 1
        if metadata.get("law_number"):
            parts.append(f"law_number = COALESCE(NULLIF(law_number, ''), ${idx})")
            values.append(metadata["law_number"]); idx += 1
        if metadata.get("law_date"):
            parts.append(f"law_date = COALESCE(NULLIF(law_date, ''), ${idx})")
            values.append(metadata["law_date"]); idx += 1
    if status in ("ready", "failed"):
        parts.append(f"processed_at = ${idx}")
        values.append(datetime.utcnow()); idx += 1

    values.append(doc_id)
    query = f"UPDATE documents SET {', '.join(parts)} WHERE id = ${idx}"
    async with pool.acquire() as conn:
        await conn.execute(query, *values)


async def get_all_documents():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM documents ORDER BY uploaded_at DESC")
        return [dict(r) for r in rows]


async def get_user_documents(user_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM documents WHERE user_id = $1 ORDER BY uploaded_at DESC",
            user_id,
        )
        return [dict(r) for r in rows]


async def get_user_ready_documents(user_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, title, original_filename, total_chunks, uploaded_at
               FROM documents WHERE user_id = $1 AND status = 'ready'
               ORDER BY uploaded_at DESC""",
            user_id,
        )
        return [dict(r) for r in rows]


async def get_all_ready_documents():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, user_id, title, original_filename, total_chunks
               FROM documents WHERE status = 'ready'
               ORDER BY uploaded_at DESC"""
        )
        return [dict(r) for r in rows]


async def get_document(doc_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM documents WHERE id = $1", doc_id)
        return dict(row) if row else None


async def get_document_for_user(doc_id: int, user_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM documents WHERE id = $1 AND user_id = $2",
            doc_id, user_id,
        )
        return dict(row) if row else None


async def delete_document(doc_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM documents WHERE id = $1", doc_id)


async def count_user_documents(user_id: int) -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM documents WHERE user_id = $1", user_id
        )


async def rename_document(doc_id: int, new_title: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET title = $1 WHERE id = $2",
            new_title.strip(), doc_id,
        )


async def update_document_page_count(doc_id: int, page_count: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET page_count = $1 WHERE id = $2",
            page_count, doc_id,
        )


# ── Document Chunks (for keyword search) ──────────────────────

async def insert_chunks(document_id: int, user_id: int, chunks: list[dict]):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        records = []
        for c in chunks:
            pages_str = ",".join(str(p) for p in c.get("pages", []))
            page_list = c.get("pages", [])
            records.append((
                document_id, user_id, c.get("chunk_index", 0),
                c["text"], c.get("article") or "",
                c.get("section_title") or "",
                pages_str,
                min(page_list) if page_list else 0,
                max(page_list) if page_list else 0,
                len(c["text"]),
            ))
        await conn.executemany(
            """INSERT INTO document_chunks
               (document_id, user_id, chunk_index, content, article,
                section_title, pages, page_start, page_end, char_count)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            records,
        )


async def delete_chunks_for_document(document_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM document_chunks WHERE document_id = $1", document_id
        )


async def keyword_search_chunks(query: str, user_id: int = None,
                                 document_id: int = None,
                                 limit: int = 30) -> list[dict]:
    """Full-text keyword search using PostgreSQL tsvector."""
    pool = await _get_pool()
    tsquery = _build_pg_tsquery(query)
    if not tsquery:
        return []

    where_parts = ["to_tsvector('simple', dc.content) @@ to_tsquery('simple', $1)"]
    params: list = [tsquery]
    idx = 2

    if user_id is not None:
        where_parts.append(f"dc.user_id = ${idx}"); params.append(user_id); idx += 1
    if document_id:
        where_parts.append(f"dc.document_id = ${idx}"); params.append(document_id); idx += 1

    params.append(limit)
    where_clause = " AND ".join(where_parts)

    sql = f"""
        SELECT dc.*,
               ts_rank(to_tsvector('simple', dc.content),
                       to_tsquery('simple', $1)) AS fts_rank
        FROM document_chunks dc
        WHERE {where_clause}
        ORDER BY fts_rank DESC
        LIMIT ${idx}
    """

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"FTS query failed: {e}")
        return []


# ── Albanian FTS helpers ──────────────────────────────────────

_ALBANIAN_STOPWORDS = frozenset(
    'dhe ose per nga nje tek te ne me se ka si do jane eshte nuk qe i e '
    'ky kjo keto ato por nese edhe mund duhet'.split()
)

_STEM_FAMILIES = {
    'arsim': [
        'arsim', 'arsimi', 'arsimin', 'arsimit', 'arsimim', 'arsimimi',
        'arsimor', 'arsimore', 'arsimtar', 'arsimtare',
    ],
    'pun': [
        'pun', 'pune', 'punen', 'punes', 'punesim', 'punesimi', 'punesimin',
        'punetor', 'punetore', 'punedhenes', 'punesimit',
    ],
    'shtet': [
        'shtet', 'shteti', 'shtetin', 'shtetit', 'shteteri', 'shteterore',
        'shteteriore', 'shtetas', 'shtetasi',
    ],
    'gjykat': [
        'gjykat', 'gjykata', 'gjykate', 'gjykaten', 'gjykates', 'gjykatesi',
        'gjyqesor', 'gjyqesore', 'gjyqtar', 'gjyqtare', 'gjyqtaret',
    ],
    'kushtetut': [
        'kushtetut', 'kushtetuta', 'kushtetuten', 'kushtetutes',
        'kushtetuese', 'kushtetutshme', 'kushtetutshem',
    ],
    'ligj': [
        'ligj', 'ligji', 'ligjin', 'ligjit', 'ligjor', 'ligjore',
        'ligjet', 'ligjeve',
    ],
    'drejt': [
        'drejt', 'drejte', 'drejta', 'drejten', 'drejtes',
        'drejtesi', 'drejtesise', 'drejtesine',
    ],
    'pronesi': [
        'pronesi', 'pronesise', 'pronesine', 'prone', 'pronen',
        'pronar', 'pronare', 'pronaret',
    ],
    'tatim': [
        'tatim', 'tatimi', 'tatimin', 'tatimit', 'tatimor', 'tatimore',
        'tatimet', 'tatimeve',
    ],
    'familj': [
        'familj', 'familja', 'familje', 'familjen', 'familjes',
        'familjar', 'familjare',
    ],
    'shendet': [
        'shendet', 'shendeti', 'shendetin', 'shendetit', 'shendetesor',
        'shendetesore', 'shendetesi', 'shendetesise',
    ],
    'mjedis': [
        'mjedis', 'mjedisi', 'mjedisin', 'mjedisit', 'mjedisor', 'mjedisore',
    ],
    'siguri': [
        'siguri', 'sigurine', 'sigurise', 'siguria', 'sigurim', 'sigurimi',
        'sigurimet',
    ],
    'zgjedh': [
        'zgjedh', 'zgjedhje', 'zgjedhjen', 'zgjedhjes', 'zgjedhjeve',
        'zgjedhur',
    ],
    'liri': ['liri', 'lirine', 'lirise', 'liria', 'lirite'],
    'detyr': [
        'detyr', 'detyra', 'detyren', 'detyres', 'detyrim', 'detyrimi',
        'detyrimeve', 'detyruesh', 'detyrueshm',
    ],
}

_WORD_TO_STEM: dict[str, str] = {}
for _root, _variants in _STEM_FAMILIES.items():
    for _v in _variants:
        _WORD_TO_STEM[_v] = _root


def _build_pg_tsquery(query: str) -> str:
    """Build a PostgreSQL tsquery string with Albanian stemming."""
    tokens = []
    words = re.findall(r'\b\w{2,}\b', query)
    seen = set()
    for w in words:
        wl = w.lower()
        if wl in _ALBANIAN_STOPWORDS or wl in seen:
            continue
        seen.add(wl)

        stem_root = _WORD_TO_STEM.get(wl)
        if stem_root:
            family = _STEM_FAMILIES[stem_root]
            tokens.append("(" + " | ".join(family) + ")")
        else:
            if len(wl) >= 4:
                tokens.append(f"{wl}:*")
            else:
                tokens.append(wl)

    return " | ".join(tokens) if tokens else ""


# ── Chat CRUD ──────────────────────────────────────────────────

async def save_chat_message(session_id: str, role: str, content: str,
                            sources: list = None):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO chat_messages (session_id, role, content, sources_json)
               VALUES ($1, $2, $3, $4)""",
            session_id, role, content, json.dumps(sources or []),
        )


async def get_chat_history(session_id: str, limit: int = 20):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM chat_messages
               WHERE session_id = $1
               ORDER BY created_at DESC LIMIT $2""",
            session_id, limit,
        )
        results = [dict(r) for r in rows]
        results.reverse()
        return results


# ── Users ────────────────────────────────────────────────────

async def create_user(
    email: str,
    password_hash: str,
    is_admin: bool = False,
    trial_ends_at: str = None,
    signup_ip: str = None,
) -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO users (email, password_hash, is_admin, trial_ends_at, signup_ip)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            email.lower().strip(), password_hash, is_admin,
            _parse_ts(trial_ends_at), signup_ip or None,
        )
        return row["id"]


async def get_user_by_id(user_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        return dict(row) if row else None


async def get_user_by_email(email: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE email = $1", email.lower().strip()
        )
        return dict(row) if row else None


async def get_users_count() -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")


async def get_user_by_supabase_uid(uid: str):
    if not uid:
        return None
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE supabase_uid = $1", uid)
        return dict(row) if row else None


async def link_supabase_uid(user_id: int, uid: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET supabase_uid = $1 WHERE id = $2", uid, user_id
        )


async def create_user_from_supabase(
    email: str,
    supabase_uid: str,
    is_admin: bool = False,
    trial_ends_at: str = None,
    signup_ip: str = None,
) -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO users (email, password_hash, is_admin, supabase_uid, trial_ends_at, signup_ip)
               VALUES ($1, '', $2, $3, $4, $5) RETURNING id""",
            email.lower().strip(), is_admin,
            supabase_uid or None, _parse_ts(trial_ends_at), signup_ip or None,
        )
        return row["id"]


async def count_signups_from_ip_last_24h(ip: str) -> int:
    if not ip or not ip.strip():
        return 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """SELECT COUNT(*) FROM users
               WHERE signup_ip = $1 AND created_at > NOW() - INTERVAL '1 day'""",
            ip.strip(),
        )


async def set_trial_ends_at(user_id: int, trial_ends_at: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET trial_ends_at = $1 WHERE id = $2",
            _parse_ts(trial_ends_at), user_id,
        )


async def mark_trial_used(user_id: int, at: str = None):
    ts = _parse_ts(at) or datetime.utcnow()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET trial_used_at = $1 WHERE id = $2", ts, user_id
        )


async def set_trial_used_on_subscription(user_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET trial_used_at = COALESCE(trial_used_at, $1) WHERE id = $2",
            datetime.utcnow(), user_id,
        )


# ── Billing helpers ───────────────────────────────────────────

async def update_user_billing(user_id: int, *,
                               is_premium: bool = None,
                               subscription_status: str = None):
    parts, values = [], []
    idx = 1
    if is_premium is not None:
        parts.append(f"is_premium = ${idx}"); values.append(is_premium); idx += 1
    if subscription_status is not None:
        parts.append(f"subscription_status = ${idx}"); values.append(subscription_status); idx += 1
    if not parts:
        return
    values.append(user_id)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE users SET {', '.join(parts)} WHERE id = ${idx}",
            *values,
        )


async def expire_user_trial(user_id: int):
    """Force-expire a user's trial (for admin debug testing)."""
    pool = await _get_pool()
    now = datetime.utcnow()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET trial_ends_at = $1, trial_used_at = $2 WHERE id = $3",
            now, now, user_id,
        )


# ── Subscriptions ─────────────────────────────────────────────

async def upsert_subscription(user_id: int, purchase_token: str,
                              product_id: str, status: str,
                              current_period_end: str,
                              platform: str = "google_play"):
    pool = await _get_pool()
    now = datetime.utcnow()
    period_end = _parse_ts(current_period_end)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM subscriptions WHERE purchase_token = $1",
            purchase_token,
        )
        if row:
            await conn.execute(
                """UPDATE subscriptions SET status = $1, current_period_end = $2,
                   updated_at = $3, product_id = $4, platform = $5
                   WHERE purchase_token = $6""",
                status, period_end, now, product_id, platform,
                purchase_token,
            )
        else:
            await conn.execute(
                """INSERT INTO subscriptions
                   (user_id, purchase_token, product_id, status,
                    current_period_end, platform, updated_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                user_id, purchase_token, product_id, status,
                period_end, platform, now,
            )


async def get_active_subscription(user_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM subscriptions
               WHERE user_id = $1 AND status IN ('active', 'trialing')
               AND (current_period_end IS NULL OR current_period_end > NOW())
               ORDER BY updated_at DESC LIMIT 1""",
            user_id,
        )
        return dict(row) if row else None


# ── Suggested Questions CRUD ──────────────────────────────────

async def get_active_suggested_questions():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, category, question FROM suggested_questions WHERE is_active = TRUE ORDER BY category, sort_order"
        )
        return [dict(r) for r in rows]


async def get_all_suggested_questions():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM suggested_questions ORDER BY category, sort_order"
        )
        return [dict(r) for r in rows]


async def create_suggested_question(category: str, question: str, sort_order: int = 0):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO suggested_questions (category, question, sort_order) VALUES ($1, $2, $3) RETURNING id",
            category, question, sort_order,
        )
        return row["id"]


async def update_suggested_question(qid: int, category: str = None, question: str = None,
                                     is_active: bool = None, sort_order: int = None):
    parts, values = [], []
    idx = 1
    if category is not None:
        parts.append(f"category = ${idx}"); values.append(category); idx += 1
    if question is not None:
        parts.append(f"question = ${idx}"); values.append(question); idx += 1
    if is_active is not None:
        parts.append(f"is_active = ${idx}"); values.append(is_active); idx += 1
    if sort_order is not None:
        parts.append(f"sort_order = ${idx}"); values.append(sort_order); idx += 1
    if not parts:
        return
    values.append(qid)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE suggested_questions SET {', '.join(parts)} WHERE id = ${idx}",
            *values,
        )


async def delete_suggested_question(qid: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM suggested_questions WHERE id = $1", qid)
