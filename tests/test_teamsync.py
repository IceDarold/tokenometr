import gzip
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chatsync  # noqa: E402
import teamsync  # noqa: E402
import usage  # noqa: E402
from test_usage import Workspace, call  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
ORG = "11111111-1111-4111-8111-111111111111"
PERSONAL = "22222222-2222-4222-8222-222222222222"
TOKEN = "tm_secret-token_1234567890abcdefghijklmnopqrstuvwxyz"
TEAM = {"id": "0b7e2c1a-3d4f-4e5a-8b6c-7d8e9f0a1b2c", "name": "Лаборатория"}
ADA = "5f0c6a52-6f64-4b8e-9a53-6c3c2f1d0a01"


def ms(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


class FakeSite:
    """The part of the Tokenometr site this program talks to, on a port of its own."""

    def __init__(self):
        self.devices = []  # what POST /api/devices/authorizations got
        self.polls = []  # the answers of /api/devices/token to give, in order; the last one repeats
        self.syncs = []  # (headers, body) of every POST /api/sync
        self.asked_team = []  # the query strings of GET /api/sync/team
        self.sync_answers = []  # an answer for each sync: a dict (200) or an int (that status); the last repeats
        self.shared, self.need_full, self.teams = [], [], []
        self.numbers = []  # what GET /api/sync/team answers
        self.max_body = None  # bodies longer than this (unpacked) get 413
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def error(self, status, code, message="x"):
                self.reply(status, {"error": {"code": code, "message": message, "request_id": "r"}})

            def body(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw, json.loads(raw) if raw else None

            def authorized(self):
                return self.headers.get("Authorization") == "Bearer " + TOKEN

            def do_POST(self):
                raw, body = self.body()
                if self.path == "/api/devices/authorizations":
                    site.devices.append(body)
                    return self.reply(200, {"device_code": "devicecode", "user_code": "ABC-DEF", "interval": 5,
                                            "verification_url": "http://site/app/connect?code=ABC-DEF",
                                            "expires_in": 600})
                if self.path == "/api/devices/token":
                    answer = site.polls.pop(0) if len(site.polls) > 1 else site.polls[0]
                    if answer == "granted":
                        return self.reply(200, {"token": TOKEN, "user": {"email": "ada@example.com",
                                                                         "display_name": "Ada"}})
                    return self.error(400, answer)
                if self.path == "/api/sync":
                    if not self.authorized():
                        return self.error(401, "unauthorized", "Not signed in")
                    if site.max_body is not None and len(raw) > site.max_body:
                        return self.error(413, "content_too_large")
                    site.syncs.append((dict(self.headers), body))
                    answer = site.sync_answers.pop(0) if len(site.sync_answers) > 1 else (
                        site.sync_answers[0] if site.sync_answers else None)
                    if isinstance(answer, int):
                        return self.error(answer, {426: "upgrade_required", 500: "internal_error"}.get(answer, "x"))
                    result = {"protocol": 1, "shared": site.shared, "needFull": site.need_full,
                              "teams": site.teams, "serverTime": 1}
                    result.update(answer or {})
                    return self.reply(200, result)
                self.error(404, "not_found")

            def do_GET(self):
                if self.path.startswith("/api/sync/team"):
                    if not self.authorized():
                        return self.error(401, "unauthorized", "Not signed in")
                    site.asked_team.append(self.path)
                    return self.reply(200, {"teams": site.numbers})
                self.error(404, "not_found")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class MemoryKeychain:
    def __init__(self):
        self.items = {}

    def get(self, account):
        return self.items.get(account)

    def set(self, account, secret):
        self.items[account] = secret

    def delete(self, account):
        self.items.pop(account, None)


def team_numbers(bank=True):
    """What GET /api/sync/team answers for a team with the shared account ORG."""
    return [{
        "generatedAt": NOW.timestamp(), "team": TEAM,
        "members": [{"id": ADA, "name": "Ada", "avatar": None, "you": True},
                    {"id": "7a1d3c52-1f64-4b8e-9a53-6c3c2f1d0b02", "name": "Bob", "avatar": None, "you": False}],
        "accounts": [{
            "org": ORG, "name": "Shared Claude", "sharedBy": ADA,
            "bank": {"total": 12e6, "remaining": 7.9e6, "usedPercent": 34, "perPercent": 120000,
                     "readingAt": NOW.timestamp() - 3600, "readingPercent": 31, "weekStart": 1, "resetsAt": 2,
                     "outsidePercent": 4, "outsideTokens": 480000,
                     "byMember": [{"member": ADA, "tokens": 1.2e6, "percent": 10}]} if bank else None,
            "periods": {name: {"since": 1, "total": 2e6, "hidden": 0,
                               "byMember": [{"member": ADA, "main": 1e6, "sub": 2e5}],
                               "chats": [{"key": "local_a", "member": ADA, "title": "Чат А", "folder": "demo",
                                          "main": 1e6, "sub": 2e5, "subagents": 1, "archived": False,
                                          "last": NOW.timestamp() - 60, "active": True},
                                         {"key": "local_b", "member": "7a1d3c52-1f64-4b8e-9a53-6c3c2f1d0b02",
                                          "title": "Чат Боба", "folder": "", "main": 8e5, "sub": 0,
                                          "subagents": 0, "archived": False, "last": NOW.timestamp() - 7200,
                                          "active": False}]}
                        for name in ("5h", "today", "week", "all")}}]}]


class Base(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.ws = Workspace()
        self.home = os.path.join(self.ws.root, "home")
        self.paths = teamsync.Paths(self.home, self.ws.projects, self.ws.sessions,
                                    os.path.join(self.ws.root, "history.json"), self.ws.cache)
        self.keychain = MemoryKeychain()
        self.site = FakeSite()

    def tearDown(self):
        self.site.close()
        self.ws.cleanup()
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def readings(self, *samples):
        """The Claude app's readings: (iso time, account, weekly percent)."""
        with open(self.paths.history, "w", encoding="utf-8") as f:
            json.dump({"samples": [{"t": ms(t), "org": org, "u": {"sd": percent}} for t, org, percent in samples]}, f)

    def chat(self, sid, cli, title, lines, **kwargs):
        self.ws.session(sid, cli, title, **kwargs)
        self.ws.transcript(cli, lines)

    def connect(self, **kwargs):
        events = []
        self.site.polls = ["granted"]
        code = teamsync.connect(self.paths, name="Mac of Ada", server=self.site.url, keychain=self.keychain,
                                emit=events.append, sleep=lambda s: None, **kwargs)
        return code, events

    def sync(self, now=NOW):
        return teamsync.sync(self.paths, now=now, keychain=self.keychain, server=self.site.url)

    def connected(self):
        """A Mac that is connected, with the site telling ORG is shared and wanting it whole."""
        self.site.shared, self.site.need_full, self.site.teams = [ORG], [ORG], [TEAM]
        self.site.sync_answers = [{"shared": [ORG], "needFull": [ORG]}, {"shared": [ORG], "needFull": []}]
        self.connect()
        self.site.syncs.clear()
        self.site.sync_answers = [{"shared": [ORG], "needFull": []}]

    def sent_usage(self, index=-1):
        return self.site.syncs[index][1]["usage"]

    def snapshot(self, now=NOW):
        """The window's numbers, with what the team told."""
        return usage.build_snapshot(self.ws.projects, self.ws.sessions, now, history_path=self.paths.history,
                                    readings_path=self.paths.readings, team_path=self.paths.view)


class ConnectTest(Base):
    def test_it_shows_the_code_then_connects_when_a_person_confirms_it(self):
        self.site.polls = ["authorization_pending", "authorization_pending", "granted"]
        events = []

        code = teamsync.connect(self.paths, name="Mac of Ada", server=self.site.url, keychain=self.keychain,
                                emit=events.append, sleep=lambda s: None)

        self.assertEqual(code, 0)
        self.assertEqual(self.site.devices, [{"client_name": "Mac of Ada"}])
        self.assertEqual([e["state"] for e in events], ["waiting", "connected"])
        self.assertEqual((events[0]["code"], events[0]["url"]), ("ABC-DEF", "http://site/app/connect?code=ABC-DEF"))
        self.assertEqual(events[1]["user"], {"email": "ada@example.com", "name": "Ada"})

    def test_the_token_goes_to_the_keychain_and_not_to_a_file(self):
        self.connect()

        host = self.site.url.split("//")[1]
        self.assertEqual(self.keychain.items, {host: TOKEN})
        for name in os.listdir(self.home):
            with open(os.path.join(self.home, name), encoding="utf-8") as f:
                self.assertNotIn(TOKEN, f.read(), name)

    def test_this_mac_keeps_its_id_when_it_connects_again(self):
        self.connect()
        first = teamsync.load_state(self.paths)["machine"]["id"]
        teamsync.disconnect(self.paths, keychain=self.keychain)

        self.connect()

        self.assertEqual(teamsync.load_state(self.paths)["machine"]["id"], first)

    def test_slow_down_makes_it_wait_longer(self):
        self.site.polls = ["slow_down", "granted"]
        waits = []

        teamsync.connect(self.paths, name="x", server=self.site.url, keychain=self.keychain,
                         emit=lambda e: None, sleep=waits.append)

        self.assertEqual(waits, [5, 10])

    def test_a_request_that_ran_out_is_told(self):
        self.site.polls = ["expired_token"]
        events = []

        code = teamsync.connect(self.paths, name="x", server=self.site.url, keychain=self.keychain,
                                emit=events.append, sleep=lambda s: None)

        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["state"], "expired")
        self.assertEqual(self.keychain.items, {})
        self.assertFalse(teamsync.load_state(self.paths).get("connected"))

    def test_a_site_that_does_not_answer_is_told(self):
        events = []

        code = teamsync.connect(self.paths, name="x", server="http://127.0.0.1:9", keychain=self.keychain,
                                emit=events.append, sleep=lambda s: None)

        self.assertEqual((code, events[-1]["state"]), (1, "error"))

    def test_connecting_asks_for_the_numbers_of_the_teams(self):
        self.site.shared, self.site.need_full, self.site.teams = [ORG], [], [TEAM]
        self.site.numbers = team_numbers()

        code, events = self.connect()

        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["teams"], [TEAM])
        self.assertEqual(teamsync.status(self.paths)["teams"][0]["name"], "Лаборатория")


class WhatIsSentTest(Base):
    def setUp(self):
        super().setUp()
        self.readings(("2026-10-07T08:00:00Z", ORG, 10), ("2026-10-07T09:30:00Z", PERSONAL, 5))
        self.chat("local_a", "cli-a", "Чат А", [call("m1", "2026-10-07T09:00:00.000Z", out=100)],
                  cwd="/Users/me/Projects/secret-project")
        self.chat("local_p", "cli-p", "Личный чат", [call("m2", "2026-10-07T10:00:00.000Z", out=200)],
                  cwd="/Users/me/Projects/personal-diary")

    def test_the_first_exchange_tells_only_which_accounts_this_mac_has_seen(self):
        self.site.shared, self.site.need_full, self.site.teams = [], [], []
        self.site.sync_answers = [{"shared": [], "needFull": []}]
        self.connect(sync_after=False)

        self.sync()

        body = self.site.syncs[0][1]
        self.assertEqual({a["org"] for a in body["accounts"]}, {ORG, PERSONAL})
        self.assertEqual((body["usage"], body["chats"], body["readings"], body["full"]), ([], [], [], []))

    def test_only_the_shared_account_leaves_and_in_a_whole_send(self):
        self.connected()
        self.site.sync_answers = [{"shared": [ORG], "needFull": [ORG]}, {"shared": [ORG], "needFull": []}]
        self.sync()

        body = self.site.syncs[-1][1]
        self.assertEqual(body["full"], [ORG])
        self.assertEqual([(u["chat"], u["org"], u["kind"], u["tokens"]) for u in body["usage"]],
                         [("local_a", ORG, "main", [0, 0, 0, 100])])
        self.assertEqual([c["key"] for c in body["chats"]], ["local_a"])

    def test_what_belongs_to_a_personal_account_is_nowhere_in_what_is_sent(self):
        self.connected()
        self.site.sync_answers = [{"shared": [ORG], "needFull": [ORG]}]
        self.sync()

        everything = json.dumps([body for _, body in self.site.syncs], ensure_ascii=False)

        for private in ("Личный чат", "local_p", "cli-p", "personal-diary", "/Users/me", "cli-a"):
            self.assertNotIn(private, everything)
        self.assertIn("Чат А", everything)

    def test_a_chat_is_told_by_its_title_and_the_name_of_its_folder_only(self):
        self.connected()
        self.site.sync_answers = [{"shared": [ORG], "needFull": [ORG]}]
        self.sync()

        chat = self.site.syncs[-1][1]["chats"][0]

        self.assertEqual({k: chat[k] for k in ("key", "title", "folder", "archived", "subagents")},
                         {"key": "local_a", "title": "Чат А", "folder": "secret-project", "archived": False,
                          "subagents": 0})
        self.assertEqual(set(chat), {"key", "title", "folder", "archived", "subagents", "lastActivity"})

    def test_calls_fall_into_buckets_of_five_minutes_by_kind(self):
        self.chat("local_a", "cli-a", "Чат А", [
            call("m1", "2026-10-07T09:01:00.000Z", inp=1, write=2, read=3, out=4),
            call("m2", "2026-10-07T09:04:59.000Z", out=10),
            call("m3", "2026-10-07T09:05:00.000Z", out=20)])
        self.ws.transcript("cli-a", [call("s1", "2026-10-07T09:02:00.000Z", out=7)], subagent="x")
        self.connected()
        self.site.sync_answers = [{"shared": [ORG], "needFull": [ORG]}]
        self.sync()

        rows = {(u["t"], u["kind"]): (u["tokens"], u["calls"]) for u in self.site.syncs[-1][1]["usage"]}

        nine = int(datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(rows, {(nine, "main"): ([1, 2, 3, 14], 2), (nine + 300, "main"): ([0, 0, 0, 20], 1),
                                (nine, "sub"): ([0, 0, 0, 7], 1)})

    def test_the_readings_of_the_shared_account_go_with_where_the_week_ends(self):
        self.connected()
        with open(self.paths.readings, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "samples": [
                {"t": ms("2026-10-07T11:00:00Z"), "org": ORG, "source": "cli", "u": {"fh": 12, "sd": 40},
                 "weekResetsAt": 1791000000},
                {"t": ms("2026-10-07T11:00:00Z"), "org": PERSONAL, "source": "cli", "u": {"sd": 1}}]}, f)
        self.site.sync_answers = [{"shared": [ORG], "needFull": [ORG]}]
        self.sync()

        readings = self.site.syncs[-1][1]["readings"]

        self.assertIn({"org": ORG, "t": NOW.timestamp() - 3600, "source": "cli", "week": 40, "session": 12,
                       "resetsAt": 1791000000}, readings)
        self.assertEqual({r["org"] for r in readings}, {ORG})

    def test_after_a_whole_send_only_what_changed_goes(self):
        self.connected()
        self.site.sync_answers = [{"shared": [ORG], "needFull": [ORG]}, {"shared": [ORG], "needFull": []}]
        self.sync()
        self.site.syncs.clear()

        self.sync()
        nothing = self.site.syncs[-1][1]
        self.chat("local_a", "cli-a", "Чат А", [call("m1", "2026-10-07T09:00:00.000Z", out=100),
                                                 call("m9", "2026-10-07T09:20:00.000Z", out=50)])
        self.sync()
        more = self.site.syncs[-1][1]

        self.assertEqual((nothing["usage"], nothing["full"]), ([], []))
        self.assertEqual([u["tokens"] for u in more["usage"]], [[0, 0, 0, 100], [0, 0, 0, 50]])
        self.assertEqual(more["full"], [])

    def test_it_sends_again_in_full_when_the_site_asks_for_it(self):
        self.connected()
        self.site.sync_answers = [{"shared": [ORG], "needFull": [ORG]}, {"shared": [ORG], "needFull": []}]
        self.sync()
        self.site.sync_answers = [{"shared": [ORG], "needFull": [ORG]}, {"shared": [ORG], "needFull": []}]
        self.site.syncs.clear()

        self.sync()

        self.assertEqual([body["full"] for _, body in self.site.syncs], [[], [ORG]])
        self.assertEqual(len(self.site.syncs[-1][1]["usage"]), 1)

    def test_a_whole_send_comes_by_itself_once_in_a_while(self):
        self.connected()
        self.site.sync_answers = [{"shared": [ORG], "needFull": [ORG]}, {"shared": [ORG], "needFull": []}]
        self.sync()
        self.site.syncs.clear()
        self.site.sync_answers = [{"shared": [ORG], "needFull": []}]

        self.sync(NOW.replace(day=15))

        self.assertEqual(self.site.syncs[-1][1]["full"], [ORG])


class BigSendTest(Base):
    def test_a_big_body_is_packed_and_a_too_big_one_comes_in_parts_of_a_month(self):
        lines = [call(f"m{i}", datetime.fromtimestamp(NOW.timestamp() - i * 3600 * 24 * 3, timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%S.000Z"), out=i + 1) for i in range(40)]
        self.chat("local_a", "cli-a", "Чат А", lines)
        self.readings(("2026-05-01T08:00:00Z", ORG, 10))
        self.connected()
        self.site.max_body = 2000
        self.site.sync_answers = [{"shared": [ORG], "needFull": [ORG]}, {"shared": [ORG], "needFull": []}]

        view = self.sync()

        parts = [body for _, body in self.site.syncs if body["usage"]]
        self.assertGreater(len(parts), 1)
        self.assertEqual([body["full"] for body in parts], [[ORG]] + [[]] * (len(parts) - 1))
        self.assertEqual(sum(len(body["usage"]) for body in parts), 40)
        self.assertEqual(view["state"], "ok")

    def test_a_packed_body_says_so(self):
        lines = [call(f"m{i}", datetime.fromtimestamp(NOW.timestamp() - i * 300, timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%S.000Z"), out=i + 1) for i in range(600)]
        self.chat("local_a", "cli-a", "Чат А", lines)
        self.readings(("2026-10-01T08:00:00Z", ORG, 10))
        self.connected()
        self.site.sync_answers = [{"shared": [ORG], "needFull": [ORG]}]

        self.sync()

        self.assertEqual(self.site.syncs[-1][0].get("Content-Encoding"), "gzip")
        self.assertEqual(len(self.sent_usage()), 600)


class TheNumbersTest(Base):
    def test_they_are_fetched_in_the_zone_of_this_mac_and_shown_in_the_window(self):
        self.readings(("2026-10-07T08:00:00Z", ORG, 10))
        self.connected()
        self.site.numbers = team_numbers()

        view = self.sync()
        snapshot = self.snapshot()

        self.assertEqual(view["state"], "ok")
        self.assertTrue(self.site.asked_team[-1].startswith("/api/sync/team?tz="))
        account = next(a for a in snapshot["accounts"] if a["id"] == ORG)
        self.assertEqual(account["name"], "Shared Claude")
        self.assertEqual(account["bank"]["remaining"], 7.9e6)
        self.assertEqual(account["bank"]["offMacPercent"], 4)
        self.assertEqual([r["title"] for r in account["periods"]["week"]["rows"]], ["Чат А", "Чат Боба"])
        self.assertEqual([(r["memberName"], r["you"]) for r in account["periods"]["week"]["rows"]],
                         [("Ada", True), ("Bob", False)])
        self.assertEqual(account["team"]["teamName"], "Лаборатория")
        self.assertEqual(snapshot["team"]["state"], "ok")
        self.assertEqual([t["name"] for t in snapshot["team"]["teams"]], ["Лаборатория"])

    def test_numbers_older_than_a_quarter_of_an_hour_are_marked(self):
        self.readings(("2026-10-07T08:00:00Z", ORG, 10))
        self.connected()
        self.site.numbers = team_numbers()
        self.sync()

        fresh = self.snapshot()
        later = self.snapshot(NOW.replace(hour=12, minute=20))

        self.assertFalse(fresh["team"]["stale"])
        self.assertTrue(later["team"]["stale"])
        self.assertEqual(later["team"]["dataAt"], NOW.timestamp())

    def test_an_account_the_team_does_not_count_stays_as_this_mac_counted_it(self):
        self.readings(("2026-10-07T08:00:00Z", ORG, 10), ("2026-10-07T09:30:00Z", PERSONAL, 5))
        self.chat("local_p", "cli-p", "Личный чат", [call("m2", "2026-10-07T10:00:00.000Z", out=200)])
        self.connected()
        self.site.numbers = team_numbers()
        self.sync()

        snapshot = self.snapshot()

        personal = next(a for a in snapshot["accounts"] if a["id"] == PERSONAL)
        self.assertNotIn("team", personal)
        self.assertEqual([r["title"] for r in personal["periods"]["all"]["rows"]], ["Личный чат"])

    def test_without_a_connection_the_window_is_as_it_was(self):
        self.readings(("2026-10-07T08:00:00Z", ORG, 10))

        snapshot = self.snapshot()

        self.assertIsNone(snapshot["team"])


class ProblemsTest(Base):
    def test_a_token_the_site_refuses_ends_the_connection_and_says_so(self):
        self.readings(("2026-10-07T08:00:00Z", ORG, 10))
        self.connected()
        self.keychain.items[self.site.url.split("//")[1]] = "revoked-token"

        view = self.sync()

        self.assertEqual((view["state"], view["message"]), ("revoked", "Mac отключён от команды"))
        self.assertEqual(self.keychain.items, {})
        self.assertFalse(teamsync.load_state(self.paths)["connected"])
        # It does not keep asking.
        asked = len(self.site.syncs)
        self.sync()
        self.assertEqual(len(self.site.syncs), asked)

    def test_a_token_that_is_gone_from_the_keychain_is_the_same(self):
        self.connected()
        self.keychain.items.clear()

        view = self.sync()

        self.assertEqual(view["state"], "revoked")

    def test_an_app_older_than_the_protocol_is_asked_to_update(self):
        self.connected()
        self.site.sync_answers = [426]

        view = self.sync()

        self.assertEqual((view["state"], view["message"]), ("update", "Обновите Токенометр"))
        self.assertTrue(teamsync.load_state(self.paths)["connected"])

    def test_a_site_that_fails_keeps_the_last_numbers_and_the_connection(self):
        self.readings(("2026-10-07T08:00:00Z", ORG, 10))
        self.connected()
        self.site.numbers = team_numbers()
        self.sync()
        self.site.sync_answers = [500]

        view = self.sync(NOW.replace(minute=5))

        self.assertEqual(view["state"], "error")
        self.assertEqual(view["message"], "Сайт ответил ошибкой 500")
        self.assertEqual(view["fetchedAt"], NOW.timestamp())
        self.assertEqual(len(view["teams"]), 1)
        self.assertEqual(view["syncedAt"], NOW.timestamp())

    def test_no_network_is_a_state_not_a_crash(self):
        self.connected()
        state = teamsync.load_state(self.paths)
        state["server"] = "http://127.0.0.1:9"
        teamsync.save_state(self.paths, state)

        view = teamsync.sync(self.paths, now=NOW, keychain=self.keychain, server="http://127.0.0.1:9")

        self.assertEqual((view["state"], view["message"]), ("error", "Нет связи с сайтом"))

    def test_a_mac_that_is_not_connected_does_nothing(self):
        view = self.sync()

        self.assertEqual(view, {"version": 1, "connected": False})
        self.assertEqual(self.site.syncs, [])

    def test_disconnecting_forgets_the_token_and_the_numbers(self):
        self.connected()
        self.site.numbers = team_numbers()
        self.sync()

        teamsync.disconnect(self.paths, keychain=self.keychain)

        self.assertEqual(self.keychain.items, {})
        self.assertEqual(usage.load_team(self.paths.view), {"version": 1, "connected": False})
        self.assertFalse(teamsync.status(self.paths)["connected"])


class AccountsSeenTest(Base):
    def test_a_name_from_accounts_json_comes_first_then_the_email_the_cli_showed_then_the_id(self):
        self.readings(("2026-10-07T08:00:00Z", ORG, 10), ("2026-10-07T09:00:00Z", PERSONAL, 5),
                      ("2026-10-07T10:00:00Z", "33333333-3333-4333-8333-333333333333", 5))
        os.makedirs(self.home)
        with open(self.paths.accounts, "w", encoding="utf-8") as f:
            json.dump({"accounts": {ORG: {"name": "Работа"}}}, f, ensure_ascii=False)
        usage.remember_label(self.paths.labels, ORG, "ignored@example.com")
        usage.remember_label(self.paths.labels, PERSONAL, "me@example.com")

        seen = teamsync.seen_accounts(self.paths, usage.collect(self.ws.projects, self.ws.sessions,
                                                                None, self.paths.history)[2], {})

        self.assertEqual({a["org"]: a["label"] for a in seen},
                         {ORG: "Работа", PERSONAL: "me@example.com",
                          "33333333-3333-4333-8333-333333333333": "Аккаунт 33333333"})


class TheBackgroundRunTest(Base):
    """chatsync.py --watch sends the team's usage beside the chat lists."""

    def chat_paths(self):
        return chatsync.Paths(os.path.join(self.ws.root, "claude"), self.home)

    def test_it_syncs_with_the_folders_of_this_mac_and_reports_nothing_when_all_is_well(self):
        asked, problems = [], []

        chatsync.team_pass(self.chat_paths(), problems.append,
                           sync=lambda paths: asked.append(paths) or {"state": "ok", "message": None})

        self.assertEqual(problems, [None])
        paths = asked[0]
        self.assertEqual(paths.home, self.home)
        self.assertEqual(paths.sessions, os.path.join(self.ws.root, "claude", "claude-code-sessions"))
        self.assertEqual(paths.history, os.path.join(self.ws.root, "claude", "plan-usage-history.json"))

    def test_it_reports_the_problem_of_the_connection(self):
        problems = []

        chatsync.team_pass(self.chat_paths(), problems.append,
                           sync=lambda paths: {"state": "error", "message": "Нет связи с сайтом"})

        self.assertEqual(problems, ["Нет связи с сайтом"])

    def test_it_never_raises_whatever_happens_to_it(self):
        problems = []

        def broken(paths):
            raise RuntimeError("boom")

        chatsync.team_pass(self.chat_paths(), problems.append, sync=broken)

        self.assertEqual(problems, ["RuntimeError: boom"])


class KeychainTest(unittest.TestCase):
    def test_the_secret_goes_through_the_standard_input_never_the_arguments(self):
        folder = tempfile.mkdtemp()
        try:
            tool = os.path.join(folder, "security")
            with open(tool, "w") as f:
                f.write('#!/bin/sh\necho "$@" > "%s/args"\ncat > "%s/input"\n' % (folder, folder))
            os.chmod(tool, os.stat(tool).st_mode | stat.S_IEXEC)

            teamsync.Keychain(tool=tool).set("tokenometr.archik.tech", TOKEN)

            with open(os.path.join(folder, "args")) as f:
                self.assertEqual(f.read().strip(), "-i")
            with open(os.path.join(folder, "input")) as f:
                text = f.read()
            self.assertIn(TOKEN, text)
            self.assertTrue(text.startswith("add-generic-password -U -s"))
        finally:
            shutil.rmtree(folder)

    def test_a_secret_is_read_back_with_the_standard_output(self):
        folder = tempfile.mkdtemp()
        try:
            tool = os.path.join(folder, "security")
            with open(tool, "w") as f:
                f.write('#!/bin/sh\necho "%s"\n' % TOKEN)
            os.chmod(tool, os.stat(tool).st_mode | stat.S_IEXEC)

            self.assertEqual(teamsync.Keychain(tool=tool).get("a"), TOKEN)
        finally:
            shutil.rmtree(folder)

    def test_a_missing_item_is_none(self):
        folder = tempfile.mkdtemp()
        try:
            tool = os.path.join(folder, "security")
            with open(tool, "w") as f:
                f.write("#!/bin/sh\nexit 44\n")
            os.chmod(tool, os.stat(tool).st_mode | stat.S_IEXEC)

            self.assertIsNone(teamsync.Keychain(tool=tool).get("a"))
        finally:
            shutil.rmtree(folder)


class ZoneTest(unittest.TestCase):
    def test_the_zone_comes_from_the_link_of_localtime_or_from_tz(self):
        old = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Europe/Moscow"
            zone = teamsync.local_zone()
            self.assertTrue(zone == "Europe/Moscow" or "/" in zone or zone == "UTC")
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old


if __name__ == "__main__":
    unittest.main()
