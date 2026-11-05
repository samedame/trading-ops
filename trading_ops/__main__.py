import sys

from .cli import main
from .sizing import CapitalNotSet

try:
    sys.exit(main())
except CapitalNotSet as exc:
    print(f"error: {exc}", file=sys.stderr)
    sys.exit(2)
