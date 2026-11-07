"""Native container check: real Qwen3 inference, JSON schema and RAM unloading."""
import json
import time
import urllib.request

import app

generator = app.Generator({"model": "qwen3:4b-instruct"})
for attempt in range(60):
    # The container starts Ollama immediately before this script.
    try:
        with urllib.request.urlopen(app.OLLAMA + "/api/tags", timeout=2):
            break
    except OSError:
        time.sleep(1)
else:
    raise RuntimeError("Ollama did not become ready")

generator.prepare()
sample = generator.chat([{"role": "user", "content":
    "Повтори дословно: Будильник успешно проверен. Ничего больше не добавляй."}], 0.0)
print("Qwen3 native inference sample:", sample, flush=True)
assert "Будильник успешно проверен" in sample, "Expected requested Russian output"
schema = {"type": "object", "properties": {"choice": {"type": "integer", "const": 1}},
          "required": ["choice"], "additionalProperties": False}
result = generator.chat([{"role": "user", "content": 'Верни JSON: {"choice": 1}'}], 0.2, schema)
assert json.loads(result) == {"choice": 1}, "Structured output unsupported"
for attempt in range(30):
    with urllib.request.urlopen(app.OLLAMA + "/api/ps", timeout=5) as response:
        loaded = json.load(response)["models"]
    if not loaded:
        break
    time.sleep(1)
else:
    raise RuntimeError("Model remained loaded after keep_alive=0")
print("Qwen3 JSON schema OK; no models left loaded in RAM", flush=True)
