# Data preparation

Downloads NYC 311, PLUTO, HPD violations, 2020 census tract polygons, and Census ACS five-year estimates. Builds a dataset with **one row per 311 request**, then optionally uploads the completed artifacts to Kaggle. The folder name `data_preperation` follows the requested spelling.

## Install

From the repository root (Python 3.11+ recommended):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r data_preperation/requirements.txt
```

## Configure

### Credentials: do this first

1. Request a free key at [Census API signup](https://api.census.gov/data/key_signup.html). Open the activation email and activate the key before using it.
2. Generate an API token in your Kaggle account settings and note your Kaggle username.
3. Create `.env` in the repository root, alongside the top-level README. Fill in your actual credentials locally:

```dotenv
CENSUS_API_KEY=your_activated_census_key
KAGGLE_API_TOKEN=your_kaggle_token
KAGGLE_USERNAME=your_kaggle_username
```

4. Activate the virtual environment and verify the public sources:

```bash
source .venv/bin/activate
python data_preperation/check_sources.py
```

All five checks should say `OK`, including ACS. This checks Census authentication; it does not test Kaggle authentication. Once they pass, build and upload:

```bash
python data_preperation/pipeline.py --publish create
```

The default destination is `KAGGLE_USERNAME/nyc-housing-service-equity`. No credentials belong in `config.json`. The uploaded dataset is private by default.

### If Census reports an invalid key

- Confirm you clicked the activation link in the Census email.
- Copy the key itself into `.env`, without an activation URL or extra spaces.
- Use either `CENSUS_API_KEY` or `CENSUS_KEY`. If both are present, `CENSUS_API_KEY` takes precedence; remove the stale duplicate.
- Previously exported shell variables also take precedence over `.env`. To use the file's value instead, run `unset CENSUS_API_KEY CENSUS_KEY` and retry the check in a new Python process.
- If the API still rejects the activated key, request a replacement from Census, activate it and update `.env`.
- Do not start the full download until `check_sources.py` reports `OK ACS ...`.

### If Kaggle authentication fails

- Confirm your token is active and your username matches the dataset owner.
- Use either `KAGGLE_API_TOKEN` or `KAGGLE_TOKEN`; the first takes precedence. Legacy credentials instead use `KAGGLE_USERNAME` and `KAGGLE_KEY`.
- Run `create` only for the first upload. If the dataset already exists, use `--publish version` or the standalone uploader's `--mode version`.
- If upload fails after the build completes, retry `publish_kaggle.py` rather than rebuilding the data.

### If Kaggle rejects the dataset title

Kaggle requires a title of 6–50 characters. Set `kaggle_title` to a shorter value such as `NYC Housing Complaints and Neighborhoods`, then retry only the upload:

```bash
python data_preperation/publish_kaggle.py --mode create
```

Your downloaded data and merged files are already saved; there is no need to rerun the pipeline. If the dataset already exists on Kaggle, use `--mode version` instead.

### Dataset configuration

All scripts automatically load the repository-root `.env` file. Existing shell environment variables take precedence. The `.env` file is gitignored. Use `CENSUS_API_KEY`, and either `KAGGLE_API_TOKEN` or the legacy `KAGGLE_USERNAME`/`KAGGLE_KEY` pair. A token alone still requires `kaggle_dataset` in the config or `--dataset owner/dataset-name`.
The existing short names `CENSUS_KEY` and `KAGGLE_TOKEN` are also supported.

Edit `config.json`, or copy it to the gitignored `config.local.json` and pass `--config data_preperation/config.local.json` to each script.

- `start_date` is inclusive and `end_date` is exclusive. Defaults select requests created in 2023–2025.
- Downloads include 365 extra days of complaint history and up to 90 days after the analysis period for outcomes. If the requested follow-up reaches the present, labels lacking full follow-up stay null.
- `categories` must use exact source labels. Defaults cover plumbing, heat/hot water, unsanitary conditions, rodents, sewer and dirty conditions. This is a selected-category dataset, not every NYC 311 category.
- `acs_year` selects one ACS five-year release. `acs_available_date` documents its release date; update both together. The default 2022 release became available December 7, 2023. Earlier closures are flagged as having unavailable ACS features.
- `pluto_csv` optionally points to a historical PLUTO CSV; `pluto_available_date` records when that release was available. With no CSV, the current PLUTO source is downloaded and its historical-availability flag is false.
- `kaggle_dataset`: `your-username/nyc-housing-service-equity`. If null, the scripts derive the owner from `KAGGLE_USERNAME` or the standard local `kaggle.json` file. Token-only authentication still requires an explicit dataset ID or username.
- `kaggle_public` defaults to false. Creating a new dataset is private; updating a dataset preserves its existing visibility.
- `kaggle_license` defaults to `other`; preserve and review the linked source terms before choosing a different redistribution license.
- `work_dir` and `pluto_csv` are resolved relative to the config file, not your shell directory.

Set `CENSUS_API_KEY` to a free Census API key from https://api.census.gov/data/key_signup.html. The live Census API required a key during verification. An optional `SOCRATA_APP_TOKEN` can help with NYC rate limits. Neither credential is stored in reports. Set credentials locally in your shell; do not paste them into source code or chat.

```bash
export CENSUS_API_KEY="your-census-key"
```

## Download and merge

```bash
python data_preperation/pipeline.py
```

The initial citywide download can take substantial time and disk space. Complete pages are cached, so rerun the same command to resume interrupted downloads. A completed cache is a fixed extract: choose a new `work_dir` when you want refreshed source data. Changing source filters creates separate cache entries. Source datasets remain live while downloading; this is not an atomic historical snapshot.

Outputs under `data_preperation/work/`:

```text
raw/                           Cached source pages and geography
housing.duckdb                 Local database, table: complaints
final/
  nyc_housing_merged.parquet    Recommended analysis format
  nyc_housing_merged.csv.gz     Compressed CSV alternative
  quality_report.json          Scope, source links, match rates and column types
  DATASET_README.md             Dataset description and column listing
```

Join rules:

1. Normalize BBL as a 10-character string; left join PLUTO. Duplicate property keys fail instead of multiplying requests.
2. Spatially intersect complaint points with 2020 tract polygons. Unmatched or ambiguous points receive no tract.
3. Left join ACS on the full 11-character tract GEOID.
4. Match HPD on BBL and aggregate earlier issued/approved violations in the preceding year at each complaint's closure cutoff.
5. Calculate earlier selected-category complaint counts and 30/60/90-day recurrence outcomes. Final filtering removes the extra history/follow-up requests from output rows.

Records without a usable property ID remain in the output. Their property history and recurrence labels are null, not zero. Current HPD status is not used as historical status. Violations without both issuance and approval dates before closure are excluded from prior counts. A zero count means no eligible recorded match, not no housing problems.

## Publish to Kaggle

Authenticate with the official Kaggle CLI using your account settings. For example, place your downloaded API credentials at `~/.kaggle/kaggle.json` and restrict permissions with `chmod 600 ~/.kaggle/kaggle.json`, or use the CLI's supported environment credentials. Never commit credentials.

Build and create the Kaggle dataset in one command:

```bash
python data_preperation/pipeline.py --publish create --dataset YOUR_USERNAME/nyc-housing-service-equity
```

Publish an already built dataset:

```bash
python data_preperation/publish_kaggle.py --mode create --dataset YOUR_USERNAME/nyc-housing-service-equity
```

For later uploads to the same Kaggle dataset:

```bash
python data_preperation/publish_kaggle.py --mode version --dataset YOUR_USERNAME/nyc-housing-service-equity
```

Only four completed artifacts in `final/` are staged for upload; raw downloads and the local database are excluded. The uploader validates the saved row-count report before invoking Kaggle. Use `create` for a new dataset and `version` for an existing one; authentication or network failures are reported, not treated as proof that a dataset does not exist.

## Download the finished dataset from Kaggle

```bash
python data_preperation/download_kaggle.py --dataset YOUR_USERNAME/nyc-housing-service-equity
```

By default downloads into `work/from_kaggle/`. Use `--output PATH` to choose another directory and `--force` to overwrite an existing download. Private datasets require authorized Kaggle credentials.

```python
import pandas as pd
df = pd.read_parquet('data_preperation/work/from_kaggle/nyc_housing_merged.parquet')
```

## Research limitations

- Administrative closure does not prove physical repair. Recurrence means another report of the same standardized category at the same tax lot, not a verified repeat defect in the same apartment.
- All `recurrence_*`, `followup_complete_*`, `next_same_category_report_date`, and `days_to_next_same_category_report` columns are outcomes/evaluation fields, not predictors.
- Prior complaint counts cover only downloaded categories and exclude the current request. Check `history_complete_365d` before using them.
- Current PLUTO and retrospectively downloaded source records may contain information unavailable historically. Availability flags help screen features but do not establish a fully leakage-free historical snapshot. Prefer documented historical releases for prediction.
- Geography is tract-level and properties are tax lots; neither is necessarily an individual building or household.
- Severity, income groups, population density and hotspot assignments are deliberately left for the analysis stage. Defining severity needs documented rules; density needs a documented land-area denominator.
- This implementation loads selected sources into memory and uses DuckDB for temporal joins. Reduce the date/category scope if memory is constrained; raw paging is resumable but the merge requires enough RAM.

## Verification

```bash
python -m unittest discover -s data_preperation/tests -v
python data_preperation/check_sources.py
```

The first command uses synthetic fixtures and makes no network calls. The second makes small live requests to validate source field names and Census geography; it does not download the full dataset or upload anything.

Sources: [311](https://data.cityofnewyork.us/d/erm2-nwe9), [PLUTO](https://data.cityofnewyork.us/d/64uk-42ks), [HPD](https://data.cityofnewyork.us/d/wvxf-dwi5), [tract boundaries](https://data.cityofnewyork.us/d/63ge-mke6), [ACS API](https://api.census.gov/data/2022/acs/acs5.html), [official Kaggle CLI](https://github.com/Kaggle/kaggle-cli).
