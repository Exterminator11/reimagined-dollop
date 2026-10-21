"""Shared configuration and Kaggle CLI helpers (no credential handling)."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent / ".env", override=False)
# Accept the short names used in the project's existing .env file.
for short_name, standard_name in [("CENSUS_KEY", "CENSUS_API_KEY"),
                                  ("KAGGLE_TOKEN", "KAGGLE_API_TOKEN")]:
    if short_name in os.environ and standard_name not in os.environ:
        os.environ[standard_name] = os.environ[short_name]


def load_config(path=None):
    path = Path(path).resolve() if path else ROOT / "config.json"
    config = json.loads(path.read_text())
    config["work_dir"] = str((path.parent / config["work_dir"]).resolve())
    if config.get("pluto_csv"):
        config["pluto_csv"] = str((path.parent / config["pluto_csv"]).resolve())
    return config


def dataset_id(config, override=None):
    value = override or config.get("kaggle_dataset")
    if not value:
        username = os.getenv("KAGGLE_USERNAME")
        credentials = Path(os.getenv("KAGGLE_CONFIG_DIR", str(Path.home() / ".kaggle"))) / "kaggle.json"
        if not username and credentials.exists():
            username = json.loads(credentials.read_text()).get("username")
        if username:
            value = f"{username}/nyc-housing-service-equity"
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", value):
        raise ValueError("Set kaggle_dataset in config.json, pass --dataset owner/dataset-name, or configure KAGGLE_USERNAME")
    return value


def kaggle_command(*args):
    candidate = Path(sys.executable).parent / "kaggle"
    executable = str(candidate) if candidate.exists() else shutil.which("kaggle")
    if not executable:
        raise RuntimeError("Install requirements.txt to use the Kaggle CLI")
    subprocess.run([executable, *map(str, args)], check=True)
