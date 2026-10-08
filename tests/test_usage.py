import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import usage  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)  # Wednesday


def call(msg_id, ts, inp=0, write=0, read=0, out=0, session="s"):
    """One transcript line as Claude Code writes it for an assistant API response."""
    return {
        "parentUuid": None,
        "isSidechain": False,
        "type": "assistant",
        "uuid": f"uuid-{msg_id}-{ts}",
        "timestamp": ts,
        "sessionId": session,
        "requestId": f"req_{msg_id}",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": write,
                "cache_read_input_tokens": read,
                "output_tokens": out,
                "cache_creation": {"ephemeral_1h_input_tokens": write, "ephemeral_5m_input_tokens": 0},
                "service_tier": "standard",
            },
        },
    }


def user_line(ts):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": "привет"}}


class Workspace:
    """Temporary ~/.claude/projects + claude-code-sessions pair."""

    def __init__(self):
        self.root = tempfile.mkdtemp()
        self.projects = os.path.join(self.root, "projects")
        self.sessions = os.path.join(self.root, "sessions")
        self.cache = os.path.join(self.root, "cache.json")
        os.makedirs(self.projects)
        os.makedirs(self.sessions)

    def transcript(self, cli_id, lines, folder="-Users-me-Projects-demo", subagent=None):
        d = os.path.join(self.projects, folder)
        if subagent:
            d = os.path.join(d, cli_id, "subagents")
            name = f"agent-{subagent}.jsonl"
        else:
            name = f"{cli_id}.jsonl"
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)) + "\n")
        return path

    def session(self, sid, cli_id, title, cwd="/Users/me/Projects/demo", prior=(), last=1, account="acc1",
                archived=False):
        d = os.path.join(self.sessions, account, "org1")
        os.makedirs(d, exist_ok=True)
        meta = {"sessionId": sid, "cliSessionId": cli_id, "priorCliSessionIds": list(prior), "cwd": cwd,
                "title": title, "lastActivityAt": last, "isArchived": archived}
        with open(os.path.join(d, f"{sid}.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

    def snapshot(self, now=NOW, cache=False):
        return usage.build_snapshot(self.projects, self.sessions, now, self.cache if cache else None)

    def cleanup(self):
        shutil.rmtree(self.root)


def rows(snapshot, period="all"):
    return {r["title"]: r for r in snapshot["periods"][period]["rows"]}


class Base(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.ws = Workspace()

    def tearDown(self):
        self.ws.cleanup()
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()


class CountingTest(Base):
    def test_api_call_split_over_several_lines_is_counted_once(self):
        line = call("msg_1", "2026-10-07T10:00:00.000Z", out=100)
        self.ws.session("local_a", "cli-a", "Чат А")
        self.ws.transcript("cli-a", [line, line, line])
        self.assertEqual(rows(self.ws.snapshot())["Чат А"]["main"], 500)

    def test_weights_reread_write_and_answer_tokens(self):
        self.ws.session("local_a", "cli-a", "Чат А")
        self.ws.transcript("cli-a", [call("m1", "2026-10-07T10:00:00.000Z", inp=1000, write=1000, read=1000,
                                          out=1000)])
        # 1000 input + 2*1000 written to memory + 0.1*1000 re-read + 5*1000 answer
        self.assertAlmostEqual(self.ws.snapshot()["periods"]["all"]["total"], 8100)

    def test_ignores_other_entries_and_broken_lines(self):
        self.ws.session("local_a", "cli-a", "Чат А")
        self.ws.transcript("cli-a", [
            user_line("2026-10-07T09:59:00.000Z"),
            '{"type": "assistant", "message": {"usage": ',
            {"type": "summary", "summary": "usage of tools"},
            call("m1", "2026-10-07T10:00:00.000Z", out=2),
        ])
        self.assertEqual(rows(self.ws.snapshot())["Чат А"]["main"], 10)


class SessionsTest(Base):
    def test_subagent_transcripts_count_as_their_sessions_subagent_usage(self):
        self.ws.session("local_a", "cli-a", "Чат А")
        self.ws.transcript("cli-a", [call("m1", "2026-10-07T10:00:00.000Z", out=1)])
        self.ws.transcript("cli-a", [call("s1", "2026-10-07T10:01:00.000Z", out=10)], subagent="x1")
        self.ws.transcript("cli-a", [call("s2", "2026-10-07T10:02:00.000Z", out=100)], subagent="x2")
        row = rows(self.ws.snapshot())["Чат А"]
        self.assertEqual((row["main"], row["sub"], row["subagents"]), (5, 550, 2))

    def test_prior_transcripts_belong_to_the_session_and_copied_calls_count_once(self):
        self.ws.session("local_a", "cli-new", "Чат А", prior=["cli-old"])
        early = call("m1", "2026-10-06T10:00:00.000Z", out=1)
        self.ws.transcript("cli-old", [early])
        self.ws.transcript("cli-new", [early, call("m2", "2026-10-07T10:00:00.000Z", out=10)])
        snap = self.ws.snapshot()
        self.assertEqual(rows(snap)["Чат А"]["main"], 55)
        self.assertEqual(len(snap["periods"]["all"]["rows"]), 1)

    def test_session_known_to_two_accounts_shows_once_with_newest_title(self):
        self.ws.session("local_a", "cli-a", "Старое название", last=100, account="acc1")
        self.ws.session("local_a", "cli-a", "Новое название", last=200, account="acc2")
        self.ws.transcript("cli-a", [call("m1", "2026-10-07T10:00:00.000Z", out=1)])
        self.assertEqual(list(rows(self.ws.snapshot())), ["Новое название"])

    def test_transcripts_without_a_session_go_to_the_other_row(self):
        self.ws.session("local_a", "cli-a", "Чат А")
        self.ws.transcript("cli-a", [call("m1", "2026-10-07T10:00:00.000Z", out=1)])
        self.ws.transcript("cli-terminal", [call("t1", "2026-10-07T10:00:00.000Z", out=10)])
        self.ws.transcript("cli-terminal", [call("t2", "2026-10-07T10:00:00.000Z", out=100)], subagent="y")
        other = [r for r in self.ws.snapshot()["periods"]["all"]["rows"] if r["other"]]
        self.assertEqual([(r["main"], r["sub"]) for r in other], [(50, 500)])

    def test_folder_is_the_project_folder_name_and_empty_for_scratch_sessions(self):
        self.ws.session("local_a", "cli-a", "Чат А", cwd="/Users/me/Projects/platform-core")
        self.ws.session("local_b", "cli-b", "Чат Б",
                        cwd="/Users/me/Library/Application Support/Claude/scratch-workspaces/acc/org/scratch-1")
        self.ws.transcript("cli-a", [call("m1", "2026-10-07T10:00:00.000Z", out=1)])
        self.ws.transcript("cli-b", [call("m2", "2026-10-07T10:00:00.000Z", out=1)])
        r = rows(self.ws.snapshot())
        self.assertEqual((r["Чат А"]["folder"], r["Чат Б"]["folder"]), ("platform-core", ""))

    def test_rows_are_sorted_by_usage(self):
        for sid, out in (("a", 1), ("b", 100), ("c", 10)):
            self.ws.session(f"local_{sid}", f"cli-{sid}", f"Чат {sid}")
            self.ws.transcript(f"cli-{sid}", [call(f"m{sid}", "2026-10-07T10:00:00.000Z", out=out)])
        titles = [r["title"] for r in self.ws.snapshot()["periods"]["all"]["rows"]]
        self.assertEqual(titles, ["Чат b", "Чат c", "Чат a"])


class PeriodsTest(Base):
    def setUp(self):
        super().setUp()
        self.ws.session("local_a", "cli-a", "Чат А")
        self.ws.transcript("cli-a", [
            call("c1", "2026-09-30T10:00:00.000Z", out=1),      # last week
            call("c2", "2026-10-04T08:59:00.000Z", out=10),     # Sunday, a minute before the weekly reset
            call("c3", "2026-10-04T09:01:00.000Z", out=100),    # Sunday, right after the reset
            call("c4", "2026-10-07T06:30:00.000Z", out=1000),   # today, 5.5 hours ago
            call("c5", "2026-10-07T07:30:00.000Z", out=10000),  # today, 4.5 hours ago
        ])

    def test_each_period_sums_only_its_own_calls(self):
        p = self.ws.snapshot()["periods"]
        self.assertEqual([p[k]["total"] for k in ("5h", "today", "week", "all")], [50000, 55000, 55500, 55555])

    def test_periods_start_5h_ago_at_midnight_at_sunday_9am_and_at_the_first_call(self):
        p = self.ws.snapshot()["periods"]
        starts = [datetime.fromtimestamp(p[k]["since"], timezone.utc).strftime("%m-%d %H:%M")
                  for k in ("5h", "today", "week", "all")]
        self.assertEqual(starts, ["10-07 07:00", "10-07 00:00", "10-04 09:00", "09-30 10:00"])

    def test_week_started_last_sunday_while_this_sundays_reset_is_still_ahead(self):
        p = self.ws.snapshot(now=datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc))["periods"]
        self.assertEqual(datetime.fromtimestamp(p["week"]["since"], timezone.utc),
                         datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc))

    def test_row_in_a_period_shows_that_periods_usage(self):
        row = rows(self.ws.snapshot(), "5h")["Чат А"]
        self.assertEqual((row["main"], row["calls"]), (50000, 1))

    def test_chat_without_calls_in_a_period_is_left_out_of_it(self):
        self.ws.session("local_b", "cli-b", "Чат Б")
        self.ws.transcript("cli-b", [call("b1", "2026-10-01T10:00:00.000Z", out=1)])
        snap = self.ws.snapshot()
        self.assertEqual((sorted(rows(snap, "today")), sorted(rows(snap, "all"))), (["Чат А"], ["Чат А", "Чат Б"]))


class ActiveTest(Base):
    def test_chat_with_a_call_in_the_last_minutes_is_marked_active(self):
        self.ws.session("local_a", "cli-a", "Идёт")
        self.ws.session("local_b", "cli-b", "Стоит")
        self.ws.transcript("cli-a", [call("a1", "2026-10-07T11:59:00.000Z", out=1)])
        self.ws.transcript("cli-b", [call("b1", "2026-10-07T11:50:00.000Z", out=1)])
        r = rows(self.ws.snapshot())
        self.assertEqual((r["Идёт"]["active"], r["Стоит"]["active"]), (True, False))


class CacheTest(Base):
    def setUp(self):
        super().setUp()
        self.ws.session("local_a", "cli-a", "Чат А")
        self.path = self.ws.transcript("cli-a", [call("m1", "2026-10-07T10:00:00.000Z", out=1)])

    def append(self, text):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)

    def main_usage(self):
        return rows(self.ws.snapshot(cache=True))["Чат А"]["main"]

    def test_lines_appended_after_a_scan_are_counted_on_the_next_one(self):
        self.main_usage()
        self.append(json.dumps(call("m2", "2026-10-07T10:05:00.000Z", out=10)) + "\n")
        self.assertEqual(self.main_usage(), 55)

    def test_unchanged_file_is_not_read_again(self):
        self.main_usage()
        st = os.stat(self.path)
        # same size and modification time, different content: only a cache hit keeps the old number
        with open(self.path, "r+", encoding="utf-8") as f:
            data = f.read()
            f.seek(0)
            f.write(data.replace('"output_tokens": 1,', '"output_tokens": 7,'))
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(self.main_usage(), 5)

    def test_half_written_last_line_is_counted_once_it_is_complete(self):
        line = json.dumps(call("m2", "2026-10-07T10:05:00.000Z", out=10))
        self.append(line[:40])
        self.assertEqual(self.main_usage(), 5)
        self.append(line[40:] + "\n")
        self.assertEqual(self.main_usage(), 55)

    def test_file_cut_back_to_its_first_line_is_read_from_the_start(self):
        first_line_length = os.path.getsize(self.path)
        self.append(json.dumps(call("m2", "2026-10-07T10:05:00.000Z", out=10)) + "\n")
        self.main_usage()
        os.truncate(self.path, first_line_length)
        self.assertEqual(self.main_usage(), 5)

    def test_file_replaced_with_longer_content_is_read_from_the_start(self):
        self.main_usage()
        self.ws.transcript("cli-a", [call("x1", "2026-10-07T10:10:00.000Z", out=100),
                                     call("x2", "2026-10-07T10:11:00.000Z", out=1000)])
        self.assertEqual(self.main_usage(), 5500)


def ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


class AccountsBase(Base):
    def history(self, *samples, name="plan-usage-history.json"):
        """Limit readings as the Claude app records them: (time, account, weekly %)."""
        path = os.path.join(self.ws.root, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "samples": [{"t": ms(t), "org": org, "u": {"fh": 0, "sd": sd}}
                                                 for t, org, sd in samples]}, f)
        return path

    def config(self, **accounts):
        path = os.path.join(self.ws.root, "accounts.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"accounts": accounts}, f, ensure_ascii=False)
        return path

    def spend(self, msg_id, ts, weighted):
        self.ws.transcript(f"cli-{msg_id}", [call(msg_id, ts, out=weighted // 5)])

    def snapshot(self, history=None, config=None, readings=None, now=NOW):
        return usage.build_snapshot(self.ws.projects, self.ws.sessions, now, None, history, config, readings)

    @staticmethod
    def account(snapshot, org):
        return next((a for a in snapshot["accounts"] if a["id"] == org), None)


class BankTest(AccountsBase):
    """The weekly limit: its size in weighted tokens and what is left of it this week."""

    def bank(self, history, now=NOW):
        account = self.account(self.snapshot(history, now=now), "acc")
        return account["bank"] if account else None

    def test_size_is_what_one_percent_of_the_limit_took_times_a_hundred(self):
        self.spend("m1", "2026-10-05T11:00:00+00:00", 2000)  # moved the limit from 10 % to 30 %
        bank = self.bank(self.history(("2026-10-05T10:00:00+00:00", "acc", 10),
                                      ("2026-10-05T12:00:00+00:00", "acc", 30)))
        self.assertEqual((bank["total"], bank["usedPercent"], bank["remaining"]), (10000, 30, 7000))

    def test_tokens_spent_after_the_latest_reading_are_added_to_it(self):
        self.spend("m1", "2026-10-05T11:00:00+00:00", 2000)
        self.spend("m2", "2026-10-06T09:00:00+00:00", 500)
        bank = self.bank(self.history(("2026-10-05T10:00:00+00:00", "acc", 10),
                                      ("2026-10-05T12:00:00+00:00", "acc", 30)))
        self.assertEqual((bank["usedPercent"], bank["remaining"], bank["readingPercent"]), (35, 6500, 30))

    def test_reading_from_before_this_weeks_reset_is_ignored(self):
        self.spend("m1", "2026-10-02T11:00:00+00:00", 2000)
        self.spend("m2", "2026-10-03T09:00:00+00:00", 9999)  # last week, after the reading
        self.spend("m3", "2026-10-06T09:00:00+00:00", 1000)  # this week (it began Sunday 9:00)
        bank = self.bank(self.history(("2026-10-02T10:00:00+00:00", "acc", 10),
                                      ("2026-10-02T12:00:00+00:00", "acc", 30)))
        self.assertEqual((bank["usedPercent"], bank["readingAt"]), (10, None))

    def test_size_ignores_intervals_that_cross_a_reset_or_an_account_switch(self):
        self.spend("m1", "2026-10-05T11:00:00+00:00", 2000)
        self.spend("m2", "2026-10-05T13:00:00+00:00", 5000)  # between readings of different accounts
        self.spend("m3", "2026-10-05T15:00:00+00:00", 5000)  # across a reset of the other account
        bank = self.bank(self.history(("2026-10-05T10:00:00+00:00", "acc", 10),
                                      ("2026-10-05T12:00:00+00:00", "acc", 30),
                                      ("2026-10-05T14:00:00+00:00", "other", 90),
                                      ("2026-10-05T16:00:00+00:00", "other", 2)))
        self.assertEqual(bank["total"], 10000)

    def test_no_bank_until_the_limit_has_moved_enough_to_measure(self):
        self.spend("m1", "2026-10-05T11:00:00+00:00", 2000)
        history = self.history(("2026-10-05T10:00:00+00:00", "acc", 10), ("2026-10-05T12:00:00+00:00", "acc", 15))
        self.assertEqual((self.bank(history), self.bank(os.path.join(self.ws.root, "missing.json"))), (None, None))

    def test_spending_past_the_limit_leaves_nothing_rather_than_a_negative_balance(self):
        self.spend("m1", "2026-10-05T11:00:00+00:00", 2000)
        self.spend("m2", "2026-10-06T09:00:00+00:00", 50000)
        bank = self.bank(self.history(("2026-10-05T10:00:00+00:00", "acc", 10),
                                      ("2026-10-05T12:00:00+00:00", "acc", 30)))
        self.assertEqual((bank["usedPercent"], bank["remaining"]), (100, 0))

    def test_size_ignores_intervals_that_span_the_weekly_reset(self):
        self.spend("m1", "2026-10-05T11:00:00+00:00", 2000)
        self.spend("m2", "2026-10-04T10:00:00+00:00", 7000)  # new week, yet the reading did not drop
        bank = self.bank(self.history(("2026-10-03T10:00:00+00:00", "acc", 40),
                                      ("2026-10-05T10:00:00+00:00", "acc", 50),
                                      ("2026-10-05T12:00:00+00:00", "acc", 70)))
        self.assertEqual(bank["total"], 10000)

    def test_part_spent_outside_this_mac_is_the_reading_minus_what_this_mac_spent_by_then(self):
        self.spend("m1", "2026-10-05T11:00:00+00:00", 2000)  # 20 % of the limit spent here
        self.spend("m2", "2026-10-06T11:00:00+00:00", 500)   # after the reading: says nothing about elsewhere
        bank = self.bank(self.history(("2026-10-05T10:00:00+00:00", "acc", 10),
                                      ("2026-10-05T12:00:00+00:00", "acc", 30)))
        self.assertEqual((bank["offMacPercent"], bank["offMacTokens"]), (10, 1000))

    def test_nothing_is_said_about_outside_spending_without_a_reading_this_week(self):
        self.spend("m1", "2026-10-02T11:00:00+00:00", 2000)
        bank = self.bank(self.history(("2026-10-02T10:00:00+00:00", "acc", 10),
                                      ("2026-10-02T12:00:00+00:00", "acc", 30)))
        self.assertIsNone(bank["offMacPercent"])

    def test_outside_spending_is_never_negative(self):
        self.spend("p1", "2026-09-30T11:00:00+00:00", 2000)  # last week: sets 100 tokens per 1 %
        self.spend("m1", "2026-10-05T11:00:00+00:00", 5000)  # this week this Mac spent 50 %...
        bank = self.bank(self.history(("2026-09-30T10:00:00+00:00", "acc", 10),
                                      ("2026-09-30T12:00:00+00:00", "acc", 30),
                                      ("2026-10-05T12:00:00+00:00", "acc", 30)))  # ...while Claude shows 30 %
        self.assertEqual(bank["offMacPercent"], 0)

    def test_bank_says_when_the_week_resets(self):
        self.spend("m1", "2026-10-05T11:00:00+00:00", 2000)
        bank = self.bank(self.history(("2026-10-05T10:00:00+00:00", "acc", 10),
                                      ("2026-10-05T12:00:00+00:00", "acc", 30)))
        self.assertEqual(datetime.fromtimestamp(bank["resetsAt"], timezone.utc),
                         datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc))


class AccountsTest(AccountsBase):
    def total(self, snapshot, org=None, period="all"):
        periods = snapshot["periods"] if org is None else self.account(snapshot, org)["periods"]
        return periods[period]["total"]

    def test_calls_belong_to_the_account_that_was_active_at_the_time(self):
        self.spend("a1", "2026-10-05T12:00:00+00:00", 100)
        self.spend("b1", "2026-10-06T12:00:00+00:00", 1000)
        snap = self.snapshot(self.history(("2026-10-05T10:00:00+00:00", "A", 0), ("2026-10-06T10:00:00+00:00", "B", 0)))
        self.assertEqual([self.total(snap, "A"), self.total(snap, "B"), self.total(snap)], [100, 1000, 1100])

    def test_calls_from_before_the_first_reading_count_only_for_all_accounts(self):
        self.spend("x1", "2026-10-05T12:00:00+00:00", 500)
        self.spend("a1", "2026-10-06T12:00:00+00:00", 100)
        snap = self.snapshot(self.history(("2026-10-06T10:00:00+00:00", "A", 0)))
        self.assertEqual([self.total(snap, "A"), self.total(snap)], [100, 600])

    def test_each_account_counts_its_week_from_its_own_reset(self):
        snap = self.snapshot(self.history(("2026-10-05T10:00:00+00:00", "A", 0), ("2026-10-06T10:00:00+00:00", "B", 0)),
                             self.config(B={"weekReset": "tue 18:00"}))
        starts = [datetime.fromtimestamp(self.account(snap, org)["periods"]["week"]["since"], timezone.utc)
                  for org in ("A", "B")]
        self.assertEqual(starts, [datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc),
                                  datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)])

    def test_bank_of_an_account_out_of_use_keeps_its_last_reading(self):
        self.spend("a1", "2026-10-05T11:00:00+00:00", 2000)  # account A: 10 % -> 30 %
        self.spend("b1", "2026-10-05T14:00:00+00:00", 300)   # after switching to B
        snap = self.snapshot(self.history(("2026-10-05T10:00:00+00:00", "A", 10), ("2026-10-05T12:00:00+00:00", "A", 30),
                                          ("2026-10-05T13:00:00+00:00", "B", 50)))
        self.assertEqual([self.account(snap, org)["bank"]["usedPercent"] for org in ("A", "B")], [30, 53])

    def test_bank_refills_once_the_accounts_own_week_resets(self):
        self.spend("a1", "2026-10-05T11:00:00+00:00", 2000)
        snap = self.snapshot(self.history(("2026-10-05T10:00:00+00:00", "A", 10), ("2026-10-05T12:00:00+00:00", "A", 30),
                                          ("2026-10-05T13:00:00+00:00", "B", 50)),
                             self.config(B={"weekReset": "tue 18:00"}))
        bank = self.account(snap, "B")["bank"]
        self.assertEqual((bank["usedPercent"], bank["remaining"]), (0, 10000))

    def test_the_account_claude_reported_last_is_the_current_one(self):
        snap = self.snapshot(self.history(("2026-10-05T10:00:00+00:00", "A", 0), ("2026-10-06T10:00:00+00:00", "B", 0)))
        self.assertEqual([(a["id"], a["current"]) for a in snap["accounts"]], [("A", False), ("B", True)])

    def test_accounts_are_named_from_the_config_or_numbered_by_first_appearance(self):
        snap = self.snapshot(self.history(("2026-10-05T10:00:00+00:00", "A", 0), ("2026-10-06T10:00:00+00:00", "B", 0)),
                             self.config(B={"name": "Запасной"}))
        self.assertEqual([a["name"] for a in snap["accounts"]], ["Аккаунт 1", "Запасной"])

    def test_readings_the_claude_app_has_dropped_still_tell_whose_calls_these_were(self):
        store = os.path.join(self.ws.root, "readings.json")
        self.snapshot(self.history(("2026-10-05T10:00:00+00:00", "A", 0)), readings=store)
        self.spend("a1", "2026-10-05T12:00:00+00:00", 100)
        snap = self.snapshot(self.history(("2026-10-06T10:00:00+00:00", "B", 0)), readings=store)
        self.assertEqual(self.total(snap, "A"), 100)


USAGE_TEXT = """You are currently using your subscription to power your Claude Code usage

Current session: 75% used · resets Oct 4 at 10:09pm (Europe/Moscow)
Current week (all models): 20% used · resets Oct 11 at 8:59am (Europe/Moscow)
Current week (Fable): 20% used · resets Oct 11 at 8:59am (Europe/Moscow)

What's contributing to your limits usage?
"""


class CliProbeTest(AccountsBase):
    """Readings taken by asking Claude Code CLI (`claude -p /usage`) instead of waiting for the Claude app."""

    def fake_claude(self, org="B", fail=False):
        path = os.path.join(self.ws.root, "claude")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
            if fail:
                f.write("echo 'not logged in' >&2\nexit 1\n")
            f.write('if [ "$1" = "auth" ]; then echo \'{"loggedIn": true, "orgId": "%s"}\'; exit 0; fi\n' % org)
            f.write("cat <<'TXT'\n" + USAGE_TEXT + "TXT\n")
        os.chmod(path, 0o755)
        return path

    def store(self, *samples):
        """Tokenometr's own readings: (time, account, weekly %, week resets at or None), taken via the CLI."""
        path = os.path.join(self.ws.root, "readings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "samples": [
                dict({"t": ms(t), "org": org, "u": {"sd": sd}, "source": "cli"},
                     **({"weekResetsAt": ms(resets) / 1000} if resets else {}))
                for t, org, sd, resets in samples]}, f)
        return path

    def test_usage_text_gives_the_session_and_week_percent_and_when_the_week_resets(self):
        self.assertEqual(usage.parse_usage_text(USAGE_TEXT, NOW),
                         {"fh": 75, "sd": 20,
                          "weekResetsAt": datetime(2026, 10, 11, 5, 59, tzinfo=timezone.utc).timestamp()})

    def test_evening_reset_without_minutes_is_read_as_such(self):
        text = "Current week (all models): 82% used · resets Oct 6 at 6pm (Europe/Moscow)\n"
        self.assertEqual(usage.parse_usage_text(text, NOW),
                         {"fh": None, "sd": 82,
                          "weekResetsAt": datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc).timestamp()})

    def test_probe_records_a_reading_for_the_account_the_cli_is_signed_in_to(self):
        path = os.path.join(self.ws.root, "readings.json")
        usage.probe(self.fake_claude(org="B"), path, NOW)
        self.assertEqual([(r["org"], r["u"], r["source"]) for r in usage.load_history(path)],
                         [("B", {"fh": 75, "sd": 20}, "cli")])

    def test_probe_records_nothing_when_the_cli_fails(self):
        path = os.path.join(self.ws.root, "readings.json")
        self.assertEqual((usage.probe(self.fake_claude(fail=True), path, NOW), usage.load_history(path)), (None, []))

    def test_cli_readings_do_not_decide_whose_calls_these_are(self):
        store = self.store(("2026-10-06T11:00:00+00:00", "B", 50, None))  # the CLI is signed in to B
        self.spend("a1", "2026-10-06T12:00:00+00:00", 100)                 # while the Claude app runs A
        snap = self.snapshot(self.history(("2026-10-06T10:00:00+00:00", "A", 0)), readings=store)
        self.assertEqual(self.account(snap, "A")["periods"]["all"]["total"], 100)

    def test_bank_starts_from_the_freshest_reading_whichever_source_took_it(self):
        self.spend("a1", "2026-10-05T11:00:00+00:00", 2000)  # 100 tokens per 1 %
        store = self.store(("2026-10-06T09:00:00+00:00", "A", 45, None))
        snap = self.snapshot(self.history(("2026-10-05T10:00:00+00:00", "A", 10), ("2026-10-05T12:00:00+00:00", "A", 30)),
                             readings=store)
        self.assertEqual(self.account(snap, "A")["bank"]["readingPercent"], 45)

    def test_week_follows_the_reset_the_cli_reported(self):
        store = self.store(("2026-10-06T19:00:00+00:00", "B", 5, "2026-10-13T18:00:00+00:00"))
        snap = self.snapshot(self.history(("2026-10-05T10:00:00+00:00", "A", 0)), readings=store)
        self.assertEqual(datetime.fromtimestamp(self.account(snap, "B")["periods"]["week"]["since"], timezone.utc),
                         datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc))

    def test_size_ignores_readings_of_an_account_while_another_one_was_in_use(self):
        self.spend("a1", "2026-10-05T11:00:00+00:00", 2000)  # A: 10 % -> 30 %
        self.spend("a2", "2026-10-06T11:00:00+00:00", 9000)  # still A, while the CLI watches B rise
        store = self.store(("2026-10-06T10:00:00+00:00", "B", 10, None), ("2026-10-06T12:00:00+00:00", "B", 40, None))
        snap = self.snapshot(self.history(("2026-10-05T10:00:00+00:00", "A", 10), ("2026-10-05T12:00:00+00:00", "A", 30)),
                             readings=store)
        self.assertEqual(self.account(snap, "A")["bank"]["total"], 10000)


class CommandLineTest(Base):
    def test_prints_the_snapshot_as_json_and_keeps_the_cache(self):
        self.ws.session("local_a", "cli-a", "Чат А")
        self.ws.transcript("cli-a", [call("m1", "2026-10-07T10:00:00.000Z", out=1)])
        scratch = os.path.join(self.ws.root, "scratch")
        out = subprocess.run([sys.executable, os.path.join(ROOT, "usage.py"),
                              "--projects", self.ws.projects, "--sessions", self.ws.sessions,
                              "--cache", self.ws.cache, "--now", "2026-10-07T12:00:00+00:00",
                              "--history", os.path.join(scratch, "history.json"),
                              "--accounts", os.path.join(scratch, "accounts.json"),
                              "--readings", os.path.join(scratch, "readings.json"),
                              "--chat-sync", os.path.join(scratch, "chat-sync.json")],
                             capture_output=True, text=True, check=True).stdout
        five_hours = json.loads(out)["periods"]["5h"]
        self.assertEqual((five_hours["total"], five_hours["rows"][0]["title"], os.path.exists(self.ws.cache)),
                         (5, "Чат А", True))

    def test_snapshot_carries_the_chat_sync_status(self):
        state = os.path.join(self.ws.root, "chat-sync.json")
        with open(state, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "checkedAt": NOW.timestamp() - 30, "chats": 35, "folders": 2}, f)
        snap = usage.build_snapshot(self.ws.projects, self.ws.sessions, NOW, chat_sync_path=state)
        self.assertEqual((snap["chatSync"]["state"], snap["chatSync"]["chats"]), ("ok", 35))


if __name__ == "__main__":
    unittest.main()
