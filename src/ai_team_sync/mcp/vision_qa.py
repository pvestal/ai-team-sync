"""Vision QA MCP Server — character visual QA against anime production specs.

Exposes tools that pull character specs, shot context, and keyframe paths from
the anime_production database so Claude can compare generated images against
what was specified in the design prompt / identity block.
"""

import asyncio
import json
import os

import asyncpg
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

DB_DSN = os.environ.get(
    "VQA_DATABASE_URL",
    "postgresql://patrick:RP78eIrW7cI2jYvL5akt1yurE@127.0.0.1:5432/anime_production",
)

_pool: asyncpg.Pool | None = None

# Slug computation matching the anime-studio convention
SLUG_EXPR = "REGEXP_REPLACE(LOWER(REPLACE(name, ' ', '_')), '[^a-z0-9_-]', '', 'g')"
C_SLUG = "REGEXP_REPLACE(LOWER(REPLACE(c.name, ' ', '_')), '[^a-z0-9_-]', '', 'g')"


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DB_DSN,
            min_size=1,
            max_size=3,
            server_settings={"search_path": "public"},
        )
    return _pool


app = Server("vision-qa")


# ---------- tool definitions ----------

TOOLS = [
    Tool(
        name="list_characters",
        description=(
            "List all characters in a project with their slugs, LoRA status, and entity type. "
            "Use this to discover which characters exist before querying specs."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Anime project ID"},
                "include_archived": {"type": "boolean", "default": False},
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="get_character_spec",
        description=(
            "Get the full visual spec for a character — design_prompt, lora_trigger, "
            "identity_block, negative_prompt, checkpoint, and generation config. "
            "This is exactly what gets sent to ComfyUI for image generation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Anime project ID"},
                "character_slug": {"type": "string", "description": "Character slug (e.g. 'alice', 'kira')"},
            },
            "required": ["project_id", "character_slug"],
        },
    ),
    Tool(
        name="get_shot_context",
        description=(
            "Get full generation context for a shot — the assembled prompt, negative prompt, "
            "characters present, motion prompt, LoRAs, engine, and scene context. "
            "Shows exactly what was (or will be) sent to ComfyUI."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "shot_id": {"type": "string", "description": "Shot UUID"},
            },
            "required": ["shot_id"],
        },
    ),
    Tool(
        name="get_keyframe_paths",
        description=(
            "Get file paths of generated keyframes for a character or shot. "
            "Returns paths you can then view with the Read tool for visual review."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Anime project ID"},
                "character_slug": {"type": "string", "description": "Filter by character slug (optional)"},
                "shot_id": {"type": "string", "description": "Filter by shot UUID (optional)"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "approved", "rejected", "unreviewed", "all"],
                    "default": "all",
                    "description": "Filter by review_status",
                },
                "limit": {"type": "integer", "default": 10, "description": "Max results"},
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="get_project_summary",
        description=(
            "Get project overview — name, genre, content rating, checkpoint, style, "
            "character count, shot counts by status. Quick orientation for QA."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Anime project ID"},
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="save_review",
        description=(
            "Save a visual QA review result for a keyframe or shot. "
            "Records pass/fail, issues found, and suggested fixes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "shot_id": {"type": "string", "description": "Shot UUID being reviewed"},
                "passed": {"type": "boolean", "description": "Whether the image passes QA"},
                "issues": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of issues found (e.g. 'wrong hair color', 'missing cat ears')",
                },
                "notes": {"type": "string", "description": "Free-form reviewer notes"},
            },
            "required": ["shot_id", "passed"],
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    pool = await get_pool()

    handlers = {
        "list_characters": _list_characters,
        "get_character_spec": _get_character_spec,
        "get_shot_context": _get_shot_context,
        "get_keyframe_paths": _get_keyframe_paths,
        "get_project_summary": _get_project_summary,
        "save_review": _save_review,
    }
    handler = handlers.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    return await handler(pool, arguments)


# ---------- implementations ----------


async def _list_characters(pool: asyncpg.Pool, args: dict) -> list[TextContent]:
    project_id = args["project_id"]
    include_archived = args.get("include_archived", False)

    query = f"""
        SELECT {SLUG_EXPR} AS slug, name, entity_type,
               lora_path IS NOT NULL AS has_lora,
               lora_trigger,
               COALESCE(archived, false) AS archived
        FROM characters
        WHERE project_id = $1
    """
    if not include_archived:
        query += " AND COALESCE(archived, false) = false"
    query += " ORDER BY name"

    rows = await pool.fetch(query, project_id)
    chars = [
        {
            "slug": r["slug"],
            "name": r["name"],
            "entity_type": r["entity_type"],
            "has_lora": r["has_lora"],
            "lora_trigger": r["lora_trigger"],
            "archived": r["archived"],
        }
        for r in rows
    ]
    return [TextContent(type="text", text=json.dumps(chars, indent=2))]


async def _get_character_spec(pool: asyncpg.Pool, args: dict) -> list[TextContent]:
    project_id = args["project_id"]
    slug = args["character_slug"]

    row = await pool.fetchrow(
        f"""
        SELECT c.name, {C_SLUG} AS slug, c.design_prompt, c.lora_path, c.lora_trigger,
               c.lora_strength, c.identity_block, c.negative_prompt,
               c.visual_prompt_template, c.checkpoint_override,
               c.entity_type, c.role, c.appearance_data,
               c.generation_config, c.reference_images,
               p.name AS project_name,
               gs.checkpoint_model, gs.negative_prompt_template AS project_negative
        FROM characters c
        JOIN projects p ON p.id = c.project_id
        LEFT JOIN generation_styles gs ON gs.style_name = p.default_style
        WHERE c.project_id = $1 AND {C_SLUG} = $2
        """,
        project_id,
        slug,
    )

    if row is None:
        return [TextContent(type="text", text=f"Character '{slug}' not found in project {project_id}")]

    spec = {
        "name": row["name"],
        "slug": row["slug"],
        "project": row["project_name"],
        "entity_type": row["entity_type"],
        "role": row["role"],
        "design_prompt": row["design_prompt"],
        "lora_trigger": row["lora_trigger"],
        "lora_path": row["lora_path"],
        "lora_strength": float(row["lora_strength"]) if row["lora_strength"] else None,
        "identity_block": row["identity_block"],
        "negative_prompt": row["negative_prompt"],
        "visual_prompt_template": row["visual_prompt_template"],
        "checkpoint": row["checkpoint_override"] or row["checkpoint_model"],
        "project_negative": row["project_negative"],
        "appearance_data": json.loads(row["appearance_data"]) if row["appearance_data"] else None,
        "generation_config": json.loads(row["generation_config"]) if row["generation_config"] else None,
        "reference_images": json.loads(row["reference_images"]) if row["reference_images"] else None,
    }
    return [TextContent(type="text", text=json.dumps(spec, indent=2, default=str))]


async def _get_shot_context(pool: asyncpg.Pool, args: dict) -> list[TextContent]:
    shot_id = args["shot_id"]

    row = await pool.fetchrow(
        """
        SELECT sh.id, sh.motion_prompt, sh.generation_prompt, sh.generation_negative,
               sh.characters_present, sh.shot_type, sh.camera_angle,
               sh.source_image_path, sh.video_engine, sh.seed,
               sh.steps, sh.guidance_scale, sh.duration_seconds,
               sh.image_lora, sh.image_lora_strength,
               sh.content_lora_high, sh.content_lora_low,
               sh.status, sh.review_status,
               sc.description AS scene_description, sc.location, sc.time_of_day, sc.mood,
               p.id AS project_id, p.name AS project_name, p.content_rating
        FROM shots sh
        JOIN scenes sc ON sc.id = sh.scene_id
        JOIN projects p ON p.id = sc.project_id
        WHERE sh.id = $1::uuid
        """,
        shot_id,
    )

    if row is None:
        return [TextContent(type="text", text=f"Shot {shot_id} not found")]

    chars_present = row["characters_present"] or []
    char_specs = []
    if chars_present:
        # characters_present stores slugs computed from names
        char_rows = await pool.fetch(
            f"""
            SELECT {SLUG_EXPR} AS slug, name, design_prompt, lora_trigger, lora_strength, negative_prompt
            FROM characters
            WHERE project_id = $1 AND {SLUG_EXPR} = ANY($2::text[])
            """,
            row["project_id"],
            chars_present,
        )
        char_specs = [
            {
                "slug": r["slug"],
                "name": r["name"],
                "design_prompt": r["design_prompt"],
                "lora_trigger": r["lora_trigger"],
                "negative_prompt": r["negative_prompt"],
            }
            for r in char_rows
        ]

    ctx = {
        "shot_id": str(row["id"]),
        "status": row["status"],
        "review_status": row["review_status"],
        "project": row["project_name"],
        "content_rating": row["content_rating"],
        "scene": {
            "description": row["scene_description"],
            "location": row["location"],
            "time_of_day": row["time_of_day"],
            "mood": row["mood"],
        },
        "shot": {
            "motion_prompt": row["motion_prompt"],
            "shot_type": row["shot_type"],
            "camera_angle": row["camera_angle"],
            "video_engine": row["video_engine"],
            "duration_seconds": float(row["duration_seconds"]) if row["duration_seconds"] else None,
            "seed": row["seed"],
            "steps": row["steps"],
            "guidance_scale": float(row["guidance_scale"]) if row["guidance_scale"] else None,
        },
        "prompts": {
            "generation_prompt": row["generation_prompt"],
            "generation_negative": row["generation_negative"],
        },
        "loras": {
            "image_lora": row["image_lora"],
            "image_lora_strength": float(row["image_lora_strength"]) if row["image_lora_strength"] else None,
            "content_lora_high": row["content_lora_high"],
            "content_lora_low": row["content_lora_low"],
        },
        "source_image": row["source_image_path"],
        "characters": char_specs,
    }
    return [TextContent(type="text", text=json.dumps(ctx, indent=2, default=str))]


async def _get_keyframe_paths(pool: asyncpg.Pool, args: dict) -> list[TextContent]:
    project_id = args["project_id"]
    character_slug = args.get("character_slug")
    shot_id = args.get("shot_id")
    status = args.get("status", "all")
    limit = args.get("limit", 10)

    query = """
        SELECT sh.id AS shot_id, sh.source_image_path, sh.review_status,
               sh.characters_present, sh.motion_prompt,
               sc.description AS scene_description
        FROM shots sh
        JOIN scenes sc ON sc.id = sh.scene_id
        WHERE sc.project_id = $1
          AND sh.source_image_path IS NOT NULL
    """
    params: list = [project_id]
    idx = 2

    if character_slug:
        query += f" AND ${{idx}}::text = ANY(sh.characters_present)"
        query = query.replace(f"${{idx}}", f"${idx}")
        params.append(character_slug)
        idx += 1

    if shot_id:
        query += f" AND sh.id = ${idx}::uuid"
        params.append(shot_id)
        idx += 1

    if status != "all":
        query += f" AND sh.review_status = ${idx}"
        params.append(status)
        idx += 1

    query += f" ORDER BY sh.id DESC LIMIT ${idx}"
    params.append(limit)

    rows = await pool.fetch(query, *params)
    results = [
        {
            "shot_id": str(r["shot_id"]),
            "image_path": r["source_image_path"],
            "review_status": r["review_status"],
            "characters": r["characters_present"],
            "motion_prompt": r["motion_prompt"],
            "scene": r["scene_description"],
        }
        for r in rows
    ]
    return [TextContent(type="text", text=json.dumps(results, indent=2))]


async def _get_project_summary(pool: asyncpg.Pool, args: dict) -> list[TextContent]:
    project_id = args["project_id"]

    proj = await pool.fetchrow(
        """
        SELECT p.id, p.name, p.genre, p.content_rating,
               gs.checkpoint_model, gs.style_name AS style_name,
               (SELECT count(*) FROM characters WHERE project_id = p.id AND COALESCE(archived,false) = false) AS char_count,
               (SELECT count(*) FROM shots sh JOIN scenes sc ON sc.id = sh.scene_id WHERE sc.project_id = p.id) AS total_shots,
               (SELECT count(*) FROM shots sh JOIN scenes sc ON sc.id = sh.scene_id WHERE sc.project_id = p.id AND sh.status = 'pending') AS pending_shots,
               (SELECT count(*) FROM shots sh JOIN scenes sc ON sc.id = sh.scene_id WHERE sc.project_id = p.id AND sh.status = 'completed') AS completed_shots,
               (SELECT count(*) FROM scenes WHERE project_id = p.id) AS scene_count
        FROM projects p
        LEFT JOIN generation_styles gs ON gs.style_name = p.default_style
        WHERE p.id = $1
        """,
        project_id,
    )

    if proj is None:
        return [TextContent(type="text", text=f"Project {project_id} not found")]

    summary = {
        "id": proj["id"],
        "name": proj["name"],
        "genre": proj["genre"],
        "content_rating": proj["content_rating"],
        "checkpoint": proj["checkpoint_model"],
        "style": proj["style_name"],
        "characters": proj["char_count"],
        "scenes": proj["scene_count"],
        "shots": {
            "total": proj["total_shots"],
            "pending": proj["pending_shots"],
            "completed": proj["completed_shots"],
        },
    }
    return [TextContent(type="text", text=json.dumps(summary, indent=2))]


async def _save_review(pool: asyncpg.Pool, args: dict) -> list[TextContent]:
    shot_id = args["shot_id"]
    passed = args["passed"]
    issues = args.get("issues", [])
    notes = args.get("notes", "")

    await pool.execute("""
        CREATE TABLE IF NOT EXISTS vision_qa_reviews (
            id SERIAL PRIMARY KEY,
            shot_id UUID NOT NULL REFERENCES shots(id),
            passed BOOLEAN NOT NULL,
            issues JSONB DEFAULT '[]',
            notes TEXT,
            reviewed_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    review_id = await pool.fetchval(
        """
        INSERT INTO vision_qa_reviews (shot_id, passed, issues, notes)
        VALUES ($1::uuid, $2, $3::jsonb, $4)
        RETURNING id
        """,
        shot_id,
        passed,
        json.dumps(issues),
        notes,
    )

    return [TextContent(type="text", text=json.dumps({"review_id": review_id, "saved": True}))]


def main():
    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
