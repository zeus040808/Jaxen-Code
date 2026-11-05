"""python -m jaxen 的入口；命令解析与交互委托给 cli。"""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
