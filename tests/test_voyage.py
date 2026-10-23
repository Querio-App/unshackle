import io
import re

import pytest
from click.testing import CliRunner

from unshackle import voyage
from unshackle.cli import main


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("NO_COLOR", "CI", "TERM"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_only_real_terminals_get_the_animation(clean_env):
    assert voyage.enabled(FakeTTY())
    assert not voyage.enabled(io.StringIO())  # piped or redirected
    assert not voyage.enabled(FakeTTY(), quiet=True)
    for var, value in (("NO_COLOR", "1"), ("CI", "true"), ("TERM", "dumb")):
        clean_env.setenv(var, value)
        assert not voyage.enabled(FakeTTY())
        clean_env.delenv(var)


def test_voyage_ends_in_open_water_with_messages_above_it():
    out = FakeTTY()
    with voyage.Voyage(["extracting", "writing"], out) as ship:
        ship.say("12 raw files")
        ship.step("writing")
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.getvalue())
    assert "12 raw files" in text
    assert text.rstrip().endswith("✔ made it out")
    last = text.rstrip().splitlines()[-1]
    assert "░" not in last and "▓" not in last  # no ice left ahead of the ship
    assert out.getvalue().endswith("\x1b[?25h")  # cursor restored


def test_failed_voyage_clears_the_ship_and_restores_the_cursor():
    out = FakeTTY()
    with pytest.raises(RuntimeError):
        with voyage.Voyage(["extracting"], out) as ship:
            ship.say("starting")
            raise RuntimeError("boom")
    assert "made it out" not in out.getvalue()
    assert out.getvalue().endswith("\x1b[?25h")


def test_cli_output_stays_plain_when_not_a_terminal(tmp_path):
    result = CliRunner().invoke(main, ["init", "--repo", str(tmp_path)])
    assert result.exit_code == 0
    assert "\x1b[" not in result.output and "UNSHACKLE" not in result.output and "created" in result.output
    quiet = CliRunner().invoke(main, ["--quiet", "tools"])
    assert quiet.exit_code == 0 and "\x1b[" not in quiet.output
