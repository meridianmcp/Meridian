import subprocess, os
os.chdir(r'C:\Users\13144\Documents\Meridian\repository')
subprocess.run(['git', 'add', '-A'])
subprocess.run(['git', 'commit', '-m', 'waitlist endpoint, drop chat tables, contrib.rocks, devlog font, schema test'])
subprocess.run(['git', 'push'])
