import sys
from pathlib import Path

# Make the repo root importable so tests can `import core` without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
