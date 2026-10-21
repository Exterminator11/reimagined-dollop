"""Small live schema checks; no full downloads or Kaggle writes."""
import os
from pipeline import (BASE, FIELDS_311, FIELDS_PLUTO, FIELDS_HPD,
                      ACS_FIELDS, fetch_json, session)
from common import load_config


def main():
    s = session()
    for dataset, fields in [("erm2-nwe9", FIELDS_311), ("64uk-42ks", FIELDS_PLUTO), ("wvxf-dwi5", FIELDS_HPD)]:
        metadata = fetch_json(s, f"{BASE}/api/views/{dataset}.json")
        available = {c["fieldName"] for c in metadata["columns"]}
        missing = set(fields.split(",")) - available
        if missing:
            raise ValueError(f"{dataset}: missing columns {missing}")
        rows = fetch_json(s, f"{BASE}/resource/{dataset}.json", {"$select": fields, "$limit": 1})
        assert len(rows) == 1, f"No sample returned for {dataset}"
        print(f"OK {dataset}: schema and sample query")
    geometry = fetch_json(s, f"{BASE}/resource/63ge-mke6.geojson", {"$limit": 1})
    assert geometry["features"][0]["properties"]["geoid"]
    print("OK census boundaries: GeoJSON and GEOID")
    year = load_config()["acs_year"]
    fields = [v + suffix for v in ACS_FIELDS for suffix in ["E", "M"]]
    params = {"get": ",".join(fields), "for": "tract:000100", "in": "state:36 county:061"}
    if os.getenv("CENSUS_API_KEY"):
        params["key"] = os.environ["CENSUS_API_KEY"]
    rows = fetch_json(s, f"https://api.census.gov/data/{year}/acs/acs5", params)
    assert len(rows) == 2 and set(fields).issubset(rows[0])
    print(f"OK ACS {year}: selected variables and tract geography")


if __name__ == "__main__":
    main()
