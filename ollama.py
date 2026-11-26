"""LLM client: DeepSeek API (OpenAI format, replaced Ollama Cloud 2026-11-06). One function, waits out rate limits instead
of failing. ponytail: module keeps its old name so team.py/chart.py/tests need no change."""
import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal

HOST = os.environ.get("DEEPSEEK_HOST", "https://api.deepseek.com")
# DeepSeek allows hundreds of concurrent requests; 3 keeps the pacing the team was built with.
_slots = threading.Semaphore(3)


def numbers(text):
    """Every number in text, sign and thousands commas dropped: "+1,705 ไม้ ลง 12.50%" -> {1705, 12.5}.
    Markdown list numbering ("1. ...") is layout, not a claim."""
    text = re.sub(r"(?m)^\s*\d+[.)]\s", "", text)
    return {Decimal(n.replace(",", "")) for n in re.findall(r"\d+(?:[.,]\d+)*", text)}


def ungrounded(text, source):
    """Numbers the model wrote that appear nowhere in the code-made source: guessed, rounded or recomputed.
    The team only shows AI text whose every number is copied from ClickHouse-derived input (real money)."""
    return sorted(str(n) for n in numbers(text) - numbers(source))


def fit(v, s):
    """Coerce a model's JSON into the schema. Cloud models treat `format` as a hint: they return the bare array
    instead of {"items": [...]}, a dict where an int belongs, enums in other casing. Unknown enum -> first value."""
    t = s.get("type")
    if t == "object":
        props = s.get("properties", {})
        if isinstance(v, list) and len(props) == 1:
            v = {next(iter(props)): v}
        v = v if isinstance(v, dict) else {}
        return {k: fit(v.get(k), ps) for k, ps in props.items()}
    if t == "array":
        v = v if isinstance(v, list) else [] if v is None else [v]
        return [fit(x, s["items"]) for x in v]
    if t == "integer":
        if isinstance(v, dict):  # e.g. {"FOO": -2, "BAR": -1} -> the worst per-coin value
            v = min((x for x in v.values() if isinstance(x, (int, float))), default=0)
        try:
            v = int(round(float(v)))
        except (TypeError, ValueError):
            v = 0
        return max(s.get("minimum", v), min(s.get("maximum", v), v))
    if t == "boolean":
        return v is True or str(v).strip().lower() == "true"
    v = "" if v is None else v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    if "enum" in s and v not in s["enum"]:
        v = v.strip().lower() if v.strip().lower() in s["enum"] else s["enum"][0]
    return v


def parse_json(text):
    """Cloud models wrap JSON in ```json fences even with `format` set."""
    return json.loads(re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text))


def chat(model, system, user, schema=None, image_path=None, max_wait_s=1800, think=None):
    """Return schema-shaped JSON when schema is given, else text. Retries 429/5xx/timeouts with backoff.
    think=False turns off reasoning (a chart read needs none); None = model default (DeepSeek: thinking on)."""
    if schema:
        system += "\n\nตอบเป็น JSON เท่านั้น ตาม JSON Schema นี้ (object ชั้นนอกตามนี้เป๊ะ):\n" + json.dumps(schema, ensure_ascii=False)
    msg = {"role": "user", "content": user}
    if image_path:  # charts are PNG (chart._shoot)
        with open(image_path, "rb") as f:
            msg["content"] = [{"type": "text", "text": user}, {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(f.read()).decode()}}]
    # temperature is ignored in thinking mode (DeepSeek default); json_object needs the word JSON in the prompt (added above)
    body = {"model": model, "stream": False, "temperature": 0,
            "messages": [{"role": "system", "content": system}, msg]}
    if schema:
        body["response_format"] = {"type": "json_object"}
    if think is not None:
        body["thinking"] = {"type": "enabled" if think else "disabled"}
    req = urllib.request.Request(
        HOST + "/chat/completions", json.dumps(body).encode(),
        {"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"], "Content-Type": "application/json"})
    waited, delay = 0, 15
    while True:
        try:
            with _slots, urllib.request.urlopen(req, timeout=300) as r:
                res = json.load(r)
            content = res["choices"][0]["message"]["content"]   # blank when JSON mode fails -> JSONDecodeError -> retried
            # token counts per call -> real monthly cost: grep "tokens" cache/team.log
            use = res.get("usage") or {}
            logging.getLogger("ollama").info("%s tokens in=%s out=%s", model, use.get("prompt_tokens"), use.get("completion_tokens"))
            return fit(parse_json(content), schema) if schema else content
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise RuntimeError(f"{model}: HTTP {e.code} {e.read()[:300]!r}") from e
            why, retry_after = f"HTTP {e.code}", e.headers.get("Retry-After", "")
            pause = int(retry_after) if retry_after.isdigit() else delay
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            why, pause = type(e).__name__, delay
        logging.getLogger("ollama").warning("%s: %s, waiting %ss (%ss so far)", model, why, pause, waited)
        if waited + pause > max_wait_s:
            raise RuntimeError(f"{model}: gave up after waiting {waited}s for rate limit/errors")
        time.sleep(pause)
        waited += pause
        delay = min(delay * 2, 300)
