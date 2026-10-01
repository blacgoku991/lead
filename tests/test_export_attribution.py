"""Régressions d'identité et de provenance des exports, sans accès au réseau."""

import csv
import tempfile
import unittest
from pathlib import Path

from autolead.db import DB
from autolead.export import _context, export_csv, export_phones_csv


class ExportAttributionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(":memory:")
        self.addCleanup(self.db.close)
        self.next_source_id = 0

    def add_business(self, *, source="osm", source_id=None, **fields):
        self.next_source_id += 1
        source_id = source_id or str(self.next_source_id)
        item = {"source": source, "source_id": source_id, "category": "garage", **fields}
        self.db.add_businesses([item])
        return self.db.conn.execute(
            "SELECT id FROM businesses WHERE source=? AND source_id=?", (source, source_id)
        ).fetchone()[0]

    def crawl(self, site_key, emails, *, entities=(), phones=()):
        self.db.save_crawl(
            site_key, "ok", f"https://{site_key}/", 1, emails, {site_key}, set(phones),
            entities=list(entities),
        )

    def read_emails(self, **kwargs):
        target = Path(self.tmp.name) / "emails.csv"
        export_csv(self.db, str(target), **kwargs)
        with target.open(encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh, delimiter=";"))

    def read_phones(self):
        target = Path(self.tmp.name) / "phones.csv"
        export_phones_csv(self.db, str(target))
        with target.open(encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh, delimiter=";"))

    def seed_explicit_alpha_contact(self, email):
        page = "https://garage-alpha.fr/contact"
        self.add_business(
            name="Garage Alpha", siret="11111111100011", website="https://garage-alpha.fr/",
        )
        self.crawl("garage-alpha.fr", {email: page}, entities=[{
            "name": "Garage Alpha", "siret": "11111111100011", "category": "garage",
            "website": "https://garage-alpha.fr/", "source_url": page,
            "source_type": "jsonld", "source_id": "alpha", "emails": [email],
        }])
        return page

    def test_partner_contact_is_not_assigned_to_host_in_email_or_phone_export(self):
        self.add_business(
            name="Garage Alpha", siret="11111111100011", website="https://garage-alpha.fr/",
            phone="0144444444",
        )
        self.add_business(
            name="Garage Beta", siret="22222222200022", website="https://garage-beta.fr/",
            phone="0155555555",
        )
        email = "atelier@garage-beta.fr"
        page = "https://garage-alpha.fr/partenaires"
        self.crawl("garage-alpha.fr", {email: page}, phones={"01 44 44 44 45"}, entities=[{
            "name": "Garage Beta", "siret": "22222222200022", "category": "garage",
            "website": "https://garage-beta.fr/", "source_url": page,
            "source_type": "jsonld", "source_id": "beta", "emails": [email],
        }])

        rows = self.read_emails(dedupe=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entreprise"], "Garage Beta")
        self.assertEqual(rows[0]["etat_rattachement"], "structured")
        self.assertEqual(rows[0]["nombre_etablissements"], "1")
        self.assertEqual(rows[0]["page_source"], page)
        self.assertEqual(rows[0]["telephone_site"], "")
        phones = {row["entreprise"]: row for row in self.read_phones()}
        self.assertEqual(phones["Garage Alpha"]["emails"], "")
        self.assertEqual(phones["Garage Beta"]["emails"], email)
        self.assertEqual(phones["Garage Beta"]["telephone_site"], "")

    def test_same_siret_in_two_sources_remains_one_attributed_establishment(self):
        siret = "12345678900012"
        for source, source_id in (("sirene", siret), ("osm", "node/123")):
            self.add_business(
                source=source, source_id=source_id, siret=siret, name="Garage Martin",
                website="https://garage-martin.fr/",
            )
        self.crawl("garage-martin.fr", {
            "contact@garage-martin.fr": "https://garage-martin.fr/contact",
        })

        rows = self.read_emails(dedupe=False, attributed_only=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["siret"], siret)
        self.assertEqual(rows[0]["entreprise"], "Garage Martin")
        self.assertEqual(rows[0]["etat_rattachement"], "site_unique")
        self.assertEqual(rows[0]["nombre_etablissements"], "1")

    def test_email_context_ignores_unrelated_businesses_but_phone_export_keeps_phone_only(self):
        direct = self.add_business(
            name="Garage Contact Source", emails=["contact@garage-source.fr"],
            source_url="https://annuaire.fr/garage-source",
        )
        on_site = self.add_business(
            name="Garage Contact Site", website="https://garage-contact.fr/",
        )
        self.crawl("garage-contact.fr", {
            "contact@garage-contact.fr": "https://garage-contact.fr/contact",
        })
        phone_only = self.add_business(name="Garage Telephone Fiche", phone="0166666666")
        site_phone_only = self.add_business(
            name="Garage Telephone Site", website="https://garage-telephone.fr/",
        )
        self.crawl("garage-telephone.fr", {}, phones={"01 77 77 77 77"})
        for i in range(20):
            self.add_business(
                name=f"Garage Sans Contact {i}", website=f"https://sans-contact-{i}.fr/",
            )

        by_id, _, _ = _context(self.db)
        self.assertEqual(set(by_id), {direct, on_site})
        self.assertNotIn(phone_only, by_id)
        self.assertNotIn(site_phone_only, by_id)
        phones = {row["entreprise"]: row for row in self.read_phones()}
        self.assertEqual(set(phones), {"Garage Telephone Fiche", "Garage Telephone Site"})
        self.assertEqual(phones["Garage Telephone Fiche"]["telephone"], "0166666666")
        self.assertEqual(phones["Garage Telephone Site"]["telephone_site"], "01 77 77 77 77")
        self.assertTrue(all(row["emails"] == "" for row in phones.values()))

    def test_explicit_contact_on_one_site_does_not_hide_uncertain_publication_on_another(self):
        email = "accueil@groupe-automobile.fr"
        alpha_page = self.seed_explicit_alpha_contact(email)
        beta_page = "https://partenaire-inconnu.fr/contact"
        gamma_page = "https://autre-partenaire.fr/contact"
        for site, page in (("partenaire-inconnu.fr", beta_page), ("autre-partenaire.fr", gamma_page)):
            self.add_business(source="lien", name="", website=f"https://{site}/")
            self.crawl(site, {email: page})

        rows = self.read_emails(dedupe=False)
        self.assertEqual(len(rows), 3)
        by_page = {row["page_source"]: row for row in rows}
        self.assertEqual(set(by_page), {alpha_page, beta_page, gamma_page})
        self.assertEqual(by_page[alpha_page]["entreprise"], "Garage Alpha")
        self.assertEqual(by_page[alpha_page]["etat_rattachement"], "structured")
        for page in (beta_page, gamma_page):
            self.assertEqual(by_page[page]["entreprise"], "")
            self.assertEqual(by_page[page]["etat_rattachement"], "unverified")
        attributed = self.read_emails(dedupe=False, attributed_only=True)
        self.assertEqual([row["page_source"] for row in attributed], [alpha_page])
        self.assertEqual(len(self.read_emails()), 1)

    def test_shared_email_on_independent_sites_keeps_each_legitimate_association(self):
        email = "accueil@groupe-automobile.fr"
        alpha_page = self.seed_explicit_alpha_contact(email)
        beta_page = "https://garage-beta.fr/contact"
        self.add_business(
            name="Garage Beta", siret="22222222200022", website="https://garage-beta.fr/",
        )
        self.crawl("garage-beta.fr", {email: beta_page})

        rows = self.read_emails(dedupe=False, attributed_only=True)
        self.assertEqual(len(rows), 2)
        by_name = {row["entreprise"]: row for row in rows}
        self.assertEqual(set(by_name), {"Garage Alpha", "Garage Beta"})
        self.assertEqual(by_name["Garage Alpha"]["etat_rattachement"], "structured")
        self.assertEqual(by_name["Garage Alpha"]["page_source"], alpha_page)
        self.assertEqual(by_name["Garage Beta"]["etat_rattachement"], "site_unique")
        self.assertEqual(by_name["Garage Beta"]["page_source"], beta_page)
        self.assertEqual({row["nombre_etablissements"] for row in rows}, {"2"})


if __name__ == "__main__":
    unittest.main()
