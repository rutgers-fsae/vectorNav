"""Operator PIN creation and verification."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets


SCRYPT_N = 16_384
SCRYPT_R = 8
SCRYPT_P = 1


def hash_pin(pin: str, salt: bytes | None = None) -> dict[str, object]:
    if not (len(pin) == 6 and pin.isdigit()):
        raise ValueError("operator PIN must contain exactly six digits")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        pin.encode(),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
    )
    return {
        "version": 1,
        "algorithm": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": base64.b64encode(salt).decode(),
        "digest": base64.b64encode(digest).decode(),
    }


def verify_pin(pin: str, record: dict[str, object]) -> bool:
    try:
        salt = base64.b64decode(str(record["salt"]), validate=True)
        expected = base64.b64decode(str(record["digest"]), validate=True)
        actual = hashlib.scrypt(
            pin.encode(),
            salt=salt,
            n=int(record["n"]),
            r=int(record["r"]),
            p=int(record["p"]),
        )
        return hmac.compare_digest(actual, expected)
    except (KeyError, TypeError, ValueError):
        return False


def create_auth_file(path: Path, pin: str | None = None) -> str:
    pin = pin or f"{secrets.randbelow(1_000_000):06d}"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(hash_pin(pin)) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, path)
    return pin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--create", type=Path, required=True)
    parser.add_argument("--pin")
    args = parser.parse_args(argv)
    print(f"PIN={create_auth_file(args.create, args.pin)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
