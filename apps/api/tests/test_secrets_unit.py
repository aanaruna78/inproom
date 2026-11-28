"""INPROOM-224: secrets never reach a memory snapshot; ordinary identifiers and prose are left alone."""

import pytest

from app.content.secrets import scrub

AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
GH = "ghp_" + "a" * 36
SLACK = "xoxb-" + "1234567890-abcdef"
JWT = "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("text", "expected", "kind"),
    [
        (
            f"Deploy key {AWS} for the bucket",
            "Deploy key [REDACTED:AWS_ACCESS_KEY] for the bucket",
            "AWS_ACCESS_KEY",
        ),
        ("Maps key AIza" + "B" * 35, "Maps key [REDACTED:GOOGLE_API_KEY]", "GOOGLE_API_KEY"),
        (f"token {GH}", "token [REDACTED:GITHUB_TOKEN]", "GITHUB_TOKEN"),
        (f"bot {SLACK} here", "bot [REDACTED:SLACK_TOKEN] here", "SLACK_TOKEN"),
        ("ATATT" + "3xFfGF0" * 4, "[REDACTED:ATLASSIAN_TOKEN]", "ATLASSIAN_TOKEN"),
        ("use sk-proj-" + "q" * 24, "use [REDACTED:API_KEY]", "API_KEY"),
        (f"jwt {JWT}", "jwt [REDACTED:JWT]", "JWT"),
        (
            "Authorization: Bearer abcdefghijklmnop1234",
            "Authorization: Bearer [REDACTED:BEARER_TOKEN]",
            "BEARER_TOKEN",
        ),
        (
            "postgres://app:hunter22@db:5432/x",
            "postgres://app:[REDACTED:URL_PASSWORD]@db:5432/x",
            "URL_PASSWORD",
        ),
        ("password = Sup3rS3cret!", "password = [REDACTED:ASSIGNED_SECRET]", "ASSIGNED_SECRET"),
        ('api_key: "abcd1234"', 'api_key: "[REDACTED:ASSIGNED_SECRET]"', "ASSIGNED_SECRET"),
        (f"key:\n{KEY}\nend", "key:\n[REDACTED:PRIVATE_KEY]\nend", "PRIVATE_KEY"),
    ],
)
def test_each_secret_kind_is_redacted_and_counted(text, expected, kind):
    out, counts = scrub(text)
    assert out == expected and counts == {kind: 1}
    assert scrub(out) == (out, {})  # idempotent: redacting twice changes nothing


def test_identifiers_and_prose_are_not_secrets():
    for text in (
        "doc 0fd38cf2-3adf-4fae-999b-d4bd8ac58f92 at commit 0493718a8cf7e2b2f02cc819a3a47295dcea6cab",
        "sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "Our password policy requires rotation; rotate the token every 90 days.",
        "Set the password in the vault, never here. ABC-12 and PAY-17 are tracked.",
        "https://acme.atlassian.net/wiki/spaces/ENG/pages/101/Architecture",
    ):
        assert scrub(text) == (text, {}), text


def test_several_secrets_in_one_text():
    out, counts = scrub(f"{AWS} and {GH} and password=abcd1234")
    assert AWS not in out and GH not in out and "abcd1234" not in out
    assert counts == {"AWS_ACCESS_KEY": 1, "GITHUB_TOKEN": 1, "ASSIGNED_SECRET": 1}
