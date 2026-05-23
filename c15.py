import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
try: os.remove('c14.py')
except: pass
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'update claude code prompt v2.2 megarun — simplified pricing, thorough docs, github oauth, demo overhaul'])
subprocess.run(['git', 'push'])
