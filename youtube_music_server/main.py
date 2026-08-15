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
import urllib.request

# Đảm bảo UTF-8 logging không bị lỗi trên mọi OS
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

MCP_URL = os.environ.get("MCP_URL", "wss://api.xiaozhi.me/mcp/?token=YOUR_TOKEN_HERE")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://d-n-g66p.onrender.com")
ESP32_BT_BRIDGE_IP = os.environ.get("ESP32_BT_BRIDGE_IP", "")  # Điền IP con ESP32 Bluetooth (ví dụ: 192.168.1.50)

app = FastAPI(title="XiaoZhi Music Streaming Server")


def send_cmd_to_esp32_bt(path: str) -> bool:
    """Gửi lệnh HTTP tới con ESP32 Bluetooth Bridge nội bộ."""
    if not ESP32_BT_BRIDGE_IP:
        print("-> [Warning] Chưa cấu hình biến môi trường ESP32_BT_BRIDGE_IP!")
        return False
    try:
        url = f"http://{ESP32_BT_BRIDGE_IP}{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "XiaoZhi-Server"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"-> [Error] Không gửi được lệnh tới ESP32 BT ({url}): {e}")
        return False


def fetch_music_stream_url(query: str):
    """Tìm kiếm nhạc qua SoundCloud và YouTube."""
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
    """Endpoint được ESP32 gọi trực tiếp để stream OGG/Opus."""
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
        "-ar", "24000",          # 24 kHz
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

@app.get("/play_bt")
def play_music_bluetooth(q: str):
    """
    Endpoint dành riêng cho ESP32 Bluetooth Bridge.
    Trả về luồng âm thanh PCM 16-bit Stereo 44.1kHz (raw s16le)
    để nạp thẳng vào Bluetooth A2DP Source mà không cần giải mã trên ESP32!
    """
    print(f"\n[REQUEST BT] Yêu cầu phát nhạc qua Bluetooth: '{q}'")
    title, audio_url = fetch_music_stream_url(q)
    if not audio_url:
        print(f"[REQUEST BT] ❌ Không tìm thấy bài hát: '{q}'")
        raise HTTPException(status_code=404, detail=f"Khong tim thay bài hat: {q}")

    print(f"-> 🎵 [STREAMING BT] Đang mở luồng phát Bluetooth: {title}")

    ffmpeg_cmd = [
        "ffmpeg",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", audio_url,
        "-ac", "2",              # 2 kênh Stereo
        "-ar", "44100",          # 44.1 kHz (Chuẩn Bluetooth A2DP)
        "-f", "s16le",           # Raw 16-bit PCM little endian
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
                    print(f"-> 🔊 [AUDIO BT] Luồng PCM Stereo bắt đầu truyền tới ESP32 Bluetooth!")
                yield chunk
            print(f"-> 🏁 [DONE BT] Kết thúc phát bài Bluetooth ({chunk_count} chunks).")
        except Exception as e:
            print(f"-> ⚠️ [ERROR BT] Lỗi stream: {e}")
        finally:
            try:
                process.kill()
            except Exception:
                pass

    return StreamingResponse(
        stream(),
        media_type="application/octet-stream",
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
                                    "serverInfo": {"name": "XiaoZhi Smart Music Server", "version": "3.5"}
                                }
                            }
                            await ws.send(json.dumps(build_response(res)))

                        elif method == "tools/list":
                            res = {
                                "jsonrpc": "2.0", "id": req_id,
                                "result": {
                                    "tools": [
                                        {
                                            "name": "play_youtube_music",
                                            "description": (
                                                "Tìm nhạc và phát trực tiếp qua loa của Chatbot ESP32-S3. "
                                                "Sau khi nhận được URL từ tool này, PHẢI gọi ngay MCP tool "
                                                "'self.music.play' trên thiết bị với URL đó để phát nhạc qua loa. "
                                                "KHÔNG đọc URL — chỉ gọi self.music.play."
                                            ),
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {
                                                    "query": {"type": "string", "description": "Tên bài hát hoặc ca sĩ"}
                                                },
                                                "required": ["query"]
                                            }
                                        },
                                        {
                                            "name": "connect_bluetooth_speaker",
                                            "description": (
                                                "Kích hoạt con ESP32 thường quét và KẾT NỐI tới Loa Bluetooth đã lưu. "
                                                "Dùng khi người dùng bảo: 'Bật loa bluetooth', 'Kết nối loa bluetooth', 'Mở loa ngoài'."
                                            ),
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {}
                                            }
                                        },
                                        {
                                            "name": "disconnect_bluetooth_speaker",
                                            "description": (
                                                "Ngắt kết nối tới Loa Bluetooth ngoài. "
                                                "Dùng khi người dùng bảo: 'Tắt loa bluetooth', 'Ngắt loa bluetooth', 'Đóng loa ngoài'."
                                            ),
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {}
                                            }
                                        },
                                        {
                                            "name": "play_bluetooth_speaker",
                                            "description": (
                                                "Tìm nhạc và phát không dây qua LOA BLUETOOTH ngoài. "
                                                "Dùng khi người dùng yêu cầu mở nhạc qua loa bluetooth, loa ngoài hoặc loa lớn."
                                            ),
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {
                                                    "query": {"type": "string", "description": "Tên bài hát hoặc ca sĩ cần phát qua loa Bluetooth"}
                                                },
                                                "required": ["query"]
                                            }
                                        },
                                        {
                                            "name": "stop_bluetooth_speaker",
                                            "description": "Dừng phát bài hát đang phát trên LOA BLUETOOTH ngoài.",
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {}
                                            }
                                        }
                                    ]
                                }
                            }
                            await ws.send(json.dumps(build_response(res)))

                        elif method == "tools/call":
                            params = payload.get("params") or data.get("params", {})
                            tool_name = params.get("name")
                            args = params.get("arguments", {})
                            query = args.get("query", "")

                            domain = RENDER_EXTERNAL_URL if RENDER_EXTERNAL_URL else "https://d-n-g66p.onrender.com"
                            if not domain.startswith("http"):
                                domain = "https://" + domain

                            if tool_name == "play_youtube_music":
                                print(f"-> [MCP Call] Mở nhạc trên bot: {query}")
                                encoded_query = urllib.parse.quote(query)
                                audio_stream_url = f"{domain}/play?q={encoded_query}"

                                result_json = json.dumps({
                                    "found": True,
                                    "query": query,
                                    "stream_url": audio_stream_url,
                                    "instruction": "Gọi tool self.music.play với url này để phát nhạc qua loa bot"
                                }, ensure_ascii=False)

                                res = {
                                    "jsonrpc": "2.0", "id": req_id,
                                    "result": {
                                        "content": [{"type": "text", "text": result_json}],
                                        "isError": False
                                    }
                                }
                                await ws.send(json.dumps(build_response(res)))

                            elif tool_name == "connect_bluetooth_speaker":
                                print("-> [MCP Call] Lệnh: KẾT NỐI LOA BLUETOOTH")
                                ok = send_cmd_to_esp32_bt("/api/connect")
                                text = "Đã gửi lệnh kết nối tới Loa Bluetooth của bạn rồi nha!" if ok else "Đã kích hoạt kết nối loa Bluetooth!"
                                res = {
                                    "jsonrpc": "2.0", "id": req_id,
                                    "result": {
                                        "content": [{"type": "text", "text": text}],
                                        "isError": False
                                    }
                                }
                                await ws.send(json.dumps(build_response(res)))

                            elif tool_name == "disconnect_bluetooth_speaker":
                                print("-> [MCP Call] Lệnh: NGẮT KẾT NỐI LOA BLUETOOTH")
                                ok = send_cmd_to_esp32_bt("/api/disconnect")
                                text = "Đã ngắt kết nối với Loa Bluetooth rồi ạ!" if ok else "Đã tắt kết nối Loa Bluetooth!"
                                res = {
                                    "jsonrpc": "2.0", "id": req_id,
                                    "result": {
                                        "content": [{"type": "text", "text": text}],
                                        "isError": False
                                    }
                                }
                                await ws.send(json.dumps(build_response(res)))

                            elif tool_name == "play_bluetooth_speaker":
                                print(f"-> [MCP Call] Mở nhạc LOA BLUETOOTH: {query}")
                                encoded_query = urllib.parse.quote(query)
                                audio_stream_url = f"{domain}/play_bt?q={encoded_query}"

                                send_cmd_to_esp32_bt(f"/play?url={urllib.parse.quote(audio_stream_url)}")

                                result_text = f"Đã mở bài '{query}' phát qua loa Bluetooth cho anh rồi nha!"
                                res = {
                                    "jsonrpc": "2.0", "id": req_id,
                                    "result": {
                                        "content": [{"type": "text", "text": result_text}],
                                        "isError": False
                                    }
                                }
                                await ws.send(json.dumps(build_response(res)))

                            elif tool_name == "stop_bluetooth_speaker":
                                print("-> [MCP Call] Dừng phát bài hát trên Loa Bluetooth")
                                send_cmd_to_esp32_bt("/stop")
                                res = {
                                    "jsonrpc": "2.0", "id": req_id,
                                    "result": {
                                        "content": [{"type": "text", "text": "Đã dừng phát nhạc trên loa Bluetooth!"}],
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
