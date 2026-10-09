#!/usr/bin/env python3
"""Weighted Claude token usage per chat, read from the transcripts on this Mac."""
import bisect
import glob
import json
import os
import re
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import chatsync
from tokenometr_limits import (WEEK_RESET, account_bank, calibrate, parse_reset, period_starts, spending, week_around,
                               week_bounds, weigh)

PERIODS = ("5h", "today", "week", "all")
ACTIVE_SECONDS = 180  # a chat with an API call this recent is "running now"
OTHER = "other"
CACHE_VERSION = 1
HEAD_BYTES = 256  # how much of a transcript's beginning is kept to notice it was rewritten
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
CLAUDE_PATHS = ("~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude")
# `/usage` is a local command: it asks the API for the limits and never calls the model. The flags keep the
# CLI from starting MCP servers, plugins and hooks, or saving the run as a session.
USAGE_COMMAND = ["-p", "/usage", "--no-session-persistence", "--strict-mcp-config",
                 "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "local"]
SESSION_LINE = re.compile(r"^Current session:\s*(\d+)%\s*used", re.M)
WEEK_LINE = re.compile(r"^Current week(?: \(all models\))?:\s*(\d+)%\s*used"
                       r"(?:.*?resets\s+(\w{3})\w*\s+(\d{1,2})\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\))?",
                       re.M | re.I)


def parse_ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def scan_file(path, offset=0):
    """API calls in the complete lines after `offset`, and the offset where reading stopped.

    A call is [message id, unix time, input, cache write, cache read, output]. A last line
    without a newline is still being written, so it is left for the next scan.
    """
    calls = []
    with open(path, "rb") as f:
        f.seek(offset)
        for line in f:
            if not line.endswith(b"\n"):
                break
            offset += len(line)
            if b'"usage"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                continue
            key = message.get("id") or entry.get("requestId") or entry.get("uuid")
            ts = parse_ts(entry.get("timestamp"))
            if key is None or ts is None:
                continue
            calls.append([key, ts,
                          usage.get("input_tokens") or 0,
                          usage.get("cache_creation_input_tokens") or 0,
                          usage.get("cache_read_input_tokens") or 0,
                          usage.get("output_tokens") or 0])
    return calls, offset


def read_head(path):
    with open(path, "rb") as f:
        return f.read(HEAD_BYTES).hex()


def scan_cached(path, cache):
    """Like scan_file, but only reads what was appended since the last scan recorded in `cache`."""
    st = os.stat(path)
    entry = cache.get(path)
    if entry and (entry["ino"], entry["size"], entry["mtime"]) == (st.st_ino, st.st_size, st.st_mtime_ns):
        return entry["calls"]
    head = read_head(path)
    # Transcripts only grow; a shorter file or a different beginning means it was rewritten.
    if entry and entry["ino"] == st.st_ino and st.st_size >= entry["offset"] and head.startswith(entry["head"]):
        new_calls, offset = scan_file(path, entry["offset"])
        calls = entry["calls"] + new_calls
    else:
        calls, offset = scan_file(path)
    cache[path] = {"ino": st.st_ino, "size": st.st_size, "mtime": st.st_mtime_ns,
                   "offset": offset, "head": head, "calls": calls}
    return calls


def load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == CACHE_VERSION:
            return data["files"]
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    return {}


def write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def save_cache(path, files):
    write_json(path, {"version": CACHE_VERSION, "files": files})


def weighted(c):
    return weigh(c[2:6])


def load_sessions(sessions_dir):
    """sessionId -> {"meta": newest copy across accounts, "ids": transcript ids from every copy}."""
    found = {}
    for path in glob.glob(os.path.join(sessions_dir, "*", "*", "local_*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        sid = meta.get("sessionId")
        if not sid:
            continue
        known = found.setdefault(sid, {"meta": meta, "ids": set()})
        if (meta.get("lastActivityAt") or 0) > (known["meta"].get("lastActivityAt") or 0):
            known["meta"] = meta
        known["ids"].update(i for i in [meta.get("cliSessionId")] + list(meta.get("priorCliSessionIds") or []) if i)
    return found


def transcript_files(projects_dir):
    """(path, transcript id, is subagent) for every transcript on disk."""
    for path in glob.glob(os.path.join(projects_dir, "*", "*.jsonl")):
        yield path, os.path.basename(path)[: -len(".jsonl")], False
    for path in glob.glob(os.path.join(projects_dir, "*", "*", "subagents", "*.jsonl")):
        yield path, os.path.basename(os.path.dirname(os.path.dirname(path))), True


def folder_name(cwd):
    if not cwd or "/scratch-workspaces/" in cwd:
        return ""
    return os.path.basename(cwd.rstrip("/"))


def load_history(path):
    """Limit readings the Claude app records: {"t": ms, "org": account, "u": {"sd": weekly %, ...}}."""
    try:
        with open(path, encoding="utf-8") as f:
            samples = json.load(f).get("samples", [])
    except (OSError, ValueError, AttributeError):
        return []
    return sorted((s for s in samples if isinstance(s, dict) and isinstance(s.get("u"), dict)
                   and isinstance(s.get("t"), (int, float))), key=lambda s: s["t"])


def merge_readings(history_path, store_path):
    """Claude's limit readings plus older ones it has already dropped (it keeps 30 days), kept in `store_path`."""
    readings = load_history(history_path) if history_path else []
    if store_path:
        known = {(r["t"], r.get("org")) for r in readings}
        readings = sorted(readings + [r for r in load_history(store_path) if (r["t"], r.get("org")) not in known],
                          key=lambda r: r["t"])
        write_json(store_path, {"version": 1, "samples": readings})
    return readings


def load_accounts_config(path):
    """{account id: {"name": ..., "weekReset": "tue 18:00"}} from the user's accounts.json."""
    try:
        with open(path, encoding="utf-8") as f:
            accounts = json.load(f).get("accounts", {})
        return {org: settings for org, settings in accounts.items() if isinstance(settings, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def make_row(rid, calls, since, sessions, now_ts, account=None):
    """One chat's usage since `since`, counting only calls made on `account` (all accounts if None)."""
    main = sub = 0.0
    agents = set()
    count = 0
    last = None
    for c, is_sub, path, made_on in calls.values():
        if account is not None and made_on != account:
            continue
        last = c[1] if last is None else max(last, c[1])
        if c[1] < since:
            continue
        count += 1
        if is_sub:
            sub += weighted(c)
            agents.add(path)
        else:
            main += weighted(c)
    if not count:
        return None
    meta = sessions[rid]["meta"] if rid != OTHER else {}
    return {
        "id": rid,
        "title": "Другое" if rid == OTHER else (meta.get("title") or "Без названия"),
        "folder": folder_name(meta.get("cwd")),
        "main": main,
        "sub": sub,
        "subagents": len(agents),
        "calls": count,
        "last": last,
        "active": now_ts - last <= ACTIVE_SECONDS,
        "archived": bool(meta.get("isArchived")),
        "other": rid == OTHER,
    }


def parse_usage_text(text, now):
    """Percentages and the weekly reset from the text `claude -p /usage` prints; None without a weekly line."""
    week = WEEK_LINE.search(text or "")
    if not week:
        return None
    session = SESSION_LINE.search(text)
    resets_at = None
    if week.group(2):
        month, day, hour, minute, half, zone = week.group(2, 3, 4, 5, 6, 7)
        try:
            tz = ZoneInfo(zone)
            moment = datetime(now.astimezone(tz).year, MONTHS.index(month[:3].lower()) + 1, int(day),
                              int(hour) % 12 + (12 if half.lower() == "pm" else 0), int(minute or 0), tzinfo=tz)
            if moment.timestamp() < now.timestamp() - 180 * 24 * 3600:  # "resets Jan 3" seen in late December
                moment = moment.replace(year=moment.year + 1)
            resets_at = moment.timestamp()
        except (KeyError, ValueError):
            resets_at = None
    return {"fh": int(session.group(1)) if session else None, "sd": int(week.group(1)), "weekResetsAt": resets_at}


def find_claude():
    for candidate in CLAUDE_PATHS:
        path = os.path.expanduser(candidate)
        if os.access(path, os.X_OK):
            return path
    return None


def run_claude(claude, args, cwd):
    """stdout of the CLI run outside any Claude session (so it uses its own sign-in), or None if it failed."""
    user = os.environ.get("USER", "")
    env = {"HOME": os.path.expanduser("~"), "USER": user, "LOGNAME": user, "LANG": "en_US.UTF-8",
           "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    try:
        done = subprocess.run([claude] + args, capture_output=True, text=True, timeout=90, env=env, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def probe(claude, store_path, now):
    """Ask Claude Code CLI for the limits of the account it is signed in to, and keep that as a reading."""
    if not claude:
        return None
    # An empty folder of its own: the CLI files its runs by folder, and finds no project settings here.
    cwd = os.path.join(os.path.dirname(os.path.abspath(store_path)), "cli")
    os.makedirs(cwd, exist_ok=True)
    try:
        org = json.loads(run_claude(claude, ["auth", "status", "--json"], cwd) or "").get("orgId")
    except (ValueError, AttributeError):
        org = None
    parsed = parse_usage_text(run_claude(claude, USAGE_COMMAND, cwd), now) if org else None
    if not parsed:
        return None
    reading = {"t": int(now.timestamp() * 1000), "org": org, "source": "cli",
               "u": {key: parsed[key] for key in ("fh", "sd") if parsed[key] is not None}}
    if parsed["weekResetsAt"]:
        reading["weekResetsAt"] = parsed["weekResetsAt"]
    write_json(store_path, {"version": 1, "samples": load_history(store_path) + [reading]})
    return reading


def build_snapshot(projects_dir, sessions_dir, now, cache_path=None, history_path=None, accounts_path=None,
                   readings_path=None, chat_sync_path=None):
    sessions = load_sessions(sessions_dir)
    owner = {}
    for sid, s in sessions.items():
        for cli_id in s["ids"]:
            owner.setdefault(cli_id, sid)

    readings = merge_readings(history_path, readings_path)
    # Only the Claude app's own readings say which account it was using; the CLI may be signed in to another.
    app_readings = [r for r in readings if r.get("source") != "cli"]
    reading_times = [r["t"] / 1000 for r in app_readings]

    def account_at(ts):
        """The account the Claude app last reported before `ts`: the one the call was made on."""
        i = bisect.bisect_right(reading_times, ts) - 1
        return app_readings[i].get("org") if i >= 0 else None

    cache = load_cache(cache_path) if cache_path else None
    calls_by_row = {}  # row id -> message id -> (call, is subagent, transcript path, account)
    scanned = set()
    for path, cli_id, is_sub in transcript_files(projects_dir):
        try:
            calls = scan_cached(path, cache) if cache is not None else scan_file(path)[0]
        except OSError:  # deleted between listing and reading
            continue
        scanned.add(path)
        seen = calls_by_row.setdefault(owner.get(cli_id, OTHER), {})
        for c in calls:
            if c[0] not in seen:
                seen[c[0]] = (c, is_sub, path, account_at(c[1]))
    if cache is not None:
        save_cache(cache_path, {p: e for p, e in cache.items() if p in scanned})

    now_ts = now.timestamp()

    def view(account, week_start):
        """Usage per period, counting only calls made on `account` (all accounts if None)."""
        first_call = min((c[1] for calls in calls_by_row.values() for c, _, _, made_on in calls.values()
                          if account is None or made_on == account), default=None)
        periods = {}
        for key, since in period_starts(now, first_call, week_start).items():
            rows = [r for rid, calls in calls_by_row.items()
                    if (r := make_row(rid, calls, since or 0, sessions, now_ts, account))]
            rows.sort(key=lambda r: r["main"] + r["sub"], reverse=True)
            periods[key] = {"since": since, "total": sum(r["main"] + r["sub"] for r in rows), "rows": rows}
        return periods

    unique = {}
    for calls in calls_by_row.values():
        for c, _, _, made_on in calls.values():
            unique.setdefault(c[0], (c[1], weighted(c), made_on))
    config = load_accounts_config(accounts_path) if accounts_path else {}
    learned_resets = {r.get("org"): r["weekResetsAt"] for r in readings if r.get("weekResetsAt")}

    def week_of(org, moment):
        """(start, end) of the account's limit week around `moment`: the reset the CLI reported, else config."""
        resets_at = learned_resets.get(org)
        if resets_at is None:
            return week_bounds(moment, parse_reset(config.get(org, {}).get("weekReset")) or WEEK_RESET)
        return week_around(resets_at, moment.timestamp())

    spent_by_account = {}

    def spent_of(org):
        if org not in spent_by_account:
            spent_by_account[org] = spending((t, w) for t, w, made_on in unique.values() if made_on == org)
        return spent_by_account[org]

    per_percent = calibrate(readings, spent_of, week_of)
    current = app_readings[-1].get("org") if app_readings else None
    accounts = []
    for number, org in enumerate(dict.fromkeys(r.get("org") for r in readings if r.get("org")), 1):
        settings = config.get(org, {})
        week_start, resets_at = week_of(org, now)
        spent_here = spent_of(org)
        accounts.append({
            "id": org,
            "name": settings.get("name") or f"Аккаунт {number}",
            "current": org == current,
            "periods": view(org, week_start),
            "bank": account_bank([r for r in readings if r.get("org") == org], per_percent, spent_here,
                                 now_ts, week_start, resets_at),
        })
    all_week_start = week_of(current, now)[0]
    return {"generatedAt": now_ts, "periods": view(None, all_week_start), "accounts": accounts,
            "chatSync": chatsync.status(chat_sync_path, now_ts) if chat_sync_path else None}


def main():
    import argparse
    from datetime import timezone
    home = os.path.expanduser("~")
    support = os.path.join(home, "Library", "Application Support")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects", default=os.path.join(home, ".claude", "projects"))
    parser.add_argument("--sessions", default=os.path.join(support, "Claude", "claude-code-sessions"))
    parser.add_argument("--cache", default=os.path.join(home, "Library", "Caches", "Tokenometr", "scan-cache.json"))
    parser.add_argument("--history", default=os.path.join(support, "Claude", "plan-usage-history.json"))
    parser.add_argument("--accounts", default=os.path.join(support, "Tokenometr", "accounts.json"))
    parser.add_argument("--readings", default=os.path.join(support, "Tokenometr", "readings.json"))
    parser.add_argument("--chat-sync", default=os.path.join(support, "Tokenometr", "chat-sync.json"),
                        help="status file of the chat sync between accounts (chatsync.py)")
    parser.add_argument("--now", help="ISO time to count from (default: now)")
    parser.add_argument("--probe", action="store_true",
                        help="take a limit reading with Claude Code CLI, keep it in --readings and print it")
    args = parser.parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    if args.probe:
        print(json.dumps(probe(find_claude(), args.readings, now), ensure_ascii=False))
        return
    snapshot = build_snapshot(args.projects, args.sessions, now, args.cache, args.history, args.accounts,
                              args.readings, args.chat_sync)
    print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
