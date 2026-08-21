from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path

from fiscal_rag.assistant import FiscalAssistant
from fiscal_rag.config import get_settings
from fiscal_rag.retrieval import LocalRetrievalIndex


def main() -> None:
    base_settings = get_settings()
    if not base_settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured.")

    with tempfile.TemporaryDirectory(prefix="raf_fallback_smoke_") as temporary_name:
        temporary = Path(temporary_name)
        extracted = temporary / "synthetic.md"
        extracted.write_text(
            "## Page 1\n\n"
            "Synthetic test rule: the demonstration levy rate is exactly 7 percent. "
            "This sentence is fabricated exclusively for an API smoke test and is not legal advice.",
            encoding="utf-8",
        )
        settings = replace(base_settings, data_dir=temporary)
        record = {
            "id": "f" * 20,
            "title": "Synthetic Fiscal Test",
            "source": "synthetic",
            "source_label": "Synthetic Test Source",
            "category": "test",
            "published_date": "",
            "source_url": "",
            "sha256": "synthetic-test-v1",
            "extracted_path": str(extracted),
        }
        LocalRetrievalIndex(settings.local_index_path).index_document(record)
        result = FiscalAssistant(settings).ask(
            "What is the demonstration levy rate?",
            ["synthetic"],
        )
        print(
            json.dumps(
                {
                    "success": "7" in result["answer"],
                    "provider": result["provider"],
                    "model": result["model"],
                    "fallback_used": result.get("fallback_used", False),
                    "retrieved_chunks": result["retrieved_chunks"],
                    "citations": len(result["citations"]),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
