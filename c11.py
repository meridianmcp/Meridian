import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
for f in ['fix_gate.py', 'fix_gate2.py', 'c10.py']:
    try: os.remove(f)
    except: pass
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'fix password gate — add Request type annotations'])
subprocess.run(['git', 'push'])
