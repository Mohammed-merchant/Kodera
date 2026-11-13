"""One-port front door for the three GPU services, so a single ngrok tunnel can serve all.

    /v1/chat/completions, /v1/models   -> LLM  (llama-server, 127.0.0.1:8001)
    /v1/audio/transcriptions           -> STT  (127.0.0.1:8002)
    /v1/audio/speech                   -> TTS  (127.0.0.1:8003)

It forwards the Authorization header; the backends check the key. Start the tunnel with:
    ngrok http 8000
Run:  uvicorn gateway:app --port 8000   (run_all.py does this for you)
"""

from __future__ import annotations

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from common import env, require_key

TARGETS = {
    "llm": f"http://127.0.0.1:{env('LLM_PORT', '8001')}",
    "stt": f"http://127.0.0.1:{env('STT_PORT', '8002')}",
    "tts": f"http://127.0.0.1:{env('TTS_PORT', '8003')}",
}
ROUTES = {
    "/v1/chat/completions": "llm",
    "/v1/models": "llm",
    "/v1/audio/transcriptions": "stt",
    "/v1/audio/speech": "tts",
}
PASS_HEADERS = {"authorization", "content-type", "accept"}

app = FastAPI(title="GPU gateway")
client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0))


@app.get("/health")
async def health():
    out = {}
    for name, base in TARGETS.items():
        try:
            path = "/v1/models" if name == "llm" else "/health"
            r = await client.get(base + path, timeout=3.0)
            out[name] = "up" if r.status_code < 500 and (name != "llm" or r.status_code in (200, 401)) else "error"
        except httpx.HTTPError:
            out[name] = "down"
    ok = all(v == "up" for v in out.values())
    # 503 when anything is down: the call app checks this before answering a call, and
    # ends the call at once instead of leaving the caller in silence.
    return JSONResponse({"status": "ok" if ok else "degraded", **out}, status_code=200 if ok else 503)


async def forward(request: Request, _key: None = Depends(require_key)):
    # The key is checked here, so no route is open even if a backend does not check keys.
    target = ROUTES[request.url.path]
    req = client.build_request(
        request.method,
        TARGETS[target] + request.url.path,
        headers={k: v for k, v in request.headers.items() if k.lower() in PASS_HEADERS},
        content=await request.body(),
    )
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError:
        return JSONResponse({"error": f"{target} service is not reachable"}, status_code=502)
    skip = {"content-length", "transfer-encoding", "connection"}
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items() if k.lower() not in skip},
        background=BackgroundTask(resp.aclose),
    )


for _path in ROUTES:
    app.add_api_route(_path, forward, methods=["GET", "POST"], include_in_schema=False)
