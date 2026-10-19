"""Character targets are guidance; request/context token budgets stay hard."""


def summary_length_info(text, target_chars=2500):
    if text is None:
        return None
    actual=len(text)
    return {'policy':'soft-target-v1','target_chars':target_chars,'actual_chars':actual,
            'target_exceeded':actual>target_chars,
            'warnings':['summary_target_exceeded'] if actual>target_chars else []}
