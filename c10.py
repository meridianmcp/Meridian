import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
try: os.remove('c9.py')
except: pass
try: os.remove('patch_gate.py')
except: pass
try: os.remove('check_server.py')
except: pass
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'add site password gate middleware — locks all routes when SITE_PASSWORD is set'])
subprocess.run(['git', 'push'])
