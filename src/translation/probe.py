"""Small synthetic provider capability checks, with explicit dimension correction."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .config import Settings
from .providers import HTTPProvider, ProviderError, require_json
from .schemas import GenerationRequest
from .storage import write_json


def probe(settings: Settings, output: Path) -> dict:
    events = []
    report = {"configuration": settings.public(), "checks": {}, "cache_metrics": "unverified",
              "native_schema": "unverified", "context_limit": "unverified", "events": events}
    output.mkdir(parents=True, exist_ok=True)
    gen = HTTPProvider(settings.generation, max_requests=4, recorder=events.append)
    try:
        result = gen.generate(GenerationRequest([
            {"role": "system", "content": 'Return only a JSON object with a string field "answer".'},
            {"role": "user", "content": 'Translate Japanese こんにちは into Chinese. Return {"answer":"..."}.'}], max_tokens=256, task="probe"))
        value = require_json(result.text, {"answer": str})
        report["checks"]["generation"] = {"status": "passed", "result": value, "model": result.model,
            "finish_reason": result.finish_reason, "usage": result.usage, "elapsed_seconds": result.elapsed_seconds,
            "structured_strategy": "JSON instruction plus local validation; native schema unverified"}
    except ProviderError as exc:
        report["checks"]["generation"] = {"status": "failed", "error": str(exc)}
    write_json(output / "provider_capabilities.json", report)
    dimensions = [settings.embedding.dimension]
    if settings.embedding.dimension != 1024:
        dimensions.append(1024)
    for dimension in dimensions:
        provider = HTTPProvider(replace(settings.embedding, dimension=dimension), max_requests=3, recorder=events.append)
        try:
            result = provider.embed(["少女は剣を手にした。", "少女拿起了剑。"])
            report["checks"][f"embedding_{dimension}"] = {"status": "passed", "count": len(result["vectors"]),
                "dimension": dimension, "usage": result["usage"], "model": result["model"]}
            report["effective_embedding_dimension"] = dimension
            if dimension != settings.embedding.dimension:
                from .storage import atomic_text
                atomic_text(output / "verified-models.toml", f"[embedding]\ndimension = {dimension}\n")
                report["dimension_correction"] = {"from": settings.embedding.dimension, "to": dimension,
                    "config": str(output / "verified-models.toml")}
            break
        except ProviderError as exc:
            report["checks"][f"embedding_{dimension}"] = {"status": "failed", "error": str(exc)}
            # Only invalid parameters justify probing a documented alternate dimension.
            if exc.kind != "parameters":
                break
        finally:
            write_json(output / "provider_capabilities.json", report)
    write_json(output / "provider_capabilities.json", report)
    return report
