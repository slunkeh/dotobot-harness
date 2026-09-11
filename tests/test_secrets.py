"""Secrets store helpers: presence-only lookups and display titles."""

from harness.secrets import secret_display_title


def test_secret_display_title_prefers_explicit():
    assert (
        secret_display_title("SMTP_PASSWORD", "SMTP password for mail.example.com")
        == "SMTP password for mail.example.com"
    )


def test_secret_display_title_humanizes():
    assert secret_display_title("DEMO_TOKEN") == "Demo token"
    assert secret_display_title("SMTP_PASSWORD") == "SMTP password"
    assert secret_display_title("SSH_PASSPHRASE") == "SSH passphrase"
    assert secret_display_title("AWS_ACCESS_KEY_ID") == "AWS Access Key ID"
    assert secret_display_title("") == "Secret"
