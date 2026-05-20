#!/usr/bin/env python3
"""
Graphify → MCP/RAG → DeepSeek V4 Flash Agent (WITH reasoning proxy)

Workflow:
1. Graphify knowledge graph is pre-built (graphify-out/graph.json)
2. Graphify MCP server exposes graph traversal tools
3. DeepSeek V4 Flash performs multi-phase analysis via tool calls
4. reasoning_content is captured and passed back each turn (the proxy)

Key Principle: Expensive one-time graph build → cheap, high-signal, persistent agent intelligence.
"""

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

# DeepSeek V4 Flash client
DEEPSEEK_API_KEY = open(os.path.expanduser("~/api_keys/deepseek_api.txt")).read().strip()
deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1")

# MCP servers
MCP_SERVERS = {
    "graphify": StdioServerParameters(
        command="/Users/orpington/.local/pipx/venvs/graphifyy/bin/python",
        args=["-m", "graphify.serve", "/Users/orpington/Documents/economy-documentary-work/graphify-out/graph.json"],
        env=None,
    ),
    "github": StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")},
    ),
    "filesystem": StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/Users/orpington/Documents/economy-documentary-work"],
        env=None,
    ),
    "tavily": StdioServerParameters(
        command="npx",
        args=["-y", "tavily-mcp"],
        env={"TAVILY_API_KEY": "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"},
    ),
    "sequential_thinking": StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-sequential-thinking"],
        env=None,
    ),
}

# The core system prompt for DeepSeek V4 Flash
SYSTEM_PROMPT = """You are a senior software architect with access to a structured knowledge graph of a codebase via the Graphify MCP.

## YOUR CAPABILITIES
- **Graphify MCP**: Query the knowledge graph (8,044 nodes, 12,851 edges, 469 communities). Tools: query_graph, get_node, get_neighbors, get_community, god_nodes, graph_stats, shortest_path
- **GitHub MCP**: Search repos, read PRs, search code across GitHub
- **Filesystem MCP**: Read actual source files when needed
- **Tavily MCP**: Web search for external context
- **Sequential Thinking**: Break complex problems into steps

## CORE PRINCIPLE
The graph is your PRIMARY MEMORY. Query it for ALL code questions. Never guess about structure — traverse the graph. The graph contains: files, functions, classes, types, relationships (imports, calls, contains, inferred semantics), and communities (modular clusters).

## WORKFLOW
When analyzing the codebase, follow this multi-phase approach:

### Phase 1: Structure
1. Call graph_stats() for corpus overview
2. Call god_nodes(top_n=20) to identify core abstractions
3. For each god node, call get_node() + get_neighbors() to understand its role
4. Identify communities via get_community() — these are your modular boundaries

### Phase 2: Architecture  
5. Trace shortest_path() between key concepts to understand coupling
6. query_graph() for cross-cutting concerns ("authentication", "database", "API")
7. query_graph() for dependency chains ("who calls X?", "what depends on Y?")

### Phase 3: Quality & Debt
8. Identify orphan nodes (degree 0) — dead code candidates
9. Identify god nodes with >50 edges — refactoring targets
10. Find communities with low cohesion — merge/split candidates
11. Look for inferred edges with low confidence — ambiguous relationships

### Phase 4: Security
12. query_graph() for security-sensitive patterns ("auth", "password", "token", "encrypt")
13. Trace data flow via shortest_path() from user input to sensitive operations
14. Check for missing validation gates in the graph

### Phase 5: Opportunities
15. Identify under-connected but high-value nodes — candidates for promotion
16. Find communities that should merge (many inter-community edges)
17. Discover reusable abstractions hiding in the graph structure

## OUTPUT FORMAT
Always structure your analysis as:

```
## Executive Summary
[3-5 bullet points of key findings]

## Phase 1: Structure
[Findings with evidence from graph tools]

## Phase 2: Architecture
[Findings with evidence]

## Phase 3: Quality & Technical Debt
[Findings with evidence]

## Phase 4: Security
[Findings with evidence]

## Phase 5: Opportunities
[Findings with evidence]

## Prioritized Recommendations
[Ranked list: P0 (critical), P1 (important), P2 (nice-to-have)]
```

## RULES
- Every claim must reference a graph node, community, or edge
- Use filesystem reads ONLY to verify graph findings (never to discover structure)
- Use GitHub search for external context (similar patterns, best practices)
- Use web search for technology-specific guidance
- Be concise but evidence-based. No filler.
- If the graph is insufficient, say so — don't hallucinate connections.
"""


class GraphifyDeepAgent:
    def __init__(self):
        self.sessions: dict[str, ClientSession] = {}
        self.tool_specs: dict[str, Any] = {}
        self.stack = AsyncExitStack()
        # Messages for DeepSeek — each assistant message includes reasoning_content
        self.messages: list[dict] = []

    async def connect(self):
        for name, params in MCP_SERVERS.items():
            try:
                read, write = await self.stack.enter_async_context(stdio_client(params))
                session = await self.stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.sessions[name] = session
                tools_result = await session.list_tools()
                self.tool_specs[name] = {t.name: t for t in tools_result.tools}
                print(f"✅ {name}: {len(tools_result.tools)} tools")
            except Exception as e:
                print(f"❌ {name}: {e}")

    def all_tools(self) -> list[dict]:
        tools = []
        for server_name, specs in self.tool_specs.items():
            for tool_name, tool in specs.items():
                tools.append({
                    "type": "function",
                    "function": {
                        "name": f"{server_name}__{tool_name}",
                        "description": f"[{server_name}] {tool.description}",
                        "parameters": tool.inputSchema,
                    }
                })
        return tools

    async def call_tool(self, full_name: str, arguments: dict) -> str:
        if "__" not in full_name:
            return f"Error: invalid tool name {full_name}"
        server, tool_name = full_name.split("__", 1)
        if server not in self.sessions:
            return f"Error: {server} not connected"
        session = self.sessions[server]
        result = await session.call_tool(tool_name, arguments)
        texts = [c.text for c in result.content if hasattr(c, "text")]
        return "\n".join(texts) if texts else str(result.content)

    def _append_assistant(self, content: str, reasoning_content: str, tool_calls: list = None):
        """Append assistant message WITH reasoning_content proxy."""
        msg = {
            "role": "assistant",
            "content": content or "",
            "reasoning_content": reasoning_content or "",
        }
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def _append_tool(self, tool_call_id: str, content: str):
        """Append tool result."""
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })

    async def run_analysis(self, user_query: str):
        await self.connect()
        tools = self.all_tools()
        
        print(f"\n🔧 Total tools: {len(tools)}")
        print(f"🎯 Query: {user_query[:100]}...\n")

        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_query},
        ]

        max_turns = 25
        total_tool_calls = 0

        for turn in range(max_turns):
            resp = deepseek.chat.completions.create(
                model="deepseek-v4-flash",
                messages=self.messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=4000,
                temperature=0.2,
            )
            
            msg = resp.choices[0].message
            content = msg.content or ""
            reasoning = getattr(msg, "reasoning_content", "") or ""
            
            if not msg.tool_calls:
                # Final answer — append and break
                self._append_assistant(content, reasoning)
                break
            
            # Tool calls — append assistant with reasoning + tools
            tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    }
                }
                for tc in msg.tool_calls
            ]
            self._append_assistant(content, reasoning, tool_calls)
            
            # Execute each tool call
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                
                print(f"  🔧 {tool_name}({json.dumps(args)[:80]}...)")
                result = await self.call_tool(tool_name, args)
                total_tool_calls += 1
                if len(result) > 4000:
                    result = result[:4000] + f"\n... [truncated, total: {len(result)} chars]"
                
                self._append_tool(tc.id, result)
        
        # Extract final report
        final_report = ""
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant" and msg.get("content") and not msg.get("tool_calls"):
                final_report = msg["content"]
                break
        
        if not final_report:
            parts = [m.get("content", "") for m in self.messages if m.get("role") == "assistant" and m.get("content")]
            final_report = "\n\n".join(parts)
        
        # Build tool trace
        tool_trace = []
        for msg in self.messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tool_trace.append(f"- {tc['function']['name']}: {tc['function']['arguments'][:100]}")
        
        report = f"""# Graphify → MCP → DeepSeek V4 Flash Analysis Report

**Query:** {user_query}  
**Model:** DeepSeek V4 Flash (thinking enabled)  
**Tools:** {len(tools)}  
**Turns:** {turn + 1}/{max_turns}  
**Tool Calls:** {total_tool_calls}  

## Tool Trace
{chr(10).join(tool_trace[:50])}

---

{final_report}
"""
        
        report_path = "/Users/orpington/Documents/economy-documentary-work/graphify-out/AGENT_ANALYSIS.md"
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w") as f:
            f.write(report)
        
        print(f"\n✅ Report: {report_path}")
        print(f"   Turns: {turn + 1}, Tool calls: {total_tool_calls}")
        return final_report

    async def close(self):
        await self.stack.aclose()


async def main():
    default_query = """Perform a rigorous multi-phase codebase analysis:

Phase 1 - Structure: What are the core abstractions? (god nodes, communities)
Phase 2 - Architecture: How do timeline, agents, and frontend interact? Trace coupling.
Phase 3 - Quality & Debt: Identify dead code, god classes, low-cohesion communities.
Phase 4 - Security: Trace data flow from user input to sensitive operations.
Phase 5 - Opportunities: Find reusable abstractions and refactoring targets.

Output structured report with executive summary and prioritized recommendations."""
    
    query = sys.argv[1] if len(sys.argv) > 1 else default_query
    agent = GraphifyDeepAgent()
    try:
        await agent.run_analysis(query)
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
