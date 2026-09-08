"""Optional LLM assist. Nothing in the scraping path imports this package."""

from uparse.assist.generate import Generated, generate
from uparse.assist.protocol import AssistError

__all__ = ["AssistError", "Generated", "generate"]
