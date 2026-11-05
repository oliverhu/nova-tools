"""Shared finish-outcome checks for current clients and older HTTP callers."""

FINISH_OUTCOMES = ("completed", "blocked", "failed", "pending_external_approval")


def finish_outcome(receipt, requested=None):
    if not isinstance(receipt, dict):
        raise ValueError("Receipt must be an object")
    declared = receipt.get("outcome")
    if "outcome" in receipt and declared not in FINISH_OUTCOMES:
        raise ValueError("Invalid receipt outcome")
    outcome = requested if requested is not None else declared or "completed"
    if outcome not in FINISH_OUTCOMES:
        raise ValueError("Invalid finish outcome")
    if declared is not None and declared != outcome:
        raise ValueError("Finish outcome contradicts the explicit receipt outcome")
    if outcome == "completed" and receipt.get("acceptance_complete") is False:
        raise ValueError("Cannot complete a task whose receipt says acceptance_complete=false")
    return outcome
