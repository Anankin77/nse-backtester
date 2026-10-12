"""Saved prices (prices/) and the daily updater."""
import pandas as pd
import pytest

from backtester import data, synthetic, update


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "PRICES_DIR", tmp_path)
    monkeypatch.setattr(update, "PRICES_DIR", tmp_path)
    return tmp_path


def prices(n, end="2026-10-09"):
    df = synthetic(n)
    df.index = pd.bdate_range(end=end, periods=n, name="Date")
    return df


AFTER_CLOSE = pd.Timestamp("2026-10-12 17:00", tz="Asia/Kolkata")


def test_file_names():
    assert data.stored_file("reliance").name == "RELIANCE.csv"
    assert data.stored_file("TCS.NS").name == "TCS.csv"
    assert data.stored_file("^NSEI").name == "IDX_NSEI.csv"
    assert data.stored_file("M&M").name == "M_AND_M.csv"


def test_update_saves_and_download_reads_it_back(store):
    df = prices(300)
    out = update.update_prices(["AAA"], AFTER_CLOSE, fetcher=lambda tk: df, pause=0, log=lambda *_: None)
    assert out["changed"] == ["AAA"] and not out["failed"]
    assert out["status"]["last_bar"] == "2026-10-09"
    got = data.download("AAA", "2026-01-01", "2026-03-02")      # `end` is excluded, like Yahoo
    assert got.index[0] >= pd.Timestamp("2026-01-01") and got.index[-1] == pd.Timestamp("2026-02-27")
    assert got["Close"].iloc[-1] == pytest.approx(df.loc["2026-02-27", "Close"], abs=1e-4)


def test_second_run_without_new_prices_changes_nothing(store):
    df = prices(300)
    kw = dict(fetcher=lambda tk: df, pause=0, log=lambda *_: None)
    update.update_prices(["AAA"], AFTER_CLOSE, **kw)
    before = (store / "_status.json").read_text()
    out = update.update_prices(["AAA"], AFTER_CLOSE + pd.Timedelta(hours=2), **kw)
    assert out["changed"] == [] and (store / "_status.json").read_text() == before


def test_bad_download_never_replaces_saved_prices(store):
    quiet = dict(pause=0, log=lambda *_: None)
    update.update_prices(["AAA"], AFTER_CLOSE, fetcher=lambda tk: prices(300), **quiet)
    saved = (store / "AAA.csv").read_text()
    out = update.update_prices(["AAA"], AFTER_CLOSE, fetcher=lambda tk: prices(100), **quiet)   # much shorter
    assert "AAA" in out["failed"] and (store / "AAA.csv").read_text() == saved
    out = update.update_prices(["AAA"], AFTER_CLOSE, fetcher=lambda tk: prices(300, end="2026-09-01"), **quiet)
    assert "AAA" in out["failed"] and (store / "AAA.csv").read_text() == saved


def test_unfinished_day_is_not_saved(store):
    df = prices(300, end="2026-10-12")                       # includes Monday's half-day bar
    during_market = pd.Timestamp("2026-10-12 11:00", tz="Asia/Kolkata")
    update.update_prices(["AAA"], during_market, fetcher=lambda tk: df, pause=0, log=lambda *_: None)
    assert data.load_stored("AAA").index[-1] == pd.Timestamp("2026-10-09")


def test_one_failing_ticker_does_not_stop_the_rest(store):
    def fetch(tk):
        if tk == "BAD":
            raise ValueError("No data for BAD.NS")
        return prices(300)
    out = update.update_prices(["AAA", "BAD", "CCC"], AFTER_CLOSE, fetcher=fetch, pause=0, log=lambda *_: None)
    assert out["changed"] == ["AAA", "CCC"] and list(out["failed"]) == ["BAD"]
    assert data.stored_status()["failed"] == {"BAD": "No data for BAD.NS"}
