"""The sole actual nonpublishing Bosch collection test."""

import json
import sys

import adapter


def main():
    records = adapter.collect()
    adapter.validate_records(records)
    print(
        f"Validated {len(records)} real Bosch programme overviews",
        file=sys.stderr,
    )
    print(json.dumps(records, ensure_ascii=False))


if __name__ == "__main__":
    main()
