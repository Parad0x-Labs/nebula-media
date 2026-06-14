# CLAUDE.md

Setup, run commands, codec requirements, and the file map live in **[AGENTS.md](AGENTS.md)**.
Read that first. Quick version:

```bash
# install (use a venv — PEP 668)
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
# ffmpeg is required for video (not for images/pages): brew install ffmpeg / apt install ffmpeg
bash scripts/check.sh          # verify deps + ffmpeg + codecs + smoke test

python -m nebula.web0 clip.mp4 --target x   # make a video X-uploadable
python -m nebula.web0 clip.mp4              # smallest AV1 for storage
```
