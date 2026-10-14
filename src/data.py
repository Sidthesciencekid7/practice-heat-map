"""Fetch and cache all raw inputs for the Boston/Cambridge heat-risk map.

Sources (all free, no API key):
  - CDC Social Vulnerability Index 2022 (tracts)  -> svi.cdc.gov
  - Census cartographic tract boundaries (TIGER)  -> www2.census.gov
  - Open-Meteo 7-day forecast on a coarse grid    -> api.open-meteo.com
  - OpenStreetMap candidate cooling sites         -> overpass-api.de
Everything is cached under data/ so the app runs offline after the first build.
"""
from pathlib import Path
import io
import time
import zipfile

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

DATA = Path(__file__).resolve().parents[1] / "data"
DATA.mkdir(exist_ok=True)

STATE, COUNTIES = "25", ["025", "017"]  # MA: Suffolk (Boston) + Middlesex (Cambridge, Somerville...)
BBOX = (42.20, -71.25, 42.48, -70.98)  # south, west, north, east: Greater Boston core


def _get(url, **kw):
    r = requests.get(url, timeout=60, **kw)
    r.raise_for_status()
    return r


def load_demographics() -> pd.DataFrame:
    """CDC/ATSDR Social Vulnerability Index 2022 (built from ACS 2018-2022), tract level.

    Keyless alternative to the Census API. We keep the heat-relevant columns:
    residents 65+, poverty (<150% of the line), disability, no vehicle, limited English,
    multi-unit housing (a density proxy) and no home internet.
    """
    f = DATA / "svi_tracts.csv"
    if f.exists():
        return pd.read_csv(f, dtype={"GEOID": str})
    url = "https://svi.cdc.gov/Documents/Data/2022/csv/states/Massachusetts.csv"
    raw = pd.read_csv(io.BytesIO(_get(url, headers={"User-Agent": "heat-risk-map-practice/0.1"}).content),
                      dtype={"FIPS": str})
    cols = {"FIPS": "GEOID", "E_TOTPOP": "pop", "E_AGE65": "pop65", "EP_AGE65": "pct65",
            "EP_POV150": "pct_poverty", "EP_DISABL": "pct_disabled", "EP_NOVEH": "pct_noveh",
            "EP_LIMENG": "pct_limeng", "EP_MUNIT": "pct_multiunit", "EP_NOINT": "pct_nointernet"}
    out = raw[list(cols)].rename(columns=cols)
    out = out[out["GEOID"].str[2:5].isin(COUNTIES)].copy()
    out = out.replace(-999, np.nan)  # SVI uses -999 for "not available"
    out.to_csv(f, index=False)
    return out


def load_tracts() -> gpd.GeoDataFrame:
    f = DATA / "tracts.gpkg"
    if f.exists():
        return gpd.read_file(f)
    url = f"https://www2.census.gov/geo/tiger/GENZ2022/shp/cb_2022_{STATE}_tract_500k.zip"
    z = zipfile.ZipFile(io.BytesIO(_get(url).content))
    tmp = DATA / "_tiger"
    z.extractall(tmp)
    g = gpd.read_file(next(tmp.glob("*.shp")))
    g = g[g["COUNTYFP"].isin(COUNTIES)].to_crs(4326)
    g = g[["GEOID", "ALAND", "geometry"]]
    # keep tracts whose centroid is inside the Greater Boston bbox
    c = g.to_crs(26986).centroid.to_crs(4326)
    s, w, n, e = BBOX
    g = g[(c.y.between(s, n)) & (c.x.between(w, e))].copy()
    g.to_file(f, driver="GPKG")
    return g


def load_forecast() -> pd.DataFrame:
    """7-day daily forecast on a 3x3 grid across the bbox (the model grid is ~11 km anyway)."""
    f = DATA / "forecast.csv"
    s, w, n, e = BBOX
    lats, lons = np.linspace(s, n, 3), np.linspace(w, e, 3)
    pts = [(la, lo) for la in lats for lo in lons]
    params = {
        "latitude": ",".join(f"{p[0]:.3f}" for p in pts),
        "longitude": ",".join(f"{p[1]:.3f}" for p in pts),
        "daily": "apparent_temperature_max,temperature_2m_max,temperature_2m_min",
        "timezone": "America/New_York",
        "forecast_days": 7,
    }
    try:
        res = _get("https://api.open-meteo.com/v1/forecast", params=params).json()
        res = res if isinstance(res, list) else [res]
        rows = []
        for (la, lo), r in zip(pts, res):
            d = pd.DataFrame(r["daily"])
            d["lat"], d["lon"] = la, lo
            rows.append(d)
        df = pd.concat(rows, ignore_index=True)
        df.to_csv(f, index=False)
        return df
    except Exception:
        if f.exists():
            return pd.read_csv(f)
        raise


def load_sites() -> pd.DataFrame:
    """Candidate cooling sites: libraries + community centres from OSM."""
    f = DATA / "sites.csv"
    if f.exists():
        return pd.read_csv(f)
    s, w, n, e = BBOX
    q = (f'[out:json][timeout:60];(nwr["amenity"="library"]({s},{w},{n},{e});'
         f'nwr["amenity"="community_centre"]({s},{w},{n},{e}););out center tags;')
    elements = None
    errors = []
    hosts = ["https://overpass-api.de/api/interpreter",
             "https://overpass.kumi.systems/api/interpreter"] * 3  # public servers 504 under load: retry
    for i, host in enumerate(hosts):
        if i:
            time.sleep(8)
        try:
            r = requests.post(host, data={"data": q}, timeout=90,
                              headers={"User-Agent": "heat-risk-map-practice/0.1"})
            r.raise_for_status()
            elements = r.json()["elements"]
            break
        except Exception as ex:
            errors.append(f"{host}: {ex!r}")
            continue
    if elements is None:
        raise RuntimeError("Overpass unreachable; rerun later.\n" + "\n".join(errors))
    rows = []
    for el in elements:
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        if lat is None:
            continue
        rows.append({"name": el.get("tags", {}).get("name", "(unnamed)"),
                     "kind": el["tags"].get("amenity"), "lat": lat, "lon": lon})
    df = pd.DataFrame(rows).drop_duplicates(["name", "lat", "lon"])
    df.to_csv(f, index=False)
    return df


if __name__ == "__main__":
    for fn in (load_demographics, load_tracts, load_forecast, load_sites):
        x = fn()
        print(f"{fn.__name__}: {len(x)} rows")
