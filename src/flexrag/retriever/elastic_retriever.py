import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from omegaconf import MISSING
from elasticsearch import Elasticsearch

from flexrag.utils import SimpleProgressLogger, LOGGER_MANAGER, TIME_METER

from .retriever_base import (
    RETRIEVERS,
    LocalRetriever,
    LocalRetrieverConfig,
    RetrievedContext,
)

logger = LOGGER_MANAGER.get_logger("flexrag.retrievers.elastic")


@dataclass
class ElasticRetrieverConfig(LocalRetrieverConfig):
    host: str = "http://localhost:9200"
    api_key: Optional[str] = None
    index_name: str = MISSING
    custom_properties: Optional[dict] = None
    verbose: bool = False
    retry_times: int = 3
    retry_delay: float = 0.5
    id_field: str = "id"


@RETRIEVERS("elastic", config_class=ElasticRetrieverConfig)
class ElasticRetriever(LocalRetriever):
    name = "ElasticSearch"

    def __init__(self, cfg: ElasticRetrieverConfig) -> None:
        super().__init__(cfg)
        # set basic args
        self.host = cfg.host
        self.api_key = cfg.api_key
        self.index_name = cfg.index_name
        self.verbose = cfg.verbose
        self.retry_times = cfg.retry_times
        self.retry_delay = cfg.retry_delay
        self.custom_properties = cfg.custom_properties
        self.id_field = cfg.id_field

        # prepare client
        self.client = Elasticsearch(
            self.host,
            api_key=self.api_key,
            max_retries=cfg.retry_times,
            retry_on_timeout=True,
        )

        # set logger
        transport_logger = logging.getLogger("elastic_transport.transport")
        es_logger = logging.getLogger("elasticsearch")
        if self.verbose:
            transport_logger.setLevel(logging.INFO)
            es_logger.setLevel(logging.INFO)
        else:
            transport_logger.setLevel(logging.WARNING)
            es_logger.setLevel(logging.WARNING)
        return

    @TIME_METER("elastic_search", "add_passages")
    def add_passages(self, passages: Iterable[dict[str, str]]):
        def generate_actions():
            index_exists = self.client.indices.exists(index=self.index_name)
            actions = []
            for n, passage in enumerate(passages):
                # build index if not exists
                if not index_exists:
                    if self.custom_properties is None:
                        properties = {
                            key: {"type": "text", "analyzer": "english"}
                            for key in passage.keys()
                        }
                    else:
                        properties = self.custom_properties
                    index_body = {
                        "settings": {"number_of_shards": 1, "number_of_replicas": 1},
                        "mappings": {
                            "properties": properties,
                        },
                    }
                    self.client.indices.create(
                        index=self.index_name,
                        body=index_body,
                    )
                    index_exists = True

                # prepare action
                if (self.id_field is not None) and (self.id_field in passage):
                    docid = passage[self.id_field]
                else:
                    docid = str(len(self) + n)
                action = {
                    "index": {
                        "_index": self.index_name,
                        "_id": docid,
                    }
                }
                actions.append(action)
                actions.append(passage)
                if len(actions) == self.batch_size * 2:
                    yield actions
                    actions = []
            if actions:
                yield actions
            return

        p_logger = SimpleProgressLogger(logger, interval=self.log_interval)
        for actions in generate_actions():
            r = self.client.bulk(
                operations=actions,
                index=self.index_name,
            )
            if r.body["errors"]:
                err_passage_ids = [
                    item["index"]["_id"]
                    for item in r.body["items"]
                    if item["index"]["status"] != 201
                ]
                raise RuntimeError(f"Failed to index passages: {err_passage_ids}")
            p_logger.update(len(actions) // 2, "Indexing")
        return

    @TIME_METER("elastic_search", "search")
    def search_batch(
        self,
        query: list[str],
        search_method: str = "full_text",
        **search_kwargs,
    ) -> list[list[RetrievedContext]]:
        # check search method
        match search_method:
            case "full_text":
                query_type = "multi_match"
            case "lucene":
                query_type = "query_string"
            case _:
                raise ValueError(f"Invalid search method: {search_method}")

        # prepare search body
        body = []
        for q in query:
            body.append({"index": self.index_name})
            body.append(
                {
                    "query": {
                        query_type: {
                            "query": q,
                            "fields": self.fields,
                        },
                    },
                    "size": search_kwargs.get("top_k", self.top_k),
                }
            )

        # search and post-process
        responses = self.client.msearch(body=body, **search_kwargs)["responses"]
        return self._form_results(query, responses)

    def clean(self) -> None:
        if self.index_name in self.indices:
            self.client.indices.delete(index=self.index_name)
        return

    def __len__(self) -> int:
        return self.client.count(index=self.index_name)["count"]

    @property
    def indices(self) -> list[str]:
        return [i["index"] for i in self.client.cat.indices(format="json")]

    def _form_results(
        self, query: list[str], responses: list[dict] | None
    ) -> list[list[RetrievedContext]]:
        results = []
        if responses is None:
            responses = [{"status": 500}] * len(query)
        for r, q in zip(responses, query):
            if r["status"] != 200:
                results.append(
                    [
                        RetrievedContext(
                            retriever=self.name,
                            query=q,
                            data={},
                            source=self.index_name,
                            score=0.0,
                        )
                    ]
                )
                continue
            r = r["hits"]["hits"]
            results.append(
                [
                    RetrievedContext(
                        retriever=self.name,
                        query=q,
                        data=i["_source"],
                        source=self.index_name,
                        score=i["_score"],
                    )
                    for i in r
                ]
            )
        return results

    @property
    def fields(self) -> list[str]:
        if self.index_name in self.indices:
            mapping = self.client.indices.get_mapping(index=self.index_name)
            return list(mapping[self.index_name]["mappings"]["properties"].keys())
        return []