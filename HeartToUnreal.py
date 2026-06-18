import asyncio
import json
import statistics

from bleak import BleakClient
import websockets
import aioconsole

DEVICE_ADDRESS = "24:AC:AC:0E:DD:2D"
WATCH_ADDRESS = "58:90:BF:55:28:33"
HR_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


baseline_hr = 91  # 静息心率（后面可以动态测）
is_calibrating = False  # 是否正在测量静息心率
calibration_data = []   # 存放测量期间的心率样本
current_state = None  # 当前状态（避免重复发送）
last_sent_time = 0
latest_hr = None
smoothed_hr = None  # 平滑后的心率
SMOOTH_ALPHA = 0.2  # 平滑系数（0.1~0.3都可以）

connected_clients = set()


def parse_heart_rate(data: bytearray) -> int:
    flags = data[0]
    hr_16bit = flags & 0x01
    if hr_16bit:
        return int.from_bytes(data[1:3], byteorder="little")
    return data[1]

def get_hr_state(hr: int) -> str:
    delta = hr - baseline_hr

    if delta < -5:
        return "calm"
    elif delta > 10:
        return "tense"
    else:
        return "normal"

def update_smoothed_hr(new_hr: int):
    global smoothed_hr

    if smoothed_hr is None:
        smoothed_hr = new_hr
    else:
        smoothed_hr = (1 - SMOOTH_ALPHA) * smoothed_hr + SMOOTH_ALPHA * new_hr


def get_hr_state_with_hysteresis(hr: float, prev_state: str):
    delta = hr - baseline_hr

    # ---- 如果之前是 tense ----
    if prev_state == "tense":
        if delta <= 8:   # 退出阈值（比进入低）
            return "normal"
        return "tense"

    # ---- 如果之前是 calm ----
    if prev_state == "calm":
        if delta >= -4:
            return "normal"
        return "calm"

    # ---- 当前是 normal 或 None ----
    if delta >= 12:
        return "tense"
    elif delta <= -8:
        return "calm"
    else:
        return "normal"

async def broadcast_hr():
    global latest_hr, current_state

    while True:
        # 只要有数据且有客户端连接就发送，不再判断状态是否改变
        if latest_hr is not None and connected_clients:

            # 1. 更新平滑心率
            update_smoothed_hr(latest_hr)

            # 2. 依然保留状态判断逻辑，用于给 Gemma 提供参考
            new_state = get_hr_state_with_hysteresis(
                smoothed_hr,
                current_state
            )
            current_state = new_state

            # 3. 构造消息：现在每一轮循环都会发送
            message = json.dumps({
                "type": "heart_rate_event",
                "state": current_state,
                "bpm": int(latest_hr),           # 实时原始心率
                "smoothed_bpm": int(smoothed_hr) # 平滑后的心率（推荐给 Lyria 用这个）
            })

            # 打印调试，确认它在持续输出
            # print(f"[SENDING] {message}")

            dead_clients = set()
            for ws in connected_clients:
                try:
                    await ws.send(message)
                except:
                    dead_clients.add(ws)
            connected_clients.difference_update(dead_clients)

        # 保持 0.5 秒一次的发送频率，这对于音乐同步已经足够了
        await asyncio.sleep(0.5)

def hr_notification_handler(sender, data):
    global latest_hr
    latest_hr = parse_heart_rate(data)
    #print(f"Heart Rate: {latest_hr} bpm")


async def polar_task():
    while True:
        try:
            print("Connecting to Polar H10...")
            async with BleakClient(DEVICE_ADDRESS) as client:
                print("Polar connected.")
                await client.start_notify(HR_CHAR_UUID, hr_notification_handler)
                while True:
                    await asyncio.sleep(1)
        except Exception as e:
            print("Polar connection error:", e)
            print("Retrying in 3 seconds...")
            await asyncio.sleep(3)


async def heart_rate_task():
    global latest_hr
    while True:
        try:
            # 扫描逻辑：自动判断哪个设备在线
            print("\n[*] 正在寻找心率源 (Polar 或 Apple Watch)...")

            # 这里简单起见，按优先级尝试：如果有 Polar 连 Polar，没有则连 Watch
            # 也可以通过 BleakScanner 扫描名字来自动切换
            target_addr = WATCH_ADDRESS  # 默认先尝试 Apple Watch

            print(f"[*] 尝试连接设备: {target_addr}")
            async with BleakClient(target_addr, timeout=15.0) as client:
                print(f"✅ 设备已连接: {target_addr}")

                # 动态获取特征值对象
                target_char = None
                for service in client.services:
                    for char in service.characteristics:
                        if char.uuid.lower() == HR_CHAR_UUID.lower():
                            target_char = char
                            break
                    if target_char: break

                if target_char:
                    print(f"[*] 成功锁定心率句柄: {target_char.handle}，开始订阅...")
                    await client.start_notify(target_char, hr_notification_handler)

                    while client.is_connected:
                        await asyncio.sleep(1)
                else:
                    print("❌ 错误：未能在该设备上发现标准心率特征。")

        except Exception as e:
            print(f"[-] 连接异常: {e}")
            print("[*] 3秒后尝试重新寻找心率源...")
            await asyncio.sleep(3)


async def ws_handler(websocket):
    connected_clients.add(websocket)
    print("WebSocket client connected.")
    try:
        await websocket.wait_closed()
    finally:
        connected_clients.discard(websocket)
        print("WebSocket client disconnected.")


async def broadcast_hr():
    global latest_hr, current_state, is_calibrating, calibration_data
    while True:
        if latest_hr is not None:
            # 如果正在校准，只记录数据，不广播状态
            if is_calibrating:
                calibration_data.append(latest_hr)

            # 正常广播逻辑
            update_smoothed_hr(latest_hr)
            new_state = get_hr_state_with_hysteresis(smoothed_hr, current_state)
            current_state = new_state

            if connected_clients:
                message = json.dumps({
                    "type": "heart_rate_event",
                    "state": current_state,
                    "bpm": int(latest_hr),
                    "smoothed_bpm": int(smoothed_hr),
                    "is_calibrating": is_calibrating  # 告知下游当前在校准
                })
                # 广播给所有客户端 (Gemma_Transpiler 等)
                await asyncio.gather(*[ws.send(message) for ws in connected_clients], return_exceptions=True)

        await asyncio.sleep(0.5)


# --- 校准控制逻辑 ---
async def calibration_controller():
    global is_calibrating, calibration_data, baseline_hr
    print("\n[系统] 输入 'calibrate' 开始测量静息心率 (持续60秒)...")

    while True:
        cmd = await aioconsole.ainput(">> ")
        if cmd.strip().lower() == 'calibrate':
            if latest_hr is None:
                print(" 错误：尚未连接到 Polar H10，请先等待蓝牙连接。")
                continue

            print(" 正在开始静息心率校准，请保持平静状态，持续60秒...")
            is_calibrating = True
            calibration_data = []

            # 收集样本
            await asyncio.sleep(60)

            is_calibrating = False
            if len(calibration_data) > 0:
                baseline_hr = int(statistics.mean(calibration_data))
                print(f" 校准完成！")
                print(f" 样本数: {len(calibration_data)}")
                print(f" 新的静息心率基准已设定为: {baseline_hr} BPM")
            else:
                print(" 校准失败：未收集到足够数据。")

async def main():
    ws_server = await websockets.serve(ws_handler, "127.0.0.1", 54320)
    print("WebSocket server started at ws://127.0.0.1:54320")

    await asyncio.gather(
        polar_task(),
        broadcast_hr()
    )

async def main():
    # 启动 WebSocket 服务
    ws_server = await websockets.serve(ws_handler, "127.0.0.1", 54320)
    print("WebSocket server started at ws://127.0.0.1:54320")

    await asyncio.gather(
        polar_task(),             # 蓝牙接收
        broadcast_hr(),           # 数据广播
        calibration_controller()  # 命令行交互
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass