"""Entry point for `python -m tools.build_image` invocation."""
from __future__ import annotations

import sys

from tools.build_image import main

if __name__ == "__main__":
    sys.exit(main())
