"""Makes tests/fixtures/auth_old_libs.json: an account (argon2 password hash) and a passkey that has signed in once,
stored by Lanloom's own code with the OLD password and passkey libraries. tests/test_auth_upgrade.py signs in with
both under the current libraries.

Run once in the base image of the Dockerfile with the old pins installed, the repository mounted read-only:
    pip install --no-deps -r <old requirements.txt> && PYTHONPATH=/src:/src/tests python /src/tests/fixtures/auth_old_libs_make.py
The output goes to stdout; nothing here holds a real secret (sample password, key derived from a label)."""
import json
import sys
from importlib import metadata

from lanloom import auth
from lanloom.store import Store
from softkey import ORIGIN, RP_ID, SoftKey

LABEL = "stored before the library update"
PASSWORD = "sample-password-before-update"


def main() -> int:
    store = Store(":memory:")
    accounts, passkeys = auth.Accounts(store), auth.Passkeys(store)
    uid = accounts.create("before-update", PASSWORD, role="admin")
    user = store.one("SELECT * FROM users WHERE id=?", (uid,))
    key = SoftKey(LABEL)
    reg = passkeys.registration_options(dict(user), RP_ID, ORIGIN)
    if not passkeys.registration_verify(uid, reg["ceremony"], key.register(reg["options"]), "Laptop"):
        print("registration refused", file=sys.stderr)
        return 1
    login = passkeys.login_options(RP_ID, ORIGIN)
    if passkeys.login_verify(login["ceremony"], key.sign_in(login["options"], user_handle=user["user_handle"])) != uid:
        print("sign-in refused", file=sys.stderr)
        return 1
    row = store.one("SELECT * FROM passkeys WHERE user_id=?", (uid,))
    user = store.one("SELECT * FROM users WHERE id=?", (uid,))
    out = {
        "made_with": {name: metadata.version(name) for name in ("argon2-cffi", "webauthn", "cbor2", "cryptography")},
        "password": PASSWORD,
        "softkey_label": LABEL,
        "user": {"username": user["username"], "pw_hash": user["pw_hash"], "user_handle": user["user_handle"].hex(),
                 "role": user["role"]},
        "passkey": {"credential_id": row["credential_id"].hex(), "public_key": row["public_key"].hex(),
                    "sign_count": row["sign_count"], "name": row["name"]},
    }
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
