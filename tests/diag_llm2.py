import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import Settings
from src.llm.factory import LLMRouter
from src.llm.prompts import extraction_messages
from src.models.schemas import ExtractedJobFacts


def say(*args):
    print(*args, flush=True)


settings = Settings.load()
router = LLMRouter(settings)
messages = extraction_messages(
    "Senior Backend Engineer",
    "ACME",
    "Requires C#, .NET 8, AWS, SQL Server, microservices. Senior, 7+ years. Salary 120000-150000 USD. Remote US, English required.",
)
schema_hint = {
    "role": "user",
    "content": "Responda APENAS com um objeto JSON valido conforme schema: "
    + json.dumps(ExtractedJobFacts.model_json_schema(), ensure_ascii=False),
}
say("prompt length", sum(len(m["content"]) for m in [*messages, schema_hint]))
t = time.time()
say("calling router.chat json_mode=False")
answer = router.chat([*messages, schema_hint], json_mode=False)
say("secs", round(time.time() - t, 1), "len", len(answer))
say(answer[:500])
