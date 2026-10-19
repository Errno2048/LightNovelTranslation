"""Conservative information-state answers; absence is not explicit ignorance."""
from .schemas import PERSPECTIVES


def information_status(records, *, person, proposition, at_anchor=None, branch="main"):
    relevant = [r for r in records if r.get("type") == "PERSPECTIVE" and r.get("person") == person
                and r.get("proposition") == proposition and r.get("branch", "main") == branch
                and (at_anchor is None or r.get("anchor") in {None, at_anchor})]
    if not relevant:
        return {"status": "unknown", "evidence": [], "explanation": "no explicit information-state evidence"}
    attitudes = {r.get("perspective") for r in relevant}
    positive = attitudes & {"belief", "learned"}
    negative = attitudes & {"explicit_unknown", "suspicion"}
    if positive and negative:
        status = "conflicting"
    elif "learned" in attitudes or "belief" in attitudes:
        status = "supported"
    elif "explicit_unknown" in attitudes:
        status = "explicitly_unknown"
    elif "suspicion" in attitudes:
        status = "uncertain_suspicion"
    else:
        status = "unknown"
    return {"status": status, "evidence": [r.get("record_id") for r in relevant], "records": relevant}
