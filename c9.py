import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'update handoff doc — v2.0 hosted tier build plan'])
subprocess.run(['git', 'push'])
