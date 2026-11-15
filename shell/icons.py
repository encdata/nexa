from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=None)
def load(name):
    if isinstance(name, bytes):
        return name
    path = Path(name)
    if path.is_file():
        return path.read_bytes()
    if path.suffix == ".png":
        return path.read_bytes()
    return (Path(__file__).parent.parent / "assets" / "icons" / f"{name}.png").read_bytes()


def preload(names):
    for name in set(names):
        load(name)
