import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("resolve_provider", ROOT / "scripts" / "resolve_provider.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def test_token_present_uses_tradier(tmp_path):
    out = tmp_path / "o"
    assert mod.main({"TRADIER_TOKEN": "x", "GITHUB_OUTPUT": str(out)}, notify=lambda t: 1 / 0) == 0
    assert out.read_text() == "provider=tradier\n"


def test_missing_token_fails_and_notifies():
    sent = []
    assert mod.main({}, notify=sent.append) == 1
    assert len(sent) == 1 and "TRADIER_TOKEN" in sent[0] and "yfinance" in sent[0]


def test_blank_token_counts_as_missing():
    assert mod.main({"TRADIER_TOKEN": "  "}, notify=lambda t: None) == 1


def test_fallback_allowed_is_yfinance_with_warning(tmp_path, capsys):
    out = tmp_path / "o"
    assert mod.main({"PROVIDER_ALLOW_FALLBACK": "1", "GITHUB_OUTPUT": str(out)}, notify=lambda t: 1 / 0) == 0
    assert out.read_text() == "provider=yfinance\n"
    assert "WARNING" in capsys.readouterr().out


def test_fallback_flag_must_be_exactly_one():
    assert mod.main({"PROVIDER_ALLOW_FALLBACK": "true"}, notify=lambda t: None) == 1


def test_notify_failure_cannot_change_exit_code():
    def boom(_):
        raise RuntimeError("discord down")
    assert mod.main({}, notify=boom) == 1


def test_no_webhook_is_fine(capsys):
    assert mod.main({}) == 1
    assert "ERROR" in capsys.readouterr().err


def test_message_never_contains_secret_values():
    sent = []
    mod.main({"DISCORD_WEBHOOK": "https://discord.test/hook-SECRET", "TRADIER_TOKEN": ""}, notify=sent.append)
    assert "SECRET" not in sent[0]


def test_marker_written_only_when_notice_was_delivered(tmp_path):
    assert mod.main({"MARKER_DIR": str(tmp_path)}, notify=lambda t: True) == 1
    assert (tmp_path / ".notified").exists()
    other = tmp_path / "b"; other.mkdir()
    assert mod.main({"MARKER_DIR": str(other)}, notify=lambda t: False) == 1
    assert not (other / ".notified").exists()
