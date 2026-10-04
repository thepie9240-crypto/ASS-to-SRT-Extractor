@echo off
cd /d "%~dp0"
pip install -r requirements.txt
pyinstaller --noconfirm --onefile --windowed --name "ASS to SRT Extractor" --collect-all tkinterdnd2 ass_to_srt_extractor.py
echo Done - see dist\ASS to SRT Extractor.exe
pause
