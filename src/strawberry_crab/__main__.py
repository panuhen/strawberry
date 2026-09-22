"""`python -m strawberry_crab` is the `strawberry` CLI (the daemon is `python -m strawberry_crab.strawberryd`)."""

import sys

from .cli import main

sys.exit(main())
