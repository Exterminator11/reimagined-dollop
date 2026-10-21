"""Publish only the completed, validated pipeline artifacts to Kaggle."""
import argparse
import json
import tempfile
import shutil
from pathlib import Path

from common import dataset_id, kaggle_command, load_config

ARTIFACTS = ["nyc_housing_merged.parquet", "nyc_housing_merged.csv.gz",
             "quality_report.json", "DATASET_README.md"]


def publish(config, output, mode, override=None):
    identifier = dataset_id(config, override)
    title = config["kaggle_title"].strip()
    if not 6 <= len(title) <= 50:
        raise ValueError("kaggle_title must contain 6–50 characters; shorten it in your config file")
    output = Path(output)
    for name in ARTIFACTS:
        if not (output / name).is_file():
            raise FileNotFoundError(f"Build the dataset first; missing {name}")
    report = json.loads((output / "quality_report.json").read_text())
    if report["rows"] < 1 or report["rows"] != report["unique_requests"]:
        raise ValueError("Refusing upload: dataset is empty or request IDs are not unique")
    # An isolated staging directory prevents accidental upload of raw data/credentials.
    with tempfile.TemporaryDirectory(prefix="housing-kaggle-") as temporary:
        stage = Path(temporary)
        for name in ARTIFACTS:
            shutil.copy2(output / name, stage / name)
        metadata = {
            "id": identifier, "title": title,
            "licenses": [{"name": config.get("kaggle_license", "other")}],
            "description": (output / "DATASET_README.md").read_text(),
        }
        (stage / "dataset-metadata.json").write_text(json.dumps(metadata, indent=2))
        if mode == "create":
            args = ["datasets", "create", "-p", stage, "--keep-tabular"]
            if config.get("kaggle_public", False):
                args.append("--public")
        elif mode == "version":
            args = ["datasets", "version", "-p", stage, "--keep-tabular", "-m",
                    "Refresh housing complaint joins and quality report"]
        else:
            raise ValueError("mode must be create or version")
        kaggle_command(*args)
    print(f"Upload submitted: https://www.kaggle.com/datasets/{identifier}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--dataset")
    parser.add_argument("--mode", choices=["create", "version"], default="create")
    args = parser.parse_args()
    config = load_config(args.config)
    publish(config, Path(config["work_dir"]) / "final", args.mode, args.dataset)


if __name__ == "__main__":
    main()
