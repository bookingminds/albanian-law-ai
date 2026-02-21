-- Albanian Law AI – 50 Suggested Questions Seed
-- Run against SQLite database: sqlite3 data/law_ai.db < backend/seed_questions.sql

CREATE TABLE IF NOT EXISTS suggested_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    question TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(category, question)
);

-- Punësim (10)
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Punësim', 'Sa ditë pushim vjetor kam sipas ligjit në Shqipëri?', 1);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Punësim', 'A mund të më pushojë punëdhënësi pa paralajmërim?', 2);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Punësim', 'Sa është periudha e njoftimit për largim nga puna?', 3);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Punësim', 'Si paguhet puna jashtë orarit?', 4);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Punësim', 'A kam të drejtë për leje lindjeje dhe sa zgjat?', 5);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Punësim', 'A kam të drejtë për ditë pushimi mjekësore të paguara?', 6);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Punësim', 'Çfarë përfshin kontrata e punës sipas ligjit?', 7);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Punësim', 'A lejohet puna me dy kontrata në të njëjtën kohë?', 8);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Punësim', 'Si llogaritet paga minimale në Shqipëri?', 9);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Punësim', 'Çfarë ndodh nëse nuk më paguajnë rrogën në kohë?', 10);

-- Tatime & Biznes (10)
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Tatime & Biznes', 'Sa është tatimi mbi fitimin për bizneset e vogla në Shqipëri?', 1);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Tatime & Biznes', 'Si regjistrohet një biznes i ri?', 2);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Tatime & Biznes', 'Cilat janë detyrimet tatimore për një freelancer?', 3);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Tatime & Biznes', 'Çfarë është TVSH dhe kur duhet të regjistrohem për të?', 4);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Tatime & Biznes', 'Si deklarohet fitimi vjetor i biznesit?', 5);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Tatime & Biznes', 'Cilat janë gjobat për mosdeklarim tatimor?', 6);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Tatime & Biznes', 'Si mbyllet një biznes sipas ligjit?', 7);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Tatime & Biznes', 'Çfarë detyrimesh ka një person i vetëpunësuar?', 8);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Tatime & Biznes', 'A duhet të paguaj sigurime shoqërore si biznes?', 9);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Tatime & Biznes', 'Si bëhet ndryshimi i statusit të biznesit?', 10);

-- Familje (10)
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Familje', 'Si bëhet procedura e divorcit në Shqipëri?', 1);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Familje', 'Si ndahet pasuria pas divorcit?', 2);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Familje', 'Si përcaktohet kujdestaria e fëmijëve?', 3);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Familje', 'Sa është detyrimi për ushqim (alimentacion)?', 4);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Familje', 'Si bëhet njohja e atësisë?', 5);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Familje', 'A mund të ndryshoj mbiemrin pas martese?', 6);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Familje', 'Si bëhet birësimi i një fëmije?', 7);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Familje', 'Cilat janë të drejtat e bashkëshortëve në martesë?', 8);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Familje', 'Si bëhet ndarja e pasurisë së përbashkët?', 9);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Familje', 'A lejohet martesa me dy mbiemra në Shqipëri?', 10);

-- Pronë & Pasuri (10)
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Pronë & Pasuri', 'Si regjistrohet një pronë në Shqipëri?', 1);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Pronë & Pasuri', 'Çfarë dokumentesh duhen për shitje prone?', 2);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Pronë & Pasuri', 'Si bëhet kalimi i pronësisë së një apartamenti?', 3);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Pronë & Pasuri', 'Si zgjidhen konfliktet e pronësisë?', 4);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Pronë & Pasuri', 'Çfarë është hipoteka dhe si vendoset mbi një pronë?', 5);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Pronë & Pasuri', 'A mund të shitet një pronë pa certifikatë pronësie?', 6);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Pronë & Pasuri', 'Si bëhet kontrata e qirasë dhe çfarë përfshin?', 7);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Pronë & Pasuri', 'Çfarë të drejtash ka qiramarrësi sipas ligjit?', 8);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Pronë & Pasuri', 'Si llogaritet taksa e pronës?', 9);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Pronë & Pasuri', 'Si bëhet trashëgimia e një prone?', 10);

-- Penale (10)
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Penale', 'Çfarë konsiderohet vepër penale sipas ligjit shqiptar?', 1);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Penale', 'Cilat janë dënimet për mashtrim?', 2);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Penale', 'Si bëhet një kallëzim penal?', 3);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Penale', 'Çfarë të drejtash ka një person i arrestuar?', 4);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Penale', 'Sa zgjat paraburgimi sipas ligjit?', 5);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Penale', 'Çfarë është masa e sigurisë "arrest në shtëpi"?', 6);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Penale', 'Si bëhet mbrojtja nga një avokat?', 7);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Penale', 'Cilat janë dënimet për drejtim pa leje drejtimi?', 8);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Penale', 'Çfarë ndodh në rast dhune në familje?', 9);
INSERT OR IGNORE INTO suggested_questions (category, question, sort_order) VALUES ('Penale', 'Si bëhet ankimi ndaj një vendimi penal?', 10);
