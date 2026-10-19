"""Local text counting, with an explicitly labelled fallback and chat allowance."""
from pathlib import Path

from ..storage import file_digest


class TokenCounter:
    def __init__(self, tokenizer_path: Path | None = None):
        self.tokenizer = None
        if tokenizer_path is not None:
            from tokenizers import Tokenizer
            self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
            self.identity = {"method": "published_tokenizer_local_text_plus_chat_allowance",
                             "sha256": file_digest(tokenizer_path), "chat_overhead_estimated": True}
        else:
            self.identity = {"method": "utf8_bytes_conservative_estimate", "chat_overhead_estimated": True}

    def count(self, text):
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids) if self.tokenizer else len(text.encode("utf-8"))

    def messages(self, messages):
        return 32 + sum(self.count(m["content"]) + 32 for m in messages)
