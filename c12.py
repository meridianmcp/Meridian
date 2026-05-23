import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
for f in ['c11.py', 'deploy_secrets.py']:
    try: os.remove(f)
    except: pass
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'update claude code prompt + secrets example + gitignore secrets.env'])
subprocess.run(['git', 'push'])
