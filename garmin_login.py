"""Connect Running Coach to your Garmin Connect account.

Usage:  uv run python garmin_login.py

Prompts for your Garmin Connect email and password (password is hidden),
plus an MFA code if Garmin sends one. Only the resulting session tokens are
saved, to ~/.garminconnect/ (override with GARMINTOKENS). Your password is
never stored. Run it again whenever the app reports a Garmin login error.
"""
from __future__ import annotations
import getpass
import os
import sys

from garminconnect import Garmin, GarminConnectAuthenticationError

TOKEN_DIR = os.path.expanduser(os.environ.get("GARMINTOKENS", "~/.garminconnect"))


def main() -> int:
    email = input("Garmin email: ").strip()
    if not email:
        print("no email given, aborting")
        return 1
    password = getpass.getpass("Garmin password (hidden): ")
    if not password:
        print("no password given, aborting")
        return 1

    print(f"logging in as {email} ...")
    try:
        g = Garmin(email=email, password=password,
                   prompt_mfa=lambda: input("Garmin MFA code: ").strip())
    except TypeError:
        # older library versions don't accept prompt_mfa in the constructor
        g = Garmin(email, password)

    try:
        # Passing the path makes the client persist fresh tokens automatically
        # once the credentials login succeeds.
        os.makedirs(TOKEN_DIR, exist_ok=True)
        g.login(TOKEN_DIR)
    except GarminConnectAuthenticationError as e:
        print(f"auth failed: {e}")
        return 2

    who = ""
    try:
        who = g.get_full_name() or ""
    except Exception:
        pass

    files = sorted(os.listdir(TOKEN_DIR))
    print(f"OK - tokens saved to {TOKEN_DIR}"
          + (f" (user: {who})" if who else ""))
    print("  files:", ", ".join(files) or "(none)")
    print()
    print("next: start the app (uv run python backend/app.py) and press Refresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
