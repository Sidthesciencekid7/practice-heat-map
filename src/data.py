"""Fetch and cache all raw inputs for the Boston/Cambridge heat-risk map.

Sources (all free, no API key):
  - Census ACS 5-year (2022) tract demographics   -> api.census.gov
  - Census cartographic tract boundaries (TIGER)  -> www2.census.gov
  - Open-Meteo 7-day forecast on a coarse grid    -> api.open-meteo.com
  - OpenStreetMap candidate cooling sites         -> overpass-api.de
Everything is cached under data/ so the app runs offline after the first build.
"""
from pathlib import Path
import io
import os
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

# ACS 2022 5-year variables
_MALE_65 = [f"B01001_{i:03d}E" for i in range(20, 26)]
_FEMALE_65 = [f"B01001_{i:03d}E" for i in range(44, 50)]
_VARS = (
    ["B01001_001E", "B11001_001E", "B11001_008E", "B19013_001E",
     "B17001_001E", "B17001_002E", "B25034_001E", "B25034_009E",
     "B25034_010E", "B25034_011E"]
    + _MALE_65 + _FEMALE_65
)


def _get(url, **kw):
    r = requests.get(url, timeout=60, **kw)
    r.raise_for_status()
    return r


def load_demographics() -> pd.DataFrame:
    f = DATA / "acs_tracts.csv"
    if f.exists():
        return pd.read_csv(f, dtype={"GEOID": str})
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise SystemExit(
            "The Census API now requires a free key.\n"
            "Get one at https://api.census.gov/data/key_signup.html (arrives by email),\n"
            "then run:  $env:CENSUS_API_KEY = '<your key>'"
        )
    frames = []
    for county in COUNTIES:
        url = (
            "https://api.census.gov/data/2022/acs/acs5"
            f"?get={','.join(_VARS)}&for=tract:*&in=state:{STATE}%20county:{county}&key={key}"
        )
        resp = _get(url)
        if "json" not in resp.headers.get("content-type", ""):
            raise SystemExit(f"Census API rejected the request (is the key valid/activated?):\n{resp.text[:200]}")
        rows = resp.json()
        frames.append(pd.DataFrame(rows[1:], columns=rows[0]))
    df = pd.concat(frames, ignore_index=True)
    df["GEOID"] = df["state"] + df["county"] + df["tract"]
    num = df[_VARS].apply(pd.to_numeric, errors="coerce")
    num = num.where(num > -1)  # Census uses -666666666 for "not available"
    out = pd.DataFrame({"GEOID": df["GEOID"]})
    out["pop"] = num["B01001_001E"]
    out["pop65"] = num[_MALE_65 + _FEMALE_65].sum(axis=1)
    out["households"] = num["B11001_001E"]
    out["alone"] = num["B11001_008E"]  # one-person non-family households
    out["median_income"] = num["B19013_001E"]
    out["pov_universe"] = num["B17001_001E"]
    out["pov"] = num["B17001_002E"]
    out["housing_units"] = num["B25034_001E"]
    out["pre1960"] = num[["B25034_009E", "B25034_010E", "B25034_011E"]].sum(axis=1)
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
