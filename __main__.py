"""`python -m AIClientCenter` 的独立启动入口。"""

from .cli.launcher import main


if __name__ == "__main__":
    raise SystemExit(main())
