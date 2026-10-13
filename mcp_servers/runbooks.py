"""Runbooks tool server: keyword search over the markdown runbooks in `docs/runbooks/`.

v1 used ChromaDB embeddings. With a handful of short runbooks, keyword scoring finds the right
one just as well, needs no extra service or API key, and fits the 8 GB laptop budget.
"""

import re
from collections import Counter
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from mcp_servers.common import ToolError, cap, to_result
from sentinelops.config import Settings

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WORD = re.compile(r"[a-z0-9_]+")
STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "for", "on", "with", "it"}


def tokens(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOPWORDS]


class RunbookLibrary:
    """Loads runbooks from a directory and ranks them by keyword overlap."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def all(self) -> dict[str, str]:
        if not self.directory.is_dir():
            raise ToolError(f"runbooks directory not found at {self.directory}")
        return {p.stem: p.read_text() for p in sorted(self.directory.glob("*.md"))}

    def search(self, query: str, limit: int = 3) -> list[dict[str, object]]:
        """Score = term matches in the body, plus triple weight for matches in the title."""
        terms = set(tokens(query))
        if not terms:
            raise ToolError("query has no searchable words")
        results = []
        for name, text in self.all().items():
            title = text.splitlines()[0] if text else name
            body_counts = Counter(tokens(text))
            title_terms = set(tokens(title)) | set(tokens(name))
            score = sum(body_counts[t] for t in terms) + 3 * len(terms & title_terms)
            if score:
                results.append({"name": name, "title": title.lstrip("# "), "score": score})
        return sorted(results, key=lambda r: -int(r["score"]))[:limit]

    def get(self, name: str) -> str:
        runbooks = self.all()
        if name not in runbooks:
            raise ToolError(f"no runbook named {name!r}; available: {sorted(runbooks)}")
        return runbooks[name]


def build_server(library: RunbookLibrary) -> MCPServer:
    """The runbooks MCP server, backed by `library`."""
    server = MCPServer("runbooks", instructions="Operational runbooks for the demo services.")

    @server.tool(
        description="Search runbooks by keywords (symptoms, alert names). Returns the best 3.",
        annotations=READ_ONLY,
    )
    def search_runbooks(query: str) -> str:
        return to_result({"query": query, "results": library.search(query)})

    @server.tool(description="Read one runbook in full, by name.", annotations=READ_ONLY)
    def read_runbook(name: str) -> str:
        return cap(library.get(name))

    return server


if __name__ == "__main__":
    build_server(RunbookLibrary(Settings().runbooks_dir)).run()
