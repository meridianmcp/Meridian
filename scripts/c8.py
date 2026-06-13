import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
try: os.remove('c7.py')
except: pass
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'add Dockerfile, fly.toml, .dockerignore, GitHub Actions deploy workflow'])
subprocess.run(['git', 'push'])
