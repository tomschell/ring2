import sys
from pathlib import Path

if not __package__:  # run as `python3 /path/to/ring2 ...`, as a session does
    sys.path[0] = str(Path(__file__).resolve().parent.parent)
from ring2.app import main

raise SystemExit(main())
