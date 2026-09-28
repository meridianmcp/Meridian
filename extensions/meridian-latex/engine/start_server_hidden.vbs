' Launches the meridian-latex outline server with zero visible window.
' Used by the Windows Startup shortcut so the server is just always running
' -- no terminal, no manual "node src/server.js" ever again.
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\13144\Documents\meridian-latex\engine"
WshShell.Run "node src/server.js", 0, False
