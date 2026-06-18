import asyncio
import json
import websockets
import pyaudio
import sys
import io
import os
import aioconsole
import wave
import time
from google import genai
from google.genai import types
from dotenv import load_dotenv

# --- 环境配置 ---
load_dotenv()
GOOGLE_API_KEY = os.getenv("GEMINI_API_KEY")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
MODEL_ID = 'models/lyria-realtime-exp'
WS_HOST = "localhost"
WS_PORT = 54321

client = genai.Client(api_key=GOOGLE_API_KEY, http_options={'api_version': 'v1alpha'})

def get_session_path():
    if os.path.exists("current_session.tmp"):
        with open("current_session.tmp", "r") as f:
            path = f.read().strip()
            if os.path.exists(path):
                # 使用 abspath 确保它是一个完整的绝对路径，避免相对路径起点错误
                # 使用 normpath 确保斜杠方向在 Windows/Mac 上都能正确解析
                return os.path.abspath(os.path.normpath(path))

    # 兜底逻辑：如果没找到，就自己创建一个
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.now().strftime("Session_%H%M%S")
    path = os.path.join("data", today, now_time)
    os.makedirs(path, exist_ok=True)
    with open("current_session.tmp", "w") as f:
        f.write(path)
    return os.path.abspath(os.path.normpath(path))

# --- 2. 拼接完整的文件路径 ---
SESSION_PATH = get_session_path()
# 确保文件名拼接时，SESSION_PATH 是一个可靠的目录前缀
WAV_FILENAME = os.path.join(SESSION_PATH, "session_audio.wav")

# --- 音频播放初始化 ---
p = pyaudio.PyAudio()
stream = p.open(format=pyaudio.paInt16, channels=2, rate=48000,frames_per_buffer=2048, output=True)

# --- 初始化 WAV 文件记录 ---
wav_file = wave.open(WAV_FILENAME, 'wb')
wav_file.setnchannels(2)
wav_file.setframerate(48000)
# 这里调用 p 实例的方法：paInt16 对应的是 2 字节
wav_file.setsampwidth(p.get_sample_size(pyaudio.paInt16))

print(f"[*] 目标保存路径确认: {WAV_FILENAME}")

# --- 全局状态管理 ---
music_state = {
    "prompts": "Lo-fi Hip Hop",
    "bpm": 80,
    "session": None,
    "is_active": False
}

start_signal = asyncio.Event()


# --- 任务 1：处理指令 ---
async def update_controller(data):
    global music_state
    if not music_state["session"]: return
    try:
        if "style" in data:
            print(f"[控制] 切换风格: {data['style']}")
            music_state["prompts"] = data["style"]
            await music_state["session"].set_weighted_prompts(
                prompts=[types.WeightedPrompt(text=data["style"], weight=1.5)]
            )
        if "bpm" in data:
            new_bpm = int(data["bpm"])
            print(f"[控制] 调节 BPM: {new_bpm}")
            music_state["bpm"] = new_bpm
            await music_state["session"].set_music_generation_config(config={'bpm': new_bpm})
    except Exception as e:
        print(f"[控制错误]: {e}")


connected_clients = set()


async def ue5_command_handler(websocket):
    print(f"[网络] 新连接已建立: {websocket.remote_address}")
    connected_clients.add(websocket)
    try:
        async for message in websocket:
            try:
                # 1. 收到消息立即打印，用于检查原始数据
                print(f"--- [收到消息] ---")
                print(f"原始内容: {message}")

                data = json.loads(message)
                cmd = data.get("command")

                # 2. 处理控制指令 (start/stop)
                if cmd == "start":
                    music_state["is_active"] = True
                    start_signal.set()
                elif cmd == "stop":
                    music_state["is_active"] = False
                    start_signal.clear()

                # 3. 处理环境上下文更新
                # 修改判断逻辑：只要有 scene 字段就尝试转发
                elif cmd == "update_context" or "scene" in data:
                    scene_text = data.get('scene', '无内容')
                    print(f" [识别成功] 环境场景: {scene_text[:50]}...")  # 只打印前50个字符避免刷屏

                    # 转发逻辑
                    if len(connected_clients) > 1:
                        print(f"正在转发给其他 {len(connected_clients) - 1} 个客户端...")
                        tasks = [
                            client.send(message)
                            for client in connected_clients
                            if client != websocket
                        ]
                        if tasks:
                            await asyncio.gather(*tasks)
                    else:
                        print(" [转发警告] 当前没有其他客户端（如 Gemma_Transpiler）连入，消息无法转发")

                # 4. 其他指令
                else:
                    await update_controller(data)

            except json.JSONDecodeError:
                print(f" [数据解析错误] 收到非 JSON 格式消息: {message}")
            except Exception as e:
                print(f" [处理错误]: {e}")
    finally:
        connected_clients.remove(websocket)
        print(f"[网络] 连接断开: {websocket.remote_address}")


async def local_input_handler():
    print("[控制台] 输入 'start', 'stop' 或风格内容。")
    while True:
        try:
            line = await aioconsole.ainput("本地指令 >> ")
            user_input = line.strip()
            if user_input.lower() == 'start':
                music_state["is_active"] = True
                start_signal.set()
            elif user_input.lower() == 'stop':
                music_state["is_active"] = False
                start_signal.clear()
            elif user_input != "":
                await update_controller({"style": user_input})
        except Exception as e:
            print(f"[本地错误]: {e}")


# --- 任务 2：音频接收、播放与【保存】 ---
async def audio_receiver_task(session):
    print("[系统] Lyria 音频接收任务已就绪...")
    try:
        async for message in session.receive():
            if not music_state["is_active"]:
                break
            if message.server_content and message.server_content.audio_chunks:
                chunk = message.server_content.audio_chunks[0].data
                if chunk:
                    # 1. 实时播放
                    stream.write(chunk)
                    # 2. 实时写入 WAV 文件记录
                    wav_file.writeframes(chunk)
    except Exception as e:
        print(f"[系统] 音频流中断: {e}")


# async def audio_receiver_task(session):
#     print("[系统] 开始延迟诊断测试...")
#     last_chunk_time = time.time()
#
#     try:
#         async for message in session.receive():
#             if not music_state["is_active"]: break
#
#             if message.server_content and message.server_content.audio_chunks:
#                 now = time.time()
#                 # 计算两个数据块之间的到达间隔
#                 delta = now - last_chunk_time
#                 last_chunk_time = now
#
#                 # 如果间隔超过 0.5 秒，说明网络或服务器端有明显的阻塞
#                 if delta > 0.5:
#                     print(f" 严重卡顿检测: 间隔 {delta:.2f}s | 状态: {music_state['prompts'][:20]}")
#
#                 chunk = message.server_content.audio_chunks[0].data
#                 if chunk:
#                     stream.write(chunk)
#                     wav_file.writeframes(chunk)
#     except Exception as e:
#         print(f"[系统] 诊断中断: {e}")

# --- 任务 3：Lyria 主循环 ---
async def lyria_main_task():
    while True:
        if not music_state["is_active"]:
            print("[等待] 正在等待 'start' 指令...")
            await start_signal.wait()

        try:
            async with client.aio.live.music.connect(model=MODEL_ID) as session:
                music_state["session"] = session
                await session.set_weighted_prompts(
                    prompts=[types.WeightedPrompt(text=music_state["prompts"], weight=1.0)])
                await session.set_music_generation_config(config={'bpm': music_state['bpm']})
                await session.play()
                print(f"[系统] Lyria 已连接: {music_state['prompts']}")

                await audio_receiver_task(session)
                music_state["session"] = None
        except Exception as e:
            print(f"[连接错误] Lyria 重连中: {e}")
            await asyncio.sleep(3)


# --- 启动 ---
async def main():
    await asyncio.gather(
        websockets.serve(ue5_command_handler, WS_HOST, WS_PORT),
        local_input_handler(),
        lyria_main_task()
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        # 显式关闭 WAV 文件，确保文件头被正确写入
        wav_file.close()
        print(f"[系统] 实验音频已保存至: {WAV_FILENAME}")
        stream.stop_stream()
        stream.close()
        p.terminate()