@echo off
set PATH=%PATH%;%USERPROFILE%\AppData\Local\Programs\Git\bin
cd /d C:\Users\13144\Documents\Meridian\repository
git add ROADMAP.md
git commit -m "roadmap: add v0.3.1 devlog — append_devlog/get_devlog MCP tools"
git log --oneline -3
