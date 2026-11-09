import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "pilar2" / "services", ROOT / "pilar2" / "services" / "miners", ROOT / "pilar2" / "tools",
          ROOT / "pilar1" / "cpu"):
    sys.path.insert(0, str(p))
