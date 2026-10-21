"""Download the merged Kaggle dataset without rebuilding its public sources."""
import argparse
from pathlib import Path

from common import dataset_id, kaggle_command, load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--dataset", help="owner/dataset-name")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true", help="Allow overwriting an existing download")
    args = parser.parse_args()
    config = load_config(args.config)
    identifier = dataset_id(config, args.dataset)
    output = args.output or Path(config["work_dir"]) / "from_kaggle"
    if output.exists() and any(output.iterdir()) and not args.force:
        raise FileExistsError(f"{output} is not empty; choose another directory or use --force")
    output.mkdir(parents=True, exist_ok=True)
    command = ["datasets", "download", identifier, "-p", output, "--unzip"]
    if args.force:
        command.append("--force")
    kaggle_command(*command)
    print(f"Dataset downloaded to {output.resolve()}")


if __name__ == "__main__":
    main()
