# NYC housing service research

Download, merge, and share the project's 311, property, housing-violation and neighborhood data using the [data preparation pipeline](data_preperation/README.md).

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r data_preperation/requirements.txt
```

Create a repository-root `.env` with your **activated** Census key and Kaggle credentials:

```dotenv
CENSUS_API_KEY=your_activated_census_key
KAGGLE_API_TOKEN=your_kaggle_token
KAGGLE_USERNAME=your_kaggle_username
```

The scripts load `.env` automatically; it is excluded from Git. Then verify the sources before starting the full build:

```bash
python data_preperation/check_sources.py
python data_preperation/pipeline.py --publish create
```

Only run the build after all source checks pass. The first upload creates a private dataset at `YOUR_USERNAME/nyc-housing-service-equity`; use `--publish version` for later uploads.

To download the completed dataset:

```bash
python data_preperation/download_kaggle.py
```

See [credential troubleshooting and configuration](data_preperation/README.md#credentials-do-this-first) for Census activation, invalid-key errors, environment-variable precedence, and upload retries.
