import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
for f in ['c2.py', 'c3.py']:
    try: os.remove(f)
    except: pass
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'cleanup helpers'])
subprocess.run(['git', 'push'])
