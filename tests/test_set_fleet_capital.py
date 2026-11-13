import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("set_fleet_capital", ROOT / "scripts" / "set_fleet_capital.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

REAL = json.loads((ROOT / "config" / "capital.json").read_text())


# A fixed ten-bot table for the script's own behaviour, so these tests do not move when the shipped table changes.
TABLE = {"total_account_usd": None, "bots": [
    {"bot": "apex_chronos", "repo": "samedame/Apex_Chronos", "variables": ["ACCOUNT_CAPITAL"], "usd": None},
    {"bot": "apex_sniper", "repo": "samedame/Apex-Sniper", "variables": ["ACCOUNT_CAPITAL"], "usd": None},
    {"bot": "apex_omega", "repo": "samedame/Apex_Omega", "variables": ["ACCOUNT_EQUITY"], "usd": None},
    {"bot": "apex_master", "repo": "samedame/Apex-Master", "variables": ["ACCOUNT_SIZE"], "usd": None},
    {"bot": "spy_bps", "repo": "samedame/spy-bps-bot", "variables": ["ACCOUNT_EQUITY"], "usd": None},
    {"bot": "ibs_dip_calls", "repo": "samedame/robinhood-1500-options-strategy", "variables": ["ACCOUNT_EQUITY"], "usd": None},
    {"bot": "options_scanner", "repo": "samedame/Daily-Stock-Options-Alert", "variables": ["ACCOUNT_CAPITAL_FOR_SIZING"], "usd": None},
    {"bot": "premium_sleeve", "repo": "samedame/Premium-Sleeve", "variables": ["ACCOUNT_CAPITAL_FOR_SIZING"], "usd": None},
    {"bot": "tier1_bot", "repo": "samedame/Tier1-Bot", "variables": ["ACCOUNT_CAPITAL_USD"], "usd": None},
    {"bot": "condor", "repo": "samedame/daytrading_bot", "variables": ["CONDOR_BUYING_POWER"], "usd": None},
]}


def nulled():
    return copy.deepcopy(TABLE)


def filled(total=10000, each=1500):
    cfg = copy.deepcopy(TABLE)
    cfg["total_account_usd"] = total
    for b in cfg["bots"]:
        b["usd"] = each
    return cfg


class FakeGh:
    def __init__(self, fail_repo=None):
        self.calls, self.fail_repo = [], fail_repo

    def __call__(self, cmd, input_text=None):
        self.calls.append(cmd)
        if self.fail_repo and cmd[-1] == self.fail_repo:
            return 1, "", "HTTP 403: Resource not accessible\n"
        return 0, "", ""


def write(tmp_path, cfg):
    p = tmp_path / "capital.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def test_shipped_table_is_filled_and_lists_only_running_bots():
    # Set on 2026-11-13: $1,500, every running bot sized against the whole account.
    assert REAL["total_account_usd"] == 1500
    assert all(b["usd"] == 1500 for b in REAL["bots"])
    assert mod.validate(REAL) == []
    names = {v for b in REAL["bots"] for v in b["variables"]}
    assert names == {"ACCOUNT_CAPITAL", "ACCOUNT_EQUITY", "ACCOUNT_SIZE", "ACCOUNT_CAPITAL_FOR_SIZING"}
    repos = {b["repo"].split("/")[1] for b in REAL["bots"]}
    assert not repos & {"Apex-Sniper", "Apex_Omega", "spy-bps-bot", "Daily-Stock-Options-Alert", "Tier1-Bot", "daytrading_bot"}


def test_shipped_table_dry_run_plans_one_variable_per_entry(capsys):
    gh = FakeGh()
    assert mod.main([], run=gh) == 0
    assert gh.calls == []
    out = capsys.readouterr().out
    assert "overcommit ratio: 5.00" in out and "5 Variable(s) would be written" in out


def test_refuses_on_nulls_and_calls_nothing(tmp_path, capsys):
    gh = FakeGh()
    assert mod.main(["--config", write(tmp_path, nulled()), "--apply"], run=gh) == 2
    assert gh.calls == []
    assert "not filled in" in capsys.readouterr().err


def test_refuses_when_one_bot_is_null(tmp_path):
    cfg = filled()
    cfg["bots"][3]["usd"] = None
    gh = FakeGh()
    assert mod.main(["--config", write(tmp_path, cfg), "--apply"], run=gh) == 2
    assert gh.calls == []


def test_refuses_when_total_is_null(tmp_path):
    cfg = filled()
    cfg["total_account_usd"] = None
    assert mod.main(["--config", write(tmp_path, cfg)], run=FakeGh()) == 2


@pytest.mark.parametrize("bad", [0, -5, "1500", True, float("nan")])
def test_rejects_non_positive_or_non_numeric(tmp_path, bad):
    cfg = filled()
    cfg["bots"][0]["usd"] = bad
    assert mod.validate(cfg)


def test_dry_run_writes_nothing(tmp_path, capsys):
    gh = FakeGh()
    assert mod.main(["--config", write(tmp_path, filled())], run=gh) == 0
    assert gh.calls == []
    assert "Dry run" in capsys.readouterr().out


def test_apply_sets_each_variable_with_exact_gh_command(tmp_path):
    gh = FakeGh()
    assert mod.main(["--config", write(tmp_path, filled(10000, 1500)), "--apply"], run=gh) == 0
    assert len(gh.calls) == 10
    assert ["gh", "variable", "set", "ACCOUNT_CAPITAL", "--body", "1500", "--repo", "samedame/Apex_Chronos"] in gh.calls
    assert all(c[:3] == ["gh", "variable", "set"] for c in gh.calls)
    assert not any("secret" in c for c in gh.calls)


def test_overcommit_ratio_and_warning(tmp_path, capsys):
    # 10 bots x 1500 = 15000 vs 10000 account -> 1.50
    assert mod.main(["--config", write(tmp_path, filled(10000, 1500))], run=FakeGh()) == 0
    out = capsys.readouterr().out
    assert "overcommit ratio: 1.50" in out and "WARNING: overcommitted" in out


def test_no_warning_when_not_overcommitted(tmp_path, capsys):
    assert mod.main(["--config", write(tmp_path, filled(20000, 1500))], run=FakeGh()) == 0
    out = capsys.readouterr().out
    assert "overcommit ratio: 0.75" in out and "WARNING" not in out


def test_failure_is_reported_and_exit_code_nonzero(tmp_path, capsys):
    gh = FakeGh(fail_repo="samedame/Tier1-Bot")
    assert mod.main(["--config", write(tmp_path, filled()), "--apply"], run=gh) == 1
    out = capsys.readouterr().out
    assert "FAIL samedame/Tier1-Bot" in out and "9 written, 1 failed" in out


def test_only_limits_repos(tmp_path):
    gh = FakeGh()
    assert mod.main(["--config", write(tmp_path, filled()), "--apply", "--only", "Tier1-Bot,Apex_Omega"], run=gh) == 0
    assert sorted(c[-1] for c in gh.calls) == ["samedame/Apex_Omega", "samedame/Tier1-Bot"]


def test_fractional_value_formatting():
    assert mod.fmt_usd(1500) == "1500" and mod.fmt_usd(1500.0) == "1500" and mod.fmt_usd(1234.5) == "1234.50"
