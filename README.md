# Lyria Realtime Music Bridge

This project connects heart-rate data, facial emotion data, LM Studio/Gemma, Unreal Engine, and Google's Lyria realtime music API.

## Python version

This project was developed and tested with:

```powershell
Python 3.13.0
```

Use Python 3.13 on a new machine if possible. Python 3.12 may work, but it has not been verified for this project.

## Install Python on a new Windows machine

1. Download and install Python 3.13 from the official Python website.
2. During installation, enable **Add python.exe to PATH**.
3. Open PowerShell and confirm Python is available:

```powershell
python --version
```

Expected result:

```text
Python 3.13.x
```

## Set up the project

Clone the repository, then run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks virtual environment activation, run PowerShell as your user and execute:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Then activate `.venv` again.

## Configure API key

Copy the example environment file:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and replace the placeholder with your real Gemini API key:

```env
GEMINI_API_KEY=your_gemini_api_key_here
```

Do not commit `.env` to GitHub.

## Runtime ports

This project expects local services on these ports:

```text
Lyria websocket server: ws://localhost:54321
Heart-rate websocket source: ws://127.0.0.1:54320
DeepFace emotion TCP source: 127.0.0.1:54322
LM Studio API: http://127.0.0.1:1234/api/v1/chat
```

## Run

Open separate terminals as needed.

Start the Lyria realtime server:

```powershell
.\.venv\Scripts\Activate.ps1
python LyriaToUnreal_00.py
```

Start the heart-rate bridge:

```powershell
.\.venv\Scripts\Activate.ps1
python HeartToUnreal.py
```

Start the Gemma orchestration bridge:

```powershell
.\.venv\Scripts\Activate.ps1
python Gemma_Transpiler.py
```

The DeepFace emotion server is a separate project. Start it before or alongside `Gemma_Transpiler.py` so the emotion listener can connect to `127.0.0.1:54322`.

## Notes

- Runtime audio/session data is written under `data/` and is intentionally not stored in Git.
- `.env` contains secrets and is intentionally ignored.
- Test and experiment files are intentionally ignored.
- `PyAudio` may require extra system audio dependencies on some Windows machines. If installation fails, install a Python 3.13 compatible wheel for `PyAudio` and then rerun `pip install -r requirements.txt`.
