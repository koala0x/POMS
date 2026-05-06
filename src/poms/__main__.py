"""
python -m poms 的入口

说明：
- 运行 `python3 -m poms ...` 时，Python 会执行本文件。
- 这里仅做最薄的一层转发到 main()，避免重复实现 CLI。
"""

from .main import main


if __name__ == "__main__":
    raise SystemExit(main())
