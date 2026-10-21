"""Download public sources and build a request-level research dataset."""
import argparse
import hashlib
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from common import load_config

LOG = logging.getLogger(__name__)
BASE = "https://data.cityofnewyork.us"
FIELDS_311 = "unique_key,created_date,closed_date,agency,complaint_type,descriptor,status,bbl,latitude,longitude,borough,incident_address,incident_zip,location_type"
FIELDS_PLUTO = "bbl,yearbuilt,unitsres,unitstotal,bldgclass,landuse,numbldgs,numfloors,version"
FIELDS_HPD = "violationid,bbl,boroid,block,lot,class,approveddate,novissueddate"
ACS_FIELDS = {
    "B01003_001": "population", "B19013_001": "median_household_income",
    "B17001_001": "poverty_universe", "B17001_002": "population_below_poverty",
    "B25003_001": "occupied_housing_units", "B25003_003": "renter_occupied_units",
}


def session():
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
    s.headers["User-Agent"] = "nyc-housing-research/1.0"
    if os.getenv("SOCRATA_APP_TOKEN"):
        s.headers["X-App-Token"] = os.environ["SOCRATA_APP_TOKEN"]
    return s


def fetch_json(s, url, params=None):
    response = s.get(url, params=params, timeout=180)
    # Do not include prepared URLs: Census puts API credentials in query strings.
    if response.status_code >= 400:
        raise RuntimeError(f"Source {url} returned HTTP {response.status_code}")
    if "Missing Key" in response.text or "Invalid Key" in response.text:
        raise RuntimeError("Census requires a valid CENSUS_API_KEY environment variable. Get a free key at https://api.census.gov/data/key_signup.html")
    try:
        data = response.json()
    except requests.exceptions.JSONDecodeError as error:
        raise RuntimeError(f"Source {url} returned a non-JSON response; check API access and credentials") from error
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Source rejected query: {data}")
    return data


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def download_table(s, dataset, fields, where, key, raw, page_size):
    """Keyset pagination; each complete query is cached atomically as Parquet."""
    signature = hashlib.sha256(json.dumps([dataset, fields, where, key]).encode()).hexdigest()[:16]
    directory = raw / f"{dataset}-{signature}"
    directory.mkdir(parents=True, exist_ok=True)
    done = directory / "complete.json"
    if not done.exists():
        last = None
        page = 0
        while True:
            target = directory / f"{page:06d}.parquet"
            if target.exists():
                frame = pd.read_parquet(target)
            else:
                condition = where
                if last is not None:
                    condition = f"({where}) AND {key} > {literal(last)}"
                rows = fetch_json(s, f"{BASE}/resource/{dataset}.json", {
                    "$select": fields, "$where": condition,
                    "$order": f"{key} ASC", "$limit": page_size,
                })
                if not rows:
                    break
                frame = pd.DataFrame(rows).reindex(columns=fields.split(","))
                temporary = target.with_suffix(".tmp")
                frame.to_parquet(temporary, index=False)
                temporary.replace(target)
            if frame.empty:
                break
            cursor = frame[key].iloc[-1]
            if cursor == last:
                raise RuntimeError(f"Pagination did not advance for {dataset}")
            last = cursor
            page += 1
            LOG.info("%s: %s pages downloaded/reused", dataset, page)
        done.write_text(json.dumps({"dataset": dataset, "where": where,
                                    "downloaded_at": pd.Timestamp.now(tz="UTC").isoformat()}))
    files = sorted(directory.glob("*.parquet"))
    return pd.concat([pd.read_parquet(p) for p in files], ignore_index=True) if files else pd.DataFrame(columns=fields.split(","))


def normalize_bbl(series):
    text = series.astype("string").str.strip().str.replace(r"\.0+$", "", regex=True)
    return text.where(text.str.fullmatch(r"[1-5]\d{9}", na=False))


def download_acs(s, year, raw):
    target = raw / f"acs-{year}.parquet"
    if target.exists():
        return pd.read_parquet(target)
    columns = [v + suffix for v in ACS_FIELDS for suffix in ["E", "M"]]
    frames = []
    for county in ["005", "047", "061", "081", "085"]:
        params = {"get": "NAME," + ",".join(columns), "for": "tract:*",
                  "in": f"state:36 county:{county}"}
        if os.getenv("CENSUS_API_KEY"):
            params["key"] = os.environ["CENSUS_API_KEY"]
        rows = fetch_json(s, f"https://api.census.gov/data/{year}/acs/acs5", params)
        frames.append(pd.DataFrame(rows[1:], columns=rows[0]))
    frame = pd.concat(frames, ignore_index=True)
    frame["tract_geoid"] = frame["state"] + frame["county"] + frame["tract"]
    for code, name in ACS_FIELDS.items():
        for suffix, output in [("E", name), ("M", name + "_moe")]:
            values = pd.to_numeric(frame[code + suffix], errors="coerce")
            frame[output] = values.where(values >= 0)
    frame["poverty_rate"] = frame.population_below_poverty.div(frame.poverty_universe.where(frame.poverty_universe > 0))
    frame["renter_share"] = frame.renter_occupied_units.div(frame.occupied_housing_units.where(frame.occupied_housing_units > 0))
    keep = ["tract_geoid", "poverty_rate", "renter_share"] + [n + x for n in ACS_FIELDS.values() for x in ["", "_moe"]]
    frame = frame[keep].assign(acs_year=year)
    frame.to_parquet(target, index=False)
    return frame


def assign_tracts(frame, tracts):
    """Only unambiguous intersections receive a tract; retain every request."""
    frame = frame.copy()
    lon = pd.to_numeric(frame.longitude, errors="coerce")
    lat = pd.to_numeric(frame.latitude, errors="coerce")
    valid = lon.between(-74.3, -73.65) & lat.between(40.45, 40.95)
    frame["latitude"], frame["longitude"] = lat, lon
    frame["has_valid_coordinates"] = valid
    points = gpd.GeoDataFrame(geometry=gpd.points_from_xy(lon[valid], lat[valid]), crs=4326)
    tracts = tracts.to_crs(4326)
    hits = gpd.sjoin(points, tracts[["geoid", "geometry"]], how="left", predicate="intersects")
    counts = hits.groupby(level=0).geoid.count()
    unique = hits[hits.index.isin(counts[counts == 1].index)]
    frame["tract_geoid"] = unique.geoid.reindex(frame.index).astype("string")
    frame["tract_matched"] = frame.tract_geoid.notna()
    frame["tract_match_ambiguous"] = frame.index.isin(counts[counts > 1].index)
    return frame


def temporal_features(frame, violations, observation_end):
    """Compute history without multiplying requests; incomplete outcomes stay null."""
    con = duckdb.connect()
    con.register("requests", frame)
    con.register("violations", violations)
    history = con.execute("""
        SELECT a.request_id,
          count(b.request_id) FILTER (WHERE b.created_date >= a.closed_date - INTERVAL '30 days') AS prior_property_complaints_30d,
          count(b.request_id) FILTER (WHERE b.created_date >= a.closed_date - INTERVAL '90 days') AS prior_property_complaints_90d,
          count(b.request_id) AS prior_property_complaints_365d,
          count(b.request_id) FILTER (WHERE b.category_standardized = a.category_standardized) AS prior_same_category_complaints_365d
        FROM requests a LEFT JOIN requests b
          ON a.bbl = b.bbl AND a.request_id <> b.request_id
          AND b.has_valid_timestamps
          AND b.created_date < a.closed_date
          AND b.created_date >= a.closed_date - INTERVAL '365 days'
        GROUP BY a.request_id
    """).df()
    hpd = con.execute("""
        SELECT a.request_id, count(v.violationid) AS prior_violations_365d,
          count(v.violationid) FILTER (WHERE v.class = 'A') AS prior_class_a_violations_365d,
          count(v.violationid) FILTER (WHERE v.class = 'B') AS prior_class_b_violations_365d,
          count(v.violationid) FILTER (WHERE v.class = 'C') AS prior_class_c_violations_365d
        FROM requests a LEFT JOIN violations v ON a.bbl = v.bbl
          AND v.issued_at < a.closed_date AND v.approved_at < a.closed_date
          AND v.issued_at >= a.closed_date - INTERVAL '365 days'
        GROUP BY a.request_id
    """).df()
    recurrence = con.execute("""
        SELECT a.request_id, min(b.created_date) AS next_same_category_report_date
        FROM requests a LEFT JOIN requests b ON a.bbl = b.bbl
          AND b.has_valid_timestamps
          AND a.category_standardized = b.category_standardized
          AND a.request_id <> b.request_id AND b.created_date > a.closed_date
        GROUP BY a.request_id
    """).df()
    con.close()
    for addition in [history, hpd, recurrence]:
        frame = frame.merge(addition, on="request_id", how="left", validate="one_to_one")
    eligible = frame.bbl.notna() & frame.closed_date.notna() & frame.has_valid_timestamps
    count_columns = [c for c in frame if c.startswith("prior_")]
    frame[count_columns] = frame[count_columns].astype("Int64")
    frame.loc[~eligible, count_columns] = pd.NA
    gap = frame.next_same_category_report_date - frame.closed_date
    frame["days_to_next_same_category_report"] = gap.dt.total_seconds() / 86400
    for days in [30, 60, 90]:
        complete = eligible & (frame.closed_date + pd.Timedelta(days=days) <= observation_end)
        frame[f"followup_complete_{days}d"] = complete
        frame[f"recurrence_{days}d"] = (gap <= pd.Timedelta(days=days)).astype("Int64").where(complete)
    return frame


def merge_sources(requests_frame, pluto, acs, tracts, violations, config, observation_end):
    frame = requests_frame.rename(columns={"unique_key": "request_id", "complaint_type": "problem_raw", "descriptor": "problem_detail_raw"}).copy()
    if frame.request_id.isna().any() or frame.request_id.duplicated().any():
        raise ValueError("311 source contains missing or duplicate request IDs; refresh/check the extract")
    frame["bbl"] = normalize_bbl(frame.bbl)
    for column in ["created_date", "closed_date"]:
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    frame["has_valid_timestamps"] = frame.created_date.notna() & (frame.closed_date.isna() | (frame.closed_date >= frame.created_date))
    frame["is_closed"] = frame.closed_date.notna() & frame.has_valid_timestamps & (frame.closed_date <= observation_end)
    # Do not use administrative closures that occurred beyond this extract's outcome cutoff.
    frame.loc[~frame.is_closed, "closed_date"] = pd.NaT
    frame["category_standardized"] = frame.problem_raw.astype("string").str.strip().str.upper()
    frame["property_match_method"] = frame.bbl.notna().map({True: "source_bbl", False: "unmatched"})
    frame = assign_tracts(frame, tracts)
    pluto = pluto.rename(columns={"yearbuilt": "year_built", "unitsres": "residential_units", "unitstotal": "total_units", "bldgclass": "building_class", "landuse": "land_use", "numbldgs": "number_of_buildings", "numfloors": "number_of_floors", "version": "pluto_release"}).copy()
    pluto["bbl"] = normalize_bbl(pluto.bbl)
    pluto = pluto[pluto.bbl.notna()]
    frame = frame.merge(pluto, on="bbl", how="left", validate="many_to_one", indicator="_pluto")
    frame["pluto_matched"] = frame.pop("_pluto").eq("both")
    frame = frame.merge(acs, on="tract_geoid", how="left", validate="many_to_one", indicator="_acs")
    frame["acs_matched"] = frame.pop("_acs").eq("both")
    for column in ["year_built", "residential_units", "total_units", "number_of_buildings", "number_of_floors"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    age = frame.created_date.dt.year - frame.year_built
    frame["property_age_at_creation"] = age.where((frame.year_built > 0) & (age >= 0))
    frame["closure_hours"] = (frame.closed_date - frame.created_date).dt.total_seconds() / 3600
    frame["created_year"] = frame.created_date.dt.year
    frame["created_month"] = frame.created_date.dt.month
    frame["created_quarter"] = frame.created_date.dt.quarter
    frame["observation_end_date"] = observation_end
    frame["acs_available_at_closure"] = frame.closed_date >= pd.Timestamp(config["acs_available_date"])
    available = config.get("pluto_available_date")
    frame["pluto_available_at_closure"] = frame.closed_date >= pd.Timestamp(available) if available else False
    frame = temporal_features(frame, violations, observation_end)
    start, end = pd.Timestamp(config["start_date"]), pd.Timestamp(config["end_date"])
    frame = frame[frame.created_date.ge(start) & frame.created_date.lt(end)].copy()
    frame["history_complete_365d"] = frame.closed_date.notna() & (frame.closed_date - pd.Timedelta(days=365) >= start - pd.Timedelta(days=config["history_days"])) & (frame.closed_date <= observation_end)
    frame["hpd_history_available"] = frame.bbl.notna() & frame.closed_date.notna()
    frame["extract_date"] = date.today().isoformat()
    if frame.request_id.duplicated().any():
        raise AssertionError("Merge multiplied request rows")
    return frame.reset_index(drop=True)


def run(config):
    start, end = date.fromisoformat(config["start_date"]), date.fromisoformat(config["end_date"])
    if end <= start or config["history_days"] < 365 or config["followup_days"] < 90:
        raise ValueError("Require end > start, history_days >= 365 and followup_days >= 90")
    if not config["categories"]:
        raise ValueError("Specify at least one category")
    work = Path(config["work_dir"])
    raw, output = work / "raw", work / "final"
    raw.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    s = session()
    # Check Census access before spending time downloading citywide complaints.
    acs = download_acs(s, config["acs_year"], raw)
    lower = start - timedelta(days=config["history_days"])
    upper = min(end + timedelta(days=config["followup_days"]), date.today())
    categories = ",".join(literal(c) for c in config["categories"])
    where = f"created_date >= '{lower}T00:00:00' AND created_date < '{upper}T00:00:00' AND complaint_type in ({categories})"
    requests_frame = download_table(s, "erm2-nwe9", FIELDS_311, where, "unique_key", raw, config["page_size"])
    if requests_frame.empty:
        raise ValueError("No complaints matched the configured dates/categories")
    if config.get("pluto_csv"):
        pluto = pd.read_csv(config["pluto_csv"], dtype="string")
        pluto.columns = pluto.columns.str.lower()
        pluto = pluto.reindex(columns=FIELDS_PLUTO.split(","))
    else:
        pluto = download_table(s, "64uk-42ks", FIELDS_PLUTO, "bbl IS NOT NULL", "bbl", raw, config["page_size"])
    hpd_where = f"novissueddate >= '{lower}T00:00:00' AND novissueddate < '{upper}T00:00:00'"
    hpd = download_table(s, "wvxf-dwi5", FIELDS_HPD, hpd_where, "violationid", raw, config["page_size"])
    hpd["bbl"] = normalize_bbl(hpd.bbl)
    fallback = hpd.boroid.astype("string") + hpd.block.astype("string").str.zfill(5) + hpd.lot.astype("string").str.zfill(4)
    hpd["bbl"] = hpd.bbl.fillna(normalize_bbl(fallback))
    hpd["issued_at"] = pd.to_datetime(hpd.novissueddate, errors="coerce")
    hpd["approved_at"] = pd.to_datetime(hpd.approveddate, errors="coerce")
    if hpd.violationid.duplicated().any():
        raise ValueError("Duplicate HPD violation IDs")
    tract_path = raw / "tracts-2020.geojson"
    if not tract_path.exists():
        data = fetch_json(s, f"{BASE}/resource/63ge-mke6.geojson", {"$limit": 10000})
        temp = tract_path.with_suffix(".tmp")
        temp.write_text(json.dumps(data))
        temp.replace(tract_path)
    tracts = gpd.read_file(tract_path)
    merged = merge_sources(requests_frame, pluto, acs, tracts, hpd, config, pd.Timestamp(upper))
    if merged.empty:
        raise ValueError("No requests in the final analysis period")
    merged.to_parquet(output / "nyc_housing_merged.parquet", index=False)
    merged.to_csv(output / "nyc_housing_merged.csv.gz", index=False, compression="gzip")
    con = duckdb.connect(str(work / "housing.duckdb"))
    con.register("merged", merged)
    con.execute("CREATE OR REPLACE TABLE complaints AS SELECT * FROM merged")
    con.close()
    report = {
        "rows": len(merged), "unique_requests": int(merged.request_id.nunique()),
        "match_rates": {c: float(merged[c].mean()) for c in ["pluto_matched", "tract_matched", "acs_matched", "has_valid_coordinates"]},
        "columns": {c: str(t) for c, t in merged.dtypes.items()},
        "config": config, "observation_end_exclusive": str(upper),
        "source_pages": {name: str(BASE + "/d/" + name) for name in ["erm2-nwe9", "64uk-42ks", "wvxf-dwi5", "63ge-mke6"]},
        "warnings": ["History covers only selected categories.", "Current-source extracts cannot fully reconstruct historical data availability.", "Use a historical PLUTO CSV and verify availability flags before prediction.", "No HPD match is not evidence of no housing problems.", "Tract demographics describe areas, not complainants."]}
    (output / "quality_report.json").write_text(json.dumps(report, indent=2, default=str))
    dictionary = "\n".join(f"- `{c}`: {t}" for c, t in merged.dtypes.items())
    (output / "DATASET_README.md").write_text(
        "# NYC housing service research\n\nOne row per 311 request. See quality_report.json for scope, sources and limitations.\n\n"
        "Joins: source BBL to PLUTO; coordinate-to-2020-tract spatial join; tract GEOID to ACS; BBL plus closure cutoff to prior HPD counts.\n\n"
        "Recurrence labels are outcomes and must never be predictors. Current-source PLUTO is descriptive unless historical availability is established. "
        "Severity, income grouping and hotspot assignments are analysis choices and are not assigned automatically. "
        "Sources: NYC Open Data (311, PLUTO, HPD, census boundaries) and US Census ACS five-year estimates. "
        "Source terms remain applicable; no new rights to source data are asserted.\n\n## Columns\n" + dictionary + "\n")
    LOG.info("Built %s rows in %s", len(merged), output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--publish", choices=["create", "version"], help="Upload after a successful build")
    parser.add_argument("--dataset", help="Kaggle owner/dataset-name")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config = load_config(args.config)
    if args.publish:
        from common import dataset_id
        dataset_id(config, args.dataset)  # Fail before downloading if the destination is missing.
    output = run(config)
    if args.publish:
        from publish_kaggle import publish
        publish(config, output, args.publish, args.dataset)


if __name__ == "__main__":
    main()
