"""Native Ollama chat client with an OpenAI-shaped façade.

Why: Ollama's OpenAI-compatible `/v1` endpoint silently drops `think` (and `options`), so
thinking models (gemma4, qwen3.x) burn thousands of hidden reasoning tokens per turn — measured
~140 s/turn on an A100 vs ~4 s with thinking off. The native `/api/chat` honours a top-level
`think` flag and `options.num_ctx`. This client speaks `/api/chat` but exposes exactly the
subset of the OpenAI SDK surface `MultiAgentArchitect._loop` uses:

    client.chat.completions.create(model=, messages=, tools=, tool_choice=, temperature=, extra_body=)
      -> .choices[0].message  (.content, .tool_calls[i].id / .function.name / .function.arguments,
                                .model_dump(exclude_none=True))
      -> .usage.prompt_tokens / .completion_tokens

Messages are kept in OpenAI format inside the loop and translated on the way out: assistant
tool-call arguments become dicts (Ollama wants objects), tool results carry `tool_name`.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

import httpx


class _Function:
    def __init__(self, name: str, arguments: str):
        self.name, self.arguments = name, arguments


class _ToolCall:
    def __init__(self, id: str, name: str, arguments: Dict[str, Any]):
        self.id, self.type = id, "function"
        self.function = _Function(name, json.dumps(arguments))

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "type": "function",
                "function": {"name": self.function.name, "arguments": self.function.arguments}}


class _Message:
    def __init__(self, content: Optional[str], tool_calls: List[_ToolCall], thinking: Optional[str]):
        self.role = "assistant"
        self.content = content or None
        self.tool_calls = tool_calls or None
        self.thinking = thinking or None

    def model_dump(self, exclude_none: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [tc.as_dict() for tc in self.tool_calls]
        if exclude_none:
            d = {k: v for k, v in d.items() if v is not None}
        return d


class _Usage:
    def __init__(self, p: int, c: int):
        self.prompt_tokens, self.completion_tokens = p, c


class _Choice:
    def __init__(self, message: _Message, finish_reason: str):
        self.message, self.finish_reason = message, finish_reason


class _Response:
    def __init__(self, message: _Message, usage: _Usage, finish_reason: str, model: str):
        self.choices, self.usage, self.model = [_Choice(message, finish_reason)], usage, model


class _Completions:
    def __init__(self, client: "OllamaChatClient"):
        self._c = client

    def create(self, model: str, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
               tool_choice: Any = None, temperature: float = 0, extra_body: Optional[Dict[str, Any]] = None,
               max_tokens: Optional[int] = None, **_: Any) -> _Response:
        return self._c._chat(model, messages, tools, temperature, extra_body, max_tokens)


class _Chat:
    def __init__(self, client: "OllamaChatClient"):
        self.completions = _Completions(client)


class OllamaChatClient:
    """`OpenAI`-shaped client over Ollama's native /api/chat."""

    def __init__(self, host: str, timeout: float = 600, think: Optional[bool] = None,
                 num_ctx: int = 0, keep_alive: str = "2h"):
        self.host = host.rstrip("/")
        if self.host.endswith("/v1"):
            self.host = self.host[:-3]
        self.think, self.num_ctx, self.keep_alive = think, num_ctx, keep_alive
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, connect=30))
        self._id2name: Dict[str, str] = {}
        self.chat = _Chat(self)

    # ---- message translation (OpenAI format -> Ollama native) ----
    def _translate(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                tcs = []
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args or "{}")
                        except json.JSONDecodeError:
                            args = {}
                    tcs.append({"function": {"name": fn.get("name"), "arguments": args}})
                out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": tcs})
            elif role == "tool":
                d = {"role": "tool", "content": m.get("content") or ""}
                name = self._id2name.get(m.get("tool_call_id", ""))
                if name:
                    d["tool_name"] = name
                out.append(d)
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    def _chat(self, model, messages, tools, temperature, extra_body, max_tokens) -> _Response:
        options: Dict[str, Any] = {"temperature": temperature, "top_p": 1.0, "top_k": 0,
                                   "repeat_penalty": 1.0}
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        if max_tokens:
            options["num_predict"] = max_tokens
        payload: Dict[str, Any] = {"model": model, "messages": self._translate(messages),
                                   "stream": False, "options": options, "keep_alive": self.keep_alive}
        if tools:
            payload["tools"] = tools
        think = self.think
        if extra_body and "think" in extra_body:
            think = extra_body["think"]
        if think is not None:
            payload["think"] = think   # top-level ChatRequest field (NOT an options key)
        r = self._http.post(f"{self.host}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
        msg = data.get("message") or {}
        tcs: List[_ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    args = {}
            tid = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
            self._id2name[tid] = fn.get("name", "")
            tcs.append(_ToolCall(tid, fn.get("name", ""), args or {}))
        message = _Message(msg.get("content"), tcs, msg.get("thinking"))
        usage = _Usage(int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0))
        finish = "tool_calls" if tcs else (data.get("done_reason") or "stop")
        return _Response(message, usage, finish, data.get("model", model))

    # ---- extras used by the CLI ----
    def list_models(self) -> List[str]:
        r = self._http.get(f"{self.host}/api/tags")
        r.raise_for_status()
        return sorted(m["name"] for m in r.json().get("models", []))
