import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
r = subprocess.run(['git', 'add', '-A'])
r = subprocess.run(['git', 'commit', '-m', 'chore: gitignore bat files, repo clean'])
print('done', r.returncode)
