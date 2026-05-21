import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
import os as _os
_os.remove('commit_s7.py')
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'cleanup'])
subprocess.run(['git', 'push'])
