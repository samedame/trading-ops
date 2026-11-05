import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "set_tradier_secret", Path(__file__).resolve().parent.parent / "scripts" / "set_tradier_secret.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

TOKEN = "TESTTOKEN1234567890abcd"

WORKFLOWS = {
    "daytrading_bot": {".github/workflows/entry.yml": "env:\n  K: ${{ secrets.TRADIER_API_KEY }}\n  D: ${{ secrets.DISCORD_WEBHOOK }}\n"},
    "trading-ops": {".github/workflows/snapshot.yml": "TRADIER_TOKEN: ${{ secrets.TRADIER_TOKEN }}\n"},
    "ibs-callout-bot": {".github/workflows/ibs-bot.yml": "x: ${{ secrets.DISCORD_WEBHOOK }}\n"},
    "no-workflows": {},
    "old": {".github/workflows/a.yml": "${{ secrets.TRADIER_TOKEN }}"},
}
EXISTING = {"trading-ops": ["TRADIER_TOKEN"]}


class FakeGh:
    def __init__(self):
        self.calls, self.stdin_seen, self.fail_repo = [], [], None

    def __call__(self, cmd, input_text=None):
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "repo", "list"]:
            repos = [{"name": n, "isArchived": n == "old", "isFork": False} for n in WORKFLOWS]
            repos.append({"name": "a-fork", "isArchived": False, "isFork": True})
            return 0, json.dumps(repos), ""
        if cmd[:2] == ["gh", "api"] and "/contents/.github/workflows" in cmd[-1] and cmd[-1].endswith("workflows"):
            repo = cmd[-1].split("/")[2]
            files = WORKFLOWS.get(repo, {})
            if not files:
                return 1, "", "404"
            return 0, json.dumps([{"name": p.split("/")[-1], "path": p} for p in files]), ""
        if cmd[:2] == ["gh", "api"]:
            parts = cmd[-1].split("/contents/")
            repo = parts[0].split("/")[2]
            return 0, WORKFLOWS[repo][parts[1]], ""
        if cmd[:3] == ["gh", "secret", "list"]:
            repo = cmd[cmd.index("--repo") + 1].split("/")[1]
            return 0, json.dumps([{"name": n} for n in EXISTING.get(repo, [])]), ""
        if cmd[:3] == ["gh", "secret", "set"]:
            self.stdin_seen.append(input_text)
            repo = cmd[cmd.index("--repo") + 1].split("/")[1]
            return (1, "", "HTTP 403") if repo == self.fail_repo else (0, "", "")
        raise AssertionError(f"unexpected call {cmd}")


def sets(gh):
    return [(c[c.index("--repo") + 1], c[3]) for c in gh.calls if c[:3] == ["gh", "secret", "set"]]


def test_regex_finds_tradier_names_only():
    text = "${{ secrets.TRADIER_API_KEY }} ${{ secrets.DISCORD_WEBHOOK }} ${{secrets.tradier_token}}"
    assert {m.group(1) for m in mod.SECRET_RE.finditer(text)} == {"TRADIER_API_KEY", "tradier_token"}


def test_dry_run_writes_nothing_and_never_asks_for_token(capsys):
    gh = FakeGh()
    rc = mod.main([], run=gh, token_source=lambda: pytest.fail("token requested in dry run"))
    out = capsys.readouterr().out
    assert rc == 0 and sets(gh) == []
    assert "daytrading_bot" in out and "TRADIER_API_KEY" in out and "Dry run" in out
    assert "a-fork" not in out and "old" not in out.split("Scanned")[1].split("\n", 1)[1]  # fork + archived excluded
    assert "already set" in out  # trading-ops secret exists


def test_apply_sets_each_detected_name_and_passes_token_on_stdin_only(capsys):
    gh = FakeGh()
    rc = mod.main(["--apply"], run=gh, token_source=lambda: TOKEN, verify=lambda t: "valid")
    out = capsys.readouterr().out
    assert rc == 0 and sets(gh) == [("samedame/daytrading_bot", "TRADIER_API_KEY")]
    assert gh.stdin_seen == [TOKEN]
    assert TOKEN not in out and all(TOKEN not in " ".join(c) for c in gh.calls)  # never in argv or output


def test_overwrite_replaces_existing(capsys):
    gh = FakeGh()
    mod.main(["--apply", "--overwrite"], run=gh, token_source=lambda: TOKEN, verify=lambda t: "valid")
    assert ("samedame/trading-ops", "TRADIER_TOKEN") in sets(gh)


def test_invalid_token_aborts_before_any_write(capsys):
    gh = FakeGh()
    rc = mod.main(["--apply"], run=gh, token_source=lambda: TOKEN, verify=lambda t: "invalid")
    assert rc == 1 and sets(gh) == []
    assert "rejected" in capsys.readouterr().err


def test_unreachable_verification_continues_with_warning(capsys):
    gh = FakeGh()
    rc = mod.main(["--apply"], run=gh, token_source=lambda: TOKEN, verify=lambda t: "unreachable")
    assert rc == 0 and len(sets(gh)) == 1 and "could not reach" in capsys.readouterr().out


def test_failure_is_reported_and_exit_code_nonzero(capsys):
    gh = FakeGh()
    gh.fail_repo = "daytrading_bot"
    rc = mod.main(["--apply"], run=gh, token_source=lambda: TOKEN, verify=lambda t: "valid")
    out = capsys.readouterr().out
    assert rc == 1 and "FAIL" in out and "HTTP 403" in out and "1 failed" in out


def test_explicit_repos_and_forced_name(capsys):
    gh = FakeGh()
    mod.main(["--repos", "ibs-callout-bot,daytrading_bot", "--secret-name", "TRADIER_TOKEN", "--apply"],
             run=gh, token_source=lambda: TOKEN, verify=lambda t: "valid")
    assert set(sets(gh)) == {("samedame/ibs-callout-bot", "TRADIER_TOKEN"),
                             ("samedame/daytrading_bot", "TRADIER_API_KEY"),
                             ("samedame/daytrading_bot", "TRADIER_TOKEN")}
    gh2 = FakeGh()
    mod.main(["--repos", "ibs-callout-bot"], run=gh2)
    assert "skip: no workflow references" in capsys.readouterr().out


def test_no_matches_message(capsys, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "WORKFLOWS", {"ibs-callout-bot": WORKFLOWS["ibs-callout-bot"]})
    gh = FakeGh()
    assert mod.main([], run=gh, token_source=lambda: pytest.fail("token requested")) == 0
    out = capsys.readouterr().out
    assert "No repo references a Tradier secret" in out and sets(gh) == []


def test_verify_token_classifies_http_errors():
    def opener_401(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

    def opener_500(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 500, "no", {}, None)

    def opener_down(req, timeout=0):
        raise OSError("down")

    class Ok:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    assert mod.verify_token("t", opener_401) == "invalid"
    assert mod.verify_token("t", opener_500) == "unreachable"
    assert mod.verify_token("t", opener_down) == "unreachable"
    assert mod.verify_token("t", lambda req, timeout=0: Ok()) == "valid"
