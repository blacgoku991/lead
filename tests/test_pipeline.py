"""Test de bout en bout sur un faux site local : collecte fichier -> crawl -> export CSV."""
import csv
import os
import tempfile
import unittest

from aiohttp import web

from autolead.crawler import Crawler, CrawlResult, run_crawl
from autolead.db import DB
from autolead.export import export_csv
from autolead.net import make_session
from autolead.sources.files import collect_file


def cf_encode(email: str, key: int = 0x2a) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


HOME = """<html><body><h1>Garage Test</h1>
<a href="/contact">Contact</a> <a href="/mentions-legales">Mentions légales</a>
<a href="/private/equipe">Notre équipe</a> <a href="/services">Services</a>
<footer>Tél. 01 23 45 67 89 — accueil@garage-test.fr</footer></body></html>"""
CONTACT = f"""<html><body>Écrivez à atelier [at] garage-test [dot] fr
<a href="/cdn-cgi/l/email-protection#{cf_encode('devis@garage-test.fr')}">[email&#160;protected]</a>
ou garage.test@gmail.com</body></html>"""
MENTIONS = "<html><body>SARL Garage Test, SIREN 123 456 789, 69003 Lyon</body></html>"
PRIVATE = "<html><body>secret@garage-test.fr</body></html>"


def make_app() -> web.Application:
    hits: list[str] = []

    def page(body: str, ctype: str = "text/html"):
        async def handler(request):
            hits.append(request.path)
            return web.Response(text=body, content_type=ctype)
        return handler

    app = web.Application()
    app["hits"] = hits
    app.router.add_get("/", page(HOME))
    app.router.add_get("/contact", page(CONTACT))
    app.router.add_get("/mentions-legales", page(MENTIONS))
    app.router.add_get("/private/equipe", page(PRIVATE))
    app.router.add_get("/robots.txt", page("User-agent: *\nDisallow: /private/\n", "text/plain"))
    return app


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = make_app()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = self.runner.addresses[0][1]
        self.base = f"http://127.0.0.1:{self.port}/"
        self.tmp = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def test_crawler_finds_emails_and_respects_robots(self):
        async with make_session(trust_env=False) as session:
            res = await Crawler(session).crawl(self.base, CrawlResult(), keep_text=True)
        self.assertEqual(res.status, "ok")
        self.assertEqual(set(res.emails), {"accueil@garage-test.fr", "atelier@garage-test.fr",
                                           "devis@garage-test.fr", "garage.test@gmail.com"})
        self.assertNotIn("/private/equipe", self.app["hits"])
        self.assertTrue(any("123 456 789" in t for t in res.texts))

    async def test_unreachable_site(self):
        async with make_session(trust_env=False, timeout=3) as session:
            res = await Crawler(session).crawl("http://127.0.0.1:1/", CrawlResult())
        self.assertEqual(res.status, "unreachable")

    async def test_end_to_end_csv(self):
        db_path = os.path.join(self.tmp.name, "t.db")
        list_path = os.path.join(self.tmp.name, "sites.csv")
        out_path = os.path.join(self.tmp.name, "out.csv")
        with open(list_path, "w", encoding="utf-8") as fh:
            fh.write(f"nom;site web;ville\nGarage Test;{self.base};Lyon\n")
        db = DB(db_path)
        try:
            self.assertEqual(collect_file(db, list_path, "garage"), 1)
            await run_crawl(db, concurrency=5, trust_env=False)
            self.assertEqual(db.pending_sites(), [])
            self.assertEqual(export_csv(db, out_path), 4)
            self.assertEqual(export_csv(db, out_path, pro_only=True), 3)
        finally:
            db.close()
        with open(out_path, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh, delimiter=";"))
        self.assertEqual({r["entreprise"] for r in rows}, {"Garage Test"})
        self.assertEqual({r["ville"] for r in rows}, {"Lyon"})
        devis = next(r for r in rows if r["email"] == "devis@garage-test.fr")
        self.assertEqual(devis["generique"], "oui")
        self.assertTrue(devis["page_source"].endswith("/contact"))

    async def test_guessed_site_is_verified_by_postcode_or_siren(self):
        db = DB(os.path.join(self.tmp.name, "g.db"))
        try:
            db.add_businesses([
                {"source": "sirene", "source_id": "1", "name": "GARAGE TEST", "postal_code": "69003",
                 "siren": "123456789", "category": "garage"},
                {"source": "sirene", "source_id": "2", "name": "GARAGE TEST", "postal_code": "75015",
                 "siren": "999999999", "category": "garage"},
            ])
            ids = {r["source_id"]: r["id"] for r in db.conn.execute("SELECT id, source_id FROM businesses")}
            key = f"127.0.0.1:{self.port}"
            db.add_guesses({key: [(ids["1"], ["69003", "123456789"]), (ids["2"], ["75015", "999999999"])]})
            await run_crawl(db, concurrency=2, trust_env=False)
            status = db.conn.execute("SELECT status FROM sites WHERE site_key=?", (key,)).fetchone()[0]
            linked = dict(db.conn.execute("SELECT source_id, site_key FROM businesses").fetchall())
            out = os.path.join(self.tmp.name, "g.csv")
            n = export_csv(db, out)
        finally:
            db.close()
        self.assertEqual(status, "ok")
        self.assertEqual(linked, {"1": key, "2": None})  # l'homonyme du 75015 n'est pas rattaché
        self.assertEqual(n, 4)

    async def test_guessed_site_rejected_without_match(self):
        db = DB(os.path.join(self.tmp.name, "r.db"))
        try:
            db.add_businesses([{"source": "sirene", "source_id": "1", "name": "GARAGE TEST",
                                "postal_code": "13001", "category": "garage"}])
            bid = db.conn.execute("SELECT id FROM businesses").fetchone()[0]
            key = f"127.0.0.1:{self.port}"
            db.add_guesses({key: [(bid, ["13001"])]})
            await run_crawl(db, concurrency=2, trust_env=False)
            status = db.conn.execute("SELECT status FROM sites").fetchone()[0]
            emails = db.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        finally:
            db.close()
        self.assertEqual((status, emails), ("unverified", 0))


if __name__ == "__main__":
    unittest.main()
