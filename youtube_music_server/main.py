import os
import asyncio
import json
import websockets
import yt_dlp
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
import subprocess
import threading
import urllib.parse

# Lấy cấu hình từ biến môi trường của Render
MCP_URL = os.environ.get("MCP_URL", "wss://api.xiaozhi.me/mcp/?token=YOUR_TOKEN_HERE")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

app = FastAPI()

def fetch_youtube_stream_url(query):
    ydl_opts = {'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            if info and 'entries' in info and info['entries']:
                video = info['entries'][0]
                return video.get('title'), video.get('url')
    except Exception as e:
        print(f"[Lỗi YT] {e}")
    return None, None

@app.get("/play")
def play_music(q: str):
    title, audio_url = fetch_youtube_stream_url(q)
    if not audio_url:
        raise HTTPException(status_code=404, detail="Khong tim thay bai hat")
    
    print(f"-> 🎵 [LOA ESP32 DANG PHAT NHAC]: {title}")
    ffmpeg_cmd = [
        'ffmpeg', '-i', audio_url, '-ac', '1', '-ar', '24000',
        '-acodec', 'libopus', '-b:a', '32k', '-f', 'opus', 'pipe:1'
    ]
    process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def stream():
        try:
            while True:
                chunk = process.stdout.read(1024)
                if not chunk: break
                yield chunk
        finally:
            process.kill()

    return StreamingResponse(stream(), media_type="audio/ogg")

def run_fastapi():
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

async def handle_mcp():
    while True:
        try:
            print("-> Đang kết nối tới XiaoZhi MCP Endpoint...")
            async with websockets.connect(MCP_URL, ping_interval=20, ping_timeout=10) as ws:
                print("=== ĐÃ KẾT NỐI THÀNH CÔNG VỚI XIAOZHI! 🟢 ===\n")
                
                async for message in ws:
                    try:
                        data = json.loads(message)
                        session_id = data.get("session_id")
                        payload = data.get("payload", data)
                        req_id = payload.get("id") if payload.get("id") is not None else data.get("id")
                        method = payload.get("method") or data.get("method")
                        
                        def build_response(result_payload):
                            if session_id:
                                return {"type": "mcp", "session_id": session_id, "payload": result_payload}
                            return result_payload

                        if method == "ping":
                            res = {"jsonrpc": "2.0", "id": req_id, "result": {}}
                            await ws.send(json.dumps(build_response(res)))

                        elif method == "initialize":
                            res = {
                                "jsonrpc": "2.0", "id": req_id,
                                "result": {
                                    "protocolVersion": "2024-11-05",
                                    "capabilities": {"tools": {}},
                                    "serverInfo": {"name": "YouTube Music Server", "version": "1.0"}
                                }
                            }
                            await ws.send(json.dumps(build_response(res)))
                            
                        elif method == "tools/list":
                            res = {
                                "jsonrpc": "2.0", "id": req_id,
                                "result": {
                                    "tools": [{
                                        "name": "play_youtube_music",
                                        "description": "BẮT BUỘC dùng công cụ này để tìm và phát nhạc từ YouTube.",
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {"query": {"type": "string", "description": "Tên bài hát hoặc ca sĩ"}},
                                            "required": ["query"]
                                        }
                                    }]
                                }
                            }
                            await ws.send(json.dumps(build_response(res)))

                        elif method == "tools/call":
                            params = payload.get("params") or data.get("params", {})
                            tool_name = params.get("name")
                            args = params.get("arguments", {})
                            query = args.get("query", "")
                            
                            print(f"-> [MCP Call] Đã nhận lệnh phát bài: {query}")
                            encoded_query = urllib.parse.quote(query)
                            base_url = RENDER_EXTERNAL_URL if RENDER_EXTERNAL_URL else "http://localhost:10000"
                            audio_stream_url = f"{base_url}/play?q={encoded_query}"
                            
                            reply_text = f"Đã tìm thấy bài hát '{query}'. Đang phát từ YouTube lên loa cho anh."

                            res = {
                                "jsonrpc": "2.0", "id": req_id,
                                "result": {
                                    "content": [{"type": "text", "text": reply_text}],
                                    "isError": False
                                }
                            }
                            await ws.send(json.dumps(build_response(res)))

                    except Exception as e:
                        print(f"Lỗi xử lý tin nhắn: {e}")

        except Exception as e:
            print(f"⚠️ Ngắt kết nối ({e}). Thử lại sau 3s...")
            await asyncio.sleep(3)

if __name__ == "__main__":
    threading.Thread(target=run_fastapi, daemon=True).start()
    asyncio.run(handle_mcp())
