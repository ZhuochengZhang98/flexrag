import json
from dataclasses import dataclass

import hydra
from hydra.core.config_store import ConfigStore
from omegaconf import MISSING

from librarian.retriever.retriever_base import RETRIEVAL_CACHE
from librarian.utils import Choices


@dataclass
class Config:
    export_path: str = MISSING
    action: Choices(["clear", "export", "_"]) = "_"  # type: ignore


cs = ConfigStore.instance()
cs.store(name="default", node=Config)


@hydra.main(version_base="1.3", config_path=None, config_name="default")
def main(config: Config):
    match config.action:
        case "clear":
            RETRIEVAL_CACHE.clear()
        case "export":
            with open(config.export_path, "w") as f:
                for key in RETRIEVAL_CACHE:
                    data = json.loads(key)
                    data["retrieved_contexts"] = RETRIEVAL_CACHE[key]
                    f.write(json.dumps(data) + "\n")
        case _:
            raise ValueError("No action specified")
    return


if __name__ == "__main__":
    main()
