import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
os.remove('check_pw.py')
os.remove('debug_pw.py')
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'conn UI overhaul, screenshots, muted font fix, DELETE connections endpoint, hard refresh on restart'])
subprocess.run(['git', 'push'])
