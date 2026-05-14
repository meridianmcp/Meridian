@echo off
set PATH=%PATH%;%USERPROFILE%\.cargo\bin;%USERPROFILE%\AppData\Local\Programs\Git\bin
cd /d C:\Users\13144\Documents\Meridian\repository
git add DEVLOG.md
git commit -m "docs: add DEVLOG.md — incident log and architecture decisions"
git log --oneline -3
