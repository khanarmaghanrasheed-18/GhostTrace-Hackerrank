"""Optional, measured Gemini interpretation. No response is treated as evidence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class GeminiInterpreter:
    def __init__(self, root: Path, model: str | None = None):
        self.key = os.environ.get("GEMINI_API_KEY", "")
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self.max_calls = max(0, int(os.environ.get("GEMINI_MAX_CALLS", "20")))
        self.cache_dir = root / ".cache" / "ghosttrace" / "gemini"
        self.calls = 0
        self.cache_hits = 0
        self.retries = 0
        self.failures: list[str] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.disabled = False

    @property
    def ready(self) -> bool:
        return bool(self.key) and not self.disabled

    def _call(self, prompt: str, allowed: set[str]) -> str | None:
        """One decision chosen from `allowed`, or None."""
        parsed = self._call_json(prompt)
        answer = parsed.get("decision") if isinstance(parsed, dict) else None
        if parsed is not None and answer not in allowed:
            self.failures.append("Gemini returned a decision outside the allowed values")
            return None
        return answer

    def _call_json(self, prompt: str) -> dict[str, Any] | None:
        """One JSON object from the model, cached by prompt. The caller validates it."""
        if not self.ready:
            return None
        digest = hashlib.sha256((self.model + "\n" + prompt).encode()).hexdigest()
        cache_file = self.cache_dir / f"{digest}.json"
        if cache_file.is_file():
            value = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(value.get("response"), dict):
                self.cache_hits += 1
                return value["response"]
        if self.calls >= self.max_calls:
            if "Gemini call budget exhausted" not in self.failures:
                self.failures.append("Gemini call budget exhausted")
            return None
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                           "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}).encode()
        request = Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "x-goog-api-key": self.key,
        })
        for attempt in range(2):
            try:
                self.calls += 1
                with urlopen(request, timeout=20) as response:
                    result: dict[str, Any] = json.load(response)
                usage = result.get("usageMetadata", {})
                self.input_tokens += int(usage.get("promptTokenCount") or 0)
                self.output_tokens += int(usage.get("candidatesTokenCount") or 0)
                raw = result["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    self.failures.append("Gemini returned JSON that is not an object")
                    return None
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps({"response": parsed, "model": self.model}), encoding="utf-8")
                return parsed
            except HTTPError as exc:
                self.failures.append(f"Gemini HTTP {exc.code}")
                if exc.code == 429 and attempt == 0:
                    self.retries += 1
                    time.sleep(2)
                    continue
                self.disabled = True
                return None
            except (URLError, TimeoutError, ValueError, KeyError, IndexError) as exc:
                self.failures.append(f"Gemini {type(exc).__name__}: {exc}")
                self.disabled = True
                return None
        return None

    def deletion_intent(self, request_text: str) -> bool | None:
        prompt = (
            "Decide whether this customer message authorizes deletion of their account or personal data. "
            "Access, correction, export, and marketing opt-out alone do not authorize deletion. "
            "Return JSON with exactly one key, decision, whose value is DELETE or OTHER.\n"
            f"Message: {request_text}"
        )
        answer = self._call(prompt, {"DELETE", "OTHER"})
        return None if answer is None else answer == "DELETE"

    def interpret_excerpt(self, excerpt: str) -> str | None:
        """Return a validated review hint, never an independent evidence finding."""
        prompt = (
            "Classify only the observed state explicitly shown by this extracted business-record excerpt. "
            "A failed request, an operation SUCCESS message, or an unclear OCR fragment is UNKNOWN. "
            "Return JSON with exactly one key, decision, chosen from PRESENT, ABSENT, "
            "EXPOSED, REDACTED, ANONYMIZED, ACTIVE, DISABLED, BLOCKED, UNKNOWN.\n"
            f"Excerpt: {excerpt[:1500]}"
        )
        return self._call(prompt, {"PRESENT", "ABSENT", "EXPOSED", "REDACTED", "ANONYMIZED",
                                   "ACTIVE", "DISABLED", "BLOCKED", "UNKNOWN"})

    def propose_actions(self, question: str, resources: list[dict[str, str]],
                        actions: list[str]) -> dict[str, Any] | None:
        """Translate a what-if question into proposed actions on this case's records.

        The answer is only a proposal: the what-if code checks every action and
        resource ID, and asks for clarification when the model is unsure.
        """
        listing = "\n".join(f"- {r['resource_id']} ({r['system_id']}, {r['category']})" for r in resources)
        prompt = (
            "You translate a question about a hypothetical data-deletion cleanup into proposed actions. "
            f"Allowed actions: {', '.join(actions)}. Use only resource IDs from this case:\n{listing}\n"
            "Return JSON: {\"actions\": [{\"action\": <allowed action>, \"resource_id\": <ID or null>, "
            "\"candidates\": [<IDs the user might mean when unsure>]}]}. "
            "Never invent an ID. If the question does not say which record, set resource_id to null "
            "and list the candidates.\n"
            f"Question: {question}"
        )
        return self._call_json(prompt)

    def reword(self, explanation: str) -> str | None:
        """Optional plain-language wording of an explanation the engine already verified."""
        prompt = ("Rewrite this verified audit explanation in plain language for a non-specialist. "
                  "Do not add, remove or change any fact, ID or conclusion. "
                  "Return JSON with exactly one key, text.\n" + explanation)
        parsed = self._call_json(prompt)
        text = parsed.get("text") if isinstance(parsed, dict) else None
        return text if isinstance(text, str) and text.strip() else None

    def usage(self) -> dict[str, Any]:
        return {"model": self.model if self.calls else None, "calls": self.calls,
                "max_calls": self.max_calls,
                "cache_hits": self.cache_hits, "retries": self.retries,
                "failures": self.failures, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens}
