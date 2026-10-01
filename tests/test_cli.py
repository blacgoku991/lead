"""Contrats CLI hors réseau : ordre des sources, limites, erreurs et réparation."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from autolead import cli


class SessionContext:
    def __init__(self):
        self.session = object()
        self.closed = False

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        self.closed = True


class CLIParserTests(unittest.TestCase):
    def test_public_sources_require_explicit_selection(self):
        parser = cli.build_parser()
        with mock.patch.dict("os.environ", {}, clear=True):
            auto = parser.parse_args(["collect"])
            self.assertEqual(cli.resolve_sources(auto), {"osm", "sirene"})
            explicit = parser.parse_args(["collect", "--sources", "dgccrf,dinum"])
            self.assertEqual(cli.resolve_sources(explicit), {"dgccrf", "dinum"})
            pipeline = parser.parse_args(["run", "--sources", "dgccrf,dinum"])
            self.assertEqual(cli.resolve_sources(pipeline), {"dgccrf", "dinum"})

    def test_nonpositive_and_nonfinite_budgets_are_rejected_before_database_open(self):
        cases = [
            ["crawl", "--concurrency", "0"],
            ["crawl", "--max-pages", "0"],
            ["crawl", "--deep-pages", "-1"],
            ["crawl", "--timeout", "0"],
            ["crawl", "--timeout", "nan"],
            ["crawl", "--timeout", "inf"],
            ["crawl", "--site-timeout", "-2"],
            ["crawl", "--site-timeout", "NaN"],
            ["crawl", "--limit", "0"],
            ["guess", "--limit", "-1"],
            ["guess", "--dns-concurrency", "0"],
            ["verify", "--dns-concurrency", "-1"],
            ["collect", "--sources", "dinum", "--source-limit", "0"],
            ["collect", "--search-pages", "0"],
            ["collect", "--search-qps", "inf"],
            ["collect", "--gmaps-pages", "0"],
            ["collect", "--gmaps-qps", "nan"],
            ["collect", "--sirene-rate", "-1"],
        ]
        for argv in cases:
            with self.subTest(argv=argv), mock.patch.object(cli, "DB") as database:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    cli.main(argv)
                self.assertEqual(error.exception.code, 2)
                database.assert_not_called()

    def test_incompatible_or_misspelled_source_options_fail_instead_of_being_ignored(self):
        cases = [
            ["collect", "--sources", "dgcffr"],
            ["collect", "--sources", ""],
            ["collect", "--sources", "auto,dinum"],
            ["collect", "--dgccrf-file", "centres.csv"],
            ["collect", "--dinum-file", "domaines.csv"],
            ["collect", "--sources", "osm", "--source-limit", "2"],
            ["collect", "--sources", "file"],
            ["collect", "--sources", "dgccrf", "--country", "CH"],
            ["collect", "--gmaps-pages", "4"],
            ["collect", "--sirene-rate", "8"],
            ["export", "--telephones", "--attributed-only"],
            ["export", "--sep", "||"],
        ]
        for argv in cases:
            with self.subTest(argv=argv), mock.patch.object(cli, "DB") as database:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    cli.main(argv)
                self.assertEqual(error.exception.code, 2)
                database.assert_not_called()

    def test_attribution_filter_is_forwarded_independently_of_email_domain_filter(self):
        args = cli.build_parser().parse_args([
            "export", "--attributed-only", "--no-dedupe", "--categories", "controle_technique",
            "-o", "contacts.csv",
        ])
        database = object()
        with mock.patch.object(cli, "export_csv") as export_csv:
            cli.export(database, args)
        export_csv.assert_called_once_with(
            database, "contacts.csv", pro_only=False, mx_only=False,
            categories=["controle_technique"], dedupe=False, sep=";", attributed_only=True,
        )

    def test_repair_command_reports_backup_and_queue_without_starting_a_crawl(self):
        database = mock.MagicMock()
        database.repair_contacts.return_value = {
            "backup": "/tmp/leads.before-repair.db", "legacy_guesses_reset": 2,
            "network_sites_requeued": 1, "source_pages_requeued": 3,
        }
        database.stats.return_value = {"contacts par rattachement": {"unverified": 2}}
        with mock.patch.object(cli, "DB", return_value=database) as factory, \
             mock.patch.object(cli, "run_crawl") as crawler, \
             mock.patch.object(cli, "log") as log:
            cli.main(["repair-contacts", "--db", "garage.db"])
        factory.assert_called_once_with("garage.db")
        database.repair_contacts.assert_called_once_with()
        database.close.assert_called_once_with()
        crawler.assert_not_called()
        self.assertTrue(any("/tmp/leads.before-repair.db" in str(call) for call in log.call_args_list))
        self.assertTrue(any("source_pages_requeued : 3" in str(call) for call in log.call_args_list))

    def test_public_source_error_sets_failure_exit_status_and_closes_database(self):
        database = mock.MagicMock()
        with mock.patch.object(cli, "DB", return_value=database), \
             mock.patch.object(cli, "collect", new=mock.AsyncMock(
                 side_effect=cli.PublicDataError("schéma de source incompatible"))), \
             mock.patch.object(cli, "log"):
            with self.assertRaises(SystemExit) as error:
                cli.main(["collect", "--sources", "dgccrf"])
        self.assertEqual(error.exception.code, 1)
        database.close.assert_called_once_with()
        database.stats.assert_not_called()

    def test_ctrl_c_sets_interruption_exit_status(self):
        database = mock.MagicMock()

        def interrupted(coroutine):
            coroutine.close()
            raise KeyboardInterrupt

        with mock.patch.object(cli, "DB", return_value=database), \
             mock.patch.object(cli, "_run", side_effect=interrupted), \
             mock.patch.object(cli, "log"):
            with self.assertRaises(SystemExit) as error:
                cli.main(["crawl"])
        self.assertEqual(error.exception.code, 130)
        database.close.assert_called_once_with()


class CLICollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_dinum_runs_after_all_sources_including_local_file(self):
        args = cli.build_parser().parse_args([
            "collect", "--sources", "sirene,dgccrf,dinum", "--file", "sites.csv",
            "--file-category", "garage", "--depts", "75,92", "--source-limit", "123",
            "--dgccrf-file", "centres.csv", "--dinum-file", "domaines.csv",
        ])
        database = object()
        session_context = SessionContext()
        known_sirets = set()
        events = []

        async def sirene(*args, **kwargs):
            await asyncio.sleep(0)
            known_sirets.add("sirene")
            events.append("sirene")

        async def dgccrf(*args, **kwargs):
            await asyncio.sleep(0)
            known_sirets.add("dgccrf")
            events.append("dgccrf")
            return 1

        def local_file(*args, **kwargs):
            known_sirets.add("file")
            events.append("file")
            return 1

        async def dinum(*args, **kwargs):
            self.assertEqual(known_sirets, {"sirene", "dgccrf", "file"})
            events.append("dinum")
            return {"matched": 3}

        with mock.patch.object(cli, "make_session", return_value=session_context), \
             mock.patch.object(cli, "collect_sirene", new=mock.AsyncMock(side_effect=sirene)), \
             mock.patch.object(cli, "collect_dgccrf", new=mock.AsyncMock(side_effect=dgccrf)) as ct, \
             mock.patch.object(cli, "collect_file", side_effect=local_file) as file_import, \
             mock.patch.object(cli, "enrich_dinum", new=mock.AsyncMock(side_effect=dinum)) as enrich, \
             mock.patch.object(cli, "log"):
            await cli.collect(database, args)
        self.assertEqual(events[-2:], ["file", "dinum"])
        ct.assert_awaited_once_with(database, session_context.session, departements=["75", "92"],
                                    categories=list(cli.CATEGORIES), limit=123, path="centres.csv")
        file_import.assert_called_once_with(database, "sites.csv", "garage")
        enrich.assert_awaited_once_with(database, session_context.session, path="domaines.csv", limit=123)
        self.assertTrue(session_context.closed)

    async def test_dinum_only_does_not_import_unselected_sources(self):
        args = cli.build_parser().parse_args(["collect", "--sources", "dinum"])
        session_context = SessionContext()
        database = object()
        with mock.patch.object(cli, "make_session", return_value=session_context), \
             mock.patch.object(cli, "collect_dgccrf", new=mock.AsyncMock()) as ct, \
             mock.patch.object(cli, "collect_sirene", new=mock.AsyncMock()) as sirene, \
             mock.patch.object(cli, "collect_osm", new=mock.AsyncMock()) as osm, \
             mock.patch.object(cli, "enrich_dinum", new=mock.AsyncMock()) as enrich, \
             mock.patch.object(cli, "log"):
            await cli.collect(database, args)
        ct.assert_not_awaited()
        sirene.assert_not_awaited()
        osm.assert_not_awaited()
        enrich.assert_awaited_once_with(database, session_context.session, path=None, limit=None)

    async def test_failed_source_cancels_siblings_before_session_close_and_skips_enrichment(self):
        args = cli.build_parser().parse_args(["collect", "--sources", "sirene,dgccrf,dinum"])
        session_context = SessionContext()
        sirene_started = asyncio.Event()
        sirene_cancelled = asyncio.Event()

        async def sirene(*args, **kwargs):
            sirene_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.assertFalse(session_context.closed)
                sirene_cancelled.set()

        async def dgccrf(*args, **kwargs):
            await sirene_started.wait()
            raise cli.PublicDataError("source DGCCRF indisponible")

        with mock.patch.object(cli, "make_session", return_value=session_context), \
             mock.patch.object(cli, "collect_sirene", new=mock.AsyncMock(side_effect=sirene)), \
             mock.patch.object(cli, "collect_dgccrf", new=mock.AsyncMock(side_effect=dgccrf)), \
             mock.patch.object(cli, "enrich_dinum", new=mock.AsyncMock()) as enrich, \
             mock.patch.object(cli, "log"):
            with self.assertRaises(cli.PublicDataError):
                await asyncio.wait_for(cli.collect(object(), args), timeout=2)
        self.assertTrue(sirene_cancelled.is_set())
        self.assertTrue(session_context.closed)
        enrich.assert_not_awaited()


class CLILocalIntegrationTests(unittest.TestCase):
    def test_local_dgccrf_then_dinum_import_is_ordered_and_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            db_path = str(base / "contacts.db")
            dgccrf_path = base / "centres.json"
            dinum_path = base / "domaines.csv"
            dgccrf_path.write_text(json.dumps([{
                "cct_siret": "12345678900011", "cct_denomination": "Centre contrôle Alpha",
                "cct_adresse": "12 rue du Centre", "cct_code_postal": "92600",
                "cct_commune": "Asnières-sur-Seine", "cct_tel": "01 23 45 67 89",
                "cct_url": "https://controle-alpha.fr/centres/alpha?id=10",
            }]), encoding="utf-8")
            dinum_path.write_text(
                "SIRET;domain_email;data_source\n12345678900011;atelier-alpha.fr;trackdechets\n",
                encoding="utf-8",
            )
            argv = ["collect", "--db", db_path, "--sources", "dgccrf,dinum",
                    "--dgccrf-file", str(dgccrf_path), "--dinum-file", str(dinum_path),
                    "--depts", "92", "--categories", "controle_technique"]
            with mock.patch.object(cli, "make_session", side_effect=lambda **kwargs: SessionContext()), \
                 mock.patch.object(cli, "log"), mock.patch("autolead.sources.public_data.log"):
                cli.main(argv)
                cli.main(argv)
            database = cli.DB(db_path)
            try:
                businesses = database.conn.execute("SELECT * FROM businesses").fetchall()
                self.assertEqual(len(businesses), 1)
                business = businesses[0]
                self.assertEqual(business["siret"], "12345678900011")
                self.assertEqual(business["category"], "controle_technique")
                self.assertEqual(business["website"], "https://controle-alpha.fr/centres/alpha?id=10")
                candidates = database.conn.execute(
                    "SELECT business_id,domain FROM domain_candidates").fetchall()
                self.assertEqual([tuple(row) for row in candidates], [(business["id"], "atelier-alpha.fr")])
                self.assertEqual(database.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0], 0)
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
