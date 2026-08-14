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
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://d-n-g66p.onrender.com")

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
    """
    Endpoint được ESP32 gọi trực tiếp qua HTTP để stream nhạc.
    Trả về OGG/Opus stream (ffmpeg -f ogg output) để OggDemuxer trên firmware parse được.
    """
    title, audio_url = fetch_youtube_stream_url(q)
    if not audio_url:
        raise HTTPException(status_code=404, detail="Khong tim thay bai hat")

    print(f"-> 🎵 [STREAM] Bài: {title}")

    # QUAN TRỌNG: Dùng -f ogg (OGG container) thay vì -f opus (raw)
    # OggDemuxer trong firmware ESP32 cần OGG container với header OggS
    # ffmpeg mặc định xuất 60ms frame với -f ogg và libopus
    ffmpeg_cmd = [
        'ffmpeg', '-i', audio_url,
        '-ac', '1',             # Mono
        '-ar', '24000',         # Sample rate 24kHz (khớp với AUDIO_OUTPUT_SAMPLE_RATE)
        '-acodec', 'libopus',
        '-b:a', '32k',
        '-frame_duration', '60',  # 60ms frame — khớp với OPUS_FRAME_DURATION_MS trong firmware
        '-f', 'ogg',            # OGG container — OggDemuxer firmware đọc được
        'pipe:1'
    ]
    process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def stream():
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
        finally:
            process.kill()

    return StreamingResponse(
        stream(),
        media_type="audio/ogg",
        headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        }
    )

@app.get("/health")
def health():
    return {"status": "ok"}

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
                                    "serverInfo": {"name": "YouTube Music Server", "version": "2.0"}
                                }
                            }
                            await ws.send(json.dumps(build_response(res)))

                        elif method == "tools/list":
                            res = {
                                "jsonrpc": "2.0", "id": req_id,
                                "result": {
                                    "tools": [{
                                        "name": "play_youtube_music",
                                        "description": (
                                            "Tìm nhạc YouTube và phát TRỰC TIẾP qua loa ESP32. "
                                            "Sau khi nhận được URL từ tool này, PHẢI gọi ngay MCP tool "
                                            "'self.music.play' trên thiết bị với URL đó để phát nhạc qua loa. "
                                            "KHÔNG đọc URL, KHÔNG mô tả URL — chỉ gọi self.music.play."
                                        ),
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {
                                                "query": {"type": "string", "description": "Tên bài hát hoặc nghệ sĩ"}
                                            },
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

                            print(f"-> [MCP Call] Tìm bài: {query}")
                            encoded_query = urllib.parse.quote(query)

                            domain = RENDER_EXTERNAL_URL if RENDER_EXTERNAL_URL else "https://d-n-g66p.onrender.com"
                            if not domain.startswith("http"):
                                domain = "https://" + domain
                            audio_stream_url = f"{domain}/play?q={encoded_query}"

                            # Trả về URL stream để AI gọi self.music.play trên thiết bị
                            # Dùng cấu trúc JSON rõ ràng để AI biết cần làm gì tiếp theo
                            result_json = json.dumps({
                                "found": True,
                                "query": query,
                                "stream_url": audio_stream_url,
                                "instruction": "Gọi tool self.music.play với url này để phát nhạc qua loa"
                            }, ensure_ascii=False)

                            res = {
                                "jsonrpc": "2.0", "id": req_id,
                                "result": {
                                    "content": [{"type": "text", "text": result_json}],
                                    "isError": False
                                }
                            }
                            await ws.send(json.dumps(build_response(res)))
                            print(f"-> [OK] Đã gửi stream URL: {audio_stream_url}")

                    except Exception as e:
                        print(f"Lỗi xử lý tin nhắn: {e}")

        except Exception as e:
            print(f"⚠️ Ngắt kết nối ({e}). Thử lại sau 3s...")
            await asyncio.sleep(3)

if __name__ == "__main__":
    threading.Thread(target=run_fastapi, daemon=True).start()
    asyncio.run(handle_mcp())
