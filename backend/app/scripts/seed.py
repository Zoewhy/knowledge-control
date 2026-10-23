import json

from app.scripts.production_seed import seed


def main() -> None:
    print(json.dumps(seed("small"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
