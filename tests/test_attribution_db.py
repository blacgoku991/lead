import csv
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from autolead.db import DB, SCHEMA
from autolead.export import export_csv, export_phones_csv
from autolead.utils import site_from_url


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "leads.db")

    def tearDown(self):
        self.tmp.cleanup()

    def read_export(self, db, **kwargs):
        target = os.path.join(self.tmp.name, "out.csv")
        export_csv(db, target, **kwargs)
        with open(target, encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh, delimiter=";"))

    def test_legacy_network_export_never_chooses_first_business(self):
        with sqlite3.connect(self.path) as conn:
            conn.executescript(SCHEMA)
            conn.executemany("INSERT INTO businesses (source,source_id,name,website,site_key,siren) "
                             "VALUES ('sirene',?,?,?,'motrio.fr','123456789')", [
                                 ("12345678900012", "Garage Un", "https://motrio.fr/garage/un"),
                                 ("12345678900020", "Garage Deux", "https://motrio.fr/garage/deux")])
            conn.execute("INSERT INTO sites (site_key,url,status) VALUES ('motrio.fr','https://motrio.fr/','ok')")
            conn.execute("INSERT INTO emails (email,domain,site_key,kind,source_url) VALUES "
                         "('atelier@garage-un.fr','garage-un.fr','motrio.fr','pro','https://motrio.fr/garage/un')")
        db = DB(self.path)
        try:
            rows = self.read_export(db)
            self.assertEqual(rows[0]["entreprise"], "")
            self.assertEqual(rows[0]["etat_rattachement"], "unverified")
            self.assertEqual(self.read_export(db, attributed_only=True), [])
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM businesses WHERE length(siret)=14").fetchone()[0], 2)
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0], 1)
        finally:
            db.close()
        backups = list(Path(self.tmp.name).glob("*.before-v2-*.db"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as old:
            self.assertEqual(old.execute("SELECT COUNT(*) FROM emails").fetchone()[0], 1)
            self.assertNotIn("siret", [r[1] for r in old.execute("PRAGMA table_info(businesses)")])
        DB(self.path).close()
        self.assertEqual(len(list(Path(self.tmp.name).glob("*.before-v2-*.db"))), 1)

    def test_structured_network_contacts_and_head_office_are_separate(self):
        db = DB(self.path)
        try:
            entities = []
            for i, name, cp in [(1, "Garage Un", "69003"), (2, "Garage Deux", "75015")]:
                url = f"https://motrio.fr/garage/{i}"
                db.add_businesses([{"source": "sirene", "source_id": str(i), "name": name,
                                    "address": f"{i} RUE DES ATELIERS", "postal_code": cp,
                                    "city": "Lyon" if i == 1 else "Paris", "phone": f"014040400{i}",
                                    "category": "garage", "website": url}])
                entities.append({"source_id": url, "source_url": url, "website": url,
                                 "source_type": "jsonld", "name": name, "address": f"{i} RUE DES ATELIERS",
                                 "postal_code": cp, "city": "Lyon" if i == 1 else "Paris",
                                 "emails": [f"atelier@garage-{i}.fr"], "category": "garage"})
            emails = {e["emails"][0]: e["source_url"] for e in entities}
            emails["siege@reseau-auto.fr"] = "https://motrio.fr/mentions"
            for _ in range(2):
                db.save_crawl("motrio.fr", "ok", "https://motrio.fr/", 3, emails, {"motrio.fr"},
                              {"01 99 99 99 99"}, entities=entities, is_network=True)
            rows = self.read_export(db, dedupe=False)
            local = {r["email"]: r for r in rows}
            self.assertEqual(local["atelier@garage-1.fr"]["entreprise"], "Garage Un")
            self.assertEqual(local["atelier@garage-2.fr"]["entreprise"], "Garage Deux")
            self.assertEqual(local["siege@reseau-auto.fr"]["entreprise"], "")
            self.assertEqual(local["atelier@garage-1.fr"]["telephone_site"], "")
            self.assertEqual(len(self.read_export(db, attributed_only=True)), 2)
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0], 2)
            phones_path = os.path.join(self.tmp.name, "phones.csv")
            export_phones_csv(db, phones_path)
            with open(phones_path, encoding="utf-8-sig") as fh:
                phones = list(csv.DictReader(fh, delimiter=";"))
            self.assertTrue(all(not r["telephone_site"] for r in phones))
            self.assertEqual({r["emails"] for r in phones}, {"atelier@garage-1.fr", "atelier@garage-2.fr"})
        finally:
            db.close()

    def test_shared_email_preserves_each_explicit_establishment(self):
        db = DB(self.path)
        try:
            db.add_businesses([{"source": "osm", "source_id": str(i), "name": f"Garage {i}",
                                "category": "garage", "website": f"https://motrio.fr/garage/{i}",
                                "emails": ["accueil@garage-groupe.fr"]} for i in (1, 2)])
            rows = self.read_export(db, dedupe=False)
            self.assertEqual({r["entreprise"] for r in rows}, {"Garage 1", "Garage 2"})
            self.assertEqual({r["nombre_etablissements"] for r in rows}, {"2"})
            self.assertEqual(len(self.read_export(db)), 1)
        finally:
            db.close()

    def test_public_import_fills_missing_values_and_preserves_local_urls(self):
        db = DB(self.path)
        try:
            db.add_businesses([{"source": "sirene", "source_id": "12345678900012", "name": "Nom existant",
                                "address": "Adresse vérifiée", "phone": "0144444444", "category": "controle_technique"}])
            item = {"source": "dgccrf", "source_id": "12345678900012", "siret": "12345678900012",
                    "name": "Autre libellé", "address": "Adresse plus ancienne", "phone": "0155555555",
                    "website": "https://reseau-auto.fr/centre?id=123", "postal_code": "92270",
                    "source_url": "https://data.economie.gouv.fr/source", "source_license": "etalab-2.0"}
            self.assertEqual(db.upsert_public_businesses([item]), 0)
            self.assertEqual(db.upsert_public_businesses([item]), 0)
            row = db.conn.execute("SELECT * FROM businesses").fetchone()
            self.assertEqual((row["name"], row["address"], row["phone"]), ("Nom existant", "Adresse vérifiée", "0144444444"))
            self.assertEqual(row["postal_code"], "92270")
            self.assertEqual(row["website"], item["website"])
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM business_pages").fetchone()[0], 1)
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM business_sources").fetchone()[0], 2)
        finally:
            db.close()

    def test_repair_revokes_legacy_guess_without_deleting_emails(self):
        db = DB(self.path)
        try:
            db.add_businesses([{"source": "sirene", "source_id": "12345678900012", "name": "Garage Ancien",
                                "postal_code": "69003", "website": "https://garage-ancien.fr/"}])
            bid = db.conn.execute("SELECT id FROM businesses").fetchone()[0]
            db.add_guesses({"garage-ancien.fr": [(bid, ["69003"])]})
            db.link_guessed("garage-ancien.fr", "https://garage-ancien.fr/", [bid])
            db.save_crawl("garage-ancien.fr", "ok", "https://garage-ancien.fr/", 1,
                          {"contact@garage-ancien.fr": "https://garage-ancien.fr/contact"}, {"garage-ancien.fr"})
            self.assertEqual(self.read_export(db)[0]["etat_rattachement"], "unverified")
            result = db.repair_contacts()
            self.assertEqual(result["legacy_guesses_reset"], 1)
            self.assertTrue(Path(result["backup"]).exists())
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0], 1)
            self.assertIsNone(db.conn.execute("SELECT site_key FROM businesses").fetchone()[0])
            self.assertEqual(db.repair_contacts()["legacy_guesses_reset"], 0)
        finally:
            db.close()

    def test_query_identifies_two_different_local_pages(self):
        one = site_from_url("https://reseau-auto.fr/centre?id=1")
        two = site_from_url("https://reseau-auto.fr/centre?id=2")
        self.assertEqual(one[0], two[0])
        self.assertNotEqual(one[1], two[1])

    def test_structured_page_matches_sirene_without_known_website(self):
        db = DB(self.path)
        try:
            db.add_businesses([{"source": "sirene", "source_id": "12345678900012",
                                "name": "Garage Martin", "address": "12 RUE DES ATELIERS",
                                "postal_code": "69003", "city": "Lyon", "category": "garage"}])
            entity = {"name": "Garage Martin", "address": "12 RUE DES ATELIERS", "postal_code": "69003",
                      "city": "Lyon", "category": "garage", "source_type": "jsonld",
                      "website": "https://motrio.fr/garage/martin", "source_url": "https://motrio.fr/garage/martin",
                      "emails": ["atelier@garage-martin.fr"]}
            self.assertEqual(db.save_crawl("motrio.fr", "ok", "https://motrio.fr/", 1,
                              {"atelier@garage-martin.fr": entity["source_url"]}, {"motrio.fr"},
                              entities=[entity], is_network=True), 1)
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0], 1)
            row = self.read_export(db, attributed_only=True)[0]
            self.assertEqual((row["source"], row["siret"], row["etat_rattachement"]),
                             ("sirene", "12345678900012", "structured"))
        finally:
            db.close()

    def test_conflicting_exact_siret_never_creates_a_new_establishment(self):
        for mismatch in ({"address": "99 RUE DIFFERENTE"}, {"postal_code": "75015"}):
            with self.subTest(mismatch=mismatch):
                db = DB(":memory:")
                try:
                    db.add_businesses([{"source": "sirene", "source_id": "12345678900012",
                                        "name": "Garage Martin", "address": "12 RUE DES ATELIERS",
                                        "postal_code": "69003", "category": "garage"}])
                    entity = {"name": "Garage Martin", "siret": "12345678900012", "category": "garage",
                              "address": "12 RUE DES ATELIERS", "postal_code": "69003",
                              "website": "https://motrio.fr/garage/martin",
                              "source_url": "https://motrio.fr/garage/martin",
                              "emails": ["atelier@garage-martin.fr"], **mismatch}
                    db.save_crawl("motrio.fr", "ok", "https://motrio.fr/", 1,
                                  {"atelier@garage-martin.fr": entity["source_url"]}, {"motrio.fr"},
                                  entities=[entity], is_network=True)
                    self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0], 1)
                    self.assertEqual(self.read_export(db, attributed_only=True), [])
                    self.assertEqual(self.read_export(db)[0]["etat_rattachement"], "unverified")
                finally:
                    db.close()

    def test_exact_siret_takes_priority_over_a_similar_unidentified_business(self):
        db = DB(self.path)
        try:
            common = {"name": "Garage Martin", "address": "12 RUE DES ATELIERS", "postal_code": "69003",
                      "category": "garage", "website": "https://motrio.fr/garage/martin"}
            db.add_businesses([{**common, "source": "sirene", "source_id": "12345678900012"},
                               {**common, "source": "osm", "source_id": "node/42"}])
            entity = {**common, "siret": "12345678900012", "source_url": common["website"],
                      "emails": ["atelier@garage-martin.fr"]}
            db.save_crawl("motrio.fr", "ok", "https://motrio.fr/", 1, {}, {"motrio.fr"},
                          entities=[entity], is_network=True)
            rows = self.read_export(db, attributed_only=True, dedupe=False)
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["source"], rows[0]["siret"]), ("sirene", "12345678900012"))
        finally:
            db.close()

    def test_network_observation_survives_reopening_with_only_one_known_garage(self):
        db = DB(self.path)
        db.add_businesses([{"source": "osm", "source_id": "1", "name": "Garage Alpha",
                            "website": "https://reseau-regional.fr/garage/alpha"}])
        db.save_crawl("reseau-regional.fr", "partial", "https://reseau-regional.fr/", 1,
                      {"atelier@garage-beta.fr": "https://reseau-regional.fr/garage/beta"},
                      {"reseau-regional.fr"}, is_network=True)
        db.close()
        db = DB(self.path)
        try:
            self.assertIn("reseau-regional.fr", db.network_sites())
            self.assertEqual(self.read_export(db)[0]["entreprise"], "")
            self.assertEqual(self.read_export(db, attributed_only=True), [])
        finally:
            db.close()

    def test_new_source_page_requeues_failed_site_without_forgetting_completed_pages(self):
        from autolead.frontier import Frontier
        db = DB(self.path)
        try:
            db.add_businesses([{"source": "sirene", "source_id": "12345678900012", "name": "Centre Martin",
                                "website": "https://centres-auto.fr/ancienne"}])
            frontier = Frontier(db.conn, "centres-auto.fr")
            frontier.enqueue(["https://centres-auto.fr/ancienne"])
            frontier.complete(["https://centres-auto.fr/ancienne"])
            db.conn.execute("UPDATE sites SET status='unreachable'")
            db.upsert_public_businesses([{"source": "dgccrf", "source_id": "12345678900012",
                                         "siret": "12345678900012", "name": "Centre Martin",
                                         "website": "https://centres-auto.fr/centre?id=42"}])
            self.assertEqual([r["site_key"] for r in db.pending_sites()], ["centres-auto.fr"])
            self.assertEqual(frontier.counts()["done"], 1)
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM business_pages").fetchone()[0], 2)
        finally:
            db.close()

    def test_retry_failed_pages_keeps_successful_pages_complete(self):
        from autolead.frontier import Frontier
        db = DB(self.path)
        try:
            db.add_businesses([{"source": "osm", "source_id": "1", "name": "Garage Martin",
                                "website": "https://motrio.fr/garage/martin"}])
            frontier = Frontier(db.conn, "motrio.fr")
            good, bad = "https://motrio.fr/garage/martin", "https://motrio.fr/garage/autre"
            frontier.enqueue([good, bad])
            frontier.complete([good])
            frontier.fail([bad], max_attempts=1)
            db.conn.execute("UPDATE sites SET status='partial'")
            self.assertEqual(db.reset_failed_sites(), 1)
            self.assertEqual(frontier.counts()["done"], 1)
            self.assertEqual(frontier.counts()["failed"], 0)
            self.assertEqual([r["url"] for r in frontier.pending()], [bad])
        finally:
            db.close()

    def test_reimports_refresh_provenance_without_duplicate_candidates(self):
        db = DB(self.path)
        try:
            source = {"source": "dgccrf", "source_id": "12345678900012", "siret": "12345678900012",
                      "name": "Centre Martin", "source_url": "https://data.economie.gouv.fr/source",
                      "source_date": "2024-09", "source_license": "licence-initiale"}
            db.upsert_public_businesses([source])
            db.upsert_public_businesses([{**source, "source_date": "2026-09", "source_license": "etalab-2.0"}])
            bid = db.conn.execute("SELECT id FROM businesses").fetchone()[0]
            candidate = {"business_id": bid, "siret": source["siret"], "domain": "centre-martin.fr",
                         "data_source": "dinum", "source_url": "https://www.data.gouv.fr/source",
                         "source_date": "2024-09", "source_license": "licence-initiale"}
            self.assertEqual(db.add_domain_candidates([candidate]), 1)
            self.assertEqual(db.add_domain_candidates([{**candidate, "source_date": "2026-09",
                                                       "source_license": "etalab-2.0"}]), 0)
            for table in ("business_sources", "domain_candidates"):
                row = db.conn.execute(f"SELECT source_date,source_license FROM {table}").fetchone()
                self.assertEqual(tuple(row), ("2026-09", "etalab-2.0"))
            guess = db.conn.execute("SELECT verified,verification_reason FROM guesses").fetchone()
            self.assertEqual(tuple(guess), (0, ""))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
