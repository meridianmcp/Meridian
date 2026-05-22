import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
try: os.remove('c6.py')
except: pass
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'sprint rewind tab, goal natural scroll, task queue actions, handoff update, rewind sprint_items_pending'])
subprocess.run(['git', 'push'])
