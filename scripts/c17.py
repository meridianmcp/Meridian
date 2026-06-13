import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
try: os.remove('c16.py')
except: pass
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'add chinampa_churchill.md vision doc + final overnight prompt v2.2'])
subprocess.run(['git', 'push'])
