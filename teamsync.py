#!/usr/bin/env python3
"""Connects this Mac to a team on the Tokenometr site and sends it the usage of the team's Claude accounts.

    teamsync.py connect [--name NAME]   ask the site for a code, wait for a person to confirm it, keep the token
    teamsync.py sync                    send what changed, then fetch the team's numbers
    teamsync.py disconnect              forget the token and the team's numbers
    teamsync.py status                  print the connection

It speaks protocol 1 of the site (POST /api/sync and GET /api/sync/team). Only the accounts a team of this
person counts together leave the Mac, and from them only what the site's privacy page lists: chat titles and
folder names, token counts by five minutes, the readings of the limit. Standard library only, Python 3.9.

Files in Tokenometr's folder:
  team-state.json  what this program keeps: the id of this Mac, what the site asked for, what it has sent
  team.json        what the window shows: the state of the connection and the numbers of the teams
The token is not in a file: it lives in the login keychain, item local.tokenometr.team.
"""
import argparse
import fcntl
import gzip
import hashlib
import json
import os
import plistlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

import usage

PROTOCOL = 1
DEFAULT_SERVER = "https://tokenometr.archik.tech"
KEYCHAIN_SERVICE = "local.tokenometr.team"
SECURITY = "/usr/bin/security"
BUCKET_SECONDS = 300
DAY_SECONDS = 86400
GZIP_FROM = 32 * 1024  # smaller bodies go as they are
MAX_BODY = 8 * 1024 * 1024  # the site takes 10 MB of JSON; a body this big is cut in parts
PART_DAYS = 30
READINGS_DAYS = 60
FULL_EVERY = 7 * DAY_SECONDS  # a whole send now and then mends whatever drifted
TIMEOUT = 30
STATE_VERSION = 1
VIEW_VERSION = 1
TEXT_LIMITS = {"title": 300, "folder": 200, "label": 200, "name": 120}


class ApiError(Exception):
    """The site answered, but not with success."""

    def __init__(self, status, code, message):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class Offline(Exception):
    """The site did not answer at all."""


class Keychain:
    """The token of the connection in the login keychain, through /usr/bin/security.

    The secret goes in through the standard input of `security -i`, so it is never in a process list.
    """

    def __init__(self, service=KEYCHAIN_SERVICE, tool=SECURITY):
        self.service, self.tool = service, tool

    def get(self, account):
        try:
            done = subprocess.run([self.tool, "find-generic-password", "-s", self.service, "-a", account, "-w"],
                                  capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return None
        secret = done.stdout.strip()
        return secret if done.returncode == 0 and secret else None

    def set(self, account, secret):
        quoted = [json.dumps(part) for part in (self.service, account, secret)]
        command = "add-generic-password -U -s {} -a {} -w {}\n".format(*quoted)
        done = subprocess.run([self.tool, "-i"], input=command, capture_output=True, text=True, timeout=20)
        if done.returncode != 0:
            raise OSError("the keychain refused the token")

    def delete(self, account):
        try:
            subprocess.run([self.tool, "delete-generic-password", "-s", self.service, "-a", account],
                           capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            pass


class Api:
    """The site, with the token of this Mac when it has one."""

    def __init__(self, server, token=None, version="dev"):
        self.server = server.rstrip("/")
        self.token = token
        self.agent = f"Tokenometr/{version}"

    def call(self, method, path, body=None, query=None):
        """(status, parsed JSON or None); raises ApiError for an answer that is not 2xx, Offline for none."""
        url = self.server + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data, headers = None, {"Accept": "application/json", "User-Agent": self.agent}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if len(data) > GZIP_FROM:
                data = gzip.compress(data, 6)
                headers["Content-Encoding"] = "gzip"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            code, message = f"http_{e.code}", e.reason
            try:
                error = json.loads(e.read()).get("error") or {}
                code, message = error.get("code") or code, error.get("message") or message
            except (ValueError, AttributeError):
                pass
            raise ApiError(e.code, code, str(message)) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise Offline(str(getattr(e, "reason", e))) from None


class Paths:
    def __init__(self, home, projects, sessions, history, cache):
        self.home = home
        self.state = os.path.join(home, "team-state.json")
        self.view = os.path.join(home, "team.json")
        self.lock = os.path.join(home, "team.lock")
        self.readings = os.path.join(home, "readings.json")
        self.accounts = os.path.join(home, "accounts.json")
        self.labels = os.path.join(home, usage.LABELS_FILE)
        self.projects, self.sessions, self.history, self.cache = projects, sessions, history, cache


def default_paths():
    home = os.path.expanduser("~")
    support = os.path.join(home, "Library", "Application Support")
    return Paths(os.path.join(support, "Tokenometr"), os.path.join(home, ".claude", "projects"),
                 os.path.join(support, "Claude", "claude-code-sessions"),
                 os.path.join(support, "Claude", "plan-usage-history.json"),
                 os.path.join(home, "Library", "Caches", "Tokenometr", "scan-cache.json"))


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_state(paths):
    state = load_json(paths.state)
    if state.get("version") != STATE_VERSION:
        return {"version": STATE_VERSION, "connected": False, "sent": {}}
    state.setdefault("sent", {})
    return state


def save_state(paths, state):
    usage.write_json(paths.state, state)


def save_view(paths, state, now_ts, teams=None, fetched=None, error=None, mode=None):
    """What the window shows. `teams` and `fetched` are kept from the last good fetch when not given."""
    old = load_json(paths.view)
    view = {
        "version": VIEW_VERSION,
        "connected": bool(state.get("connected")) or mode == "revoked",
        "state": mode or ("error" if error else "ok"),
        "message": error,
        "site": state.get("server"),
        "machine": state.get("machine"),
        "user": state.get("user"),
        "syncedAt": now_ts if not error and mode is None else old.get("syncedAt"),
        "fetchedAt": fetched if fetched is not None else old.get("fetchedAt"),
        "teams": teams if teams is not None else old.get("teams") or [],
    }
    usage.write_json(paths.view, view)
    return view


def mac_name():
    try:
        done = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"], capture_output=True, text=True,
                              timeout=5)
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return socket.gethostname().split(".")[0] or "Mac"


def app_version():
    """The version of the Tokenometr this script is part of, from the Info.plist of the app."""
    plist = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Info.plist")
    try:
        with open(plist, "rb") as f:
            return str(plistlib.load(f).get("CFBundleShortVersionString") or "dev")
    except (OSError, ValueError):
        return "dev"


def local_zone():
    """The name of this Mac's time zone, such as Europe/Moscow, which the site counts "today" in."""
    for candidate in (os.path.realpath("/etc/localtime"), os.environ.get("TZ", "")):
        if "zoneinfo/" in candidate:
            return candidate.split("zoneinfo/", 1)[1]
        if candidate and "/" in candidate and not candidate.startswith("/"):
            return candidate
    return "UTC"


def clip(text, field):
    return (text or "")[:TEXT_LIMITS[field]]


def bucket_of(moment):
    return int(moment // BUCKET_SECONDS) * BUCKET_SECONDS


def usage_rows(calls_by_row, orgs):
    """{(chat, account, bucket, kind): [input, cache write, cache read, output, calls]} for these accounts."""
    rows = {}
    for rid, calls in calls_by_row.items():
        for call, is_sub, _path, made_on in calls.values():
            if made_on not in orgs:
                continue
            total = rows.setdefault((rid, made_on, bucket_of(call[1]), "sub" if is_sub else "main"), [0, 0, 0, 0, 0])
            for index in range(4):
                total[index] += call[2 + index]
            total[4] += 1
    return rows


def chat_infos(sessions, calls_by_row, orgs):
    """What the site is told of each chat that has usage on these accounts."""
    infos = {}
    for rid, calls in calls_by_row.items():
        last, subs = None, set()
        for call, is_sub, path, made_on in calls.values():
            if made_on not in orgs:
                continue
            last = call[1] if last is None else max(last, call[1])
            if is_sub:
                subs.add(path)
        if last is None:
            continue
        meta = sessions[rid]["meta"] if rid in sessions else {}
        infos[rid] = {"key": rid, "title": clip("Другое" if rid == usage.OTHER else meta.get("title"), "title"),
                      "folder": clip(usage.folder_name(meta.get("cwd")), "folder"),
                      "archived": bool(meta.get("isArchived")), "subagents": len(subs), "lastActivity": last}
    return infos


def seen_accounts(paths, readings, calls_by_row):
    """[{org, label}] of every account this Mac has seen: the person alone sees this list on the site."""
    configured = usage.load_accounts_config(paths.accounts)
    labels = usage.load_labels(paths.labels)
    orgs = [r.get("org") for r in readings]
    orgs += [made_on for calls in calls_by_row.values() for _c, _s, _p, made_on in calls.values()]
    orgs += list(configured)
    seen = []
    for org in dict.fromkeys(o for o in orgs if isinstance(o, str) and o):
        label = (configured.get(org) or {}).get("name") or labels.get(org) or "Аккаунт " + org[:8]
        seen.append({"org": org, "label": clip(label, "label")})
    return seen


def reading_rows(readings, orgs, now_ts):
    rows = []
    for r in readings:
        if r.get("org") not in orgs or r.get("t", 0) / 1000 < now_ts - READINGS_DAYS * DAY_SECONDS:
            continue
        levels = r.get("u") or {}
        week, session = levels.get("sd"), levels.get("fh")
        if week is None and session is None:
            continue
        row = {"org": r["org"], "t": r["t"] / 1000, "source": "cli" if r.get("source") == "cli" else "app"}
        if week is not None:
            row["week"] = int(week)
        if session is not None:
            row["session"] = int(session)
        if r.get("weekResetsAt"):
            row["resetsAt"] = r["weekResetsAt"]
        rows.append(row)
    return rows


def day_digests(rows):
    """{org: {day: digest}} of the usage rows: a day changed when its digest did."""
    days = {}
    for (chat, org, bucket, kind), counts in rows.items():
        days.setdefault(org, {}).setdefault(bucket // DAY_SECONDS, []).append([chat, bucket, kind] + counts)
    return {org: {str(day): hashlib.sha1(json.dumps(sorted(parts), separators=(",", ":")).encode()).hexdigest()
                  for day, parts in by_day.items()} for org, by_day in days.items()}


def wire_usage(rows, keep):
    """The usage rows the site takes: `keep` says which of the (chat, account, bucket, kind) to send."""
    return [{"chat": chat, "org": org, "t": bucket, "kind": kind, "tokens": counts[:4], "calls": counts[4]}
            for (chat, org, bucket, kind), counts in sorted(rows.items(), key=lambda item: item[0][2])
            if keep(org, bucket // DAY_SECONDS)]


def post_parts(api, payload):
    """POST /api/sync; a body the site finds too big is sent again in parts of 30 days."""
    try:
        return api.call("POST", "/api/sync", payload)[1]
    except ApiError as e:
        if e.status != 413 or not payload["usage"]:
            raise
    parts = {}
    for row in payload["usage"]:
        parts.setdefault(row["t"] // DAY_SECONDS // PART_DAYS, []).append(row)
    answer = None
    for number, key in enumerate(sorted(parts)):
        # `full` replaces what the site holds, so only the first part may say it.
        part = dict(payload, usage=parts[key], full=payload["full"] if number == 0 else [],
                    readings=payload["readings"] if number == 0 else [], chats=payload["chats"])
        answer = api.call("POST", "/api/sync", part)[1]
    return answer


def send(paths, state, api, collected, now_ts):
    """One exchange with the site; returns what it answered. Updates `state` but does not save it."""
    sessions, calls_by_row, readings = collected
    machine = state["machine"]
    shared = [o for o in state.get("shared") or [] if isinstance(o, str)]
    need_full = set(state.get("needFull") or [])
    now_full = state.get("lastFull") or 0
    if shared and now_ts - now_full >= FULL_EVERY:
        need_full.update(shared)
    rows = usage_rows(calls_by_row, set(shared))
    digests = day_digests(rows)
    sent = state["sent"]
    full = [org for org in shared if org in need_full]

    def changed(org, day):
        return org in full or sent.get(org, {}).get(str(day)) != digests.get(org, {}).get(str(day))

    wire = wire_usage(rows, changed)
    chats = chat_infos(sessions, calls_by_row, set(shared))
    payload = {
        "protocol": PROTOCOL,
        "machine": {"id": machine["id"], "name": clip(machine.get("name"), "name"), "app": app_version()},
        "accounts": seen_accounts(paths, readings, calls_by_row),
        "full": full,
        "readings": reading_rows(readings, set(shared), now_ts),
        "chats": [chats[chat] for chat in sorted({row["chat"] for row in wire}) if chat in chats],
        "usage": wire,
    }
    answer = post_parts(api, payload)
    for org in full:
        sent[org] = dict(digests.get(org, {}))
    for org in shared:
        if org not in full:
            for day, digest in digests.get(org, {}).items():
                if changed(org, int(day)):
                    sent.setdefault(org, {})[day] = digest
    if full:
        state["lastFull"] = now_ts
    for org in [o for o in sent if o not in (answer.get("shared") or [])]:
        del sent[org]  # no longer shared: whatever is shared again is sent whole
    state["shared"] = answer.get("shared") or []
    state["needFull"] = answer.get("needFull") or []
    state["teams"] = answer.get("teams") or []
    return answer


def fetch_numbers(api):
    return api.call("GET", "/api/sync/team", query={"tz": local_zone()})[1].get("teams") or []


def sync(paths, now=None, keychain=None, server=None, collect=None):
    """One round: send what changed, ask for the numbers of the teams, and write both down.

    Returns the view the window shows. Never raises for problems of the site or the network: they are the
    state of the connection.
    """
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    os.makedirs(paths.home, exist_ok=True)
    with open(paths.lock, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # the window and the background run take turns
        state = load_state(paths)
        if not state.get("connected"):
            return {"version": VIEW_VERSION, "connected": False}
        keychain = keychain or Keychain()
        token = keychain.get(state.get("account") or "")
        if not token:
            state["connected"] = False
            save_state(paths, state)
            return save_view(paths, state, now_ts, teams=[], mode="revoked", error="Mac отключён от команды")
        api = Api(server or state.get("server") or DEFAULT_SERVER, token, app_version())
        collected = (collect or usage.collect)(paths.projects, paths.sessions, paths.cache, paths.history,
                                               paths.readings)
        try:
            # A first look tells which accounts the teams count, then those are sent whole at once.
            for _ in range(2):
                answer = send(paths, state, api, collected, now_ts)
                if not state["needFull"]:
                    break
            teams = fetch_numbers(api) if state["shared"] else []
            save_state(paths, state)
            return save_view(paths, state, now_ts, teams=teams, fetched=now_ts)
        except ApiError as e:
            if e.status == 401:
                keychain.delete(state.get("account") or "")
                state["connected"] = False
                save_state(paths, state)
                return save_view(paths, state, now_ts, teams=[], mode="revoked", error="Mac отключён от команды")
            if e.status == 426:
                return save_view(paths, state, now_ts, mode="update", error="Обновите Токенометр")
            return save_view(paths, state, now_ts, error=f"Сайт ответил ошибкой {e.status}")
        except Offline:
            return save_view(paths, state, now_ts, error="Нет связи с сайтом")


def connect(paths, name=None, server=None, keychain=None, emit=None, sleep=time.sleep, clock=time.time,
            sync_after=True):
    """Ask for a code, show it, and wait until a person confirms it on the site. Returns 0 when connected."""
    emit = emit or (lambda event: None)
    server = (server or DEFAULT_SERVER).rstrip("/")
    keychain = keychain or Keychain()
    name = name or "Токенометр на " + mac_name()
    api = Api(server, version=app_version())
    try:
        started = api.call("POST", "/api/devices/authorizations", {"client_name": name})[1]
    except (ApiError, Offline) as e:
        emit({"state": "error", "message": "Не удалось связаться с сайтом: " + str(e)})
        return 1
    emit({"state": "waiting", "code": started["user_code"], "url": started["verification_url"],
          "expiresIn": started["expires_in"]})
    interval = max(1, int(started.get("interval") or 5))
    deadline = clock() + int(started["expires_in"])
    token = None
    while clock() < deadline:
        sleep(interval)
        try:
            granted = api.call("POST", "/api/devices/token", {"device_code": started["device_code"]})[1]
            token = granted["token"]
            user = granted.get("user") or {}
            break
        except ApiError as e:
            if e.code == "authorization_pending":
                continue
            if e.code == "slow_down":
                interval += 5
                continue
            emit({"state": "expired" if e.code == "expired_token" else "error", "message": e.message})
            return 1
        except Offline:
            continue
    if token is None:
        emit({"state": "expired"})
        return 1

    account = urllib.parse.urlparse(server).netloc
    try:
        keychain.set(account, token)
    except OSError as e:
        emit({"state": "error", "message": str(e)})
        return 1
    os.makedirs(paths.home, exist_ok=True)
    with open(paths.lock, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(paths)
        machine = state.get("machine") or {"id": str(uuid.uuid4())}
        machine["name"] = mac_name()
        state.update({"connected": True, "server": server, "account": account, "machine": machine,
                      "user": {"email": user.get("email"), "name": user.get("display_name")},
                      "shared": [], "needFull": [], "sent": {}})
        save_state(paths, state)
    view = sync(paths, keychain=keychain, server=server) if sync_after else save_view(
        paths, state, clock(), teams=[])
    state = load_state(paths)
    emit({"state": "connected", "teams": state.get("teams") or [], "user": state.get("user"), "view": view})
    return 0


def disconnect(paths, keychain=None):
    """Forgets the token and the team's numbers on this Mac. The site keeps the list of devices: the person
    revokes the token there too, in the settings, under «Устройства»."""
    keychain = keychain or Keychain()
    os.makedirs(paths.home, exist_ok=True)
    with open(paths.lock, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(paths)
        if state.get("account"):
            keychain.delete(state["account"])
        state.update({"connected": False, "shared": [], "needFull": [], "sent": {}, "teams": []})
        save_state(paths, state)
        usage.write_json(paths.view, {"version": VIEW_VERSION, "connected": False})


def status(paths, keychain=None):
    state = load_state(paths)
    view = load_json(paths.view)
    return {"connected": bool(state.get("connected")), "server": state.get("server"),
            "user": state.get("user"), "machine": state.get("machine"), "shared": state.get("shared") or [],
            "state": view.get("state"), "message": view.get("message"), "syncedAt": view.get("syncedAt"),
            "teams": [s.get("team") for s in view.get("teams") or [] if isinstance(s, dict)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["connect", "sync", "disconnect", "status"])
    parser.add_argument("--name", help="how this Mac is called in the site's list of devices")
    parser.add_argument("--server", default=os.environ.get("TOKENOMETR_SERVER") or None)
    parser.add_argument("--home", help="Tokenometr's data folder")
    args = parser.parse_args()
    paths = default_paths()
    if args.home:
        paths = Paths(args.home, paths.projects, paths.sessions, paths.history, paths.cache)

    def emit(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)

    if args.command == "connect":
        sys.exit(connect(paths, args.name, args.server, emit=emit))
    if args.command == "sync":
        emit(sync(paths, server=args.server))
    elif args.command == "disconnect":
        disconnect(paths)
        emit({"connected": False})
    else:
        emit(status(paths))


if __name__ == "__main__":
    main()
