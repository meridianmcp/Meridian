import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
try: os.remove('c4.py')
except: pass
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'conn modal toggle colors, toml path link, DELETE endpoint, Neon waitlist table'])
subprocess.run(['git', 'push'])
