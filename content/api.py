"""
TrueGraph - Knowledge Graph & Relationship Intelligence Service
================================================================
Takes extracted entities from TrueEngine and builds a knowledge graph
showing how topics, products, places, people, verses, etc. interrelate
across a creator's entire body of content.

API service: POST /build-graph
Input: Extracted entities + engagement data from TrueEngine
Output: Graph JSON (nodes, edges, clusters, actionable insights)

The graph answers: "When you talk about X, what else comes with it,
and what does that combination do to your engagement?"
"""

import os
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

app = FastAPI(title="TrueGraph API", version="0.1.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# API key auth (WinTech canonical: x-api-key header). Opt-in — when
# TRUEGRAPH_API_KEY is unset the service stays open for local dev, so a deploy
# that predates the env var cannot lock out callers. /health stays public.
TRUEGRAPH_API_KEY = os.getenv("TRUEGRAPH_API_KEY", "")

@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if TRUEGRAPH_API_KEY and request.url.path != "/health" and request.method != "OPTIONS":
        if request.headers.get("x-api-key") != TRUEGRAPH_API_KEY:
            return JSONResponse(status_code=401, content={"error": "invalid or missing x-api-key"})
    return await call_next(request)

# OpenRouter for insight generation
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "qwen/qwen-2.5-72b-instruct")

# Win/loss outcome intelligence (ported from the retired CPP-Engine).
# Needs DATABASE_URL; without it these endpoints 503 and graph compute
# is unaffected.
import outcomes as _outcomes
app.include_router(_outcomes.router)

@app.on_event("startup")
def _init_outcomes_schema():
    if os.getenv("DATABASE_URL", ""):
        try:
            _outcomes.init_schema()
        except Exception as e:
            # Outcome layer is additive — never block graph compute on it
            print(f"[outcomes] schema init failed: {e}")


# ── Request Models ──────────────────────────────────────────

class SourceEntities(BaseModel):
    """Entities extracted from a single source (video, sermon, etc.)"""
    source_id: str
    title: str = ''
    published_at: Optional[str] = None
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    topics: List[str] = []
    food: List[str] = []
    products: List[str] = []
    places: List[str] = []
    people: List[str] = []
    verses: List[str] = []
    themes: List[str] = []
    tags: List[str] = []

class BuildGraphRequest(BaseModel):
    collection_id: str
    collection_name: str = ''
    template: str = 'default'  # couple, influencer, church, food, etc.
    sources: List[SourceEntities]
    generate_insights: bool = True  # set False to skip LLM calls (saves cost)


# ── Graph Data Structures ──────────────────────────────────

class GraphNode:
    def __init__(self, id: str, label: str, node_type: str):
        self.id = id
        self.label = label
        self.type = node_type  # topic, food, product, place, person, verse, theme
        self.frequency = 0  # how many sources mention this
        self.total_views = 0
        self.total_likes = 0
        self.total_comments = 0
        self.source_ids = set()

    @property
    def avg_views(self):
        return self.total_views // self.frequency if self.frequency else 0

    def to_dict(self):
        return {
            "id": self.id, "label": self.label, "type": self.type,
            "frequency": self.frequency, "avg_views": self.avg_views,
            "total_views": self.total_views, "total_likes": self.total_likes,
            "total_comments": self.total_comments,
            "source_count": len(self.source_ids),
        }


class GraphEdge:
    def __init__(self, source_id: str, target_id: str):
        self.source = source_id
        self.target = target_id
        self.weight = 0  # co-occurrence count
        self.shared_sources = set()
        self.combined_views = 0
        self.combined_likes = 0
        self.combined_comments = 0

    @property
    def avg_views_together(self):
        return self.combined_views // self.weight if self.weight else 0

    def to_dict(self):
        return {
            "source": self.source, "target": self.target,
            "weight": self.weight,
            "avg_views_together": self.avg_views_together,
            "combined_views": self.combined_views,
            "shared_source_count": len(self.shared_sources),
        }


# ── Core Graph Builder ─────────────────────────────────────

def normalize_entity(text: str) -> str:
    """Normalize entity text for deduplication."""
    text = text.strip().lower()
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text


def build_graph(req: BuildGraphRequest) -> dict:
    """Build the knowledge graph from extracted entities."""
    nodes: Dict[str, GraphNode] = {}
    edges: Dict[str, GraphEdge] = {}

    # Channel-level stats for comparison
    all_views = [s.view_count for s in req.sources if s.view_count > 0]
    channel_avg_views = sum(all_views) // len(all_views) if all_views else 0

    # Phase 1: Build nodes from all sources
    for source in req.sources:
        source_entities = []

        # Collect all entities from this source with their types
        entity_lists = [
            (source.topics, 'topic'),
            (source.food, 'food'),
            (source.products, 'product'),
            (source.places, 'place'),
            (source.people, 'person'),
            (source.verses, 'verse'),
            (source.themes, 'theme'),
            (source.tags, 'tag'),
        ]

        for entity_list, entity_type in entity_lists:
            for raw_entity in entity_list:
                if not raw_entity or len(raw_entity.strip()) < 2:
                    continue
                normalized = normalize_entity(raw_entity)
                if not normalized or len(normalized) < 2:
                    continue

                node_id = f"{entity_type}:{normalized}"

                if node_id not in nodes:
                    # Use original casing for label (first occurrence)
                    nodes[node_id] = GraphNode(node_id, raw_entity.strip(), entity_type)

                node = nodes[node_id]
                node.frequency += 1
                node.total_views += source.view_count
                node.total_likes += source.like_count
                node.total_comments += source.comment_count
                node.source_ids.add(source.source_id)
                source_entities.append(node_id)

        # Phase 2: Build edges from co-occurrence within this source
        unique_entities = list(set(source_entities))
        for a, b in combinations(unique_entities, 2):
            edge_key = tuple(sorted([a, b]))
            edge_id = f"{edge_key[0]}||{edge_key[1]}"

            if edge_id not in edges:
                edges[edge_id] = GraphEdge(edge_key[0], edge_key[1])

            edge = edges[edge_id]
            edge.weight += 1
            edge.shared_sources.add(source.source_id)
            edge.combined_views += source.view_count
            edge.combined_likes += source.like_count
            edge.combined_comments += source.comment_count

    # Phase 3: Detect clusters (connected components with strong edges)
    clusters = detect_clusters(nodes, edges, min_edge_weight=2)

    # Phase 4: Compute graph-level analytics
    analytics = compute_analytics(nodes, edges, clusters, channel_avg_views, len(req.sources))

    return {
        "collection_id": req.collection_id,
        "collection_name": req.collection_name,
        "template": req.template,
        "generated_at": datetime.now().isoformat(),
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "total_clusters": len(clusters),
            "total_sources": len(req.sources),
            "channel_avg_views": channel_avg_views,
        },
        "nodes": [n.to_dict() for n in sorted(nodes.values(), key=lambda x: -x.frequency)],
        "edges": [e.to_dict() for e in sorted(edges.values(), key=lambda x: -x.weight) if e.weight >= 1],
        "clusters": clusters,
        "analytics": analytics,
    }


def detect_clusters(nodes, edges, min_edge_weight=2):
    """Find clusters of strongly connected entities using simple union-find."""
    # Filter to strong edges
    strong_edges = {k: e for k, e in edges.items() if e.weight >= min_edge_weight}

    # Union-Find
    parent = {}
    def find(x):
        if x not in parent:
            parent[x] = x
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for edge in strong_edges.values():
        union(edge.source, edge.target)

    # Group by root
    groups = defaultdict(set)
    for node_id in nodes:
        if node_id in parent:
            groups[find(node_id)].add(node_id)

    # Build cluster objects (only clusters with 3+ nodes are interesting)
    clusters = []
    for root, members in groups.items():
        if len(members) < 3:
            continue

        member_nodes = [nodes[m] for m in members if m in nodes]
        cluster_types = Counter(n.type for n in member_nodes)
        cluster_views = sum(n.total_views for n in member_nodes)
        cluster_freq = sum(n.frequency for n in member_nodes)

        # Find the strongest internal edges
        internal_edges = []
        for eid, edge in strong_edges.items():
            if edge.source in members and edge.target in members:
                internal_edges.append(edge)
        internal_edges.sort(key=lambda e: -e.weight)

        clusters.append({
            "id": f"cluster_{len(clusters)}",
            "size": len(members),
            "nodes": [n.to_dict() for n in sorted(member_nodes, key=lambda x: -x.frequency)],
            "dominant_types": dict(cluster_types.most_common(3)),
            "total_views": cluster_views,
            "total_frequency": cluster_freq,
            "strongest_connections": [
                {"from": e.source, "to": e.target, "weight": e.weight, "avg_views": e.avg_views_together}
                for e in internal_edges[:10]
            ],
        })

    clusters.sort(key=lambda c: -c["total_views"])
    return clusters


def compute_analytics(nodes, edges, clusters, channel_avg, total_sources):
    """Compute actionable analytics from the graph."""
    analytics = {
        "power_combinations": [],
        "orphan_topics": [],
        "engagement_multipliers": [],
        "content_gaps": [],
    }

    # Power combinations: edges where combined avg views > channel avg
    for edge in sorted(edges.values(), key=lambda e: -e.avg_views_together):
        if edge.weight >= 2 and edge.avg_views_together > channel_avg * 1.2:
            source_node = nodes.get(edge.source)
            target_node = nodes.get(edge.target)
            if source_node and target_node:
                analytics["power_combinations"].append({
                    "entity_a": source_node.label,
                    "type_a": source_node.type,
                    "entity_b": target_node.label,
                    "type_b": target_node.type,
                    "times_together": edge.weight,
                    "avg_views_together": edge.avg_views_together,
                    "vs_channel_avg": round(edge.avg_views_together / channel_avg, 2) if channel_avg else 0,
                    "insight": f"When you combine '{source_node.label}' with '{target_node.label}', views average {edge.avg_views_together:,} ({round(edge.avg_views_together / channel_avg * 100) if channel_avg else 0}% of channel avg). You've done this {edge.weight} times.",
                })
    analytics["power_combinations"] = analytics["power_combinations"][:20]

    # Orphan topics: nodes with no strong edges (islands)
    connected_nodes = set()
    for edge in edges.values():
        if edge.weight >= 2:
            connected_nodes.add(edge.source)
            connected_nodes.add(edge.target)

    for node_id, node in nodes.items():
        if node_id not in connected_nodes and node.frequency >= 2 and node.type in ('topic', 'theme'):
            analytics["orphan_topics"].append({
                "entity": node.label,
                "type": node.type,
                "frequency": node.frequency,
                "avg_views": node.avg_views,
                "insight": f"'{node.label}' appears in {node.frequency} videos but never strongly pairs with anything else. Either integrate it with your main themes or consider dropping it.",
            })
    analytics["orphan_topics"] = analytics["orphan_topics"][:10]

    # Engagement multipliers: pairs where views together >> views separately
    for edge in edges.values():
        if edge.weight < 2:
            continue
        source_node = nodes.get(edge.source)
        target_node = nodes.get(edge.target)
        if not source_node or not target_node:
            continue
        avg_separate = (source_node.avg_views + target_node.avg_views) / 2
        if avg_separate > 0 and edge.avg_views_together > avg_separate * 1.3:
            multiplier = round(edge.avg_views_together / avg_separate, 2)
            analytics["engagement_multipliers"].append({
                "entity_a": source_node.label,
                "entity_b": target_node.label,
                "multiplier": multiplier,
                "avg_separate": round(avg_separate),
                "avg_together": edge.avg_views_together,
                "insight": f"'{source_node.label}' + '{target_node.label}' together get {multiplier}x the views they get separately. This is a proven combination.",
            })
    analytics["engagement_multipliers"].sort(key=lambda x: -x["multiplier"])
    analytics["engagement_multipliers"] = analytics["engagement_multipliers"][:15]

    # Content gaps: high-performing nodes that have never been combined
    top_nodes = sorted(nodes.values(), key=lambda n: -n.avg_views)[:20]
    for a, b in combinations(top_nodes, 2):
        edge_key = tuple(sorted([a.id, b.id]))
        edge_id = f"{edge_key[0]}||{edge_key[1]}"
        if edge_id not in edges and a.type != b.type:
            analytics["content_gaps"].append({
                "entity_a": a.label, "type_a": a.type,
                "entity_b": b.label, "type_b": b.type,
                "a_avg_views": a.avg_views, "b_avg_views": b.avg_views,
                "insight": f"'{a.label}' ({a.avg_views:,} avg views) and '{b.label}' ({b.avg_views:,} avg views) both perform well but you've NEVER combined them. Test this.",
            })
    analytics["content_gaps"] = analytics["content_gaps"][:15]

    return analytics


# ── LLM Insight Generation ──────────────────────────────────

async def generate_cluster_insights(graph: dict) -> list:
    """Call LLM to generate actionable insights per cluster."""
    if not OPENROUTER_API_KEY:
        return []

    import httpx

    insights = []
    for cluster in graph.get("clusters", [])[:5]:  # Top 5 clusters only
        node_summary = ", ".join(f"{n['label']} ({n['type']}, {n['frequency']}x)" for n in cluster["nodes"][:10])
        connections = ", ".join(f"{c['from'].split(':')[1]} ↔ {c['to'].split(':')[1]} ({c['weight']}x)" for c in cluster["strongest_connections"][:5])

        prompt = f"""Analyze this content cluster from a creator's video library and provide 2-3 specific, actionable insights.

CLUSTER ({cluster['size']} entities, {cluster['total_views']:,} total views):
Entities: {node_summary}
Strongest connections: {connections}

For each insight, explain:
1. What the pattern means for the creator's content strategy
2. A specific action they should take
3. Why this matters for audience growth or monetization

Return JSON array:
[
  {{
    "insight": "The pattern you see",
    "action": "What to do about it",
    "why": "Why this matters",
    "priority": "high|medium|low"
  }}
]"""

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                    json={"model": ANALYSIS_MODEL, "messages": [
                        {"role": "system", "content": "You are a content strategy analyst. Return ONLY valid JSON."},
                        {"role": "user", "content": prompt}
                    ], "max_tokens": 1500, "temperature": 0.3}
                )
                data = resp.json()
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                # Parse JSON from response
                text = re.sub(r'^```json\s*', '', text.strip())
                text = re.sub(r'\s*```$', '', text)
                parsed = json.loads(text)
                insights.append({"cluster_id": cluster["id"], "insights": parsed})
        except Exception as e:
            print(f"  Insight generation failed for {cluster['id']}: {e}")

    return insights


# ── API Endpoints ───────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "truegraph", "version": "0.1.0"}


@app.post("/build-graph")
async def build_graph_endpoint(req: BuildGraphRequest):
    """Build a knowledge graph from extracted entities.

    Input: Array of sources with their extracted entities + engagement data.
    Output: Full graph JSON with nodes, edges, clusters, and analytics.
    """
    if not req.sources:
        raise HTTPException(400, "No sources provided")

    print(f"\n  Building graph for {req.collection_id}: {len(req.sources)} sources")

    # Build the graph (no LLM calls)
    graph = build_graph(req)

    print(f"  Graph built: {graph['stats']['total_nodes']} nodes, {graph['stats']['total_edges']} edges, {graph['stats']['total_clusters']} clusters")

    # Generate LLM insights if requested
    if req.generate_insights and graph["clusters"]:
        print(f"  Generating insights for {len(graph['clusters'])} clusters...")
        graph["cluster_insights"] = await generate_cluster_insights(graph)
        print(f"  Generated {len(graph.get('cluster_insights', []))} cluster insight sets")
    else:
        graph["cluster_insights"] = []

    return graph


# ── Run ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8300"))
    uvicorn.run(app, host="0.0.0.0", port=port)
