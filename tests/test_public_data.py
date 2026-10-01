"""Imports publics : schémas réels, provenance, jointures exactes et limites.

Les serveurs sont locaux ; aucun téléchargement public ni aucune boîte e-mail
n'est contactée par cette suite. Les identifiants et coordonnées sont fictifs.
"""
import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from autolead.db import DB
from autolead.net import make_session
from autolead.sources.public_data import (
    DINUM_URL,
    PublicDataError,
    collect_dgccrf,
    enrich_dinum,
    normalize_candidate_domain,
)


SIRET_A = "12345678900011"
SIRET_B = "12345678900029"  # même SIREN, établissement différent
SIRET_C = "98765432100017"


def csv_bytes(rows, *, fields=("SIRET", "domain_email", "data_source"), sep=";"):
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter=sep, lineterminator="\r\n")
    writer.writerow(fields)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


def centre(siret=SIRET_A, **changes):
    return {
        "cct_siret": siret, "cct_denomination": "Centre contrôle Alpha",
        "cct_adresse": "12 rue du Centre", "cct_code_postal": "92600", "cct_commune": "Asnières-sur-Seine",
        "cct_tel": "01 23 45 67 89", "cct_url": "https://reseau-controle.fr/centres/alpha?id=10&zone=92",
        "lat": 48.9, "long": 2.3, **changes,
    }


class CandidateDomainTests(unittest.TestCase):
    def test_accepts_only_bare_public_business_domains(self):
        self.assertEqual(normalize_candidate_domain(" Atelier-Dupont.FR. "), "atelier-dupont.fr")
        self.assertEqual(normalize_candidate_domain("réparation-auto.fr"), "xn--rparation-auto-bkb.fr")
        for raw in (
            "gmail.com", "sub.hotmail.fr", "mail.proton.me", "qq.com", "facebook.com",
            "google.com", "example.org", "localhost", "127.0.0.1", "[::1]", "192.168.1.1",
            "https://atelier-dupont.fr", "atelier@atelier-dupont.fr", "atelier-dupont.fr:443",
            "-atelier.fr", "atelier-.fr", "atelier..fr", "atelier.fr..", "atelier.fr/contact",
            "atelier.fr?x=1", "atelier.local", "atelier.invalid", "atelier.123", "atelier_1.fr", "",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_candidate_domain(raw))


class PublicDataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(os.path.join(self.tmp.name, "public.db"))
        self.responses = {}
        self.hits = []

        async def handler(request):
            self.hits.append(request.path)
            spec = self.responses.get(request.path)
            if callable(spec):
                return await spec(request)
            if spec is None:
                return web.Response(status=404)
            return web.Response(**spec)

        app = web.Application()
        app.router.add_get("/{tail:.*}", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.db.close()
        self.tmp.cleanup()

    def json_response(self, path, payload, **headers):
        self.responses[path] = {"text": json.dumps(payload), "content_type": "application/json", "headers": headers}
        return self.base + path

    def csv_response(self, path, rows, **headers):
        self.responses[path] = {"body": csv_bytes(rows), "content_type": "text/csv", "headers": headers}
        return self.base + path

    def seed(self, siret=SIRET_A, *, source="sirene", source_id=None, **values):
        self.db.add_businesses([{
            "source": source, "source_id": source_id or siret, "siret": siret,
            "name": "Garage Alpha", "category": "garage", "siren": siret[:9], **values,
        }])
        return self.db.conn.execute("SELECT id FROM businesses WHERE source=? AND source_id=?",
                                    (source, source_id or siret)).fetchone()[0]

    async def test_dgccrf_merges_exact_siret_preserves_existing_data_and_full_url(self):
        existing = self.seed(phone="01 00 00 00 00", website="https://atelier-verifie.fr/", name="Nom vérifié")
        url = self.json_response("/centres", [centre(), centre(SIRET_B, cct_denomination="Centre Beta")],
                                 **{"Last-Modified": "Wed, 30 Sep 2026 09:00:00 GMT"})
        async with make_session(trust_env=False) as session:
            self.assertEqual(await collect_dgccrf(self.db, session, url=url), 1)
            self.assertEqual(await collect_dgccrf(self.db, session, url=url), 0)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0], 2)
        a = self.db.conn.execute("SELECT * FROM businesses WHERE id=?", (existing,)).fetchone()
        self.assertEqual((a["name"], a["phone"], a["website"]), ("Nom vérifié", "01 00 00 00 00", "https://atelier-verifie.fr/"))
        self.assertEqual(a["address"], "12 rue du Centre")
        b = self.db.conn.execute("SELECT * FROM businesses WHERE siret=?", (SIRET_B,)).fetchone()
        self.assertEqual(b["website"], "https://reseau-controle.fr/centres/alpha?id=10&zone=92")
        self.assertEqual(b["category"], "controle_technique")
        self.assertEqual(b["source_date"], "2026-09-30")
        self.assertEqual(b["source_url"], url)
        self.assertEqual(b["source_license"], "")  # notre serveur local n'est pas une source officielle
        provenance = self.db.conn.execute("SELECT source_url FROM business_sources WHERE business_id=? AND source='dgccrf'",
                                         (existing,)).fetchall()
        self.assertEqual([row[0] for row in provenance], [url])

    async def test_dgccrf_filters_and_limit_are_before_selection(self):
        url = self.json_response("/centres", [centre(cct_code_postal="93000"), centre(SIRET_B)])
        async with make_session(trust_env=False) as session:
            self.assertEqual(await collect_dgccrf(self.db, session, url=url, departements=["92"], limit=1), 0)
            self.assertEqual(await collect_dgccrf(self.db, session, url=url, departements=["92"]), 1)
            calls = len(self.hits)
            self.assertEqual(await collect_dgccrf(self.db, session, url=url, categories=["garage"]), 0)
            self.assertEqual(await collect_dgccrf(self.db, session, url=url, limit=0), 0)
            self.assertEqual(len(self.hits), calls)
        self.assertEqual(self.db.conn.execute("SELECT siret FROM businesses").fetchone()[0], SIRET_B)

    async def test_dgccrf_local_csv_utf8_bom_quoted_fields_and_missing_values(self):
        path = Path(self.tmp.name) / "centres.csv"
        fields = ("cct_siret", "cct_denomination", "cct_adresse", "cct_code_postal", "cct_commune", "cct_tel", "cct_url", "lat", "long")
        path.write_bytes(csv_bytes([
            [SIRET_A, "Contrôle; Étoile", "Bâtiment A\r\n12 rue du Centre", "01000", "Bourg-en-Bresse", "", "", "NaN", "200"],
            ["123456789", "Identifiant trop court", "", "01000", "", "", "", "", ""],
        ], fields=fields))
        async with make_session(trust_env=False) as session:
            self.assertEqual(await collect_dgccrf(self.db, session, path=path), 1)
        row = self.db.conn.execute("SELECT * FROM businesses").fetchone()
        self.assertEqual(row["name"], "Contrôle; Étoile")
        self.assertEqual(row["postal_code"], "01000")
        self.assertEqual(row["source_url"], str(path))
        self.assertEqual(row["source_date"], "")
        self.assertEqual(row["source_license"], "")
        self.assertIsNone(row["lat"])
        self.assertIsNone(row["lon"])
        self.assertEqual(self.hits, [])

    async def test_dgccrf_current_geographic_fields_and_explicit_department(self):
        url = self.json_response("/geography", [centre(
            latitude=48.91, longitude=2.29, lat=None, long=None,
            cct_code_postal="", code_departement="92", cct_code_commune="92004",
        )])
        async with make_session(trust_env=False) as session:
            self.assertEqual(await collect_dgccrf(self.db, session, url=url, departements=["92"]), 1)
        row = self.db.conn.execute("SELECT lat, lon FROM businesses").fetchone()
        self.assertEqual((row["lat"], row["lon"]), (48.91, 2.29))

    async def test_dgccrf_rejects_partial_pagination_and_unexpected_schema(self):
        for payload in ({"total_count": 6000, "results": [centre()]}, [{"new_id": SIRET_A}], {"error": "unavailable"}):
            with self.subTest(payload=payload):
                url = self.json_response("/invalid", payload)
                async with make_session(trust_env=False) as session:
                    with self.assertRaises(PublicDataError):
                        await collect_dgccrf(self.db, session, url=url)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0], 0)

    async def test_dinum_exact_join_multiple_entities_filtering_and_idempotence(self):
        first = self.seed()
        second = self.seed(source="osm", source_id="node/10", website="https://deja-verifie.fr/")
        # Pas de SIRET_B dans la base : partager un SIREN ne suffit pas.
        origin = "TrackDéchets; extraction\r\npublique"
        rows = [
            [SIRET_A, " Atelier-Dupont.FR. ", origin],
            [SIRET_A, "atelier-dupont.fr", origin],
            [SIRET_B, "autre-etablissement.fr", "source"],
            [SIRET_C, "centre-inconnu.fr", "source"],
            [SIRET_A, "gmail.com", "source"],
            [SIRET_A, "127.0.0.1", "source"],
            [SIRET_A, "https://garage-alpha.fr", "source"],
            [SIRET_A, "atelier@garage-alpha.fr", "source"],
            ["123456789", "garage-alpha.fr", "source"],
        ]
        url = self.csv_response("/domaines", rows, **{"Last-Modified": "Mon, 09 Sep 2024 12:00:00 GMT"})
        async with make_session(trust_env=False) as session:
            counts = await enrich_dinum(self.db, session, url=url)
            again = await enrich_dinum(self.db, session, url=url)
        self.assertEqual(counts, {"rows_read": 9, "matched_rows": 6, "candidates": 2, "invalid_rows": 1,
                                  "ignored_domains": 4, "unmatched_rows": 2, "limited": False})
        self.assertEqual(again["candidates"], 0)
        candidates = self.db.conn.execute("SELECT * FROM domain_candidates ORDER BY business_id").fetchall()
        self.assertEqual([(row["business_id"], row["domain"]) for row in candidates],
                         [(first, "atelier-dupont.fr"), (second, "atelier-dupont.fr")])
        self.assertTrue(all(row["data_source"] == origin and row["source_url"] == url and row["source_date"] == "2024-09-09" for row in candidates))
        self.assertEqual(self.db.conn.execute("SELECT website FROM businesses WHERE id=?", (first,)).fetchone()[0], "")
        self.assertEqual(self.db.conn.execute("SELECT website FROM businesses WHERE id=?", (second,)).fetchone()[0], "https://deja-verifie.fr/")
        site = self.db.conn.execute("SELECT guessed FROM sites WHERE site_key='atelier-dupont.fr'").fetchone()
        self.assertEqual(site[0], 1)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0], 0)

    async def test_dinum_local_snapshot_and_line_limit(self):
        self.seed()
        path = Path(self.tmp.name) / "domaines.csv"
        path.write_bytes(csv_bytes([[SIRET_C, "inconnu.fr", "source"], [SIRET_A, "atelier-connu.fr", "source"]], sep=","))
        async with make_session(trust_env=False) as session:
            sample = await enrich_dinum(self.db, session, path=path, limit=1)
            complete = await enrich_dinum(self.db, session, path=path, limit=2)
        self.assertEqual((sample["rows_read"], sample["candidates"], sample["limited"]), (1, 0, True))
        self.assertEqual((complete["rows_read"], complete["candidates"], complete["limited"]), (2, 1, False))
        candidate = self.db.conn.execute("SELECT * FROM domain_candidates").fetchone()
        self.assertEqual(candidate["source_url"], str(path))
        self.assertEqual(candidate["source_date"], "")
        self.assertEqual(candidate["source_license"], "")
        self.assertEqual(self.hits, [])

    async def test_dinum_legacy_sirene_identifier_and_multiple_provenances(self):
        business_id = self.seed()
        self.db.conn.execute("UPDATE businesses SET siret='' WHERE id=?", (business_id,))
        self.db.commit()
        url = self.csv_response("/legacy", [
            [SIRET_A, "atelier-connu.fr", "trackdechets"],
            [SIRET_A, "atelier-connu.fr", "la_bonne_alternance"],
        ])
        async with make_session(trust_env=False) as session:
            counts = await enrich_dinum(self.db, session, url=url)
        self.assertEqual(counts["candidates"], 1)
        sources = self.db.conn.execute("SELECT data_source FROM domain_candidates WHERE business_id=?", (business_id,)).fetchall()
        self.assertEqual({row[0] for row in sources}, {"trackdechets", "la_bonne_alternance"})

    async def test_dinum_official_source_metadata_without_external_request(self):
        self.seed()
        local_url = self.csv_response("/official-fixture", [[SIRET_A, "atelier-connu.fr", "trackdechets"]],
                                      **{"Last-Modified": "Thu, 02 Jul 2026 15:55:53 GMT"})
        async with make_session(trust_env=False) as session:
            class LocalFixtureSession:
                def get(self, url, **kwargs):
                    if url != DINUM_URL:
                        raise AssertionError("L'import doit utiliser la ressource officielle par défaut")
                    return session.get(local_url, **kwargs)

            await enrich_dinum(self.db, LocalFixtureSession())
        candidate = self.db.conn.execute("SELECT * FROM domain_candidates").fetchone()
        self.assertEqual(candidate["source_url"], DINUM_URL)
        self.assertEqual(candidate["source_license"], "etalab-2.0")
        self.assertEqual(candidate["source_date"], "2024-09")

    async def test_http_errors_and_html_are_not_reported_as_empty_success(self):
        self.responses["/forbidden"] = {"status": 403, "text": "blocked"}
        self.responses["/html"] = {"text": "<html>Authentification requise</html>", "content_type": "text/html"}
        async with make_session(trust_env=False) as session:
            for fn in (collect_dgccrf, enrich_dinum):
                for path in ("/forbidden", "/html"):
                    with self.subTest(fn=fn.__name__, path=path):
                        with self.assertRaises(PublicDataError):
                            await fn(self.db, session, url=self.base + path)

    async def test_size_guard_on_content_length_and_streamed_response(self):
        self.responses["/large"] = {"body": b"x" * 1024, "content_type": "text/csv"}

        async def streamed(request):
            response = web.StreamResponse(headers={"Content-Type": "text/csv"})
            await response.prepare(request)
            await response.write(b"x" * 1024)
            await response.write_eof()
            return response

        self.responses["/streamed"] = streamed
        async with make_session(trust_env=False) as session:
            with patch("autolead.sources.public_data.MAX_DINUM_BYTES", 100):
                for path in ("/large", "/streamed"):
                    with self.subTest(path=path), self.assertRaisesRegex(PublicDataError, "limite"):
                        await enrich_dinum(self.db, session, url=self.base + path)

    async def test_malformed_csv_detected_instead_of_silently_shifted_columns(self):
        self.seed()
        path = Path(self.tmp.name) / "broken.csv"
        for content in (
            "SIRET;domain_email;data_source\n12345678900011;garage-alpha.fr;source;extra\n",
            'SIRET;domain_email;data_source\n12345678900011;garage-alpha.fr;"never closed\n',
            "SIRET;domain_email;domain_email;data_source\n",
        ):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                async with make_session(trust_env=False) as session:
                    with self.assertRaises(PublicDataError):
                        await enrich_dinum(self.db, session, path=path)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM domain_candidates").fetchone()[0], 0)

    async def test_interrupted_csv_reports_already_committed_batch(self):
        self.seed()
        path = Path(self.tmp.name) / "partial.csv"
        path.write_bytes(csv_bytes([[SIRET_A, "garage-alpha.fr", "source"]] * 200)
                         + b"12345678900011;garage-beta.fr;source;unexpected\r\n")
        async with make_session(trust_env=False) as session:
            with self.assertRaisesRegex(PublicDataError, "1 candidats déjà enregistrés"):
                await enrich_dinum(self.db, session, path=path)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM domain_candidates").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
