"""
Unit tests for auth.py's password hashing. Covers the scrypt maxmem gotcha
in CLAUDE.md: "OpenSSL caps scrypt memory at 32MB by default, and
n=2**15, r=8 exceeds it - hashing fails outright ... unless maxmem is
passed explicitly." A regression here would raise on every login, so it's
worth a fast, explicit check rather than only discovering it via a live
login attempt.
"""

import auth


def test_hash_and_verify_round_trip():
    encoded = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", encoded)


def test_verify_rejects_wrong_password():
    encoded = auth.hash_password("correct horse battery staple")
    assert not auth.verify_password("wrong password", encoded)


def test_hash_does_not_raise_memory_limit_error():
    # This is the actual regression this test exists to catch: omitting
    # maxmem makes hashlib.scrypt raise "memory limit exceeded" at n=2**15,
    # r=8 under OpenSSL's default 32MB cap - see the module's own comment.
    auth.hash_password("any password")
