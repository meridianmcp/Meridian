@echo off
set PATH=%PATH%;%USERPROFILE%\AppData\Local\Programs\Git\bin
cd /d C:\Users\13144\Documents\Meridian\repository
git add AGENTS.md
git commit -m "docs: add AGENTS.md for Commander workers"
git log --oneline -3
