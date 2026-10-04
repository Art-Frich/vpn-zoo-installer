import sys
from pathlib import Path

# python -m unittest discover -s zoo/tests -t zoo  (или из каталога zoo/)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
