"""Turn cached data into a 0-100 heat-risk score per census tract.

    risk = 100 * hazard * (0.7 * vulnerability + 0.3 * heat_island)

  hazard        0-1, from the worst forecast "feels like" temperature (26 C -> 0, 38 C -> 1)
  vulnerability 0-1, equal-weighted percentile ranks of the people-based factors below
  heat_island   0-1, percentile rank of population density (proxy: no tract-level surface temps yet)

Percentile ranks keep any single extreme tract (e.g. a dorm tract at 100% poverty)
from dominating: each factor just says "how this tract compares with its neighbours".
"""
from functools import lru_cache

import pandas as pd

import data

HAZARD_LOW_C, HAZARD_HIGH_C = 26.0, 38.0
MIN_POP = 100      # parks, airport, harbour islands
LOW_CONF_POP = 500  # tiny tracts: percentages are noisy

# label shown in the app for each vulnerability factor
FACTORS = {
    "pct65": "residents 65+",
    "pct_poverty": "below 150% poverty line",
    "pct_disabled": "living with a disability",
    "pct_noveh": "households without a car",
    "pct_limeng": "limited English",
}
DEFAULT_WEIGHTS = {k: 1.0 for k in FACTORS}  # equal weights; adjustable in the app

# Assumption, not data: during an outage AC stops, so indoor heat exposure rises.
# We model that as a 30% boost to the hazard. Replace with utility outage data later.
OUTAGE_BOOST = 1.3


def load_base() -> pd.DataFrame:
    """Join boundaries + demographics and add static per-tract columns. Call once and reuse."""
    g = data.load_tracts().merge(data.load_demographics(), on="GEOID", how="left")
    g = g[g["pop"] >= MIN_POP].copy()
    g["low_confidence"] = g["pop"] < LOW_CONF_POP

    g["density_km2"] = g["pop"] / (g["ALAND"] / 1e6)
    g["heat_island"] = g["density_km2"].rank(pct=True)
    for col in FACTORS:
        # missing values get the median rank (0.5) rather than being dropped
        g[f"r_{col}"] = g[col].rank(pct=True).fillna(0.5)

    # nearest forecast grid point for each tract
    pts = data.load_forecast()[["lat", "lon"]].drop_duplicates().to_numpy()
    c = g.to_crs(26986).centroid.to_crs(4326)
    g["lat"], g["lon"] = c.y, c.x
    d2 = (g["lat"].to_numpy()[:, None] - pts[:, 0]) ** 2 + (g["lon"].to_numpy()[:, None] - pts[:, 1]) ** 2
    g["grid_lat"], g["grid_lon"] = pts[d2.argmin(axis=1)].T
    return g


@lru_cache(maxsize=1)
def forecast_peak() -> pd.DataFrame:
    """Worst-day feels-like temperature per grid point over the 7-day forecast.

    Cached so moving a slider doesn't re-download the forecast; restart the app to refresh.
    """
    f = data.load_forecast()
    i = f.groupby(["lat", "lon"])["apparent_temperature_max"].idxmax()
    peak = f.loc[i, ["lat", "lon", "time", "apparent_temperature_max"]]
    return peak.rename(columns={"lat": "grid_lat", "lon": "grid_lon",
                                "time": "peak_day", "apparent_temperature_max": "feels_like_c"})


def compute(base: pd.DataFrame, weights=None, scenario_temp_c=None,
            delta_c=0.0, outage=False) -> pd.DataFrame:
    """Score every tract.

    scenario_temp_c  use this feels-like temperature everywhere instead of the forecast
                     (handy for demos when the real forecast is mild)
    delta_c          add this many degrees to the forecast ("what if it's hotter?")
    outage           apply the assumed outage boost to the hazard
    """
    w = pd.Series(weights or DEFAULT_WEIGHTS)
    g = base.copy()

    if scenario_temp_c is None:
        g = g.merge(forecast_peak(), on=["grid_lat", "grid_lon"], how="left")
        g["feels_like_c"] += delta_c
    else:
        g["peak_day"] = "scenario"
        g["feels_like_c"] = scenario_temp_c

    hazard = ((g["feels_like_c"] - HAZARD_LOW_C) / (HAZARD_HIGH_C - HAZARD_LOW_C)).clip(0, 1)
    if outage:
        hazard = (hazard * OUTAGE_BOOST).clip(0, 1)
    g["hazard"] = hazard

    ranks = g[[f"r_{c}" for c in w.index]].to_numpy()
    g["vulnerability"] = ranks @ (w.to_numpy() / w.sum())
    g["risk"] = (100 * g["hazard"] * (0.7 * g["vulnerability"] + 0.3 * g["heat_island"])).round(1)
    # people the city should worry about: older residents, scaled by how risky the tract is
    g["at_risk_pop"] = (g["pop65"].fillna(0) * g["risk"] / 100).round().astype(int)
    g["drivers"] = explain(g, w)
    return g


def explain(g: pd.DataFrame, w: pd.Series) -> pd.Series:
    """Top three reasons a tract scores high, as plain text with the real values."""
    cols = [c for c in w.index if w[c] > 0]
    out = []
    for _, row in g.iterrows():
        top = sorted(cols, key=lambda c: row[f"r_{c}"], reverse=True)[:3]
        parts = [f"{row[c]:.0f}% {FACTORS[c]}" for c in top if pd.notna(row[c])]
        if row["heat_island"] > 0.8:
            parts.append(f"dense ({row['density_km2']:,.0f} people/km²)")
        out.append("; ".join(parts))
    return pd.Series(out, index=g.index)


if __name__ == "__main__":
    base = load_base()
    print(f"{len(base)} tracts scored "
          f"({base['low_confidence'].sum()} flagged low-confidence, pop < {LOW_CONF_POP})\n")

    fc = compute(base)
    print(f"Real 7-day forecast: peak feels-like {fc['feels_like_c'].max():.1f} C "
          f"-> max risk {fc['risk'].max():.1f}\n")

    s = compute(base, scenario_temp_c=37.0)  # roughly the July 2022 Boston heat wave
    show = ["GEOID", "pop", "pct65", "pct_poverty", "pct_disabled", "risk", "at_risk_pop", "drivers"]
    pd.set_option("display.width", 220, "display.max_colwidth", 90)
    print("Heat-wave scenario (37 C feels-like) - TOP 10")
    print(s.nlargest(10, "risk")[show].to_string(index=False))
    print("\nBOTTOM 5")
    print(s.nsmallest(5, "risk")[show].to_string(index=False))
    print(f"\nTotal at-risk older residents: {s['at_risk_pop'].sum():,}")
