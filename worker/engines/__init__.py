"""All engines, in one place. An engine whose tool isn't installed simply isn't offered."""

from __future__ import annotations

import json
from pathlib import Path

from worker.engines.archives import archive_engines
from worker.engines.base import Engine
from worker.engines.documents import document_engines
from worker.engines.images import image_engines
from worker.engines.media import media_engines
from worker.engines.misc import misc_engines
from worker.engines.technical import technical_engines


def all_engines() -> list[Engine]:
    return [
        *media_engines(),
        *image_engines(),
        *document_engines(),
        *technical_engines(),
        *archive_engines(),
        *misc_engines(),
    ]


def load_engines(data_path: Path) -> dict[str, Engine]:
    data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    engines = {}
    for engine in all_engines():
        if engine.name in data:
            engine.load_data(data[engine.name])
            engines[engine.name] = engine
    return engines
