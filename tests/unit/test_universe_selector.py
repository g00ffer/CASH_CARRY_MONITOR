from __future__ import annotations

from decimal import Decimal

import pytest

from monitor.universe.selector import (
    UniverseCandidate,
    UniverseSelectorParams,
    select_universe,
)


def _params(**overrides) -> UniverseSelectorParams:
    base = dict(
        max_active_symbols=3,
        min_predicted_funding=Decimal("0.0003"),
        min_quote_volume_24h=Decimal("5000000"),
        min_open_interest=None,
        max_spread=Decimal("0.0015"),
        always_include=(),
        weight_funding=0.7,
        weight_liquidity=0.3,
    )
    base.update(overrides)
    return UniverseSelectorParams(**base)


def _cand(
    name: str,
    funding: str,
    volume: str,
    spread: str = "0.0001",
    oi: str | None = None,
) -> UniverseCandidate:
    base = name.replace("_CARRY", "")
    return UniverseCandidate(
        symbol_name=name,
        base=base,
        quote="USDT",
        perp_symbol=f"{base}/USDT:USDT",
        predicted_funding_rate=Decimal(funding),
        quote_volume_24h=Decimal(volume),
        open_interest=Decimal(oi) if oi is not None else None,
        spread=Decimal(spread),
    )


class TestFilters:
    def test_low_volume_excluded(self):
        res = select_universe(
            [_cand("AAA_CARRY", "0.001", "1000000")], _params(),
        )
        assert res.selected == ()
        assert res.excluded["AAA_CARRY"] == "low_liquidity"

    def test_low_funding_excluded(self):
        res = select_universe(
            [_cand("AAA_CARRY", "0.0001", "9000000")], _params(),
        )
        assert res.excluded["AAA_CARRY"] == "funding_below_minimum"

    def test_wide_spread_excluded(self):
        res = select_universe(
            [_cand("AAA_CARRY", "0.001", "9000000", spread="0.01")],
            _params(),
        )
        assert res.excluded["AAA_CARRY"] == "spread_too_wide"

    def test_low_open_interest_excluded(self):
        res = select_universe(
            [_cand("AAA_CARRY", "0.001", "9000000", oi="1000000")],
            _params(min_open_interest=Decimal("3000000")),
        )
        assert res.excluded["AAA_CARRY"] == "low_open_interest"

    def test_open_interest_not_checked_when_none(self):
        res = select_universe(
            [_cand("AAA_CARRY", "0.001", "9000000", oi=None)],
            _params(min_open_interest=None),
        )
        assert res.selected == ("AAA_CARRY",)


class TestScoring:
    def test_top_n_by_score(self):
        cands = [
            _cand("HI_FUND", "0.001", "6000000"),   # funding максимум
            _cand("HI_VOL", "0.0004", "90000000"),  # volume максимум
            _cand("MID", "0.0006", "20000000"),
            _cand("LOW", "0.00035", "6000000"),
        ]
        res = select_universe(cands, _params(max_active_symbols=2))
        assert len(res.selected) == 2
        assert "LOW" not in res.selected
        assert res.scores["HI_FUND"] > res.scores["LOW"]

    def test_funding_weight_dominates(self):
        cands = [
            _cand("F", "0.001", "6000000"),
            _cand("V", "0.0004", "90000000"),
        ]
        res = select_universe(
            cands, _params(weight_funding=1.0, weight_liquidity=0.0),
        )
        assert res.scores["F"] > res.scores["V"]


class TestAnchors:
    def test_anchor_included_even_if_filtered(self):
        cands = [
            _cand("BTC_CARRY", "0.0001", "90000000"),  # funding ниже порога
            _cand("GOOD", "0.001", "9000000"),
        ]
        res = select_universe(
            cands, _params(always_include=("BTC_CARRY",)),
        )
        assert "BTC_CARRY" in res.selected
        assert "BTC_CARRY" not in res.excluded

    def test_anchor_not_in_candidates_ignored(self):
        res = select_universe(
            [_cand("GOOD", "0.001", "9000000")],
            _params(always_include=("NOPE_CARRY",)),
        )
        assert res.selected == ("GOOD",)


class TestEdge:
    def test_empty_candidates(self):
        res = select_universe([], _params())
        assert res.selected == ()
        assert res.scores == {}
        assert res.excluded == {}

    def test_params_validation(self):
        with pytest.raises(ValueError):
            _params(max_active_symbols=0)
        with pytest.raises(ValueError):
            _params(weight_funding=0.0, weight_liquidity=0.0)