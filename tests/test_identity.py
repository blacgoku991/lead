"""Régressions sur les coordonnées locales et les rapprochements d'identité."""
import base64
import json
import sqlite3
import unittest
from urllib.parse import quote

from autolead.identity import extract_entities, match_identity


def ld(value):
    return '<script type="application/ld+json">' + json.dumps(value) + '</script>'


def garage(name="Garage Dupont", email="atelier@garage-dupont.fr", **changes):
    return {
        "@type": "AutoRepair", "name": name, "email": email,
        "url": "https://reseau-auto.fr/garages/dupont",
        "address": {"@type": "PostalAddress", "streetAddress": "12 avenue de la République",
                    "postalCode": "69003", "addressLocality": "Lyon"},
        "telephone": "+33 4 78 00 00 01", **changes,
    }


class EntityTests(unittest.TestCase):
    def test_list_jsonld_keeps_neighbours_and_head_office_separate(self):
        first = garage()
        second = garage("Carrosserie Martin", "garage.martin@gmail.com", **{
            "@type": "AutoBodyShop", "url": "https://reseau-auto.fr/garages/martin",
            "address": {"streetAddress": "9 rue des Lilas", "postalCode": "75015", "addressLocality": "Paris"},
        })
        first["parentOrganization"] = {"@type": "Organization", "name": "Siège du réseau", "email": "siege@reseau-auto.fr"}
        page = ld([first, {"@type": "BreadcrumbList", "itemListElement": []}, second])
        page += '<script id="__NEXT_DATA__">{"neighbours": ["autre@garage-ailleurs.fr"]}</script>'
        entities = {e["name"]: e for e in extract_entities(page, first["url"])}
        self.assertEqual(set(entities), {"Garage Dupont", "Carrosserie Martin"})
        self.assertEqual(entities["Garage Dupont"]["emails"], ["atelier@garage-dupont.fr"])
        self.assertEqual(entities["Carrosserie Martin"]["emails"], ["garage.martin@gmail.com"])
        self.assertEqual(entities["Carrosserie Martin"]["website"], second["url"])
        self.assertEqual(entities["Carrosserie Martin"]["category"], "carrosserie")
        self.assertEqual(entities["Garage Dupont"]["source_url"], first["url"])
        self.assertEqual(entities["Garage Dupont"]["phone"], "04 78 00 00 01")

    def test_graph_references_and_multiple_types(self):
        page = ld({"@graph": [
            {"@id": "#garage", "@type": ["Organization", "https://schema.org/AutoRepair"],
             "name": "Garage Dupont", "address": {"@id": "#adresse"},
             "contactPoint": {"@id": "#contact"},
             "identifier": [{"@type": "PropertyValue", "propertyID": "SIRET", "value": "123 456 789 00012"}]},
            {"@id": "#adresse", "@type": "PostalAddress", "streetAddress": "12 rue des Lilas",
             "postalCode": "69003", "addressLocality": "Lyon"},
            {"@id": "#contact", "@type": "ContactPoint", "email": ["devis@garage-dupont.fr", "atelier@garage-dupont.fr"],
             "telephone": "04 78 00 00 01"},
        ]})
        entities = extract_entities(page, "https://garage-dupont.fr/contact")
        self.assertEqual(len(entities), 1)
        entity = entities[0]
        self.assertEqual(entity["siret"], "12345678900012")
        self.assertEqual(entity["siren"], "123456789")
        self.assertEqual(entity["source_id"], "https://garage-dupont.fr/contact#garage")
        self.assertEqual(entity["postal_code"], "69003")
        self.assertEqual(set(entity["emails"]), {"atelier@garage-dupont.fr", "devis@garage-dupont.fr"})

    def test_neighbour_canonical_id_supplies_its_own_page(self):
        node = garage("Garage Martin", "contact@garage-martin.fr", **{
            "@id": "https://reseau-auto.fr/garages/martin#garage", "url": ""})
        entity = extract_entities(ld(node), "https://reseau-auto.fr/garages/dupont")[0]
        self.assertEqual(entity["website"], "https://reseau-auto.fr/garages/martin")

    def test_stable_ids_do_not_collapse_branches_with_shared_homepage(self):
        a = garage(url="https://reseau-auto.fr/")
        b = garage("Garage Martin", "contact@garage-martin.fr", url="https://reseau-auto.fr/",
                   address={"streetAddress": "9 rue des Lilas", "postalCode": "75015", "addressLocality": "Paris"})
        first = {e["name"]: e for e in extract_entities(ld([a, b]), "https://reseau-auto.fr/liste")}
        a["email"] = "contact@garage-dupont.fr"
        second = {e["name"]: e for e in extract_entities(ld([b, a]), "https://reseau-auto.fr/liste")}
        self.assertNotEqual(first["Garage Dupont"]["source_id"], first["Garage Martin"]["source_id"])
        self.assertEqual(first["Garage Dupont"]["source_id"], second["Garage Dupont"]["source_id"])

    def test_broken_reused_id_still_keeps_different_branches_distinct(self):
        first = garage(**{"@id": "#localbusiness"})
        other = garage("Garage Martin", "contact@garage-martin.fr", **{
            "@id": "#localbusiness", "address": {"streetAddress": "99 rue des Lilas", "postalCode": "75015"}})
        entities = extract_entities(ld([first, other]), "https://reseau-auto.fr/liste")
        self.assertEqual(len({e["source_id"] for e in entities}), 2)

    def test_malformed_and_application_json_cannot_supply_an_entity(self):
        page = '<script type="application/ld+json">{broken}</script>'
        page += '<script type="application/json">' + json.dumps(garage()) + '</script>'
        self.assertEqual(extract_entities(page, "https://garage-dupont.fr"), [])

    def test_generic_local_business_does_not_acquire_automotive_category(self):
        entity = extract_entities(ld(garage("Prestataire du site", "contact@agence-creatrice.fr",
                                           **{"@type": "LocalBusiness"})), "https://reseau-auto.fr")[0]
        self.assertEqual(entity["category"], "")
        self.assertEqual(entity["entity_type"], "LocalBusiness")

    def test_top_garage_profile_supplies_automotive_category_context(self):
        url = "https://garage.top-garage.fr/fr/france-FR/CUST-000001/2jc/details"
        entity = extract_entities(ld(garage("2JC", "contact@garage-2jc.fr", **{
            "@type": "AutomotiveBusiness", "url": url})), url)[0]
        self.assertEqual(entity["category"], "garage")

    def test_vcard4_folded_properties_and_business_org(self):
        card = "\r\n".join([
            "BEGIN:VCARD", "VERSION:4.0", "FN:Jean Responsable", "ORG:Garage Dupont\\; Frères;Atelier",
            "ADR;TYPE=work:;;12 avenue de la République;Lyon;;69003;France",
            'item1.EMAIL;TYPE="work:main":atelier@garage-', " dupont.fr",
            "EMAIL:garage.dupont@gmail.com", "TEL;VALUE=uri:tel:+33-4-78-00-00-01",
            "URL:https://garage-dupont.fr/contact", "X-SIRET:12345678900012", "END:VCARD",
        ])
        entities = extract_entities(card, "https://www.axial.org/nos-carrossiers-axial/1/vcard")
        self.assertEqual(len(entities), 1)
        entity = entities[0]
        self.assertEqual(entity["name"], "Garage Dupont; Frères")
        self.assertEqual(entity["address"], "12 avenue de la République")
        self.assertEqual((entity["postal_code"], entity["city"]), ("69003", "Lyon"))
        self.assertEqual(entity["phone"], "04 78 00 00 01")
        self.assertEqual(set(entity["emails"]), {"atelier@garage-dupont.fr", "garage.dupont@gmail.com"})
        self.assertEqual(entity["source_type"], "vcard")
        self.assertEqual(entity["siren"], "123456789")

    def test_inline_and_multiple_vcards_keep_own_contacts(self):
        cards = ["BEGIN:VCARD\nVERSION:4.0\nORG:Garage Dupont\nEMAIL:atelier@garage-dupont.fr\nEND:VCARD",
                 "BEGIN:VCARD\nVERSION:4.0\nORG:Garage Martin\nEMAIL:contact@garage-martin.fr\nEND:VCARD"]
        urls = ["data:text/vcard;charset=utf-8," + quote(cards[0]),
                "data:text/vcard;base64," + base64.b64encode(cards[1].encode()).decode()]
        page = ''.join('<a href="' + u + '">Contact</a>' for u in urls)
        entities = extract_entities(page, "https://reseau-auto.fr/contact")
        self.assertEqual(len(entities), 2)
        self.assertEqual({tuple(e["emails"]) for e in entities}, {("atelier@garage-dupont.fr",), ("contact@garage-martin.fr",)})
        self.assertEqual(len({e["source_id"] for e in entities}), 2)

    def test_motrio_public_contact_block_excludes_footer_and_next_widgets(self):
        def detail(icon, content):
            return ('<div data-testid="motrio-workshop-location-detail"><svg><use href="#' + icon + '"></use></svg>'
                    '<div data-testid="motrio-workshop-location-detail-content">' + content + '</div></div>')
        page = '<h1 data-testid="motrio-workshop-hero-section-title">Garage Dupont</h1>'
        page += '<section data-testid="motrio-workshop-location-section">'
        page += detail("svg-travel/marker-pin-01", "12 rue des Lilas, 69003, Lyon")
        page += detail("svg-communication/mail-01", "garage.dupont@gmail.com")
        page += detail("svg-communication/phone", "04 78 00 00 01") + '</section>'
        page += '<footer>siege@motrio.fr</footer><script>self.__next_f.push(["voisin@garage-martin.fr"])</script>'
        url = "https://www.motrio.fr/garage-reparateur/garage-dupont-1"
        entity = extract_entities(page, url)[0]
        self.assertEqual(entity["emails"], ["garage.dupont@gmail.com"])
        self.assertEqual((entity["postal_code"], entity["city"]), ("69003", "Lyon"))
        self.assertEqual(entity["website"], url)
        self.assertEqual(entity["source_type"], "motrio_public")
        self.assertEqual(extract_entities(page, "https://autre-site.fr/garage-reparateur/garage-dupont-1"), [])


class MatchIdentityTests(unittest.TestCase):
    def setUp(self):
        self.business = {"name": "GARAGE DUPONT", "postal_code": "69003", "city": "Lyon",
                         "address": "12 AV. DE LA REPUBLIQUE 69003 LYON", "phone": "0478000001",
                         "siren": "123456789", "siret": "12345678900012"}

    def test_exact_siret_and_address_normalization(self):
        entity = extract_entities(ld(garage(identifier={"propertyID": "SIRET", "value": "12345678900012"})), "https://reseau-auto.fr/fiche")[0]
        self.assertEqual(match_identity(self.business, entity=entity), (True, "siret_exact"))

    def test_conflicts_override_matching_phone_name_and_even_siret(self):
        entity = {**self.business, "emails": []}
        for changed, reason in [({"siret": "12345678900020"}, "conflit_siret"),
                                ({"address": "99 rue des Lilas"}, "conflit_adresse"),
                                ({"postal_code": "75015"}, "conflit_code_postal")]:
            with self.subTest(changed=changed):
                self.assertEqual(match_identity(self.business, entity={**entity, **changed}), (False, reason))

    def test_postcode_siren_and_generic_name_are_insufficient(self):
        for entity in ({"postal_code": "69003"}, {"siren": "123456789"},
                       {"postal_code": "69003", "city": "Lyon"}):
            self.assertFalse(match_identity(self.business, entity=entity)[0])
        self.assertFalse(match_identity({"name": "GARAGE AUTO", "postal_code": "69003"}, "Garage auto 69003")[0])
        self.assertFalse(match_identity(self.business, "69003 Lyon SIREN 123 456 789")[0])

    def test_short_distinctive_name_requires_address_or_phone(self):
        business = {"name": "Garage Un", "address": "1 rue des Ateliers", "postal_code": "69003", "city": "Lyon"}
        self.assertTrue(match_identity(business, entity=dict(business))[0])
        self.assertFalse(match_identity(business, entity={"name": "Garage Un", "postal_code": "69003"})[0])
        self.assertFalse(match_identity(business, entity={"name": "Contactez un garage", "address": "1 rue des Ateliers"})[0])

    def test_legacy_business_with_name_siren_postcode_still_matches(self):
        business = {"name": "GARAGE TEST", "postal_code": "69003", "siren": "123456789"}
        text = "<html><body>SARL Garage Test, SIREN 123 456 789, 69003 Lyon</body></html>"
        self.assertTrue(match_identity(business, text)[0])
        self.assertFalse(match_identity({**business, "postal_code": "75015", "siren": "999999999"}, text)[0])

    def test_script_hidden_and_separate_cards_cannot_supply_proofs(self):
        business = {"name": "Garage Dupont", "postal_code": "69003"}
        for page in [
            '<h1>Garage Martin</h1><script>Garage Dupont 69003</script>',
            '<h1>Garage Martin</h1><style>/* Garage Dupont 69003 */</style>',
            '<div hidden>Garage Dupont 69003</div>',
            '<article>Garage Dupont</article><article>Garage Martin, 69003 Lyon</article>',
            '<header>Garage Dupont</header><footer>Agence web, 69003 Lyon</footer>',
        ]:
            with self.subTest(page=page):
                self.assertFalse(match_identity(business, page)[0])

    def test_entity_cannot_borrow_global_text_from_another_establishment(self):
        entity = {"name": "Garage Martin", "postal_code": "75015", "emails": ["contact@garage-martin.fr"]}
        self.assertFalse(match_identity(self.business, "Garage Dupont 69003 Lyon 04 78 00 00 01", entity)[0])

    def test_visible_contradictory_registration_blocks_other_matching_content(self):
        page = '<h1>Garage Dupont</h1><p>69003 Lyon</p><footer>SIRET : 999 999 999 00012</footer>'
        self.assertEqual(match_identity(self.business, page), (False, "conflit_siret"))

    def test_sqlite_row_and_old_sirene_source_id(self):
        con = sqlite3.connect(":memory:")
        try:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT 'sirene' source, '12345678900012' source_id, 'GARAGE DUPONT' name").fetchone()
            self.assertEqual(match_identity(row, entity={"siret": "12345678900012"}), (True, "siret_exact"))
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
