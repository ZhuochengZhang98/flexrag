import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
from omegaconf import MISSING

from .model_base import (
    GeneratorBase,
    GeneratorConfig,
    GenerationConfig,
    EncoderBase,
    EncoderConfig,
)


@dataclass
class OpenAIGeneratorConfig(GeneratorConfig):
    model_name: str = MISSING
    base_url: Optional[str] = None
    api_key: str = "EMPTY"
    verbose: bool = False


@dataclass
class OpenAIEncoderConfig(EncoderConfig):
    model_name: str = MISSING
    base_url: Optional[str] = None
    api_key: str = "EMPTY"
    verbose: bool = False
    dimension: int = 512


class OpenAIGenerator(GeneratorBase):
    def __init__(self, cfg: OpenAIGeneratorConfig) -> None:
        from openai import OpenAI

        self.client = OpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
        )
        self.model_name = cfg.model_name
        if not cfg.verbose:
            logger = logging.getLogger("httpx")
            logger.setLevel(logging.WARNING)
        self._check()
        return

    def chat(
        self,
        prompts: list[list[dict[str, str]]],
        generation_config: GenerationConfig = None,
    ) -> list[str]:
        responses = []
        if "llama-3" in self.model_name.lower():
            extra_body = {"stop_token_ids": [128009]}  # hotfix for llama-3
        else:
            extra_body = None
        for conv in prompts:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=conv,
                temperature=generation_config.temperature,
                max_tokens=generation_config.max_new_tokens,
                top_p=generation_config.top_p,
                extra_body=extra_body,
            )
            responses.append(response.choices[0].message.content)
        return responses

    def generate(
        self, prefixes: list[str], generation_config: GenerationConfig = None
    ) -> list[str]:
        responses = []
        for prefix in prefixes:
            response = self.client.completions.create(
                model=self.model_name,
                prompt=prefix,
                temperature=generation_config.temperature,
                max_tokens=generation_config.max_new_tokens,
                top_p=generation_config.top_p,
            )
            responses.append(response.choices[0].text)
        return responses

    def _check(self):
        model_lists = [i.id for i in self.client.models.list().data]
        assert self.model_name in model_lists, f"Model {self.model_name} not found"


class OpenAIEncoder(EncoderBase):
    def __init__(self, cfg: OpenAIEncoderConfig) -> None:
        from openai import OpenAI

        self.client = OpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
        )
        self.model_name = cfg.model_name
        self.dimension = cfg.dimension
        if not cfg.verbose:
            logger = logging.getLogger("httpx")
            logger.setLevel(logging.WARNING)
        self._check()
        return

    def encode(self, texts: list[str]) -> np.ndarray:
        embeddings = []
        for text in texts:
            text = text.replace("\n", " ")
            embeddings.append(
                self.client.embeddings.create(model=self.model_name, text=text)
                .data[0]
                .embedding
            )[: self.dimension]
        return np.array(embeddings)

    @property
    def embedding_size(self):
        return self.dimension

    def _check(self):
        model_lists = [i.id for i in self.client.models.list().data]
        assert self.model_name in model_lists, f"Model {self.model_name} not found"
