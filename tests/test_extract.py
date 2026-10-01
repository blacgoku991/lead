import unittest

from autolead.config import refine_category
from autolead.crawler import verify_tokens
from autolead.extract import clean_email, discover_links, email_kind, extract_emails, is_role
from autolead.guess import candidate_domains
from autolead.sources.gmaps import place_to_business
from autolead.sources.osm import element_to_business, selector_index
from autolead.sources.sirene import result_to_businesses
from autolead.utils import registrable, site_from_url


def cf_encode(email: str, key: int = 0x42) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


class ExtractTests(unittest.TestCase):
    def test_plain_and_mailto(self):
        html = """<p>Écrivez-nous : <a href="mailto:Contact%40Garage-Dupont.fr?subject=Devis">ici</a></p>
                  <footer>atelier@garage-dupont.fr.</footer>"""
        self.assertEqual(extract_emails(html), {"contact@garage-dupont.fr", "atelier@garage-dupont.fr"})

    def test_entities_and_unicode_escapes(self):
        html = 'info&#64;auto-ecole.fr <script>var m="vente\\u0040concession.com";</script>'
        self.assertEqual(extract_emails(html), {"info@auto-ecole.fr", "vente@concession.com"})

    def test_obfuscated(self):
        html = "contact [at] carrosserie-martin [dot] fr — rdv(at)pneus.fr"
        self.assertEqual(extract_emails(html), {"contact@carrosserie-martin.fr", "rdv@pneus.fr"})

    def test_cloudflare(self):
        html = f'<a href="/cdn-cgi/l/email-protection#{cf_encode("sav@moto.fr")}">x</a>' \
               f'<span data-cfemail="{cf_encode("info@ct-auto.fr", 0x1f)}">[email protected]</span>'
        self.assertEqual(extract_emails(html), {"sav@moto.fr", "info@ct-auto.fr"})

    def test_false_positives_filtered(self):
        html = """<img src="logo@2x.png"> votre@email.com nom@domaine.fr
                  @media screen {} 7f4a9c2b1e8d4f6a9b3c5d7e@sentry.wixpress.com
                  noreply@garage.fr user@example.com"""
        self.assertEqual(extract_emails(html), set())

    def test_newline_before_at_is_not_glued(self):
        self.assertEqual(extract_emails("Contact\n@garage.fr"), set())

    def test_clean_email(self):
        self.assertEqual(clean_email(" MAILTO:Info@Garage.FR. "), "info@garage.fr")
        self.assertIsNone(clean_email("a..b@garage.fr"))
        self.assertIsNone(clean_email("contact@garage"))

    def test_kind_and_role(self):
        self.assertEqual(email_kind("contact@garage-dupont.fr", {"garage-dupont.fr"}), "domaine_site")
        self.assertEqual(email_kind("contact@autre-domaine.fr", {"garage-dupont.fr"}), "pro")
        self.assertEqual(email_kind("garage.dupont@orange.fr", {"garage-dupont.fr"}), "gratuit")
        self.assertTrue(is_role("contact@x.fr"))
        self.assertFalse(is_role("jean.martin@x.fr"))

    def test_discover_links(self):
        html = """<a href="/nos-services">Services</a>
                  <a href="/contactez-nous">Nous écrire</a>
                  <a href='https://www.garage.fr/mentions-legales'>Mentions</a>
                  <a href="/page-7"><span>À propos</span></a>
                  <a href="https://facebook.com/contact">FB</a>
                  <a href="/plaquette-contact.pdf">PDF</a>"""
        links = discover_links(html, "https://garage.fr/")
        self.assertEqual(links, ["https://garage.fr/contactez-nous", "https://www.garage.fr/mentions-legales",
                                 "https://garage.fr/page-7"])


class DomainTests(unittest.TestCase):
    def test_registrable(self):
        self.assertEqual(registrable("www.garage-dupont.fr"), "garage-dupont.fr")
        self.assertEqual(registrable("mail.garage.co.uk"), "garage.co.uk")
        self.assertEqual(registrable("127.0.0.1"), "127.0.0.1")

    def test_site_from_url(self):
        self.assertEqual(site_from_url("www.Garage.fr"), ("garage.fr", "https://www.Garage.fr/"))
        self.assertEqual(site_from_url("http://garage.fr/accueil; http://autre.fr"),
                         ("garage.fr", "http://garage.fr/accueil"))
        self.assertEqual(site_from_url("https://www.facebook.com/garage"), (None, None))
        self.assertEqual(site_from_url("https://sites.google.com/view/garage-x/home")[0],
                         "sites.google.com/view/garage-x")

    def test_candidate_domains(self):
        self.assertEqual(set(candidate_domains("SARL GARAGE DUPONT (DUPONT JEAN)")),
                         {"garage-dupont.com", "garage-dupont.fr", "garagedupont.com", "garagedupont.fr"})
        self.assertEqual(candidate_domains("GARAGE DU CENTRE"), [])
        self.assertEqual(set(candidate_domains("Éts Rénov'Auto")),
                         {"renov-auto.com", "renov-auto.fr", "renovauto.com", "renovauto.fr"})

    def test_verify_tokens(self):
        page = "Garage Dupont, 12 rue X, 69003 Lyon — SIREN 123 456 789"
        self.assertTrue(verify_tokens(page, ["123456789"]))
        self.assertTrue(verify_tokens(page, ["69003"]))
        self.assertFalse(verify_tokens(page, ["75015", "987654321"]))
        self.assertFalse(verify_tokens("tel 0169003123", ["69003"]))


class SourceParsingTests(unittest.TestCase):
    def test_osm_element(self):
        index = selector_index(["garage", "lavage"])
        el = {"type": "node", "id": 42, "lat": 45.7, "lon": 4.8,
              "tags": {"shop": "car_repair", "name": "Carrosserie Martin", "website": "carrosserie-martin.fr",
                       "email": "contact@carrosserie-martin.fr", "addr:postcode": "69003",
                       "addr:city": "Lyon", "ref:FR:SIRET": "123 456 789 00012"}}
        b = element_to_business(el, index)
        self.assertEqual(b["category"], "carrosserie")
        self.assertEqual(b["siren"], "123456789")
        self.assertEqual(b["emails"], ["contact@carrosserie-martin.fr"])
        self.assertIsNone(element_to_business({"type": "node", "id": 1, "tags": {"shop": "bakery"}}, index))

    def test_sirene_result(self):
        r = {"siren": "123456789", "nom_complet": "DUPONT JEAN", "activite_principale": "45.20A",
             "matching_etablissements": [
                 {"siret": "12345678900012", "etat_administratif": "A", "liste_enseignes": ["GARAGE DUPONT"],
                  "adresse": "1 RUE X 75015 PARIS", "code_postal": "75015", "libelle_commune": "PARIS",
                  "latitude": "48.84", "longitude": "2.29"},
                 {"siret": "12345678900020", "etat_administratif": "F"}]}
        out = result_to_businesses(r, "garage", "45.20A")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "GARAGE DUPONT")
        self.assertEqual(out[0]["alt_name"], "DUPONT JEAN")
        self.assertEqual(out[0]["lat"], 48.84)
        self.assertEqual(result_to_businesses({"nom_complet": "[NON-DIFFUSIBLE]"}, "garage", "45.20A"), [])

    def test_gmaps_place(self):
        p = {"id": "abc", "displayName": {"text": "Carrosserie Martin"}, "websiteUri": "https://carrosserie-martin.fr/",
             "nationalPhoneNumber": "04 78 00 00 00", "formattedAddress": "1 Rue X, 69003 Lyon",
             "addressComponents": [{"longText": "69003", "types": ["postal_code"]},
                                   {"longText": "Lyon", "types": ["locality", "political"]}],
             "location": {"latitude": 45.7, "longitude": 4.8}}
        b = place_to_business(p, "garage")
        self.assertEqual((b["category"], b["postal_code"], b["city"]), ("carrosserie", "69003", "Lyon"))
        self.assertIsNone(place_to_business({**p, "businessStatus": "CLOSED_PERMANENTLY"}, "garage"))

    def test_refine_category(self):
        self.assertEqual(refine_category("garage", "Pare-Brise Express"), "vitrage")
        self.assertEqual(refine_category("garage", "Garage Martin"), "garage")
        self.assertEqual(refine_category("auto_ecole", "Auto-école du pneu"), "auto_ecole")


if __name__ == "__main__":
    unittest.main()
