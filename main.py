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
    ydl_opts = {
        'format': 'bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
        'quiet': False,
        'no_warnings': False,
        'socket_timeout': 30,
        # Dùng ANDROID_VR client — không cần JS runtime, ít bị block nhất
        'extractor_args': {'youtube': {'player_client': ['tv_embedded']}},
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        },
        'geo_bypass': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"[YT] Tìm kiếm: {query}")
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            if info and 'entries' in info and info['entries']:
                video = info['entries'][0]
                title = video.get('title', 'Unknown')
                url = video.get('url')
                ext = video.get('ext', '?')
                print(f"[YT] OK: {title} | ext={ext} | url={'ok' if url else 'NONE'}")
                return title, url
            else:
                print(f"[YT] Không có kết quả cho: {query}")
    except Exception as e:
        print(f"[YT ERROR] {type(e).__name__}: {e}")
    return None, None


@app.get("/play")
def play_music(q: str):
    """
    Endpoint được ESP32 gọi trực tiếp qua HTTP để stream nhạc.
    Trả về OGG/Opus stream (ffmpeg -f ogg output) để OggDemuxer trên firmware parse được.
    """
    print(f"[PLAY] Request: q={q}")
    title, audio_url = fetch_youtube_stream_url(q)
    if not audio_url:
        print(f"[PLAY] Không tìm thấy bài: {q}")
        raise HTTPException(status_code=404, detail=f"Khong tim thay bai hat: {q}")

    print(f"-> 🎵 [STREAM] Bài: {title}")

    ffmpeg_cmd = [
        'ffmpeg',
        '-reconnect', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '5',
        '-i', audio_url,
        '-ac', '1',              # Mono
        '-ar', '24000',          # 24 kHz
        '-acodec', 'libopus',
        '-b:a', '32k',
        '-frame_duration', '60', # 60 ms frame
        '-f', 'ogg',             # OGG container cho OggDemuxer
        'pipe:1'
    ]
    print(f"[FFMPEG] Bắt đầu stream...")
    process = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE   # Capture stderr để debug nếu cần
    )

    def stream():
        try:
            chunk_count = 0
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                chunk_count += 1
                if chunk_count == 1:
                    print(f"[FFMPEG] Đang stream (chunk đầu tiên OK)")
                yield chunk
            print(f"[FFMPEG] Stream xong, tổng {chunk_count} chunks")
        except Exception as e:
            print(f"[FFMPEG] Stream lỗi: {e}")
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
