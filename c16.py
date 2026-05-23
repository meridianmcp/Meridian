import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
try: os.remove('c15.py')
except: pass
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'final overnight prompt v2.2 — github oauth + neon architecture + mkdocs + pricing'])
subprocess.run(['git', 'push'])
