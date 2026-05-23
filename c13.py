import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
try: os.remove('c12.py')
except: pass
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'update claude code prompt v2.1 — demo route + MCP session startup + no license changes'])
subprocess.run(['git', 'push'])
