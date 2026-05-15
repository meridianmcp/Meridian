src = open('meridian/dashboard.py').read()
hits = [(i+1, l) for i, l in enumerate(src.splitlines()) if any(x in l for x in ['429', 'rate', 'Rate', 'model', 'Model', 'sendChat', 'status'])]
for n, l in hits:
    print(n, l)
