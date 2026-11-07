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


def filled(total=10000, each=1500):
    cfg = copy.deepcopy(REAL)
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


def test_shipped_table_is_all_null_and_lists_every_known_variable():
    assert REAL["total_account_usd"] is None
    assert all(b["usd"] is None for b in REAL["bots"])
    names = {v for b in REAL["bots"] for v in b["variables"]}
    assert names == {"ACCOUNT_CAPITAL", "ACCOUNT_EQUITY", "ACCOUNT_SIZE", "ACCOUNT_CAPITAL_FOR_SIZING",
                     "ACCOUNT_CAPITAL_USD", "CONDOR_BUYING_POWER"}


def test_refuses_on_shipped_nulls_and_calls_nothing(capsys):
    gh = FakeGh()
    assert mod.main(["--apply"], run=gh) == 2
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
