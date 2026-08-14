import os
import sys
import io
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

# Đảm bảo UTF-8 logging không bị lỗi trên mọi OS
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

MCP_URL = os.environ.get("MCP_URL", "wss://api.xiaozhi.me/mcp/?token=YOUR_TOKEN_HERE")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://d-n-g66p.onrender.com")

app = FastAPI(title="XiaoZhi Music Streaming Server")


def fetch_music_stream_url(query: str):
    """
    Tìm kiếm nhạc đa nguồn:
    1. SoundCloud: Tuyệt đối không bị Cloud/Datacenter IP block, tốc độ rất nhanh, đầy đủ nhạc Việt & Quốc tế.
    2. YouTube: Thử qua các client chống bot (tv_embedded, android, web).
    """
    sources = [
        ("SoundCloud", f"scsearch1:{query}", {
            "format": "bestaudio/best",
            "socket_timeout": 15,
        }),
        ("YouTube", f"ytsearch1:{query}", {
            "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
            "socket_timeout": 20,
            "extractor_args": {"youtube": {"player_client": ["tv_embedded", "android", "web"]}},
            "geo_bypass": True,
        })
    ]

    for name, search_str, opts in sources:
        try:
            print(f"[{name}] Đang tìm: {query} ...")
            opts["quiet"] = True
            opts["no_warnings"] = True
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_str, download=False)
                if info and "entries" in info and info["entries"]:
                    video = info["entries"][0]
                    title = video.get("title", "Unknown")
                    url = video.get("url")
                    if url:
                        print(f"[{name} THÀNH CÔNG] Bài: {title}")
                        return title, url
        except Exception as e:
            print(f"[{name} Lỗi] {e}")

    return None, None


@app.get("/play")
def play_music(q: str):
    """
    Endpoint được ESP32 gọi trực tiếp qua HTTP/HTTPS để stream nhạc.
    Trả về OGG/Opus stream (ffmpeg -f ogg) để OggDemuxer trên ESP32 phát ra loa.
    """
    print(f"\n[REQUEST] Yêu cầu phát nhạc: '{q}'")
    title, audio_url = fetch_music_stream_url(q)
    if not audio_url:
        print(f"[REQUEST] ❌ Không tìm thấy bài hát: '{q}'")
        raise HTTPException(status_code=404, detail=f"Khong tim thay bài hat: {q}")

    print(f"-> 🎵 [STREAMING] Đang mở luồng phát: {title}")

    ffmpeg_cmd = [
        "ffmpeg",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", audio_url,
        "-ac", "1",              # Mono
        "-ar", "24000",          # 24 kHz (phù hợp codec ESP32)
        "-acodec", "libopus",
        "-b:a", "32k",
        "-frame_duration", "60", # 60ms Opus frame
        "-f", "ogg",             # OGG container cho OggDemuxer
        "pipe:1"
    ]

    process = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL
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
                    print(f"-> 🔊 [AUDIO] Luồng OGG/Opus bắt đầu truyền tới ESP32!")
                yield chunk
            print(f"-> 🏁 [DONE] Kết thúc phát bài ({chunk_count} chunks).")
        except Exception as e:
            print(f"-> ⚠️ [ERROR] Lỗi stream: {e}")
        finally:
            try:
                process.kill()
            except Exception:
                pass

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
    return {"status": "ok", "service": "XiaoZhi Music Streaming Server"}


def run_fastapi():
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


async def handle_mcp():
    while True:
        try:
            print("-> Đang kết nối tới XiaoZhi MCP Endpoint...")
            async with websockets.connect(MCP_URL, ping_interval=20, ping_timeout=10) as ws:
                print("=== ĐÃ KẾT NỐI THÀNH CÔNG VỚI XIAOZHI MCP! 🟢 ===\n")

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
                                    "serverInfo": {"name": "XiaoZhi Music Server", "version": "3.0"}
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
                                            "Tìm nhạc và phát TRỰC TIẾP qua loa ESP32. "
                                            "Sau khi nhận được URL từ tool này, PHẢI gọi ngay MCP tool "
                                            "'self.music.play' trên thiết bị với URL đó để phát nhạc qua loa. "
                                            "KHÔNG đọc URL, KHÔNG mô tả URL — chỉ gọi self.music.play."
                                        ),
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {
                                                "query": {"type": "string", "description": "Tên bài hát hoặc ca sĩ"}
                                            },
                                            "required": ["query"]
                                        }
                                    }]
                                }
                            }
                            await ws.send(json.dumps(build_response(res)))

                        elif method == "tools/call":
                            params = payload.get("params") or data.get("params", {})
                            args = params.get("arguments", {})
                            query = args.get("query", "")

                            print(f"-> [MCP Call] Yêu cầu tìm nhạc: {query}")
                            encoded_query = urllib.parse.quote(query)

                            domain = RENDER_EXTERNAL_URL if RENDER_EXTERNAL_URL else "https://d-n-g66p.onrender.com"
                            if not domain.startswith("http"):
                                domain = "https://" + domain
                            audio_stream_url = f"{domain}/play?q={encoded_query}"

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
                            print(f"-> [OK] Đã gửi stream URL về cho AI: {audio_stream_url}")

                    except Exception as e:
                        print(f"Lỗi xử lý tin nhắn: {e}")

        except Exception as e:
            print(f"⚠️ Ngắt kết nối ({e}). Thử lại sau 3s...")
            await asyncio.sleep(3)


if __name__ == "__main__":
    threading.Thread(target=run_fastapi, daemon=True).start()
    asyncio.run(handle_mcp())
