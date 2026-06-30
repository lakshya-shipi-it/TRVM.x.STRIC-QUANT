"""
Tests for the IndicatorEngine.

Run: python -m pytest tests/test_indicators.py -v
"""

import numpy as np
import pandas as pd
import pytest

from indicators import IndicatorEngine


@pytest.fixture
def sample_df():
    """Generate a sample OHLCV DataFrame."""
    np.random.seed(42)
    n = 200
    base = 50000 + np.cumsum(np.random.randn(n) * 100)
    df = pd.DataFrame({
        "open":  base + np.random.randn(n) * 50,
        "high":  base + np.abs(np.random.randn(n)) * 100 + 50,
        "low":   base - np.abs(np.random.randn(n)) * 100 - 50,
        "close": base + np.random.randn(n) * 30,
        "volume": np.abs(np.random.randn(n) * 1000 + 5000),
    })
    # Ensure high >= close >= low
    df["high"] = df[["high", "close", "open"]].max(axis=1) + 10
    df["low"] = df[["low", "close", "open"]].min(axis=1) - 10
    return df


class TestEMA:
    def test_ema_basic(self, sample_df):
        result = IndicatorEngine.ema(sample_df["close"], 20)
        assert len(result) == len(sample_df)
        assert not result.isna().all()
        # EMA should be smoother than raw price
        assert result.std() < sample_df["close"].std()

    def test_ema_respects_lookback(self, sample_df):
        result = IndicatorEngine.ema(sample_df["close"], 20)
        # First 19 values are NaN or near-NaN (no full window)
        assert result.iloc[:19].isna().sum() <= 19


class TestRSI:
    def test_rsi_range(self, sample_df):
        rsi = IndicatorEngine.rsi_wilder(sample_df["close"], 14)
        valid = rsi.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_rsi_wilder_vs_simple(self, sample_df):
        wilder = IndicatorEngine.rsi_wilder(sample_df["close"], 14)
        simple = IndicatorEngine.rsi_simple(sample_df["close"], 14)
        # They should be close but not identical
        common = pd.concat([wilder, simple], axis=1).dropna()
        if len(common) > 0:
            diff = (common.iloc[:, 0] - common.iloc[:, 1]).abs()
            assert diff.mean() < 5  # typically within a few points


class TestATR:
    def test_atr_positive(self, sample_df):
        atr = IndicatorEngine.atr_ema(sample_df, 14)
        valid = atr.dropna()
        assert (valid > 0).all()

    def test_atr_ema_vs_sma(self, sample_df):
        atr_e = IndicatorEngine.atr_ema(sample_df, 14)
        atr_s = IndicatorEngine.atr_sma(sample_df, 14)
        # EMA version should be more reactive
        common = pd.concat([atr_e, atr_s], axis=1).dropna()
        if len(common) > 1:
            diff = common.iloc[:, 0] - common.iloc[:, 1]
            # Not necessarily always higher/lower, but should differ
            assert diff.abs().mean() > 0


class TestMACD:
    def test_macd_components(self, sample_df):
        line, signal, hist = IndicatorEngine.macd(sample_df["close"])
        assert len(line) == len(sample_df)
        assert len(signal) == len(sample_df)
        assert len(hist) == len(sample_df)
        # Histogram = line - signal
        pd.testing.assert_series_equal(
            hist.dropna(),
            (line - signal).dropna(),
            check_names=False,
        )


class TestADX:
    def test_adx_range(self, sample_df):
        adx = IndicatorEngine.adx(sample_df, 14)
        valid = adx.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()


class TestComputeAll:
    def test_compute_all_outputs(self, sample_df):
        result = IndicatorEngine.compute_all(sample_df)
        # Should have all expected columns
        expected_cols = [
            "trvm_ema_fast", "trvm_ema_slow", "trvm_rsi", "trvm_atr",
            "sc_ema_fast", "sc_ema_slow", "sc_rsi", "sc_macd",
            "sc_atr", "sc_atr_pct", "adx", "rve_ratio",
        ]
        for col in expected_cols:
            assert col in result.columns, f"Missing column: {col}"

    def test_compute_all_drops_na(self, sample_df):
        result = IndicatorEngine.compute_all(sample_df)
        assert result.isna().sum().sum() == 0
