#!/usr/bin/env python3
"""
DeepSeek v4 Flash + Graphify MCP Agent
Recursively explores codebase knowledge graph to build deep understanding.
"""

import asyncio
import json
import os
import sys
import re
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

# DeepSeek client
DEEPSEEK_API_KEY = open(os.path.expanduser("~/api_keys/deepseek_api.txt")).read().strip()
deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1")

# MCP server configs
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
}


class GraphifyAgent:
    def __init__(self):
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, Any] = {}
        self.explored_nodes: set[str] = set()
        self.findings: list[str] = []
        self.stack = AsyncExitStack()

    async def connect(self):
        """Connect to all MCP servers."""
        for name, params in MCP_SERVERS.items():
            try:
                read, write = await self.stack.enter_async_context(stdio_client(params))
                session = await self.stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.sessions[name] = session
                tools_result = await session.list_tools()
                self.tools[name] = {t.name: t for t in tools_result.tools}
                print(f"✅ Connected to {name} ({len(tools_result.tools)} tools)")
            except Exception as e:
                print(f"❌ Failed to connect to {name}: {e}")

    async def call_tool(self, server: str, tool_name: str, arguments: dict) -> str:
        """Call an MCP tool and return the result text."""
        if server not in self.sessions:
            return f"Error: {server} not connected"
        session = self.sessions[server]
        result = await session.call_tool(tool_name, arguments)
        texts = [c.text for c in result.content if hasattr(c, "text")]
        return "\n".join(texts) if texts else str(result.content)

    async def ask_deepseek(self, system: str, user: str, max_tokens: int = 4000) -> str:
        """Call DeepSeek v4 flash."""
        resp = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        return resp.choices[0].message.content or ""

    def extract_node_names(self, god_nodes_text: str) -> list[str]:
        """Extract actual node names from god_nodes output."""
        lines = god_nodes_text.split("\n")
        nodes = []
        for line in lines:
            # Match patterns like "1. Escalation - 60 edges" or "  1. Escalation - 60 edges"
            m = re.match(r"\s*\d+\.\s+(.+?)\s+-\s+\d+\s+edges", line)
            if m:
                nodes.append(m.group(1).strip())
        return nodes

    async def explore_god_node(self, node_label: str, depth: int = 2):
        """Recursively explore a god node."""
        if node_label in self.explored_nodes or not node_label:
            return
        self.explored_nodes.add(node_label)
        print(f"\n🔍 Exploring: {node_label}")

        node_info = await self.call_tool("graphify", "get_node", {"label": node_label})
        neighbors = await self.call_tool("graphify", "get_neighbors", {"label": node_label})
        
        # Also try filesystem read if source file is known
        source_file = None
        for line in node_info.split("\n"):
            if "src=" in line.lower() or "source=" in line.lower():
                m = re.search(r"src=([^\s]+)", line)
                if m:
                    source_file = m.group(1).strip()
                    break
        
        source_snippet = ""
        if source_file and "filesystem" in self.sessions:
            try:
                read_result = await self.call_tool("filesystem", "read_file", {"path": source_file})
                source_snippet = read_result[:2000]
            except Exception as e:
                source_snippet = f"(Could not read: {e})"

        prompt = f"""Analyze this code node from a knowledge graph:

NODE: {node_label}

NODE INFO:
{node_info[:3000]}

NEIGHBORS:
{neighbors[:3000]}

SOURCE CODE SNIPPET:
{source_snippet[:2000]}

Answer concisely:
1. What is this node? (function, class, module, type?)
2. What does it do in the codebase?
3. Is it over-connected or doing too much?
4. Which neighbors are most important?
5. Should it be refactored, and how?"""
        
        analysis = await self.ask_deepseek(
            "You are a senior software architect analyzing code structure.",
            prompt,
            max_tokens=2000,
        )
        
        self.findings.append(f"\n## {node_label}\n{analysis}\n")
        print(f"  📝 {analysis[:150]}...")

        # Explore top neighbors
        neighbor_lines = [l for l in neighbors.split("\n") if "-->" in l or "<--" in l][:5]
        for line in neighbor_lines:
            parts = line.replace("-->", "").replace("<--", "").strip().split()
            if parts:
                neighbor = parts[0]
                if neighbor not in self.explored_nodes and depth > 0 and len(neighbor) > 2:
                    await self.explore_god_node(neighbor, depth - 1)

    async def run(self, query: str = None):
        """Main exploration loop."""
        await self.connect()
        
        stats = await self.call_tool("graphify", "graph_stats", {})
        print(f"\n📊 Graph Stats:\n{stats}\n")
        
        gods = await self.call_tool("graphify", "god_nodes", {"top_n": 15})
        print(f"\n👑 God Nodes:\n{gods}\n")
        
        node_names = self.extract_node_names(gods)
        if not node_names:
            node_names = ["Escalation", "ArchitectureMap", "Timeline Guardian", "OtioTimeline", "SyncOtioClient"]
        
        print(f"🎯 Targets: {node_names[:5]}\n")
        
        for node in node_names[:5]:
            await self.explore_god_node(node, depth=2)
        
        # Cross-cutting query
        if query:
            cross = await self.call_tool("graphify", "query_graph", {
                "question": query,
                "mode": "bfs",
                "depth": 3,
                "token_budget": 2000,
            })
            cross_analysis = await self.ask_deepseek(
                "You are analyzing a codebase.",
                f"Based on this graph query result for '{query}', what does it tell us about the architecture?\n\n{cross[:4000]}",
                max_tokens=1500,
            )
            self.findings.append(f"\n## Cross-cutting Query: {query}\n{cross_analysis}\n")
        
        all_findings = "\n".join(self.findings)
        summary = await self.ask_deepseek(
            "You are summarizing a codebase analysis.",
            f"Based on these findings from exploring a knowledge graph, provide a concise summary:\n\n{all_findings[:6000]}\n\nSummarize: What are the core abstractions? What needs refactoring? What's the overall architecture?",
            max_tokens=2000,
        )
        
        report = f"""# Codebase Deep Understanding Report

**Model:** DeepSeek v4 Flash  
**Graph:** {stats.strip().replace(chr(10), ' | ')}  
**Explored nodes:** {len(self.explored_nodes)}  
**Query:** {query or 'general exploration'}  

## Summary

{summary}

## Detailed Findings

{all_findings}
"""
        
        report_path = "/Users/orpington/Documents/economy-documentary-work/graphify-out/DEEP_UNDERSTANDING.md"
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w") as f:
            f.write(report)
        
        print(f"\n✅ Report saved to: {report_path}")
        print(f"   Explored {len(self.explored_nodes)} nodes")
        
        return report

    async def close(self):
        await self.stack.aclose()


async def main():
    query = sys.argv[1] if len(sys.argv) > 1 else None
    agent = GraphifyAgent()
    try:
        await agent.run(query)
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
