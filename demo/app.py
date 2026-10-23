"""Alexa+ simulator: a voice web app whose agent (Amazon Nova on Amazon Bedrock) uses the
GridShift MCP server over Streamable HTTP, the same way Alexa+ uses brand MCP servers.

Run:  python demo/app.py   ->  http://localhost:8080   (needs AWS credentials with Bedrock access)
"""

import asyncio
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from mcp import Client

MCP_URL = os.getenv("MCP_URL", "http://localhost:8000/mcp")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.amazon.nova-pro-v1:0")
TZ = ZoneInfo(os.getenv("GRIDSHIFT_TZ", "Europe/Madrid"))

bedrock = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
app = FastAPI(title="GridShift Alexa+ simulator")

SYSTEM = """You are Alexa+, a voice assistant in the user's home. You are connected to GridShift,
a service that knows the household's hourly electricity prices.
- For any question about electricity prices or when to run an appliance, call a GridShift tool. Never guess prices.
- Map requests to appliances: dishes -> dishwasher, laundry/washing -> washing_machine, drying -> tumble_dryer,
  car/EV -> ev_charger. Several appliances in one request -> plan_my_day.
- Pass times as local 24h 'HH:MM' (e.g. "before 7 in the morning" -> deadline "07:00").
- Reply in 1-3 short spoken sentences, based on the tool's `speech` field. No lists, no markdown.
Current local time: {now}."""


class ChatIn(BaseModel):
    messages: list[dict]


def _text(message: dict) -> str:
    text = " ".join(c["text"] for c in message.get("content", []) if "text" in c)
    return re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.S).strip()


@app.post("/api/chat")
async def chat(body: ChatIn):
    messages, trace = body.messages, []
    system = [{"text": SYSTEM.format(now=datetime.now(TZ).strftime("%A %H:%M"))}]
    try:
        async with Client(MCP_URL) as mcp:
            tools = (await mcp.list_tools()).tools
            tool_config = {"tools": [{"toolSpec": {"name": t.name, "description": t.description or "",
                                                   "inputSchema": {"json": t.input_schema}}} for t in tools]}
            for _ in range(5):
                resp = await asyncio.to_thread(
                    bedrock.converse, modelId=MODEL_ID, system=system, messages=messages,
                    toolConfig=tool_config, inferenceConfig={"maxTokens": 500, "temperature": 0.1})
                message = resp["output"]["message"]
                messages.append(message)
                uses = [c["toolUse"] for c in message["content"] if "toolUse" in c]
                if not uses:
                    break
                results = []
                for use in uses:
                    r = await mcp.call_tool(use["name"], use.get("input") or {})
                    data = r.structured_content or {"text": " ".join(getattr(c, "text", "") for c in r.content)}
                    trace.append({"tool": use["name"], "input": use.get("input"), "output": data, "error": r.is_error})
                    results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": [{"json": data}],
                                                   "status": "error" if r.is_error else "success"}})
                messages.append({"role": "user", "content": results})
    except Exception as e:  # surface Bedrock / MCP problems in the UI
        raise HTTPException(502, f"{type(e).__name__}: {e}")
    return {"reply": _text(messages[-1]), "trace": trace, "messages": messages}


@app.get("/api/outlook")
async def outlook():
    """Price chart for the screen, straight from the MCP tool (no LLM involved)."""
    async with Client(MCP_URL) as mcp:
        r = await mcp.call_tool("price_outlook", {})
    return r.structured_content


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("DEMO_PORT", "8080")))
