import asyncio
import json
import websockets
import requests
import time
import csv
import os
from datetime import datetime

# --- 配置 ---
LM_STUDIO_URL = "http://127.0.0.1:1234/api/v1/chat"
LYRIA_SERVER_WS = "ws://localhost:54321"
HR_WS_SOURCE = "ws://127.0.0.1:54320"
EMOTION_PORT = 54322

# --- DEBUG 模式配置 ---
DEBUG_MODE = False  # 设置为 True 则启动模拟数据模式  False True
DEBUG_DATA = {
    "bpm": 120,
    "smoothed_bpm": 118,
    "state": "tense",      # 可选: calm, normal, tense
    "emotion": "fear"      # 可选: neutral, happiness, surprise, fear, anger, etc.
}

# --- 文件夹同步逻辑 ---
def sync_session_path():
    if os.path.exists("current_session.tmp"):
        with open("current_session.tmp", "r") as f:
            path = f.read().strip()
            if os.path.exists(path):
                return path

    today = datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.now().strftime("Session_%H%M%S")
    path = os.path.join("data", today, now_time)
    os.makedirs(path, exist_ok=True)
    with open("current_session.tmp", "w") as f:
        f.write(path)
    return path


SESSION_PATH = sync_session_path()
LOG_FILE = os.path.join(SESSION_PATH, "experiment_data.csv")

bio_state = {
    "bpm": 75, "smoothed_bpm": 75, "state": "normal",
    "emotion": "neutral", "last_prompt": "None"
}


class ExperimentLogger:
    def __init__(self, filename):
        self.filename = filename
        self.fieldnames = ["timestamp", "event_type", "bpm", "smoothed_bpm", "hr_state", "emotion", "prompt"]

    def log(self, event_type, data_dict=None):
        if data_dict is None: data_dict = {}
        file_exists = os.path.isfile(self.filename)
        row = {
            "timestamp": time.time(),
            "event_type": event_type,
            "bpm": data_dict.get("bpm", bio_state["bpm"]),
            "smoothed_bpm": data_dict.get("smoothed_bpm", bio_state["smoothed_bpm"]),
            "hr_state": data_dict.get("state", bio_state["state"]),
            "emotion": bio_state["emotion"],
            "prompt": bio_state["last_prompt"]
        }
        with open(self.filename, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=self.fieldnames)
            if not file_exists: writer.writeheader()
            writer.writerow(row)


logger = ExperimentLogger(LOG_FILE)


async def hr_listener():
    print(f"[*] 正在连接心率源: {HR_WS_SOURCE}...")
    while True:
        try:
            async with websockets.connect(HR_WS_SOURCE) as websocket:
                async for message in websocket:
                    data = json.loads(message)
                    if data.get("type") == "heart_rate_event":
                        if DEBUG_MODE:
                            # 如果是 Debug 模式，强制覆盖为模拟数据
                            bio_state.update({
                                "bpm": DEBUG_DATA["bpm"],
                                "smoothed_bpm": DEBUG_DATA["smoothed_bpm"],
                                "state": DEBUG_DATA["state"]
                            })
                        else:
                            # 正常模式：使用实时传感器数据
                            bio_state.update({
                                "bpm": data["bpm"],
                                "smoothed_bpm": data["smoothed_bpm"],
                                "state": data["state"]
                            })
                        logger.log("BIO_UPDATE", bio_state)  # 这样 log 记录的也是你设定的 debug 值
        except:
            await asyncio.sleep(5)


async def emotion_listener():
    print(f"[*] 正在连接表情源: 127.0.0.1:        ...")
    while True:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", EMOTION_PORT)
            while True:
                line = await reader.readline()
                if not line: break
                data = json.loads(line.decode("utf-8"))
                if DEBUG_MODE:
                    bio_state["emotion"] = DEBUG_DATA["emotion"]
                else:
                    bio_state["emotion"] = data.get("dominant", "neutral")
        except:
            await asyncio.sleep(5)


async def gemma_orchestrator():
    while True:
        listener_task = None
        try:
            # 1. 建立连接
            async with websockets.connect(LYRIA_SERVER_WS) as lyria_ws:
                print(" Gemma 已连入 Lyria 服务端")

                # 2. 定义内部监听函数
                async def listen_for_ue5_context(ws):
                    try:
                        async for message in ws:
                            try:
                                data = json.loads(message)
                                # 只有包含 scene 的消息才更新环境上下文
                                if "scene" in data:
                                    new_scene = data["scene"]
                                    bio_state["ue5_scene"] = data["scene"]
                                    print(f" [UE5环境更新]: {data['scene']}")

                                print("\n" + "=" * 40)
                                print(f"📡 [DEBUG] 收到来自 UE5 的环境更新:")
                                print(f"   内容: {new_scene}")
                                print(f"   时间: {asyncio.get_event_loop().time():.2f}")
                                print("=" * 40 + "\n")

                            except json.JSONDecodeError:
                                pass
                    except websockets.ConnectionClosed:
                        print("[-] 环境监听连接断开")

                # 3. 启动并跟踪后台监听任务
                listener_task = asyncio.create_task(listen_for_ue5_context(lyria_ws))

                # 4. 主循环：每12秒请求一次 Gemma
                while True:
                    await asyncio.sleep(12)

                    prompt_input = (
                        f"Physiological State: {bio_state['state']}, "
                        f"Heart Rate: {bio_state['smoothed_bpm']} BPM, "
                        f"Facial Emotion: {bio_state['emotion']}, "
                        f"Current Environment: {bio_state.get('ue5_scene', 'Default Forest')}"
                    )

                    # --- 关键修正：适配你 LM Studio 的 'input' 格式 ---
                    # 将 System Prompt 和 User Input 拼接成一个字符串
                    full_text_input = (
                        f"System: Output ONLY: [Genre], [BPM], [Instruments], [Mood]. "
                        f"Mandatory: Always include 'Piano' in the [Instruments] section.\n"
                        f"User: {prompt_input}\n"
                        f"Assistant:"
                    )

                    payload = {
                        "model": "google/gemma-3-1b",
                        "input": full_text_input  # 使用之前报错要求的 input 字段
                    }

                    try:
                        # 使用线程池处理同步请求，避免阻塞异步循环
                        response = await asyncio.to_thread(
                            requests.post, LM_STUDIO_URL, json=payload, timeout=5
                        )

                        if response.status_code == 200:
                            res_data = response.json()

                            # 注意：使用 'input' 格式时，返回结构可能与 'messages' 格式不同
                            # 如果 res_data 里直接有 "output"，请按以下逻辑取值：
                            if "output" in res_data:
                                # 某些版本返回的是 {"output": [{"content": "..."}]}
                                new_style = res_data["output"][0]["content"].strip()
                            else:
                                # 或者是兼容 OpenAI 格式的 choices
                                new_style = res_data['choices'][0]['message']['content'].strip()

                            if new_style != bio_state["last_prompt"]:
                                bio_state["last_prompt"] = new_style
                                # 发送给 Lyria 服务端进行实时音乐更新
                                await lyria_ws.send(json.dumps({
                                    "style": new_style,
                                    "bpm": bio_state["smoothed_bpm"]
                                }))
                                logger.log("GEN_PROMPT", bio_state)
                                print(f" [Gemma]: {new_style}")
                        else:
                            print(f" LM Studio 状态码异常: {response.status_code}, 内容: {response.text}")

                    except Exception as e:
                        print(f" Gemma 请求异常: {e}")

        except Exception as e:
            print(f" 连接异常，5秒后重试: {e}")
            if listener_task:
                listener_task.cancel()  # 连接断开时取消后台监听任务
            await asyncio.sleep(5)


async def main():
    if DEBUG_MODE:
        print("\n" + "!" * 40)
        print(f"!!! DEBUG 模式已开启 !!!")
        print(f"使用固定数据: {DEBUG_DATA}")
        print("!" * 40 + "\n")

    print(f" 实验日志路径: {LOG_FILE}")
    await asyncio.gather(hr_listener(), emotion_listener(), gemma_orchestrator())


if __name__ == "__main__":
    asyncio.run(main())