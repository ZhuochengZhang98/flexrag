import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import torch
from omegaconf import MISSING
from torch.nn.parallel import DataParallel as DP
from transformers import (
    AutoConfig,
    AutoModel,
    AutoModelForCausalLM,
    AutoModelForSeq2SeqLM,
    AutoModelForSequenceClassification,
    AutoTokenizer,
    BertPreTrainedModel,
    BertModel,
    RobertaModel,
    XLMRobertaModel,
    XLMRobertaPreTrainedModel,
)
from transformers import GenerationConfig as HFGenerationConfig
from transformers import PreTrainedModel, PreTrainedTokenizer
from transformers.dynamic_module_utils import get_class_from_dynamic_module

from kylin.prompt import ChatPrompt, load_template
from kylin.utils import Choices, TimeMeter

from .model_base import (
    EncoderBase,
    EncoderBaseConfig,
    Encoders,
    GenerationConfig,
    GeneratorBase,
    GeneratorBaseConfig,
    Generators,
    RankerBase,
    RankerConfig,
    RankingResult,
)
from .utils import guess_model_name

logger = logging.getLogger(__name__)


def get_colbert_model(
    base_model: str = "bert",
    output_dim: int = 128,
    model_path: str = None,
):
    """Code adapted from https://github.com/hotchpotch/JQaRA/blob/main/evaluator/reranker/colbert_reranker.py"""
    match base_model:
        case "bert":
            pretrained_class = BertPreTrainedModel
            model_class = BertModel
        case "xlm-roberta":
            pretrained_class = XLMRobertaPreTrainedModel
            model_class = XLMRobertaModel
        case "self_implemented":
            model_cfg = AutoConfig.from_pretrained(model_path, trust_remote_code=True)
            assert "AutoModel" in model_cfg.auto_map
            model_class_str = model_cfg.auto_map["AutoModel"]
            pretrained_class_str = model_class_str.replace("Model", "PreTrainedModel")
            model_class = get_class_from_dynamic_module(model_class_str, model_path)
            pretrained_class = get_class_from_dynamic_module(
                pretrained_class_str, model_path
            )
        case _:
            raise ValueError(f"Unsupported base model: {base_model}")

    class ColBERTModel(pretrained_class):
        def __init__(self, config):
            super().__init__(config)
            setattr(self, self.base_model_prefix, model_class(config))
            self.linear = torch.nn.Linear(config.hidden_size, output_dim, bias=False)
            self.init_weights()
            return

        def forward(
            self,
            input_ids=None,
            attention_mask=None,
            token_type_ids=None,
            position_ids=None,
            head_mask=None,
            inputs_embeds=None,
            encoder_hidden_states=None,
            encoder_attention_mask=None,
            output_attentions=None,
            output_hidden_states=None,
        ):
            outputs = getattr(self, self.base_model_prefix)(
                input_ids,
                attention_mask=attention_mask,
                token_type_ids=token_type_ids,
                position_ids=position_ids,
                head_mask=head_mask,
                inputs_embeds=inputs_embeds,
                encoder_hidden_states=encoder_hidden_states,
                encoder_attention_mask=encoder_attention_mask,
                output_attentions=output_attentions,
                output_hidden_states=True,  # Always output hidden states
            )

            sequence_output = outputs[0]
            return self.linear(sequence_output)

    return ColBERTModel


def load_hf_model(
    model_path: str,
    tokenizer_path: Optional[str] = None,
    model_type: Optional[str] = None,
    device_id: list[int] = [],
    load_dtype: str = "auto",
    trust_remote_code: bool = False,
    pipeline_parallel: bool = False,
    is_training: bool = False,
    colbert_base_model: str = "bert",
    colbert_dim: int = 128,
) -> tuple[PreTrainedModel, PreTrainedTokenizer]:
    # prepare dtype
    load_in_4bit = False
    load_in_8bit = False
    match load_dtype:
        case "bfloat16":
            load_dtype = torch.bfloat16
        case "bf16":
            load_dtype = torch.bfloat16
        case "float32":
            load_dtype = torch.float32
        case "fp32":
            load_dtype = torch.float32
        case "float16":
            load_dtype = torch.float16
        case "fp16":
            load_dtype = torch.float16
        case "half":
            load_dtype = torch.float16
        case "8bit":
            load_dtype = None
            load_in_8bit = True
        case "4bit":
            load_dtype = None
            load_in_4bit = True
        case "auto":
            load_dtype = "auto"
        case _:
            raise ValueError(f"Unsupported load_dtype: {load_dtype}")

    # prepare device
    if pipeline_parallel:
        device_map = "auto"
    elif torch.cuda.is_available() and (len(device_id) > 0):
        device_map = device_id[0]
    else:
        device_map = None

    # load model
    match model_type:
        case "causal_lm":
            model_class = AutoModelForCausalLM
        case "seq2seq":
            model_class = AutoModelForSeq2SeqLM
        case "sequence_classification":
            model_class = AutoModelForSequenceClassification
        case "colbert":
            model_class = get_colbert_model(colbert_base_model, colbert_dim, model_path)
        case _:
            model_class = AutoModel
    model = model_class.from_pretrained(
        model_path,
        device_map=device_map,
        torch_dtype=load_dtype,
        load_in_4bit=load_in_4bit,
        load_in_8bit=load_in_8bit,
        trust_remote_code=trust_remote_code,
    )
    if not is_training:
        model.eval()

    # load tokenizer
    if tokenizer_path is not None:
        tokenizer_path = tokenizer_path
    else:
        tokenizer_path = model_path
    tokenizer = AutoTokenizer.from_pretrained(
        tokenizer_path,
        trust_remote_code=trust_remote_code,
    )
    return model, tokenizer


@dataclass
class HFModelConfig:
    model_path: str = MISSING
    tokenizer_path: Optional[str] = None
    trust_remote_code: bool = False
    device_id: list[int] = field(default_factory=list)
    load_dtype: Choices(  # type: ignore
        [
            "bfloat16",
            "bf16",
            "float32",
            "fp32",
            "float16",
            "fp16",
            "half",
            "8bit",
            "4bit",
            "auto",
        ]
    ) = "auto"


@dataclass
class HFGeneratorConfig(GeneratorBaseConfig, HFModelConfig):
    pipeline_parallel: bool = False
    use_minference: bool = False


@Generators("hf", config_class=HFGeneratorConfig)
class HFGenerator(GeneratorBase):
    model: PreTrainedModel

    def __init__(self, cfg: HFGeneratorConfig) -> None:
        # load model
        self.model, self.tokenizer = load_hf_model(
            model_path=cfg.model_path,
            tokenizer_path=cfg.tokenizer_path,
            model_type="causal_lm",
            device_id=cfg.device_id,
            load_dtype=cfg.load_dtype,
            trust_remote_code=cfg.trust_remote_code,
            pipeline_parallel=cfg.pipeline_parallel,
        )
        self._patch_model()

        # prepare prompt function
        model_name = guess_model_name(self.model.config)
        self.template = load_template(model_name=model_name, tokenizer=self.tokenizer)

        # load minference
        if cfg.use_minference:
            assert (
                not cfg.pipeline_parallel
            ), "Minference does not support pipeline parallel"
            from minference import MInference

            try:
                inf_patch = MInference("minference", model_name)
                self.model = inf_patch(self.model)
            except Exception as e:
                logger.warning(f"Unable to load minference: {e}")
        return

    @TimeMeter("hf_generate")
    @torch.no_grad()
    def generate(
        self,
        prefixes: list[str],
        generation_config: GenerationConfig = GenerationConfig(),
    ) -> list[list[str]]:
        bsz = len(prefixes)
        sample_num = generation_config.sample_num
        inputs = self.tokenizer(
            prefixes, return_tensors="pt", padding=True, truncation=True
        )
        inputs = inputs.to(self.model.device)

        # prepare generation config
        hf_gen_cfg = self._get_options(generation_config)
        if generation_config.eos_token_id is not None:
            inputs["eos_token_id"] = generation_config.eos_token_id
        else:
            inputs["eos_token_id"] = self.tokenizer.eos_token_id

        # generate
        outputs = self.model.generate(
            **inputs,
            generation_config=hf_gen_cfg,
        )

        # truncate the input tokens
        outputs = outputs.view(bsz, sample_num, -1)
        input_lengths = inputs["attention_mask"].sum(dim=1)
        responses = []
        for i in range(bsz):
            samples = [sample[input_lengths[i] :] for sample in outputs[i]]
            samples = [
                self.tokenizer.decode(sample, skip_special_tokens=True)
                for sample in samples
            ]
            responses.append(samples)
        return responses

    def chat(
        self,
        prompts: list[ChatPrompt],
        generation_config: GenerationConfig = GenerationConfig(),
    ) -> list[list[str]]:
        assert self.template is not None, "Chat function is disabled."
        prefixes = [self.template.render_to_text(prompt) for prompt in prompts]
        return self.generate(prefixes, generation_config)

    def _get_options(self, generation_config: GenerationConfig) -> HFGenerationConfig:
        return HFGenerationConfig(
            do_sample=generation_config.do_sample,
            temperature=generation_config.temperature,
            max_new_tokens=generation_config.max_new_tokens,
            top_p=generation_config.top_p,
            top_k=generation_config.top_k,
            num_return_sequences=generation_config.sample_num,
        )

    def _patch_model(self) -> None:
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.add_special_tokens({"pad_token": "<pad>"})
            self.model.resize_token_embeddings(len(self.tokenizer))
        return


@dataclass
class HFEncoderConfig(EncoderBaseConfig, HFModelConfig):
    max_encode_length: int = 512
    encode_method: Choices(["cls", "mean"]) = "mean"  # type: ignore


@Encoders("hf", config_class=HFEncoderConfig)
class HFEncoder(EncoderBase):
    def __init__(self, cfg: HFEncoderConfig):
        self.devices = cfg.device_id
        # load model
        self.model, self.tokenizer = load_hf_model(
            model_path=cfg.model_path,
            tokenizer_path=cfg.tokenizer_path,
            load_dtype=cfg.load_dtype,
            device_id=cfg.device_id,
            trust_remote_code=cfg.trust_remote_code,
        )
        if len(self.devices) > 1:
            self.dp_model = DP(self.model, device_ids=self.devices)
        else:
            self.dp_model = None

        # setup arguments
        self.max_encode_length = cfg.max_encode_length
        self.encode_method = cfg.encode_method
        return

    def get_embedding(
        self, hidden: torch.Tensor, attn_mask: torch.Tensor
    ) -> np.ndarray:
        if self.encode_method == "mean":
            attn_mask = attn_mask.to(hidden.device)
            embeddings = hidden.masked_fill(~attn_mask[..., None].bool(), 0.0)
            embeddings = embeddings.sum(dim=1) / attn_mask.sum(dim=1)[..., None]
            embeddings = embeddings.cpu().numpy()
        elif self.encode_method == "cls":
            embeddings = hidden[:, 0].cpu().numpy()
        else:
            raise ValueError(f"Unsupported encode method: {self.encode_method}")
        return embeddings

    def encode(self, texts: list[str]) -> np.ndarray:
        if (len(texts) >= len(self.devices) * 8) and (self.dp_model is not None):
            encoder = self.dp_model
        else:
            encoder = self.model
        return self._encode(texts, encoder)

    @torch.no_grad()
    def _encode(self, texts: list[str], model: torch.nn.Module | DP) -> np.ndarray:
        input_dict = self.tokenizer.batch_encode_plus(
            texts,
            return_tensors="pt",
            max_length=self.max_encode_length,
            padding=True,
            truncation=True,
        )
        if not isinstance(model, DP):
            input_dict = input_dict.to(model.device)
        mask = input_dict["attention_mask"]
        output = model(**input_dict).last_hidden_state
        embeddings = self.get_embedding(output, mask)
        return embeddings

    @property
    def embedding_size(self) -> int:
        return self.model.config.hidden_size


@dataclass
class HFCrossEncoderRankerConfig(RankerConfig, HFModelConfig):
    max_encode_length: int = 512


class HFCrossEncoderRanker(RankerBase):
    def __init__(self, cfg: HFCrossEncoderRankerConfig):
        # load model
        self.model, self.tokenizer = load_hf_model(
            cfg.model_path,
            tokenizer_path=cfg.tokenizer_path,
            model_type="sequence_classification",
            device_id=cfg.device_id,
            load_dtype=cfg.load_dtype,
            trust_remote_code=cfg.trust_remote_code,
        )
        self.max_encode_length = cfg.max_encode_length
        return

    @TimeMeter("hf_rank")
    @torch.no_grad()
    def rank(self, query: str, candidates: list[str]) -> RankingResult:
        # score the candidates
        input_texts = [(query, cand) for cand in candidates]
        inputs = self.tokenizer(
            input_texts,
            return_tensors="pt",
            max_length=self.max_encode_length,
            padding=True,
            truncation=True,
        )
        inputs = inputs.to(self.model.device)
        scores = self.model(**inputs).logits.squeeze().cpu().numpy()
        # rank the candidates
        rank_indices = np.argsort(-scores)
        return RankingResult(
            query=query,
            candidates=candidates,
            scores=list(scores),
            ranking=list(rank_indices),
        )


@dataclass
class HFSeq2SeqRankerConfig(RankerConfig, HFModelConfig):
    max_encode_length: int = 512
    input_template: str = "Query: {query} Document: {candidate} Relevant:"
    positive_token: str = "▁true"
    negative_token: str = "▁false"


class HFSeq2SeqRanker(RankerBase):
    def __init__(self, cfg: HFSeq2SeqRankerConfig):
        # load model
        self.model, self.tokenizer = load_hf_model(
            cfg.model_path,
            tokenizer_path=cfg.tokenizer_path,
            model_type="seq2seq",
            device_id=cfg.device_id,
            load_dtype=cfg.load_dtype,
            trust_remote_code=cfg.trust_remote_code,
        )
        self.max_encode_length = cfg.max_encode_length
        self.input_template = cfg.input_template
        self.positive_token = self.tokenizer.convert_tokens_to_ids(cfg.positive_token)
        self.negative_token = self.tokenizer.convert_tokens_to_ids(cfg.negative_token)
        self.generation_config = HFGenerationConfig(
            max_new_tokens=1, output_logits=True
        )
        return

    @TimeMeter("hf_rank")
    @torch.no_grad()
    def rank(self, query: str, candidates: list[str]) -> RankingResult:
        # prepare prompts
        input_texts = [
            self.input_template.format(query=query, candidate=cand)
            for cand in candidates
        ]
        inputs = self.tokenizer(
            input_texts,
            return_tensors="pt",
            max_length=self.max_encode_length,
            padding=True,
            truncation=True,
        )
        inputs = inputs.to(self.model.device)
        outputs = self.model.generate(
            **inputs,
            generation_config=self.generation_config,
            return_dict_in_generate=True,
        )
        logits = outputs.logits[0]
        positive_scores = logits[:, self.positive_token : self.positive_token + 1]
        negative_scores = logits[:, self.negative_token : self.negative_token + 1]
        scores = torch.softmax(
            torch.cat([positive_scores, negative_scores], dim=1), dim=1
        )[:, 0].cpu().numpy()  # fmt: skip
        # rank the candidates
        rank_indices = np.argsort(-scores)
        return RankingResult(
            query=query,
            candidates=candidates,
            scores=list(scores),
            ranking=list(rank_indices),
        )


@dataclass
class HFColBertRankerConfig(RankerConfig, HFModelConfig):
    base_model_type: str = "bert"
    output_dim: int = 128
    max_encode_length: int = 512
    query_token: str = "[unused0]"
    document_token: str = "[unused1]"
    normalize_embeddings: bool = True


class HFColBertRanker(RankerBase):
    """Code adapted from https://github.com/hotchpotch/JQaRA/blob/main/evaluator/reranker/colbert_reranker.py"""

    def __init__(self, cfg: HFColBertRankerConfig) -> None:
        self.model, self.tokenizer = load_hf_model(
            cfg.model_path,
            tokenizer_path=cfg.tokenizer_path,
            model_type="colbert",
            device_id=cfg.device_id,
            load_dtype=cfg.load_dtype,
            trust_remote_code=cfg.trust_remote_code,
            colbert_base_model=cfg.base_model_type,
            colbert_dim=cfg.output_dim,
        )
        self.max_encode_length = cfg.max_encode_length
        self.query_token_id = self.tokenizer.convert_tokens_to_ids(cfg.query_token)
        self.document_token_id = self.tokenizer.convert_tokens_to_ids(
            cfg.document_token
        )
        self.normalize = cfg.normalize_embeddings
        return

    def rank(self, query: str, candidates: list[str]) -> RankingResult:
        # tokenize the query & candidates
        query_inputs = self._query_encode([query])
        cand_inputs = self._document_encode(candidates)
        # encode the query & candidates
        query_embeds = self._encode(query_inputs)
        cand_embeds = self._encode(cand_inputs)
        # compute the scores using maxsim(max-cosine)
        token_scores = torch.einsum("qin,pjn->qipj", query_embeds, cand_embeds)
        token_scores = token_scores.masked_fill(
            cand_inputs["attention_mask"].unsqueeze(0).unsqueeze(0) == 0, -1e4
        )
        scores, _ = token_scores.max(-1)
        scores = scores.sum(1) / query_inputs["attention_mask"].sum(-1, keepdim=True)
        scores = scores.cpu().squeeze().float().numpy()
        # rank the candidates
        rank_indices = np.argsort(-scores)
        return RankingResult(
            query=query,
            candidates=candidates,
            scores=list(scores),
            ranking=list(rank_indices),
        )

    @torch.no_grad()
    def _tokenize(self, texts: list[str], insert_token_id: int, is_query: bool = False):
        # tokenize the input
        inputs = self.tokenizer(
            texts,
            return_tensors="pt",
            padding=True,
            max_length=self.max_encode_length - 1,  # for insert token
            truncation=True,
        )
        inputs = self._insert_token(inputs, insert_token_id)  # type: ignore

        # padding for query
        if is_query:
            mask_token_id = self.tokenizer.mask_token_id

            new_encodings = {"input_ids": [], "attention_mask": []}

            for i, input_ids in enumerate(inputs["input_ids"]):
                original_length = (
                    (input_ids != self.tokenizer.pad_token_id).sum().item()
                )

                # Calculate QLEN dynamically for each query
                if original_length % 16 <= 8:
                    QLEN = original_length + 8
                else:
                    QLEN = math.ceil(original_length / 16) * 16

                if original_length < QLEN:
                    pad_length = QLEN - original_length
                    padded_input_ids = input_ids.tolist() + [mask_token_id] * pad_length
                    padded_attention_mask = (
                        inputs["attention_mask"][i].tolist() + [0] * pad_length
                    )
                else:
                    padded_input_ids = input_ids[:QLEN].tolist()
                    padded_attention_mask = inputs["attention_mask"][i][:QLEN].tolist()

                new_encodings["input_ids"].append(padded_input_ids)
                new_encodings["attention_mask"].append(padded_attention_mask)

            for key in new_encodings:
                new_encodings[key] = torch.tensor(
                    new_encodings[key], device=self.model.device
                )

            inputs = new_encodings

        return {key: value.to(self.model.device) for key, value in inputs.items()}

    def _encode(self, inputs: dict[str, torch.Tensor]) -> torch.Tensor:
        # encode
        with torch.no_grad():
            embs = self.model(**inputs)
        if self.normalize:
            embs = embs / embs.norm(dim=-1, keepdim=True)
        return embs

    def _insert_token(
        self,
        output: dict,
        insert_token_id: int,
        insert_position: int = 1,
        token_type_id: int = 0,
        attention_value: int = 1,
    ):
        updated_output = {}
        for key in output:
            updated_tensor_list = []
            for seqs in output[key]:
                if len(seqs.shape) == 1:
                    seqs = seqs.unsqueeze(0)
                for seq in seqs:
                    first_part = seq[:insert_position]
                    second_part = seq[insert_position:]
                    new_element = (
                        torch.tensor([insert_token_id])
                        if key == "input_ids"
                        else torch.tensor([token_type_id])
                    )
                    if key == "attention_mask":
                        new_element = torch.tensor([attention_value])
                    updated_seq = torch.cat(
                        (first_part, new_element, second_part), dim=0
                    )
                    updated_tensor_list.append(updated_seq)
            updated_output[key] = torch.stack(updated_tensor_list)
        return updated_output

    def _query_encode(self, query: list[str]):
        return self._tokenize(query, self.query_token_id, is_query=True)

    def _document_encode(self, documents: list[str]):
        return self._tokenize(documents, self.document_token_id)
