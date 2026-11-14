"""Local browser microphone -> STT -> Groq -> TTS demo."""
import base64

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from model_server.gnani_config import setting

app = FastAPI(title="Browser voice chat")

HTML = '''<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice chat</title><style>body{font:18px system-ui;max-width:650px;margin:10vh auto;padding:20px;color:#17243a}button,select{font:inherit;padding:12px;margin:8px 0}button{background:#4338ca;color:white;border:0;border-radius:8px;cursor:pointer}p{white-space:pre-wrap}audio{width:100%}</style></head><body>
<h1>Voice chat</h1><p>Speak English, Hindi, or Gujarati. Click again to stop recording.</p>
<label>Language: <select id="lang"><option value="">Auto-detect</option><option value="en-IN">English</option><option value="hi-IN">Hindi</option><option value="gu-IN">Gujarati</option></select></label><br>
<button id="record">Click to Speak</button><p id="status"></p><p id="transcript"></p><p id="answer"></p><audio id="audio" controls></audio>
<script>
const button=document.querySelector('#record'), status=document.querySelector('#status'); let recorder, stream, chunks=[];
button.onclick=async()=>{if(recorder && recorder.state==='recording'){recorder.stop();button.disabled=true;return}
try{stream=await navigator.mediaDevices.getUserMedia({audio:true});chunks=[];recorder=new MediaRecorder(stream);
recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};recorder.onstop=async()=>{stream.getTracks().forEach(t=>t.stop());button.textContent='Click to Speak';status.textContent='Thinking...';
try{const data=new FormData();data.append('file',new Blob(chunks,{type:recorder.mimeType}),'recording.webm');data.append('language',document.querySelector('#lang').value);
const res=await fetch('api/chat',{method:'POST',body:data});const result=await res.json();if(!res.ok)throw new Error(result.detail || 'Request failed');
document.querySelector('#transcript').textContent='You: '+result.transcript;document.querySelector('#answer').textContent='AI: '+result.answer;
const audio=document.querySelector('#audio');audio.src='data:audio/wav;base64,'+result.audio;await audio.play();status.textContent='Ready';
}catch(e){status.textContent=e.message}finally{button.disabled=false}};recorder.start();button.textContent='Stop recording';status.textContent='Listening...';
}catch(e){status.textContent='Microphone error: '+e.message}};
</script></body></html>'''


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


@app.post("/api/chat")
async def chat(file: UploadFile = File(...), language: str = Form("")):
    if language not in {"", "en-IN", "hi-IN", "gu-IN"}:
        raise HTTPException(400, "Unsupported language")
    key = setting("LLM_API_KEY")
    if not key or key.startswith("put_your_"):
        raise HTTPException(503, "Set LLM_API_KEY in .env")
    data = await file.read(25 * 1024 * 1024 + 1)
    if not data or len(data) > 25 * 1024 * 1024:
        raise HTTPException(400, "Audio must be nonempty and at most 25 MB")
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            stt = await client.post(setting("STT_BASE_URL", "http://127.0.0.1:8002/v1").rstrip("/") + "/audio/transcriptions",
                                    files={"file": (file.filename or "recording.webm", data, file.content_type or "audio/webm")},
                                    data={"language": language})
            stt.raise_for_status()
            result = stt.json()
            transcript = result.get("text", "").strip()
            if not transcript:
                raise HTTPException(422, "No speech detected; please try again")
            detected = result.get("language", "")
            lang = language or (detected if detected in {"en-IN", "hi-IN", "gu-IN"} else {"en": "en-IN", "hi": "hi-IN", "gu": "gu-IN"}.get(detected, "hi-IN"))
            llm = await client.post(setting("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/") + "/chat/completions",
                                    headers={"Authorization": f"Bearer {key}"},
                                    json={"model": setting("LLM_MODEL", "llama-3.3-70b-versatile"), "messages": [
                                        {"role": "system", "content": "Reply briefly in the same language as the user. Your answer will be spoken aloud."},
                                        {"role": "user", "content": transcript}]})
            llm.raise_for_status()
            answer = llm.json()["choices"][0]["message"]["content"]
            tts = await client.post(setting("TTS_BASE_URL", "http://127.0.0.1:8003/v1").rstrip("/") + "/audio/speech",
                                    json={"input": answer, "language": lang, "response_format": "wav"})
            tts.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail")
        except ValueError:
            detail = None
        raise HTTPException(502, detail or "Speech or language provider request failed; check server logs and keys") from exc
    except (httpx.RequestError, ValueError, KeyError, IndexError) as exc:
        raise HTTPException(502, "Could not reach a service or parse its response") from exc
    return {"transcript": transcript, "answer": answer, "audio": base64.b64encode(tts.content).decode()}
