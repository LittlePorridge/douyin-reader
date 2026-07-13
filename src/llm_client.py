from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import urllib.request
import urllib.error


class LLMProvider(Protocol):
    name: str
    model: str

    def chat(self, messages: list[dict]) -> str: ...


@dataclass
class OpenAICompatibleProvider:
    name: str
    api_base: str
    api_key: str
    model: str
    temperature: float = 0.3
    max_tokens: int = 8000
    json_mode: bool = False

    def chat(self, messages: list[dict]) -> str:
        url = f"{self.api_base.rstrip('/')}/chat/completions"
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        return _http_post(url, body, self.api_key)


@dataclass
class AnthropicCompatibleProvider:
    """Anthropic /v1/messages 协议（minimax-m3、qwen3.7-max 等走这个）"""
    name: str
    api_base: str
    api_key: str
    model: str
    temperature: float = 0.3
    max_tokens: int = 8000
    json_mode: bool = False

    def chat(self, messages: list[dict]) -> str:
        url = f"{self.api_base.rstrip('/')}/messages"
        # Anthropic 协议：system 是单独字段，messages 只有 user/assistant
        system_content = ""
        conv_messages = []
        for m in messages:
            if m["role"] == "system":
                system_content += m["content"] + "\n"
            else:
                conv_messages.append(m)
        body = {
            "model": self.model,
            "messages": conv_messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if system_content.strip():
            body["system"] = system_content.strip()
        raw = _http_post(url, body, self.api_key, anthropic=True)
        # Anthropic 响应格式：{"content": [{"type": "text", "text": "..."}]}
        # _http_post 已经处理了 OpenAI 格式，这里需要覆盖
        # 但 _http_post 返回的是原始字符串，我们需要在这里解析 Anthropic 格式
        return raw

    def _chat_raw(self, messages: list[dict]) -> str:
        url = f"{self.api_base.rstrip('/')}/messages"
        system_content = ""
        conv_messages = []
        for m in messages:
            if m["role"] == "system":
                system_content += m["content"] + "\n"
            else:
                conv_messages.append(m)
        body = {
            "model": self.model,
            "messages": conv_messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if system_content.strip():
            body["system"] = system_content.strip()
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "x-api-key": self.api_key,
                "Content-Type": "application/json",
                "User-Agent": "douyin-reader/0.1 (macOS) Python-urllib",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                # Anthropic 格式: {"content": [{"type": "text", "text": "..."}]}
                if "content" in payload and isinstance(payload["content"], list):
                    texts = [b["text"] for b in payload["content"] if b.get("type") == "text"]
                    return "\n".join(texts)
                # 有些兼容层返回 OpenAI 格式
                if "choices" in payload:
                    return payload["choices"][0]["message"]["content"]
                raise RuntimeError(f"unknown response format: {str(payload)[:300]}")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM HTTP {e.code}: {err_body}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM URL error: {e}") from None


def _http_post(url: str, body: dict, api_key: str, anthropic: bool = False) -> str:
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "douyin-reader/0.1 (macOS) Python-urllib",
        "Accept": "application/json",
    }
    if anthropic:
        headers["x-api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            if "choices" in payload:
                return payload["choices"][0]["message"]["content"]
            if "content" in payload and isinstance(payload["content"], list):
                texts = [b["text"] for b in payload["content"] if b.get("type") == "text"]
                return "\n".join(texts)
            raise RuntimeError(f"unknown response format: {str(payload)[:300]}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM HTTP {e.code}: {err_body}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM URL error: {e}") from None


def _parse_json_lenient(raw: str) -> dict:
    """容错 JSON 解析：去除 markdown 围栏、找最外层 {} 块"""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3].strip()
    if not s.startswith("{"):
        start = s.find("{")
        if start == -1:
            raise ValueError(f"no JSON object in response: {raw[:200]!r}")
        s = s[start:]
    end = s.rfind("}")
    if end != -1 and end < len(s):
        s = s[: end + 1]
    return json.loads(s)


def load_provider_from_yaml(yaml_path: Path) -> LLMProvider:
    import yaml

    with yaml_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    active = cfg.get("active_provider")
    providers = cfg.get("providers", {})
    if active not in providers:
        raise KeyError(f"active_provider {active!r} not in providers")

    return _build_provider(active, providers[active])


def load_all_providers(yaml_path: Path) -> list[LLMProvider]:
    """加载 yaml 中所有 provider（用于 --all-providers 模式）"""
    import yaml

    with yaml_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    providers_cfg = cfg.get("providers", {})
    result = []
    for name, pcfg in providers_cfg.items():
        try:
            result.append(_build_provider(name, pcfg))
        except RuntimeError as e:
            print(f"[llm] skip provider {name}: {e}")
    return result


def _build_provider(name: str, p: dict) -> LLMProvider:
    api_key = p.get("api_key") or os.environ.get(p.get("api_key_env", ""), "")
    if not api_key:
        raise RuntimeError(
            f"no api_key for provider {name!r}; "
            f"set 'api_key' in yaml or export env var {p.get('api_key_env')}"
        )
    protocol = p.get("protocol", "openai")
    common = dict(
        name=name,
        api_base=p["api_base"],
        api_key=api_key,
        model=p["model"],
        temperature=p.get("temperature", 0.3),
        max_tokens=p.get("max_tokens", 8000),
        json_mode=p.get("json_mode", False),
    )
    if protocol == "anthropic":
        return AnthropicCompatibleProvider(**common)
    return OpenAICompatibleProvider(**common)


if __name__ == "__main__":
    import sys
    from .config import load_config

    cfg = load_config()
    if not cfg.llm_providers_path.exists():
        print(f"missing {cfg.llm_providers_path}; copy from docs/llm_providers.yaml.example")
        sys.exit(1)
    p = load_provider_from_yaml(cfg.llm_providers_path)
    print(f"provider: {p.name} model: {p.model} type: {type(p).__name__}")
    msg = [{"role": "user", "content": "输出一个 JSON：{\"ok\": true, \"n\": 42}，只输出 JSON"}]
    out = p.chat(msg)
    print("raw:", out[:200])
    print("json:", _parse_json_lenient(out))