# Devpost submission kit — GridShift

Copy each block into the matching Devpost field. Edit anything that doesn't match your own experience (especially the feedback).

---

## Project name
GridShift — the electricity-price copilot for Alexa+

## Tagline (short description)
Ask Alexa when to run the dishwasher, charge the car or do the laundry. GridShift reads hourly electricity prices and answers in one spoken sentence, with the savings.

## Track and mini challenges
- Primary track: **Alexa+** (self-hosted MCP server, Streamable HTTP, spec 2025-11-25 and 2026-07-28)
- Mini challenge: **AWS Builder** — the Alexa+ simulator's agent runs on **Amazon Nova Pro through the Amazon Bedrock Converse API**

## Built with
python, model-context-protocol, mcp-python-sdk, streamable-http, amazon-bedrock, amazon-nova, fastapi, web-speech-api, red-electrica-esios

---

## About the project

### Inspiration
In Spain, millions of homes are on PVPC, a regulated tariff whose price changes every hour; the UK, the Nordics and many US utilities offer similar dynamic or time-of-use plans. The same dishwasher cycle can cost two to three times more in the evening peak than at night, yet almost nobody opens a price chart before pressing *Start*. The kitchen already has the right interface: a voice assistant. We wanted Alexa+ to answer "when should I…?" with a time and a number.

### What it does
GridShift is a self-hosted MCP server that gives Alexa+ three skills-like tools:

- **price_outlook** — "Is electricity expensive right now?" Current price and level, the cheapest 3-hour stretch ahead, the hour to avoid, and an hourly series for screens such as Echo Show.
- **find_best_time** — "Charge my car before 7 am." The cheapest start time at 15-minute resolution inside the user's window, the cost versus starting now, and the percentage saved.
- **plan_my_day** — "Plan my laundry and the car tonight." Schedules several appliances at their cheapest times without exceeding the home's contracted power limit (in Spain 4.6 kW is typical, and running the dryer and the car charger together trips the breaker). It knows the dryer must run after the washing machine, and estimates monthly savings.

Every response carries a `speech` field written to be spoken ("tomorrow at 2 AM", "8 cents") plus typed structured content for agents and screens.

### How we built it
- **MCP server** in Python with the official MCP Python SDK v2 (`MCPServer`), served over **Streamable HTTP** at `/mcp`. It negotiates protocol 2026-07-28 and accepts 2025-11-25 clients (we tested both handshakes). Tools declare annotations (read-only, idempotent, open-world) and output schemas; there is also a resource (appliance catalog) and a prompt (morning energy briefing).
- **Live data**: Spain's PVPC prices from Red Eléctrica's public e·sios daily file (no API key). Our parser matches aiopvpc, the library behind Home Assistant's PVPC integration, including 23- and 25-hour DST days (this weekend has one). A time-of-use mode covers US/UK plans, and a sample mode keeps demos working offline.
- **Planner**: exact cost integration over hourly prices, a 15-minute start grid, and a greedy power-aware scheduler.
- **Alexa+ simulator**: a FastAPI web app with browser speech recognition and synthesis. Its agent is **Amazon Nova Pro on Amazon Bedrock** (Converse API with tool use); MCP tool schemas are passed to Bedrock unchanged and every call goes to the MCP server over HTTP, exactly as a brand integration would be called by Alexa+. An "Under the hood" panel shows each MCP call with its structured result, and the price chart highlights the planned runs.

### Challenges we ran into
- Turning numbers into speech: ISO timestamps and "€/kWh" are unreadable aloud, so every tool composes its own short sentence ("tomorrow at 2 AM", "less than 1 cent").
- Daylight-saving days have 23 or 25 hourly prices; we anchor on local midnight converted to UTC so the repeated hour is priced correctly.
- Tomorrow's prices are only published in the evening, so tools say so instead of guessing.
- Scheduling under a power cap: the cheapest hour for each appliance is not a valid plan if they all overlap.

### Accomplishments that we're proud of
- A full voice round-trip — speech in, Nova on Bedrock, MCP over Streamable HTTP, live grid prices, speech out — in one afternoon.
- Answers that are actionable in one sentence, with the saving in money, not a chart.
- Power-limit-aware planning, which is a real pain point for Spanish households.

### What we learned
MCP tool schemas map one-to-one onto the Bedrock Converse tool format, so a brand can serve Alexa+ and any other agent from the same server. Designing for voice changes the API: the most useful output field turned out to be a sentence.

### What's next for GridShift
- Act on the plan: start smart plugs and EV chargers through Alexa+ routines.
- Proactive "cheap hour starting now" notifications; solar, home-battery and CO₂ awareness.
- More live markets (UK Agile, US real-time pricing, Nordics) behind the same three tools.

---

## Product feedback (required)

**Alexa+ (MCP integration path)**
- Used for: building a self-hosted MCP server that Alexa+ would call for energy-price questions.
- Worked well: building on open standards (MCP, Streamable HTTP) meant we could reuse the official SDK and inspector, and the same server works with any agent.
- Needs work: there is no way for outside developers to point a real Alexa+ device or a test console at a self-hosted MCP server, so we had to simulate the assistant. Guidance on voice-specific conventions (a standard field for spoken output, latency budget, how Alexa+ renders structured content on Echo Show) and on account linking/OAuth for MCP servers would help a lot.
- Onboarding: fast to start because it's plain MCP, but unclear what "good" looks like for Alexa+ specifically.
- Would build again: yes, especially with a test harness.

**Amazon Bedrock + Amazon Nova (AWS Builder)**
- Used for: the simulator's agent — Converse API with tool use, Nova Pro.
- Worked well: MCP `inputSchema` drops straight into `toolSpec.inputSchema.json`; tool results accept JSON, so MCP `structuredContent` passes through unchanged.
- Needs work: Nova can return `<thinking>` blocks in the text output alongside tool calls, which a voice app must strip; cross-region inference profile IDs (`us.` / `eu.` prefixes) are easy to get wrong.
- Would build again: yes.

**MCP Python SDK v2**
- Worked well: typed return models become `outputSchema` + `structuredContent` automatically; in-process `Client(server)` makes testing trivial.
- Needs work: the rename from `FastMCP` to `MCPServer` breaks most tutorials and samples written for v1.

## Feature requests (optional)
1. **Alexa+ MCP sandbox** — register a self-hosted MCP URL and talk to it from the Alexa app or developer console. *Critical.*
2. **Screen hints for Echo Show** — a convention so structured content (e.g. an hourly series) renders as a visual card. *Important.*
3. **Proactive notifications from MCP servers** — let a server ask Alexa+ to announce "the cheap hour starts now". *Important.*

## Friction log (optional, up to +10%)

| Task | Steps | Expected vs actual | Severity | Workaround | Suggestion |
|---|---|---|---|---|---|
| Test the MCP server with a real Alexa+ | Read the track resources (Agent Skills, Streamable HTTP spec) and looked for a console | Expected a way to connect a self-hosted MCP server to Alexa+; found only the specs | High | Built a web simulator with Bedrock + Web Speech | Ship an Alexa+ MCP test console |
| Start from current MCP Python examples | `pip install mcp`, ran a v1-style `from mcp.server.fastmcp import FastMCP` | Expected the import to work; v2 raised `ModuleNotFoundError` (renamed to `MCPServer`) | Medium | Read the migration guide and switched to `mcp.server.mcpserver.MCPServer` | Point hackathon resources to v2-ready samples |
| Voice output from Nova with tools | Converse API with toolConfig | Expected plain text replies; responses can include `<thinking>` blocks | Low | Strip them before speech synthesis | Option to suppress reasoning text in Converse |

---

## Demo video script (under 3 minutes, English, public on YouTube)

**0:00–0:20 — Problem** (show the price chart in the simulator)
"In Spain, millions of homes pay a different electricity price every hour, and many US and UK homes have time-of-use plans. The same dishwasher cycle can cost two or three times more at 9 PM than at night. Nobody checks a chart before pressing Start. So we asked Alexa."

**0:20–1:30 — Demo** (simulator, voice on, "Under the hood" panel visible)
1. Say: "Alexa, is electricity expensive right now?" → reply + chart.
2. Say: "When should I run the dishwasher?" → best time, cost, % saved; highlighted band on the chart; point at the `find_best_time` call and its structured result.
3. Say: "Plan my laundry and charge my car tonight, I leave at 7." → plan; mention it staggered runs so the 4.6 kW breaker doesn't trip and the dryer runs right after the washer; show the monthly saving.

**1:30–2:20 — How it works** (README architecture diagram, then code)
"GridShift is a self-hosted MCP server over Streamable HTTP — spec 2025-11-25 and 2026-07-28. Three read-only tools with annotations and output schemas, plus a resource and a prompt. Each tool returns a sentence written to be spoken, plus structured data for agents and Echo Show screens. Prices come live from Red Eléctrica's public data, and it also supports any time-of-use plan. The assistant here is Amazon Nova Pro on Amazon Bedrock, calling the server exactly as Alexa+ calls brand MCP servers." (Optional 10 s: MCP Inspector listing the tools.)

**2:20–2:50 — Impact and next**
"A household that shifts its dishwasher, laundry and EV charging saves real money every month and moves demand away from the evening peak. Next: let Alexa+ start the smart plug at the planned time, and add proactive cheap-hour alerts."
