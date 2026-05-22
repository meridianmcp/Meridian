import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
try: os.remove('c5.py')
except: pass
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'goal tab fullscreen, session log collapsible, timeline filter auto-blocks, queue delete/backlog/done actions'])
subprocess.run(['git', 'push'])
