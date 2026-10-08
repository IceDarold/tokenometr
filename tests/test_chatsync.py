import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chatsync  # noqa: E402

A, ORG_A = "aaaaaaaa-0000-4000-8000-000000000001", "11111111-0000-4000-8000-000000000001"
B, ORG_B = "bbbbbbbb-0000-4000-8000-000000000002", "22222222-0000-4000-8000-000000000002"
C, ORG_C = "cccccccc-0000-4000-8000-000000000003", "33333333-0000-4000-8000-000000000003"
NOW = 1791400000.0
CLAUDE = 4242  # PID of the running Claude app
NOTHING = {"copied": 0, "updated": 0, "removed": 0, "deferred": 0, "addedToOpen": 0}


class Mac:
    """A temporary Claude data folder and Tokenometr data folder, with Claude open on account A."""

    def __init__(self):
        self.root = tempfile.mkdtemp()
        self.claude = os.path.join(self.root, "Claude")
        self.home = os.path.join(self.root, "Tokenometr")
        self.paths = chatsync.Paths(self.claude, self.home)
        os.makedirs(self.paths.sessions)
        self.cache = {}
        self.open_account(A)

    def open_account(self, account):
        with open(self.paths.config, "w", encoding="utf-8") as f:
            json.dump({"lastKnownAccountUuid": account, "locale": "ru-RU"}, f)

    def folder(self, account, org):
        path = os.path.join(self.paths.sessions, account, org)
        os.makedirs(path, exist_ok=True)
        return path

    def card(self, account, org, cid, mtime=None, **fields):
        data = {"sessionId": cid, "cliSessionId": "cli-" + cid, "cwd": "/Users/me/Projects/demo",
                "title": "Чат " + cid, "lastActivityAt": 1000, "isArchived": False}
        data.update(fields)
        path = os.path.join(self.folder(account, org), cid + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def read(self, account, org, cid):
        path = os.path.join(self.paths.sessions, account, org, cid + ".json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def ids(self, account, org):
        return sorted(n[:-5] for n in os.listdir(self.folder(account, org)) if chatsync.CARD.match(n))

    def delete(self, account, org, cid):
        os.remove(os.path.join(self.paths.sessions, account, org, cid + ".json"))

    def sync(self, claude=None, now=NOW):
        return chatsync.sync(self.paths, now=now, cache=self.cache, running=lambda: claude)

    def status(self, now=NOW):
        return chatsync.status(self.paths.state, now)

    def backups(self, pattern):
        return glob.glob(os.path.join(self.paths.backups, pattern))


class Base(unittest.TestCase):
    def setUp(self):
        self.mac = Mac()
        quiet = mock.patch.object(chatsync, "log", lambda message: None)
        quiet.start()
        self.addCleanup(quiet.stop)

    def tearDown(self):
        shutil.rmtree(self.mac.root)


class CopyTest(Base):
    def test_each_account_gets_the_chats_it_is_missing(self):
        self.mac.card(A, ORG_A, "local_x")
        self.mac.card(B, ORG_B, "local_y")
        self.mac.sync()
        self.assertEqual((self.mac.ids(A, ORG_A), self.mac.ids(B, ORG_B)),
                         (["local_x", "local_y"], ["local_x", "local_y"]))

    def test_copy_is_the_same_card_changed_at_the_same_time(self):
        source = self.mac.card(A, ORG_A, "local_x", mtime=NOW - 100, title="Мой чат")
        self.mac.card(B, ORG_B, "local_y")
        self.mac.sync()
        copy = os.path.join(self.mac.paths.sessions, B, ORG_B, "local_x.json")
        self.assertEqual((self.mac.read(B, ORG_B, "local_x"), os.stat(copy).st_mtime),
                         (self.mac.read(A, ORG_A, "local_x"), os.stat(source).st_mtime))

    def test_copy_with_newer_activity_wins_even_if_the_other_file_changed_later(self):
        self.mac.card(A, ORG_A, "local_x", lastActivityAt=2000, title="новое", mtime=NOW - 500)
        self.mac.card(B, ORG_B, "local_x", lastActivityAt=1000, title="старое", mtime=NOW - 10)
        self.mac.sync()
        self.assertEqual(self.mac.read(B, ORG_B, "local_x")["title"], "новое")

    def test_with_equal_activity_the_card_changed_last_wins(self):
        # Archiving a chat does not move its activity time.
        self.mac.card(A, ORG_A, "local_x", isArchived=True, mtime=NOW - 10)
        self.mac.card(B, ORG_B, "local_x", isArchived=False, mtime=NOW - 500)
        self.mac.sync()
        self.assertTrue(self.mac.read(B, ORG_B, "local_x")["isArchived"])

    def test_archive_hint_lists_the_archived_chats_a_list_received(self):
        self.mac.card(A, ORG_A, "local_x", isArchived=True)
        self.mac.card(B, ORG_B, "local_y")
        self.mac.sync()
        with open(os.path.join(self.mac.paths.sessions, B, ORG_B, chatsync.ARCHIVE_INDEX), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"v": 1, "archived": ["local_x"]})

    def test_second_pass_rewrites_nothing(self):
        self.mac.card(A, ORG_A, "local_x", bridgeSessionIds=["a-bridge"], steeredByRemoteClient=True)
        self.mac.card(B, ORG_B, "local_y", error="You've hit your limit · resets 6pm")
        self.mac.sync()
        files = glob.glob(os.path.join(self.mac.paths.sessions, "*", "*", "*"))
        before = {p: os.stat(p).st_mtime_ns for p in files}
        result = self.mac.sync(now=NOW + 5)
        self.assertEqual((result, {p: os.stat(p).st_mtime_ns for p in files}), (NOTHING, before))


class AccountFieldsTest(Base):
    def test_remote_control_links_stay_with_the_account_that_made_them(self):
        self.mac.card(A, ORG_A, "local_x", bridgeSessionIds=["a-bridge"], remoteControlUserEnabled=True,
                      steeredByRemoteClient=True)
        self.mac.card(B, ORG_B, "local_y")
        self.mac.sync()
        copy = self.mac.read(B, ORG_B, "local_x")
        self.assertEqual(("bridgeSessionIds" in copy, "remoteControlUserEnabled" in copy, copy["steeredByRemoteClient"]),
                         (False, False, False))

    def test_an_update_keeps_the_links_of_the_account_it_lands_in(self):
        self.mac.card(A, ORG_A, "local_x", lastActivityAt=2000, title="новое", bridgeSessionIds=["a-bridge"])
        self.mac.card(B, ORG_B, "local_x", lastActivityAt=1000, bridgeSessionIds=["b-bridge"])
        self.mac.sync()
        copy = self.mac.read(B, ORG_B, "local_x")
        self.assertEqual((copy["title"], copy["bridgeSessionIds"]), ("новое", ["b-bridge"]))

    def test_an_update_keeps_the_connectors_of_the_account_it_lands_in(self):
        self.mac.card(A, ORG_A, "local_x", lastActivityAt=2000, title="новое",
                      remoteMcpServersConfig=[{"uuid": "a-docs"}], enabledMcpTools={"a-docs:read": True})
        self.mac.card(B, ORG_B, "local_x", lastActivityAt=1000, remoteMcpServersConfig=[{"uuid": "b-mail"}])
        self.mac.sync()
        copy = self.mac.read(B, ORG_B, "local_x")
        self.assertEqual((copy["title"], copy["remoteMcpServersConfig"], "enabledMcpTools" in copy),
                         ("новое", [{"uuid": "b-mail"}], False))

    def test_new_copy_starts_with_the_connectors_of_its_source(self):
        self.mac.card(A, ORG_A, "local_x", remoteMcpServersConfig=[{"uuid": "a-docs"}], enabledMcpTools={"a-docs:read": True})
        self.mac.card(B, ORG_B, "local_y")
        self.mac.sync()
        copy = self.mac.read(B, ORG_B, "local_x")
        self.assertEqual((copy["remoteMcpServersConfig"], copy["enabledMcpTools"]), ([{"uuid": "a-docs"}], {"a-docs:read": True}))

    def test_connectors_alone_differing_is_not_a_change(self):
        self.mac.card(A, ORG_A, "local_x", remoteMcpServersConfig=[{"uuid": "a-docs"}], mtime=NOW - 10)
        self.mac.card(B, ORG_B, "local_x", remoteMcpServersConfig=[{"uuid": "b-mail"}], mtime=NOW - 500)
        self.assertEqual(self.mac.sync(), NOTHING)

    def test_usage_limit_error_of_another_account_is_not_copied(self):
        self.mac.card(A, ORG_A, "local_x", error="You've hit your limit · resets 6pm", errorAt=5)
        self.mac.card(B, ORG_B, "local_y", error="API Error: Can't reach the API server", errorAt=6)
        self.mac.sync()
        self.assertEqual((self.mac.read(B, ORG_B, "local_x").get("error"), self.mac.read(A, ORG_A, "local_y")["error"]),
                         (None, "API Error: Can't reach the API server"))


class OpenAccountTest(Base):
    """Claude runs with account A, so A's cards are Claude's to write."""

    def setUp(self):
        super().setUp()
        self.mac.card(A, ORG_A, "local_x", lastActivityAt=1000, title="старое")
        self.mac.card(B, ORG_B, "local_x", lastActivityAt=2000, title="новое")

    def test_its_cards_are_not_overwritten_while_claude_runs(self):
        result = self.mac.sync(claude=CLAUDE)
        self.assertEqual((self.mac.read(A, ORG_A, "local_x")["title"], result["deferred"]), ("старое", 1))

    def test_waiting_change_lands_once_claude_quits(self):
        self.mac.sync(claude=CLAUDE)
        self.mac.sync(claude=None, now=NOW + 10)
        self.assertEqual(self.mac.read(A, ORG_A, "local_x")["title"], "новое")

    def test_other_accounts_are_updated_while_claude_runs(self):
        self.mac.card(A, ORG_A, "local_y", lastActivityAt=3000, title="свежее")
        self.mac.card(B, ORG_B, "local_y", lastActivityAt=1000, title="давнее")
        self.mac.sync(claude=CLAUDE)
        self.assertEqual(self.mac.read(B, ORG_B, "local_y")["title"], "свежее")

    def test_it_still_gets_new_chats_and_claude_shows_them_after_a_restart(self):
        self.mac.card(B, ORG_B, "local_y")
        self.mac.sync(claude=CLAUDE)
        status = self.mac.status()
        self.assertEqual((self.mac.ids(A, ORG_A), status["state"], status["pending"]),
                         (["local_x", "local_y"], "restart", 1))

    def test_restart_notice_goes_once_claude_has_restarted(self):
        self.mac.card(B, ORG_B, "local_y")
        self.mac.sync(claude=CLAUDE)
        self.mac.sync(claude=CLAUDE + 1, now=NOW + 10)
        self.assertEqual(self.mac.status(NOW + 10)["state"], "ok")

    def test_restart_notice_stays_while_the_same_claude_runs(self):
        self.mac.card(B, ORG_B, "local_y")
        self.mac.sync(claude=CLAUDE)
        self.mac.sync(claude=CLAUDE, now=NOW + 10)
        self.assertEqual(self.mac.status(NOW + 10)["state"], "restart")


class DeleteTest(Base):
    def setUp(self):
        super().setUp()
        for cid in ("local_x", "local_y", "local_z"):
            self.mac.card(A, ORG_A, cid)
        self.mac.card(B, ORG_B, "local_y")
        self.mac.sync(claude=CLAUDE)

    def test_chat_deleted_in_the_open_account_is_removed_from_the_others(self):
        self.mac.delete(A, ORG_A, "local_x")
        self.mac.sync(claude=CLAUDE, now=NOW + 10)
        self.assertEqual(self.mac.ids(B, ORG_B), ["local_y", "local_z"])

    def test_removed_card_is_kept_in_the_backups(self):
        self.mac.delete(A, ORG_A, "local_x")
        self.mac.sync(claude=CLAUDE, now=NOW + 10)
        self.assertEqual(len(self.mac.backups(os.path.join("2*", B, ORG_B, "local_x.json"))), 1)

    def test_deleted_chat_is_not_copied_back(self):
        self.mac.delete(A, ORG_A, "local_x")
        self.mac.sync(claude=CLAUDE, now=NOW + 10)
        self.mac.sync(claude=None, now=NOW + 20)
        self.assertEqual((self.mac.ids(A, ORG_A), self.mac.ids(B, ORG_B)),
                         (["local_y", "local_z"], ["local_y", "local_z"]))

    def test_open_account_loses_a_deleted_chat_only_once_claude_quits(self):
        self.mac.delete(B, ORG_B, "local_x")
        self.mac.sync(claude=CLAUDE, now=NOW + 10)
        kept = self.mac.ids(A, ORG_A)
        self.mac.sync(claude=None, now=NOW + 20)
        self.assertEqual((kept, self.mac.ids(A, ORG_A)), (["local_x", "local_y", "local_z"], ["local_y", "local_z"]))

    def test_list_that_loses_every_chat_at_once_gets_them_back(self):
        for cid in ("local_x", "local_y", "local_z"):
            self.mac.delete(A, ORG_A, cid)
        self.mac.sync(claude=None, now=NOW + 10)
        self.assertEqual((self.mac.ids(A, ORG_A), self.mac.ids(B, ORG_B)),
                         (["local_x", "local_y", "local_z"], ["local_x", "local_y", "local_z"]))

    def test_chat_that_turns_up_again_is_no_longer_deleted(self):
        self.mac.delete(A, ORG_A, "local_x")
        self.mac.sync(claude=None, now=NOW + 10)
        self.mac.card(B, ORG_B, "local_x", lastActivityAt=5000)  # put back from a backup by hand
        self.mac.sync(claude=None, now=NOW + 20)
        self.assertEqual(self.mac.ids(A, ORG_A), ["local_x", "local_y", "local_z"])


class FolderTest(Base):
    def test_folder_left_behind_by_an_account_switch_gets_no_chats(self):
        self.mac.card(A, ORG_A, "local_x")
        self.mac.card(B, ORG_B, "local_y")
        stray = self.mac.folder(B, ORG_A)  # old account with the new org, holding only scheduled tasks
        with open(os.path.join(stray, "scheduled-tasks.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        self.mac.sync()
        self.assertEqual(os.listdir(stray), ["scheduled-tasks.json"])

    def test_brand_new_account_gets_every_chat(self):
        self.mac.card(A, ORG_A, "local_x")
        self.mac.card(B, ORG_B, "local_y")
        self.mac.folder(C, ORG_C)
        self.mac.open_account(C)
        self.mac.sync(claude=CLAUDE)
        self.assertEqual((self.mac.ids(C, ORG_C), self.mac.status()["state"]), (["local_x", "local_y"], "restart"))

    def test_half_written_card_is_left_for_the_next_pass(self):
        self.mac.card(A, ORG_A, "local_x", lastActivityAt=2000)
        path = self.mac.card(B, ORG_B, "local_x", lastActivityAt=1000)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"sessionId": "local_x", "tit')
        self.mac.sync()
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"sessionId": "local_x", "tit')

    def test_single_account_has_nothing_to_sync(self):
        self.mac.card(A, ORG_A, "local_x")
        self.assertEqual((self.mac.sync(), self.mac.status()["state"]), (NOTHING, "single"))

    def test_a_pass_runs_when_a_card_or_the_open_account_changes(self):
        path = self.mac.card(A, ORG_A, "local_x")
        first = chatsync.signature(self.mac.paths)
        os.utime(path, (NOW, NOW))
        second = chatsync.signature(self.mac.paths)
        self.mac.open_account(B)
        os.utime(self.mac.paths.config, (NOW + 1, NOW + 1))
        self.assertEqual(len({first, second, chatsync.signature(self.mac.paths)}), 3)


class SafetyTest(Base):
    def test_every_list_is_saved_before_the_first_sync(self):
        self.mac.card(A, ORG_A, "local_x")
        self.mac.card(B, ORG_B, "local_y")
        self.mac.sync()
        saved = self.mac.backups("before-first-sync-*")[0]
        self.assertEqual(sorted(os.listdir(os.path.join(saved, B, ORG_B))), ["local_y.json"])

    def test_overwritten_card_keeps_its_first_version_of_the_day(self):
        self.mac.card(A, ORG_A, "local_x", lastActivityAt=2000, title="новое")
        self.mac.card(B, ORG_B, "local_x", lastActivityAt=1000, title="старое")
        self.mac.sync()
        self.mac.card(A, ORG_A, "local_x", lastActivityAt=3000, title="новейшее")
        self.mac.sync(now=NOW + 60)
        backup = self.mac.backups(os.path.join("2*", B, ORG_B, "local_x.json"))
        with open(backup[0], encoding="utf-8") as f:
            self.assertEqual((len(backup), json.load(f)["title"]), (1, "старое"))

    def test_off_switch_stops_the_sync(self):
        self.mac.card(A, ORG_A, "local_x")
        self.mac.card(B, ORG_B, "local_y")
        os.makedirs(self.mac.home, exist_ok=True)
        open(self.mac.paths.off, "w").close()
        self.mac.sync()
        self.assertEqual((self.mac.ids(B, ORG_B), self.mac.status()["state"]), (["local_y"], "off"))


class StatusTest(Base):
    def test_says_how_many_chats_every_account_lists(self):
        self.mac.card(A, ORG_A, "local_x")
        self.mac.card(B, ORG_B, "local_y")
        self.mac.sync()
        status = self.mac.status()
        self.assertEqual((status["state"], status["chats"], status["accounts"]), ("ok", 2, 2))

    def test_is_stale_once_the_sync_stops_running(self):
        self.mac.card(A, ORG_A, "local_x")
        self.mac.card(B, ORG_B, "local_y")
        self.mac.sync()
        self.assertEqual(self.mac.status(NOW + chatsync.STALE_SECONDS + 1)["state"], "stale")

    def test_reports_a_failed_pass(self):
        chatsync.save_status(self.mac.paths, chatsync.load_state(self.mac.paths.state), NOW, error="OSError: disk full")
        self.assertEqual(self.mac.status()["state"], "error")

    def test_there_is_none_before_the_first_pass(self):
        self.assertIsNone(self.mac.status())


class ClaudeProcessTest(unittest.TestCase):
    HELPER = "/Users/me/Library/Application Support/Claude/claude-code/2.1/x/claude.app/Contents/MacOS/claude"

    def pid(self, ps_output):
        with mock.patch.object(chatsync.subprocess, "run", return_value=mock.Mock(stdout=ps_output)):
            return chatsync.claude_pid()

    def test_finds_the_claude_app(self):
        self.assertEqual(self.pid(f"  101 {self.HELPER}\n  202 /Applications/Claude.app/Contents/MacOS/Claude\n"), 202)

    def test_claude_codes_own_helpers_are_not_the_app(self):
        self.assertIsNone(self.pid(f"  101 {self.HELPER}\n"))


class CommandLineTest(Base):
    def test_one_pass_prints_what_it_did(self):
        self.mac.card(A, ORG_A, "local_x")
        self.mac.card(B, ORG_B, "local_y")
        out = subprocess.run([sys.executable, os.path.join(ROOT, "chatsync.py"),
                              "--claude", self.mac.claude, "--home", self.mac.home],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out)["copied"], 2)


if __name__ == "__main__":
    unittest.main()
