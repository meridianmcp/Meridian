lines = open('meridian/dashboard.py', encoding='utf-8').readlines()
for i, l in enumerate(lines):
    if any(x in l for x in ['north_star','sprint','version_goal','textarea','Save','dirty','goal-section','loadGoal','saveGoal','fetchGoal']):
        print(i+1, l.rstrip())
