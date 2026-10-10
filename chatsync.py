#!/usr/bin/env python3
"""Keeps the Code-tab chat list the same in every Claude account on this Mac.

Claude keeps a folder of chat cards per account (claude-code-sessions/<account>/<org>/local_*.json),
while the conversations themselves are shared in ~/.claude/projects. This copies the cards between those
folders, so whichever account Claude switches to already lists every chat.

Claude reads an account's folder when it starts and when it switches to that account, and may write back
the cards it has loaded. So while Claude runs, cards of the account it has open are never overwritten or
removed: such changes wait until Claude quits. New cards can still be added there; Claude shows them after
a restart. Every card that is overwritten or removed is kept in the backup folder first.

  chatsync.py           one pass
  chatsync.py --watch   a pass whenever the folders change (what the LaunchAgent runs); it also sends the
                        usage of the team's accounts to the site every few minutes, if this Mac is
                        connected to a team (teamsync.py)
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

CARD = re.compile(r"^(local_[A-Za-z0-9_-]+)\.json$")
UUID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
ARCHIVE_INDEX = "archived-sessions.idx"
# Parts of a card that belong to its account rather than to the chat. Remote Control links never go to
# another account. Connectors (claude.ai MCP servers and which of their tools are on) stay as each account
# has them; a new copy starts from the source's, and Claude reconciles them with its account.
LINK_FIELDS = ("bridgeSessionIds", "remoteControlUserEnabled", "steeredByRemoteClient")
CONNECTOR_FIELDS = ("remoteMcpServersConfig", "enabledMcpTools")
CLAUDE_EXECUTABLE = "/Claude.app/Contents/MacOS/Claude"
STATE_VERSION = 1
POLL_SECONDS = 2
FULL_PASS_SECONDS = 60  # a pass even without changes, so the status shows the sync is alive
RETRY_SECONDS = 10  # how often to look whether Claude has quit while changes wait for that
STALE_SECONDS = 300  # a status older than this means the sync is not running
TEAM_SYNC_SECONDS = 300  # how often the usage goes to the team's site
BACKUP_DAYS = 14
TOMBSTONE_DAYS = 90
EMPTIED_GUARD = 3  # a list that loses all of at least this many chats at once was not emptied by hand


class Paths:
    def __init__(self, claude_dir, home_dir):
        self.sessions = os.path.join(claude_dir, "claude-code-sessions")
        self.config = os.path.join(claude_dir, "config.json")
        self.state = os.path.join(home_dir, "chat-sync.json")
        self.backups = os.path.join(home_dir, "chat-sync-backups")
        self.off = os.path.join(home_dir, "chat-sync-off")
        self.lock = os.path.join(home_dir, "chat-sync.lock")


class Folder:
    """One account's chat list: claude-code-sessions/<account>/<org>."""

    def __init__(self, account, org, path):
        self.account, self.org, self.path = account, org, path
        self.key = account + "/" + org


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", file=sys.stderr, flush=True)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data, mtime_ns=None):
    """Atomic and compact, like Claude's own files. The temporary name is hidden so Claude never loads it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tokenometr-tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.chmod(tmp, 0o600)
    if mtime_ns is not None:
        os.utime(tmp, ns=(time.time_ns(), mtime_ns))
    os.replace(tmp, path)


def active_account(config_path):
    """The account Claude has open (or last had open)."""
    try:
        account = load_json(config_path).get("lastKnownAccountUuid")
    except (OSError, ValueError, AttributeError):
        return None
    return account if isinstance(account, str) else None


def has_cards(path):
    try:
        return any(CARD.match(name) for name in os.listdir(path))
    except OSError:
        return False


def mtime_ns(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def list_folders(sessions_dir, active):
    """Every account/org folder with chats, plus the open account's folder while it is still empty.

    During an account switch Claude briefly creates a folder for the old account with the new org; it holds
    no chats and is left alone. A brand-new account has no chats yet either, but it is the one Claude has
    open, so it gets the chats.
    """
    found = []
    try:
        accounts = sorted(os.listdir(sessions_dir))
    except OSError:
        return []
    for account in accounts:
        account_dir = os.path.join(sessions_dir, account)
        if not UUID.match(account) or not os.path.isdir(account_dir):
            continue
        try:
            orgs = sorted(o for o in os.listdir(account_dir)
                          if UUID.match(o) and os.path.isdir(os.path.join(account_dir, o)))
        except OSError:
            continue
        folders = [Folder(account, o, os.path.join(account_dir, o)) for o in orgs]
        with_chats = [f for f in folders if has_cards(f.path)]
        if not with_chats and account == active and folders:
            with_chats = [max(folders, key=lambda f: mtime_ns(f.path))]
        found.extend(with_chats)
    return found


class Card:
    """What a pass needs to know about one chat card. The card itself is read again only to copy it."""
    __slots__ = ("path", "mtime", "activity", "archived", "digest")

    def __init__(self, path, mtime, activity, archived, digest):
        self.path, self.mtime, self.activity, self.archived, self.digest = path, mtime, activity, archived, digest

    @classmethod
    def of(cls, path, mtime, data):
        try:
            activity = float(data.get("lastActivityAt") or 0)
        except (TypeError, ValueError):
            activity = 0.0
        text = json.dumps(portable(data), sort_keys=True, ensure_ascii=False)
        return cls(path, mtime, activity, bool(data.get("isArchived")), hashlib.sha1(text.encode("utf-8")).hexdigest())

    def copied_to(self, path):
        return Card(path, self.mtime, self.activity, self.archived, self.digest)

    def freshness(self):
        """Which copy of a chat is the newest: the latest activity, then the latest change to its card."""
        return self.activity, self.mtime


def read_card(path):
    try:
        data = load_json(path)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_cards(folder, cache):
    """card id -> Card, or None for a card that cannot be read right now (it is being written).

    `cache` keeps what was learned about each card between passes, so only changed cards are read again.
    """
    cards = {}
    try:
        names = os.listdir(folder.path)
    except OSError:
        return cards
    for name in names:
        match = CARD.match(name)
        if not match:
            continue
        path = os.path.join(folder.path, name)
        try:
            st = os.stat(path)
        except OSError:  # removed meanwhile
            continue
        stamp = (st.st_mtime_ns, st.st_size)
        hit = cache.get(path)
        if hit is None or hit[0] != stamp:
            data = read_card(path)
            if data is None:
                cards[match.group(1)] = None
                continue
            hit = cache[path] = (stamp, Card.of(path, st.st_mtime_ns, data))
        cards[match.group(1)] = hit[1]
    return cards


def is_limit_error(text):
    text = text.lower() if isinstance(text, str) else ""
    return "hit your" in text and "limit" in text


def portable(card):
    """The part of a card that is the same chat in every account: no links, connectors or usage-limit error."""
    out = {k: v for k, v in card.items() if k not in LINK_FIELDS and k not in CONNECTOR_FIELDS}
    if is_limit_error(out.get("error")):
        out.pop("error", None)
        out.pop("errorAt", None)
    return out


def copy_for(winner, target):
    """What a card becomes when `winner` replaces it (`target` is the card there now, or None)."""
    out = portable(winner)
    own = target if target is not None else {}
    for field in LINK_FIELDS:
        if field in own:
            out[field] = own[field]
    for field in CONNECTOR_FIELDS:
        source = own if target is not None else winner
        if field in source:
            out[field] = source[field]
    if "steeredByRemoteClient" in winner and "steeredByRemoteClient" not in out:
        out["steeredByRemoteClient"] = False
    return out


def claude_pid():
    """PID of the running Claude app, or None. (Claude Code's own `claude.app` helpers are lowercase.)"""
    try:
        out = subprocess.run(["/bin/ps", "-axo", "pid=,comm="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if command.strip().endswith(CLAUDE_EXECUTABLE) and pid.isdigit():
            return int(pid)
    return None


def load_state(path):
    try:
        state = load_json(path)
        if isinstance(state, dict) and state.get("version") == STATE_VERSION:
            return state
    except (OSError, ValueError):
        pass
    return {"version": STATE_VERSION}


def save_status(paths, state, now, off=False, error=None):
    state["version"] = STATE_VERSION
    state["checkedAt"] = now
    state["off"] = off
    state["error"] = error
    write_json(paths.state, state)


def backup_dir(paths, folder, now):
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    path = os.path.join(paths.backups, day, folder.account, folder.org)
    os.makedirs(path, exist_ok=True)
    return path


def keep_before_overwrite(paths, folder, name, now):
    """Keeps the first version of the day of a card that is about to be overwritten."""
    dest = os.path.join(backup_dir(paths, folder, now), name)
    if not os.path.exists(dest):
        shutil.copy2(os.path.join(folder.path, name), dest)


def backup_everything(paths, now):
    """Before the first sync: a copy of every account's chat list as it was."""
    dest = os.path.join(paths.backups, "before-first-sync-" + datetime.fromtimestamp(now).strftime("%Y-%m-%d"))
    if not os.path.exists(dest):
        shutil.copytree(paths.sessions, dest)
        log(f"saved every chat list to {dest} before the first sync")
    return dest


def prune_backups(backups, now):
    """Daily backups older than BACKUP_DAYS go; the one made before the first sync stays."""
    try:
        names = os.listdir(backups)
    except OSError:
        return
    cutoff = datetime.fromtimestamp(now - BACKUP_DAYS * 86400).strftime("%Y-%m-%d")
    for name in names:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", name) and name < cutoff:
            shutil.rmtree(os.path.join(backups, name), ignore_errors=True)


def remove_card(paths, folder, cid, now):
    """Moves a card into the backup folder."""
    dest = os.path.join(backup_dir(paths, folder, now), cid + ".json")
    if os.path.exists(dest):
        dest += "." + datetime.fromtimestamp(now).strftime("%H%M%S")
    try:
        os.replace(os.path.join(folder.path, cid + ".json"), dest)
    except FileNotFoundError:
        return
    log(f"removed {cid} from {folder.key}: it was deleted in another account")


def write_archive_index(folder, cards):
    """Claude's hint of which chats are archived, rebuilt from the cards, which have the final say."""
    archived = sorted(cid for cid, card in cards.items() if card and card.archived)
    path = os.path.join(folder.path, ARCHIVE_INDEX)
    try:
        current = load_json(path)
    except (OSError, ValueError):
        current = None
    if isinstance(current, dict) and sorted(current.get("archived") or []) == archived:
        return
    if current is None and not archived:
        return
    write_json(path, {"v": 1, "archived": archived})


def sync(paths, now=None, cache=None, running=claude_pid):
    """One pass: brings every account's chat list up to date. Returns what it did and what waits."""
    now = time.time() if now is None else now
    cache = {} if cache is None else cache
    state = load_state(paths.state)
    result = {"copied": 0, "updated": 0, "removed": 0, "deferred": 0, "addedToOpen": 0}
    if os.path.exists(paths.off):
        save_status(paths, state, now, off=True)
        return result

    active = active_account(paths.config)
    folders = list_folders(paths.sessions, active)
    cards = {f.key: read_cards(f, cache) for f in folders}
    seen = state.get("seen") or {}
    deleted = dict(state.get("deleted") or {})
    if len(folders) >= 2 and not state.get("backedUp"):
        state["backedUp"] = backup_everything(paths, now)

    # A chat that was in a list on the last pass and is gone now was deleted by the user.
    for f in folders:
        before = set(seen.get(f.key) or ())
        present = set(cards[f.key])
        gone = before - present
        if gone and (present or len(before) < EMPTIED_GUARD):
            for cid in gone:
                deleted.setdefault(cid, now)

    claude = []

    def pid():
        if not claude:
            claude.append(running())
        return claude[0]

    def is_open(folder):
        """Claude is running with this account, so its cards must not be overwritten or removed."""
        return folder.account == active and pid() is not None

    changed = set()
    for cid in sorted(set().union(*cards.values())):
        holders = [f for f in folders if cid in cards[f.key]]
        if any(cards[f.key][cid] is None for f in holders):
            continue  # a copy is being written; the next pass gets it
        if cid in deleted:
            if all(cid in (seen.get(f.key) or ()) for f in holders):
                for f in holders:
                    if is_open(f):
                        result["deferred"] += 1
                        continue
                    remove_card(paths, f, cid, now)
                    del cards[f.key][cid]
                    changed.add(f.key)
                    result["removed"] += 1
                continue
            del deleted[cid]  # it turned up in a list that did not have it, so it is back

        winner_folder = max(holders, key=lambda f: cards[f.key][cid].freshness())
        winner = cards[winner_folder.key][cid]
        winner_data = None
        for f in folders:
            have = cards[f.key].get(cid)
            if f is winner_folder or (have is not None and have.digest == winner.digest):
                continue
            if have is not None and is_open(f):
                result["deferred"] += 1
                continue
            winner_data = winner_data or read_card(winner.path)
            path = os.path.join(f.path, cid + ".json")
            current = read_card(path) if have is not None else None
            if winner_data is None or (have is not None and current is None):
                continue  # a card changed under us; the next pass sees it settled
            if have is None:
                write_json(path, copy_for(winner_data, None), winner.mtime)
                result["copied"] += 1
                if is_open(f):
                    result["addedToOpen"] += 1
            else:
                keep_before_overwrite(paths, f, cid + ".json", now)
                write_json(path, copy_for(winner_data, current), winner.mtime)
                result["updated"] += 1
            cards[f.key][cid] = winner.copied_to(path)
            changed.add(f.key)

    for f in folders:
        if f.key in changed and not is_open(f):
            write_archive_index(f, cards[f.key])
    if result["copied"]:
        log(f"copied {result['copied']} chat card(s) into other accounts' lists")
    if changed:
        prune_backups(paths.backups, now)

    everywhere = set().union(*cards.values())
    live = {c.path for folder_cards in cards.values() for c in folder_cards.values() if c}
    for path in [p for p in cache if p not in live]:
        del cache[path]
    state["seen"] = {f.key: sorted(cards[f.key]) for f in folders}
    state["deleted"] = {cid: at for cid, at in deleted.items()
                        if cid in everywhere or now - at < TOMBSTONE_DAYS * 86400}
    pending = state.get("pending")
    if result["addedToOpen"]:
        same_run = bool(pending) and pending.get("claudePid") == pid() and pending.get("account") == active
        count = (pending.get("count") or 0) if same_run else 0
        pending = {"count": count + result["addedToOpen"], "claudePid": pid(), "account": active}
    elif pending and (pending.get("account") != active or pid() != pending.get("claudePid")):
        pending = None  # Claude restarted or switched accounts, so it has read the list again
    state["pending"] = pending
    if result["copied"] or result["removed"]:
        state["lastChange"] = {"at": now, "copied": result["copied"], "removed": result["removed"]}
    state["chats"] = len(everywhere - set(state["deleted"]))
    state["folders"] = len(folders)
    state["deferred"] = result["deferred"]
    save_status(paths, state, now)
    return result


def status(state_path, now):
    """The sync as Tokenometr shows it, or None if it never ran.

    state: "ok", "restart" (chats were added to the open account; Claude shows them after a restart),
    "single" (one account, nothing to sync), "off", "error" or "stale" (the sync is not running).
    """
    try:
        state = load_json(state_path)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    checked = state.get("checkedAt") or 0
    pending = (state.get("pending") or {}).get("count") or 0
    out = {"chats": state.get("chats") or 0, "accounts": state.get("folders") or 0, "checkedAt": checked,
           "pending": pending, "error": state.get("error")}
    if state.get("off"):
        out["state"] = "off"
    elif now - checked > STALE_SECONDS:
        out["state"] = "stale"
    elif state.get("error"):
        out["state"] = "error"
    elif out["accounts"] < 2:
        out["state"] = "single"
    elif pending:
        out["state"] = "restart"
    else:
        out["state"] = "ok"
    return out


def signature(paths):
    """Cheap fingerprint of everything a pass looks at; a pass runs when it changes."""
    parts = [os.path.exists(paths.off)]
    for path in (paths.config, paths.sessions):
        try:
            st = os.stat(path)
            parts.append((st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append(None)
    try:
        for account in sorted(os.listdir(paths.sessions)):
            account_dir = os.path.join(paths.sessions, account)
            if not os.path.isdir(account_dir):
                continue
            for org in sorted(os.listdir(account_dir)):
                org_dir = os.path.join(account_dir, org)
                if not os.path.isdir(org_dir):
                    continue
                with os.scandir(org_dir) as entries:
                    for entry in entries:
                        if CARD.match(entry.name):
                            st = entry.stat()
                            parts.append((account, org, entry.name, st.st_mtime_ns, st.st_size))
    except OSError:
        pass
    return tuple(parts)


def file_stamp(path):
    try:
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size, st.st_ino
    except OSError:
        return None


def locked_pass(paths, cache):
    os.makedirs(os.path.dirname(paths.lock), exist_ok=True)
    with open(paths.lock, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # a manual run and the LaunchAgent take turns
        return sync(paths, cache=cache)


def team_pass(paths, report, sync=None):
    """Sends the usage of the team's accounts, if this Mac is connected to a team. Never raises.

    It runs beside the chat sync, in its own thread: a slow site must not hold the chat lists back.
    `report` gets the problem of the pass, or None when it went well.
    """
    try:
        import teamsync
        support = os.path.dirname(os.path.abspath(paths.state))
        claude = os.path.dirname(paths.sessions)
        home = os.path.expanduser("~")
        view = (sync or teamsync.sync)(teamsync.Paths(
            support, os.path.join(home, ".claude", "projects"), paths.sessions,
            os.path.join(claude, "plan-usage-history.json"),
            os.path.join(home, "Library", "Caches", "Tokenometr", "scan-cache.json")))
        report(view.get("message") if view.get("state") in ("error", "update", "revoked") else None)
    except Exception as e:  # the chat sync goes on whatever happens to the team's
        report(f"{type(e).__name__}: {e}")


def watch(paths):
    script = os.path.abspath(__file__)
    script_stamp = file_stamp(script)
    cache, last_signature, last_pass, waiting, last_error = {}, None, 0.0, False, None
    last_team, team_thread, team_error = 0.0, None, [None]

    def team_reported(problem):
        if problem != team_error[0]:  # a lasting problem is logged once, not at every try
            log(f"team sync: {problem}" if problem else "team sync is fine")
        team_error[0] = problem

    log("watching Claude's chat lists")
    while True:
        if time.time() - last_team >= TEAM_SYNC_SECONDS and not (team_thread and team_thread.is_alive()):
            last_team = time.time()
            team_thread = threading.Thread(target=team_pass, args=(paths, team_reported), daemon=True)
            team_thread.start()
        stamp = file_stamp(script)
        if stamp != script_stamp:
            if stamp is None:
                log("Tokenometr was removed; stopping")
                return
            log("Tokenometr was updated; restarting")
            os.execv(sys.executable, [sys.executable, script] + sys.argv[1:])
        now = time.time()
        current = signature(paths)
        if (current != last_signature or now - last_pass >= FULL_PASS_SECONDS
                or (waiting and now - last_pass >= RETRY_SECONDS)):
            try:
                result = locked_pass(paths, cache)
                waiting = bool(result["deferred"] or load_state(paths.state).get("pending"))
                last_error = None
            except Exception as e:  # keep watching; the status shows the problem
                error = f"{type(e).__name__}: {e}"
                if error != last_error:  # a lasting problem is logged once, not every retry
                    log(f"pass failed: {error}")
                last_error = error
                save_status(paths, load_state(paths.state), now, error=error)
                waiting = True
            last_signature, last_pass = signature(paths), now
        time.sleep(POLL_SECONDS)


def main():
    support = os.path.expanduser("~/Library/Application Support")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--claude", default=os.path.join(support, "Claude"), help="Claude's data folder")
    parser.add_argument("--home", default=os.path.join(support, "Tokenometr"), help="Tokenometr's data folder")
    parser.add_argument("--watch", action="store_true", help="keep syncing whenever the chat lists change")
    args = parser.parse_args()
    paths = Paths(args.claude, args.home)
    if args.watch:
        watch(paths)
    else:
        print(json.dumps(locked_pass(paths, {}), ensure_ascii=False))


if __name__ == "__main__":
    main()
