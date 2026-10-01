"""Régressions du crawl réseau : identités, budgets, robots et reprise réelle."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web

from autolead.crawler import run_crawl
from autolead.db import DB
from autolead.frontier import Frontier, canonical_url, is_network_key, reset_pages


class NetworkCrawlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hits = []
        self.names = ["alpha", "bravo", "charlie", "delta", "echo"]
        self.root_html = '<a href="/garage/alpha">Garage Alpha</a><a href="/garage/bravo">Garage Bravo</a>'
        self.extra = {}
        self.robots = "User-agent: *\nDisallow: /private/\n"
        self.slow_started = asyncio.Event()
        self.slow_release = asyncio.Event()
        self.slow = False
        app = web.Application()
        app.router.add_get("/{tail:.*}", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        server = web.TCPSite(self.runner, "127.0.0.1", 0)
        await server.start()
        self.base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        self.path = os.path.join(self.tmp.name, "network.db")
        self.db = DB(self.path)
        self.add_business("alpha")
        self.add_business("bravo")
        self.key = self.db.conn.execute("SELECT site_key FROM sites").fetchone()[0]

    async def asyncTearDown(self):
        self.slow_release.set()
        self.db.close()
        await self.runner.cleanup()
        self.tmp.cleanup()

    def entity(self, slug):
        number = self.names.index(slug) + 1 if slug in self.names else 9
        return {
            "@context": "https://schema.org", "@type": "AutoRepair",
            "@id": self.base + "/garage/" + slug + "#garage",
            "name": "Garage " + slug.title(), "url": self.base + "/garage/" + slug,
            "address": {"@type": "PostalAddress", "streetAddress": f"{number} rue des Ateliers",
                        "postalCode": "92270", "addressLocality": "Bois-Colombes"},
            "telephone": f"01 23 45 67 {number:02d}", "email": f"atelier@garage-{slug}.fr",
        }

    def html_entity(self, slug, *, neighbors=()):
        entities = [self.entity(slug), *[self.entity(n) for n in neighbors]]
        return '<script type="application/ld+json">' + json.dumps(entities) + '</script>'

    def add_business(self, slug):
        entity = self.entity(slug)
        self.db.add_businesses([{
            "source": "fixture", "source_id": slug, "name": entity["name"], "category": "garage",
            "address": entity["address"]["streetAddress"], "postal_code": "92270",
            "city": "Bois-Colombes", "phone": entity["telephone"], "website": entity["url"],
        }])

    async def handle(self, request):
        self.hits.append(request.path_qs)
        if request.path == "/robots.txt":
            return web.Response(text=self.robots, content_type="text/plain")
        if request.path == "/":
            return web.Response(text=self.root_html, content_type="text/html")
        if request.path == "/slow" and self.slow:
            self.slow_started.set()
            await self.slow_release.wait()
            return web.Response(text=self.html_entity("slow"), content_type="text/html")
        if request.path == "/redirect-private":
            raise web.HTTPFound("/private/garage")
        if request.path in self.extra or request.path_qs in self.extra:
            body = self.extra.get(request.path_qs, self.extra.get(request.path))
            ctype = "application/xml" if request.path.endswith(".xml") else "text/html"
            if "/vcard" in request.path:
                ctype = "application/octet-stream"
            return web.Response(text=body, content_type=ctype)
        if request.path.startswith("/garage/"):
            slug = request.path.rsplit("/", 1)[1]
            if slug in self.names or slug == "slow":
                return web.Response(text=self.html_entity(slug), content_type="text/html")
        raise web.HTTPNotFound()

    async def crawl(self, **kwargs):
        options = dict(concurrency=2, trust_env=False, deep_pages=20, timeout=2, site_timeout=2)
        options.update(kwargs)
        await run_crawl(self.db, **options)

    def status(self):
        return self.db.conn.execute("SELECT status FROM sites WHERE site_key=?", (self.key,)).fetchone()[0]

    def attributed(self):
        return {(row["name"], row["email"]) for row in self.db.conn.execute(
            "SELECT b.name,e.email FROM emails e JOIN businesses b ON b.id=e.business_id")}

    async def test_two_local_fiches_and_neighbors_are_not_merged(self):
        self.extra["/garage/alpha"] = self.html_entity("alpha", neighbors=["bravo"]) + (
            '<footer>Siège du réseau : siege@reseau-central.fr</footer>')
        await self.crawl()
        self.assertEqual(self.attributed(), {
            ("Garage Alpha", "atelier@garage-alpha.fr"), ("Garage Bravo", "atelier@garage-bravo.fr")})
        self.assertIn("/garage/alpha", self.hits)
        self.assertIn("/garage/bravo", self.hits)
        source = self.db.conn.execute(
            "SELECT e.source_url FROM emails e JOIN businesses b ON b.id=e.business_id "
            "WHERE b.source_id='alpha'").fetchone()[0]
        self.assertEqual(source, self.base + "/garage/alpha")
        self.assertEqual(self.status(), "ok")

    async def test_budget_resumes_remaining_pages_without_fetching_home_again(self):
        self.root_html = "".join(f'<a href="/garage/{name}">Garage {name}</a>' for name in self.names)
        await self.crawl(deep_pages=3)
        first = {hit for hit in self.hits if hit == "/" or hit.startswith("/garage/")}
        self.assertEqual(len(first), 3)
        self.assertEqual(self.status(), "partial")
        self.assertEqual(Frontier(self.db.conn, self.key).counts()["pending"], 3)
        self.hits.clear()
        self.db.close()
        self.db = DB(self.path)  # nouvelle connexion, comme le passage suivant sur le VPS
        await self.crawl(deep_pages=3)
        second = {hit for hit in self.hits if hit == "/" or hit.startswith("/garage/")}
        self.assertEqual(len(second), 3)
        self.assertFalse(first & second)
        self.assertEqual(len(self.attributed()), 5)
        self.assertEqual(self.status(), "ok")

    async def test_robots_sitemap_index_and_query_pagination_are_discovered(self):
        self.robots += f"Sitemap: {self.base}/public-index.xml\n"
        self.extra["/public-index.xml"] = (
            f'<sitemapindex><sitemap><loc>{self.base}/local-sitemap.xml?part=1&amp;lang=fr</loc>'
            '</sitemap></sitemapindex>')
        self.extra["/local-sitemap.xml?part=1&lang=fr"] = (
            f'<urlset><url><loc>{self.base}/garage/charlie?city=Lyon</loc></url></urlset>')
        self.root_html = '<head><link rel="next" href="/list?page=2"></head>'
        self.extra["/list?page=2"] = '<a href="/garage/delta?region=92">Garage Delta</a>'
        await self.crawl()
        for expected in ("/public-index.xml", "/local-sitemap.xml?part=1&lang=fr",
                         "/garage/charlie?city=Lyon", "/list?page=2", "/garage/delta?region=92"):
            self.assertIn(expected, self.hits)
        self.assertIn(("Garage Charlie", "atelier@garage-charlie.fr"), self.attributed())
        self.assertIn(("Garage Delta", "atelier@garage-delta.fr"), self.attributed())

    async def test_robots_also_applies_to_redirect_destination(self):
        self.robots = "User-agent: AutoLeadBot\nDisallow: /private/\n\nUser-agent: *\nAllow: /\n"
        self.root_html = ('<a href="/private/garage">Garage privé</a>'
                          '<a href="/redirect-private">Garage redirigé</a>')
        self.extra["/private/garage"] = "secret@garage-prive.fr"
        await self.crawl()
        self.assertNotIn("/private/garage", self.hits)
        self.assertIn("/redirect-private", self.hits)
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM emails WHERE email='secret@garage-prive.fr'").fetchone()[0], 0)
        self.assertEqual(Frontier(self.db.conn, self.key).counts()["blocked"], 2)
        self.assertEqual(self.status(), "partial")

    async def test_neighbor_only_in_jsonld_is_visited_even_without_contact(self):
        neighbor = self.entity("charlie")
        neighbor.pop("email")
        self.extra["/garage/alpha"] = ('<script type="application/ld+json">' +
            json.dumps([self.entity("alpha"), neighbor]) + '</script>')
        await self.crawl()
        self.assertIn("/garage/charlie", self.hits)
        self.assertIn(("Garage Charlie", "atelier@garage-charlie.fr"), self.attributed())

    async def test_vcard_without_extension_keeps_local_identity(self):
        self.extra["/garage/alpha"] = self.html_entity("alpha") + (
            '<a href="/garage/alpha/vcard">Enregistrer le contact</a>')
        self.extra["/garage/alpha/vcard"] = (
            "BEGIN:VCARD\nVERSION:3.0\nORG:Garage Alpha\nADR:;;1 rue des Ateliers;"
            "Bois-Colombes;;92270;France\nEMAIL:devis@garage-alpha.fr\nEND:VCARD\n")
        await self.crawl()
        self.assertIn("/garage/alpha/vcard", self.hits)
        self.assertIn(("Garage Alpha", "devis@garage-alpha.fr"), self.attributed())
        self.assertNotIn(("Garage Bravo", "devis@garage-alpha.fr"), self.attributed())

    async def test_all_business_source_urls_are_seeds_including_queries(self):
        bid = self.db.conn.execute("SELECT id FROM businesses WHERE source_id='alpha'").fetchone()[0]
        url = self.base + "/secondary?city=92&garage=alpha"
        self.db.conn.execute("INSERT INTO business_pages(business_id,site_key,url,source_type) VALUES(?,?,?,?)",
                             (bid, self.key, url, "dgccrf"))
        self.db.commit()
        self.extra["/secondary?city=92&garage=alpha"] = self.html_entity("alpha")
        await self.crawl(deep_pages=3)
        self.assertIn("/secondary?city=92&garage=alpha", self.hits)

    async def test_candidates_on_existing_site_are_checked_without_postcode_only_match(self):
        self.db.add_businesses([
            {"source": "sirene", "source_id": "charlie-candidate", "name": "Garage Charlie",
             "address": "3 rue des Ateliers", "postal_code": "92270", "city": "Bois-Colombes"},
            {"source": "sirene", "source_id": "wrong-candidate", "name": "Garage Inconnu",
             "address": "99 avenue Autre", "postal_code": "92270", "city": "Bois-Colombes"},
        ])
        ids = {r["source_id"]: r["id"] for r in self.db.conn.execute("SELECT id,source_id FROM businesses")}
        self.db.add_guesses({self.key: [(ids["charlie-candidate"], ["92270"]),
                                        (ids["wrong-candidate"], ["92270"])]})
        self.root_html += self.html_entity("charlie")
        self.assertEqual(self.db.conn.execute("SELECT guessed FROM sites WHERE site_key=?",
                                              (self.key,)).fetchone()[0], 0)
        await self.crawl()
        checks = {r["source_id"]: (r["verified"], r["site_key"]) for r in self.db.conn.execute(
            "SELECT b.source_id,g.verified,b.site_key FROM guesses g JOIN businesses b ON b.id=g.business_id")}
        self.assertEqual(checks["charlie-candidate"], (1, self.key))
        self.assertEqual(checks["wrong-candidate"], (0, None))
        self.assertIn(("Garage Alpha", "atelier@garage-alpha.fr"), self.attributed())

    async def test_conflicting_structured_identifier_cannot_be_overridden_by_visible_text(self):
        self.db.add_businesses([{
            "source": "sirene", "source_id": "conflicting", "name": "Garage Charlie",
            "siret": "12345678900012", "postal_code": "92270", "city": "Bois-Colombes",
        }])
        bid = self.db.conn.execute("SELECT id FROM businesses WHERE source_id='conflicting'").fetchone()[0]
        self.db.add_guesses({self.key: [(bid, ["92270"])]})
        entity = self.entity("charlie")
        entity["siret"] = "99999999900012"
        self.root_html = ('<html><body><h1>Garage Charlie</h1><p>92270 Bois-Colombes</p>'
                          '<script type="application/ld+json">' + json.dumps(entity) + '</script></body></html>')
        # La fiche canonique garde le même identifiant contradictoire.
        self.extra["/garage/charlie"] = self.root_html
        await self.crawl()
        row = self.db.conn.execute("SELECT b.site_key,g.verified FROM businesses b "
                                   "JOIN guesses g ON g.business_id=b.id WHERE b.id=?", (bid,)).fetchone()
        self.assertEqual(tuple(row), (None, 0))

    async def test_normal_site_uses_new_complete_source_url_within_page_budget(self):
        self.db.conn.execute("DELETE FROM business_pages WHERE site_key=?", (self.key,))
        self.db.conn.execute("DELETE FROM businesses WHERE source_id='bravo'")
        self.db.conn.execute("UPDATE sites SET url=? WHERE site_key=?", (self.base + "/missing", self.key))
        self.db.conn.execute("UPDATE businesses SET website=? WHERE site_key=?", (self.base + "/missing", self.key))
        bid = self.db.conn.execute("SELECT id FROM businesses WHERE source_id='alpha'").fetchone()[0]
        fresh = self.base + "/garage/alpha?source=official"
        self.db.conn.execute("INSERT INTO business_pages(business_id,site_key,url,source_type) VALUES(?,?,?,?)",
                             (bid, self.key, fresh, "dgccrf"))
        self.db.commit()
        self.assertNotIn(self.key, self.db.network_sites())
        await self.crawl(max_pages=1)
        page_hits = [h for h in self.hits if h != "/robots.txt"]
        self.assertEqual(page_hits, ["/garage/alpha?source=official"])
        self.assertIn(("Garage Alpha", "atelier@garage-alpha.fr"), self.attributed())

    async def test_timeout_preserves_contacts_and_unfinished_page_for_next_pass(self):
        self.slow = True
        self.root_html = '<a href="/slow">Garage Slow</a>'
        await self.crawl(site_timeout=0.05)
        self.assertTrue(self.slow_started.is_set())
        self.assertEqual(self.status(), "partial")
        self.assertIn(("Garage Alpha", "atelier@garage-alpha.fr"), self.attributed())
        row = self.db.conn.execute("SELECT status FROM crawl_frontier WHERE url=?", (self.base + "/slow",)).fetchone()
        self.assertEqual(row[0], "pending")
        self.db.close()
        self.db = DB(self.path)
        self.slow_release.set()
        self.hits.clear()
        await self.crawl()
        self.assertNotIn("/", self.hits)
        self.assertIn(("Garage Slow", "atelier@garage-slow.fr"), self.attributed())
        self.assertEqual(self.status(), "ok")

    async def test_cancellation_checkpoints_completed_pages_before_propagating(self):
        self.slow = True
        self.root_html = '<a href="/slow">Garage Slow</a>'
        task = asyncio.create_task(self.crawl())
        await asyncio.wait_for(self.slow_started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.db.close()
        self.db = DB(self.path)
        self.assertEqual(self.status(), "partial")
        self.assertIn(("Garage Bravo", "atelier@garage-bravo.fr"), self.attributed())
        self.assertEqual(self.db.conn.execute("SELECT status FROM crawl_frontier WHERE url=?",
                                              (self.base + "/slow",)).fetchone()[0], "pending")

    async def test_failed_contact_save_never_marks_page_done(self):
        original = self.db.save_crawl

        def fail_after_write(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic storage failure")

        with patch.object(self.db, "save_crawl", side_effect=fail_after_write):
            with self.assertRaisesRegex(RuntimeError, "synthetic storage failure"):
                await self.crawl()
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0], 0)
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM crawl_frontier WHERE kind='page' AND status='done'").fetchone()[0], 0)

    async def test_repair_requeues_only_selected_source_page(self):
        await self.crawl()
        self.hits.clear()
        reset_pages(self.db.conn, self.key, [self.base + "/garage/alpha"])
        self.db.commit()
        await self.crawl()
        self.assertIn("/garage/alpha", self.hits)
        self.assertNotIn("/garage/bravo", self.hits)
        self.assertNotIn("/", self.hits)


class NetworkRecognitionTests(unittest.TestCase):
    def test_known_network_is_recognized_with_one_establishment(self):
        db = DB(":memory:")
        try:
            db.add_businesses([{"source": "fixture", "source_id": "one", "name": "Garage One",
                                "website": "https://carrosserie.five-star.fr/garage/one?ville=Paris"}])
            self.assertIn("carrosserie.five-star.fr", db.network_sites())
            self.assertTrue(is_network_key("garage.top-garage.fr"))
            self.assertFalse(is_network_key("top-garage.fr.other.test"))
            self.assertEqual(canonical_url("https://reseau.test/garage?city=Paris&page=2#contact"),
                             "https://reseau.test/garage?city=Paris&page=2")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
