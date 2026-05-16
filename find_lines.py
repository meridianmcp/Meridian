import sys
sys.stdout.reconfigure(encoding='utf-8')
lines = open('meridian/dashboard.py', encoding='utf-8').readlines()
for i, l in enumerate(lines):
    if any(x in l for x in ['localhost', '7878', '7700', 'ws://', "'/api", 'fetch(']):
        print(i+1, l.rstrip())
