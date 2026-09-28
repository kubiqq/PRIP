@echo off
cd /d "%~dp0"
python prip_update.py >> out\update.log 2>&1
