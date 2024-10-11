from dataclasses import dataclass

import numpy as np
from omegaconf import MISSING

from kylin.utils import TimeMeter

from .ranker import RankerBase, RankerConfig, Rankers


@dataclass
class VoyageRankerConfig(RankerConfig):
    model: str = "rerank-2"
    api_key: str = MISSING
    timeout: float = 3.0
    max_retries: int = 3


@Rankers("voyage", config_class=VoyageRankerConfig)
class VoyageRanker(RankerBase):
    def __init__(self, cfg: VoyageRankerConfig) -> None:
        super().__init__(cfg)
        from voyageai import Client

        self.client = Client(
            api_key=cfg.api_key, max_retries=cfg.max_retries, timeout=cfg.timeout
        )
        self.model = cfg.model
        return

    @TimeMeter("voyage_rank")
    def _rank(self, query: str, candidates: list[str]) -> tuple[np.ndarray, np.ndarray]:
        result = self.client.rerank(
            query=query,
            documents=candidates,
            model=self.model,
            top_k=len(candidates),
        )
        scores = [i.relevance_score for i in result.results]
        return None, scores