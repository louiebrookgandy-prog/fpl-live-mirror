#!/usr/bin/env python3
"""Fetch the FPL API and write the JSON to disk, for committing to a git repo.

WHY THIS EXISTS
The FPL decision system runs in a sandbox behind an allowlisting proxy. Verified
2026-08-25: github.com and pypi.org answer, fantasy.premierleague.com and even
raw.githubusercontent.com are refused at the connection (curl exit 56). So the
only way live data reaches the scheduled job is as a git clone from github.com.
This script runs on a GitHub Actions runner, which has ordinary internet, and
commits the API responses into the repo. The job then clones the repo.

WHAT IT WRITES (into --out, default ./data)
  bootstrap.json      /api/bootstrap-static/     public
  fixtures.json       /api/fixtures/             public
  entry.json          /api/entry/{id}/           public
  history.json        /api/entry/{id}/history/   public
  picks_gw{n}.json    /api/entry/{id}/event/{n}/picks/   public, per finished GW
  transfers.json      /api/entry/{id}/transfers/  public, FULL transfer history
  my_team.json        /api/my-team/{id}/         LOGIN REQUIRED
  status.json         what succeeded, what did not, and when

WHY transfers.json (added 2026-09-01, after a hand-pasted squad went stale and
wrong). picks_gw{n} only shows the squad LOCKED at gameweek n's deadline, so
between deadlines it is blind to any transfer already made. transfers.json is
the full, public, ordered log of every transfer on the entry (element in,
element out, event, timestamp), including wildcard transfers (cost 0). Replayed
on top of picks_gw1 (the first locked squad) it reconstructs the CURRENT squad
at any point in the season, live, with no login and no dependence on a deadline
having passed. See ingest_live_mirror.py:squad_from_transfers.

STATUS.JSON IS NOT OPTIONAL DECORATION. It is the whole point of the honesty of
this thing: an authentication failure here would otherwise show up in the
weekly report as silence, and silence reads exactly like "nothing has changed".
The consumer (src/ingest_live_mirror.py) reads status.json and says out loud
which endpoints are stale and why.

ON THE LOGIN
`my-team` is the only endpoint that needs one, and it is the only way to see
transfers you have already made but that have not yet passed a deadline. Two
routes are supported, tried in this order:

  1. FPL_COOKIE   a raw Cookie header copied from a logged-in browser. Survives
                  bot protection because it IS a real browser session. Expires,
                  so it needs re-pasting occasionally.
  2. FPL_EMAIL + FPL_PASSWORD  a scripted login. Self-renewing when it works,
                  but FPL sits behind bot protection that frequently rejects
                  datacentre IPs, and a GitHub runner is a datacentre IP. Treat
                  this as the nice-to-have, not the plan.

Neither present, or both failing, is NOT an error: the public files are still
written, the run still succeeds, and status.json records the reason. Nothing
downstream is allowed to depend on my_team.json existing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://fantasy.premierleague.com/api"
LOGIN = "https://users.premierleague.com/accounts/login/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get(sess: requests.Session, url: str, tries: int = 4):
    """GET with a short backoff. FPL rate-limits and occasionally 403s a first
    request that a second identical one serves fine, so a bare failure is not
    evidence of anything until it has been asked a few times."""
    last = None
    for i in range(tries):
        try:
            r = sess.get(url, timeout=30)
            if r.status_code == 200:
                return r.json(), None
            last = f"HTTP {r.status_code}"
        except Exception as e:                                 # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(2 * (i + 1))
    return None, last


def write(out: Path, name: str, obj) -> int:
    p = out / name
    p.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    return p.stat().st_size


def authenticate(sess: requests.Session) -> tuple[bool, str]:
    cookie = os.environ.get("FPL_COOKIE", "").strip()
    if cookie:
        sess.headers["Cookie"] = cookie
        return True, "cookie"
    email = os.environ.get("FPL_EMAIL", "").strip()
    pw = os.environ.get("FPL_PASSWORD", "").strip()
    if not (email and pw):
        return False, "no FPL_COOKIE and no FPL_EMAIL/FPL_PASSWORD set"
    try:
        r = sess.post(LOGIN, data={
            "login": email, "password": pw,
            "app": "plfpl-web", "redirect_uri": "https://fantasy.premierleague.com/a/login",
        }, timeout=30)
        if "pl_profile" in sess.cookies.get_dict() or r.status_code in (200, 302):
            return True, "password"
        return False, f"login returned HTTP {r.status_code}"
    except Exception as e:                                     # noqa: BLE001
        return False, f"login failed: {type(e).__name__}: {e}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--entry", default=os.environ.get("FPL_ENTRY_ID", "420549"),
                    help="your FPL team id (public)")
    ap.add_argument("--out", default="data")
    ap.add_argument("--back", type=int, default=3,
                    help="how many finished gameweeks of picks to keep")
    a = ap.parse_args(argv)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    entry = str(int(a.entry))

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept": "application/json"})

    status = {"fetched_at": now(), "entry": entry, "ok": [], "failed": {},
              "auth": None, "current_gw": None, "next_gw": None}

    bs, err = get(sess, f"{BASE}/bootstrap-static/")
    if bs is None:
        # Without bootstrap there is no gameweek to anchor anything to, so stop
        # rather than commit a half-mirror that looks complete.
        status["failed"]["bootstrap"] = err
        write(out, "status.json", status)
        print(f"FATAL: bootstrap-static failed ({err})", file=sys.stderr)
        return 1
    status["ok"].append("bootstrap")
    write(out, "bootstrap.json", bs)

    events = bs.get("events") or []
    cur = next((e["id"] for e in events if e.get("is_current")), None)
    nxt = next((e["id"] for e in events if e.get("is_next")), None)

    # WHICH GAMEWEEKS HAVE PICKS. Keyed on the DEADLINE HAVING PASSED, not on
    # the `finished` flag. Picks become public the moment the deadline goes;
    # `finished` is set later, after bonus points and data checking, and FPL is
    # in no hurry about it. First run of this mirror, 2026-08-25 at 10:14 UTC:
    # GW1 had been over for fifteen hours, every match played, and the flag was
    # still False, so a finished-only rule fetched no picks at all and the
    # decision system had no squad. Same trap the scheduled job already handles
    # for results ("the clock, not the flag"); this is the same lesson learned
    # twice in one week, in two different files.
    passed = []
    for e in events:
        dl = e.get("deadline_time")
        if not dl:
            continue
        try:
            t = datetime.fromisoformat(str(dl).replace("Z", "+00:00"))
        except ValueError:
            continue
        if t < datetime.now(timezone.utc):
            passed.append(e["id"])
    status["current_gw"], status["next_gw"] = cur, nxt
    status["deadlines_passed"] = passed[-6:]

    for name, url in (("fixtures", f"{BASE}/fixtures/"),
                      ("entry", f"{BASE}/entry/{entry}/"),
                      ("history", f"{BASE}/entry/{entry}/history/")):
        obj, err = get(sess, url)
        if obj is None:
            status["failed"][name] = err
        else:
            status["ok"].append(name)
            write(out, f"{name}.json", obj)

    for gw in sorted(passed)[-max(1, a.back):]:
        obj, err = get(sess, f"{BASE}/entry/{entry}/event/{gw}/picks/")
        if obj is None:
            status["failed"][f"picks_gw{gw}"] = err
        else:
            status["ok"].append(f"picks_gw{gw}")
            write(out, f"picks_gw{gw}.json", obj)

    # Full transfer history, public, no login. This is what lets the consumer
    # reconstruct the CURRENT squad between deadlines, when picks_gw{n} is
    # already out of date. See the module docstring.
    obj, err = get(sess, f"{BASE}/entry/{entry}/transfers/")
    if obj is None:
        status["failed"]["transfers"] = err
    else:
        status["ok"].append("transfers")
        write(out, "transfers.json", obj)

    # ---- the authenticated bit, which is allowed to fail.
    authed, how = authenticate(sess)
    status["auth"] = how
    if authed:
        obj, err = get(sess, f"{BASE}/my-team/{entry}/", tries=2)
        if obj is None:
            status["failed"]["my_team"] = err
            status["auth"] = f"{how} (accepted but my-team refused: {err})"
        else:
            status["ok"].append("my_team")
            write(out, "my_team.json", obj)
    else:
        status["failed"]["my_team"] = how

    write(out, "status.json", status)
    print(f"ok: {', '.join(status['ok'])}")
    if status["failed"]:
        print("failed: " + json.dumps(status["failed"]))
    # A missing my_team is a degraded run, not a broken one. Only a missing
    # bootstrap is fatal, and that returned above.
    return 0


if __name__ == "__main__":
    sys.exit(main())
