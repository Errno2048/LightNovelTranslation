"""Dialogue candidate access preserving channel and ambiguity."""
from .schemas import P6Error


def dialogue_candidates(records, *, chapter_id=None, include_unreviewed=True):
    result = []
    for record in records:
        if record.get("type") != "UTTERANCE":
            continue
        if chapter_id and record.get("chapter_id") not in {None, chapter_id}:
            continue
        if not include_unreviewed and record.get("review_status") != "reviewed":
            continue
        # Never collapse multiple candidates to the first one.
        result.append({"record_id": record["record_id"], "quote": record["quote"],
            "channel": record.get("channel", "unknown"), "speaker_candidates": list(record.get("speaker_candidates", [])),
            "addressee_candidates": list(record.get("addressee_candidates", [])),
            "coreference_candidates": list(record.get("coreference_candidates", [])),
            "review_status": record.get("review_status", "candidate"), "evidence_ref": record["evidence_ref"]})
    return result
