"""Local LLM via mlx-lm, with a reusable KV cache for the (long, rarely changing) system prompt.

All MLX calls must happen on the single MLX worker thread owned by the Assistant.
"""

from __future__ import annotations

import copy
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    cached_tokens: int = 0
    gen_tokens: int = 0
    seconds: float = 0.0


class BaseLLM:
    status = "loading"
    model_id = "?"

    def load(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def chat(self, system: str, user: str, max_tokens: int = 512, stop_on_json: bool = True,
             assistant_prefix: str = "") -> LLMResult:  # pragma: no cover - interface
        raise NotImplementedError


class MlxLLM(BaseLLM):
    PREFIX_CACHE_SLOTS = 4  # gate prompt, plan prompt, plus a little churn when the catalog changes

    def __init__(self, model_id: str, wire_max_gb: float = 10.0):
        self.model_id = model_id
        self.wire_max_gb = wire_max_gb
        self.model = None
        self.tokenizer = None
        self.status = "loading"
        self._prefix_cache: OrderedDict[str, tuple[list[int], list]] = OrderedDict()

    def load(self) -> None:
        from mlx_lm import load

        t0 = time.time()
        try:
            self.model, self.tokenizer = load(self.model_id)
        except Exception:
            self.status = "error"
            raise
        log.info("LLM %s loaded in %.1fs", self.model_id, time.time() - t0)
        self._keep_wired(self.model, self.wire_max_gb)
        t0 = time.time()
        self.chat("You are a helpful assistant. Reply with JSON.", 'Say {"ok":true}', max_tokens=8)
        log.info("LLM warm-up %.1fs", time.time() - t0)
        self.status = "ready"

    def unload(self) -> None:
        """Free the weights so LUNA gets the memory back (the memory guard calls this)."""
        import gc

        import mlx.core as mx

        self.status = "paused"
        self.model = None
        self._prefix_cache.clear()
        gc.collect()
        try:
            mx.set_wired_limit(0)
            mx.clear_cache()
        except Exception as e:
            log.warning("could not release MLX memory: %s", e)
        log.info("LLM unloaded")

    @staticmethod
    def _keep_wired(model, max_gb: float) -> None:
        """Keep small models' weights wired for the life of the process. mlx-lm only wires memory while
        stream_generate runs; between requests (and during our own prefill) macOS may then
        compress/swap the 20 GB of weights, which turned 2-3 s plans into 10-20 s ones.
        The limit is the weights plus 2 GB of headroom, not Metal's whole recommended working
        set, so the KV cache can never pin memory LUNA and its plugins need."""
        import mlx.core as mx
        from mlx.utils import tree_flatten

        try:
            weights = sum(v.nbytes for _, v in tree_flatten(model.parameters()))
            if weights > max_gb * 1024**3:
                # A big model locked in RAM starved LUNA and macOS paused it. Leave big
                # models swappable; the memory guard unloads them under pressure anyway.
                log.info("not wiring %.1f GB of weights (limit %.0f GB)", weights / 1e9, max_gb)
                return
            limit = min(mx.device_info()["max_recommended_working_set_size"], weights + 2 * 1024**3)
            mx.set_wired_limit(limit)
            log.info("MLX wired limit set to %.1f GB", limit / 1e9)
        except Exception as e:  # older MLX / not Metal
            log.warning("could not set MLX wired limit: %s", e)

    # ---- prompt building ---------------------------------------------------------------
    def _template(self, messages: list[dict], add_generation_prompt: bool) -> str:
        kwargs = dict(tokenize=False, add_generation_prompt=add_generation_prompt)
        try:
            # Qwen3.x hybrid models: no <think> phase, we want the JSON straight away.
            return self.tokenizer.apply_chat_template(messages, enable_thinking=False, **kwargs)
        except TypeError:
            return self.tokenizer.apply_chat_template(messages, **kwargs)

    _SENTINEL = "\u2063CPSPLIT\u2063"

    def _system_prefix(self, system: str) -> str:
        """Rendered text up to the end of the system turn. Some templates (Qwen3.5+) refuse a
        system-only conversation, so render with a sentinel user turn and cut before it."""
        text = self._template([{"role": "system", "content": system}, {"role": "user", "content": self._SENTINEL}],
                              add_generation_prompt=False)
        head = text[: text.find(self._SENTINEL)]
        # drop the user-turn header line (e.g. "<|im_start|>user\n")
        body = head.rstrip("\n")
        cut = body.rfind("\n") + 1
        return head[:cut] if cut > 0 else ""

    def _encode(self, text: str) -> list[int]:
        return self.tokenizer.encode(text, add_special_tokens=False)

    def _prefill(self, tokens: list[int]):
        import mlx.core as mx
        from mlx_lm.models.cache import make_prompt_cache

        cache = make_prompt_cache(self.model)
        arr = mx.array(tokens)
        step = 2048
        for i in range(0, len(tokens), step):
            self.model(arr[i : i + step][None], cache=cache)
            mx.eval([c.state for c in cache])
        return cache

    def _cache_for(self, prefix_text: str) -> tuple[list[int], list]:
        hit = self._prefix_cache.get(prefix_text)
        if hit is None:
            toks = self._encode(prefix_text)
            hit = (toks, self._prefill(toks))
            self._prefix_cache[prefix_text] = hit
            while len(self._prefix_cache) > self.PREFIX_CACHE_SLOTS:
                self._prefix_cache.popitem(last=False)
        else:
            self._prefix_cache.move_to_end(prefix_text)
        return hit

    def warm(self, system: str) -> None:
        """Prefill (and cache) a system prompt and compile kernels for its shapes."""
        t0 = time.time()
        self.chat(system, "warm-up: reply {}", max_tokens=2)
        log.info("warmed prompt prefix (%d chars) in %.1fs", len(system), time.time() - t0)

    # ---- generation --------------------------------------------------------------------
    def chat(self, system: str, user: str, max_tokens: int = 512, stop_on_json: bool = True,
             assistant_prefix: str = "") -> LLMResult:
        from mlx_lm import stream_generate

        t0 = time.time()
        prefix_text = self._system_prefix(system)
        full_text = self._template([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                   add_generation_prompt=True)
        full_text += assistant_prefix

        if full_text.startswith(prefix_text):
            prefix_toks, base_cache = self._cache_for(prefix_text)
            cache = copy.deepcopy(base_cache)
            rest = self._encode(full_text[len(prefix_text):])
            cached = len(prefix_toks)
        else:  # template does not put the system message first; no reuse
            from mlx_lm.models.cache import make_prompt_cache

            cache = make_prompt_cache(self.model)
            rest = self._encode(full_text)
            cached = 0

        out = []
        depth, in_str, esc, started = 0, False, False, False
        # account for an assistant_prefix like "{"
        for c in assistant_prefix:
            if c == "{":
                depth += 1
                started = True
        n = 0
        for resp in stream_generate(self.model, self.tokenizer, rest, max_tokens=max_tokens, prompt_cache=cache):
            n += 1
            out.append(resp.text)
            if stop_on_json:
                done = False
                for c in resp.text:
                    if in_str:
                        if esc:
                            esc = False
                        elif c == "\\":
                            esc = True
                        elif c == '"':
                            in_str = False
                    elif c == '"':
                        in_str = True
                    elif c == "{":
                        depth += 1
                        started = True
                    elif c == "}":
                        depth -= 1
                        if started and depth == 0:
                            done = True
                if done:
                    break
        return LLMResult(
            text=assistant_prefix + "".join(out),
            prompt_tokens=cached + len(rest),
            cached_tokens=cached,
            gen_tokens=n,
            seconds=time.time() - t0,
        )
