"""Allow `python -m hub` as a fallback entry point."""

from hub.cli import main


if __name__ == "__main__":
    main()
