"""`python -m strawberry` is the `strawberry` CLI (the daemon is `python -m strawberry.strawberryd`)."""

import sys

from .cli import main

sys.exit(main())
