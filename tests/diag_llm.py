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


say("1 load settings")
settings = Settings.load()
say("2 build router")
router = LLMRouter(settings)
say("3 messages")
messages = extraction_messages(
    "Senior Backend Engineer",
    "ACME",
    "Requires C#, .NET 8, AWS, SQL Server, microservices. Senior, 7+ years. Salary 120000-150000 USD. Remote US, English required.",
)
say("4 schema", len(ExtractedJobFacts.model_json_schema()))
t = time.time()
say("5 calling chat (no json)")
answer = router.chat([{"role": "user", "content": "Responda apenas: OK"}])
say("6 chat secs", round(time.time() - t, 1), repr(answer[:40]))
t = time.time()
say("7 calling structured")
result = router.structured(messages, ExtractedJobFacts)
say("8 structured secs", round(time.time() - t, 1))
say(result.model_dump_json()[:500])
