import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'chore: final repo clean'])
