"""Builds timetable.json (per class, per day) from MASTER_SECTIONS in index.html.
Pattern follows the common CBSE-school layout: assembly, 7 periods Mon-Fri, short Saturday, two breaks.
Edit PLANS / SLOTS below, then run:  python generate_timetable.py"""
import json, random, re, pathlib
ROOT = pathlib.Path(__file__).parent
idx = (ROOT / 'index.html').read_text(encoding='utf-8')
a = idx.index('const MASTER_SECTIONS = ') + len('const MASTER_SECTIONS = '); b = idx.index('];', a) + 1
SECTIONS = json.loads(idx[a:b])
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
ASSEMBLY = ['Prayer & Pledge', 'News & Thought of the Day', 'House Assembly', 'Value Education Talk', 'Cultural Assembly', 'Club & Activity Briefing']
# (time, label, kind)  kind: asm / per / brk
MORNING = [[('06:55 - 07:15', 'Assembly', 'asm'), ('07:15 - 08:00', 'Period 1', 'per'), ('08:00 - 08:45', 'Period 2', 'per'), ('08:45 - 09:30', 'Period 3', 'per'), ('09:30 - 10:00', 'Recess', 'brk'), ('10:00 - 10:45', 'Period 4', 'per'), ('10:45 - 11:30', 'Period 5', 'per'), ('11:30 - 12:15', 'Period 6', 'per'), ('12:15 - 01:10', 'Period 7', 'per')],
           [('06:55 - 07:15', 'Assembly', 'asm'), ('07:15 - 07:55', 'Period 1', 'per'), ('07:55 - 08:35', 'Period 2', 'per'), ('08:35 - 08:55', 'Short Break', 'brk'), ('08:55 - 09:35', 'Period 3', 'per'), ('09:35 - 10:15', 'Period 4', 'per')]]
PRIMARY = [[('08:45 - 09:00', 'Assembly', 'asm'), ('09:00 - 09:40', 'Period 1', 'per'), ('09:40 - 10:20', 'Period 2', 'per'), ('10:20 - 10:30', 'Short Break', 'brk'), ('10:30 - 11:10', 'Period 3', 'per'), ('11:10 - 11:50', 'Period 4', 'per'), ('11:50 - 12:20', 'Lunch', 'brk'), ('12:20 - 01:00', 'Period 5', 'per'), ('01:00 - 01:40', 'Period 6', 'per'), ('01:40 - 01:50', 'Short Break', 'brk'), ('01:50 - 02:30', 'Period 7', 'per'), ('02:30 - 02:45', 'Dispersal', 'brk')],
           [('08:45 - 09:00', 'Assembly', 'asm'), ('09:00 - 09:40', 'Period 1', 'per'), ('09:40 - 10:20', 'Period 2', 'per'), ('10:20 - 10:30', 'Short Break', 'brk'), ('10:30 - 11:10', 'Period 3', 'per'), ('11:10 - 11:50', 'Period 4', 'per')]]
PREPRIMARY = [[('12:30 - 12:45', 'Circle Time', 'asm'), ('12:45 - 01:15', 'Activity 1', 'per'), ('01:15 - 01:45', 'Activity 2', 'per'), ('01:45 - 02:05', 'Snack Break', 'brk'), ('02:05 - 02:30', 'Activity 3', 'per'), ('02:30 - 02:45', 'Activity 4', 'per')],
              [('12:30 - 12:45', 'Circle Time', 'asm'), ('12:45 - 01:15', 'Activity 1', 'per'), ('01:15 - 01:45', 'Activity 2', 'per')]]
# subject: (periods per week, kind, teacher regex, room hint, priority 0 core / 1 mid / 2 light)
def S(n, kind='theory', rx='', room='', pr=1): return (n, kind, rx, room, pr)
LIGHT = dict(Library=S(2, 'cocurricular', '', 'Library', 2), **{'Value Education': S(2, 'cocurricular', '', '', 2)})
PLANS = {
 'pre_nursery': {'Alphabet Phonics': S(4, rx='phonic|alphabet', pr=0), 'Number Concepts': S(4, rx='number', pr=0), 'Rhymes & Storytelling': S(4, 'cocurricular', 'rhyme|story', pr=1), 'Art & Craft': S(3, 'cocurricular', 'art|craft', 'Fine Arts Studio', 2), 'Motor Skills & Play': S(4, 'sports', 'sport', 'Nursery Park', 2), 'Sensory Activity': S(3, 'cocurricular', 'sensory', 'Activity Room', 2)},
 'pre_prep': {'English Reading': S(4, rx='english|read', pr=0), 'Pre-Math': S(4, rx='number|math', pr=0), 'EVS & Story Time': S(3, rx='story|evs', pr=1), 'Hindi Varnamala': S(2, rx='hindi', pr=1), 'Rhymes & Music': S(3, 'cocurricular', 'music|vocal', 'Sangeet Bhavan', 2), 'Art & Craft': S(3, 'cocurricular', 'art|craft', 'Fine Arts Studio', 2), 'Motor Skills & Play': S(3, 'sports', 'sport', 'Play Area', 2)},
 'primary_low': {'English': S(6, rx='english', pr=0), 'Mathematics': S(6, rx='math', pr=0), 'Hindi': S(5, rx='hindi', pr=0), 'EVS': S(7, rx='evs|science', pr=1), 'Computer Basics': S(2, 'lab', 'comput', 'Computer Lab', 1), 'Art & Craft': S(2, 'cocurricular', 'art|craft', 'Fine Arts Studio', 2), 'Music': S(2, 'cocurricular', 'music|vocal', 'Sangeet Bhavan', 2), 'Physical Education': S(3, 'sports', 'sport|physical', 'Sports Arena', 2), 'Library': LIGHT['Library'], 'Value Education': LIGHT['Value Education'], 'General Knowledge': S(2, rx='', pr=2)},
 'primary_up': {'English': S(6, rx='english', pr=0), 'Mathematics': S(6, rx='math', pr=0), 'Hindi': S(5, rx='hindi', pr=0), 'EVS / Science': S(4, rx='evs|science', pr=1), 'Social Studies': S(3, rx='social', pr=1), 'Computer Science': S(2, 'lab', 'comput', 'Computer Lab', 1), 'Art & Craft': S(2, 'cocurricular', 'art|craft', 'Fine Arts Studio', 2), 'Music': S(2, 'cocurricular', 'music|vocal', 'Sangeet Bhavan', 2), 'Physical Education': S(3, 'sports', 'sport|physical', 'Sports Arena', 2), 'Library': LIGHT['Library'], 'Value Education': LIGHT['Value Education'], 'General Knowledge': S(2, rx='', pr=2)},
 'middle': {'English': S(5, rx='english', pr=0), 'Mathematics': S(6, rx='math', pr=0), 'Science': S(5, rx='science', pr=0), 'Science Lab Activity': S(2, 'lab', 'science', 'Science Lab', 1), 'Hindi': S(5, rx='hindi', pr=1), 'Sanskrit': S(3, rx='sanskrit|hindi', pr=1), 'Social Science': S(5, rx='social', pr=1), 'Computer Science': S(2, 'lab', 'comput', 'Computer Lab', 1), 'Art & Craft': S(1, 'cocurricular', 'art|craft', 'Fine Arts Studio', 2), 'Music': S(1, 'cocurricular', 'music|vocal', 'Sangeet Bhavan', 2), 'Physical Education': S(2, 'sports', 'sport|physical', 'Sports Arena', 2), 'Library': S(1, 'cocurricular', '', 'Library', 2), 'Value Education': S(1, 'cocurricular', '', '', 2)},
 'secondary': {'English': S(6, rx='english', pr=0), 'Mathematics': S(7, rx='math', pr=0), 'Physics': S(2, rx='physic|science', pr=0), 'Chemistry': S(2, rx='chem|science', pr=0), 'Biology': S(2, rx='bio|science', pr=0), 'Science Lab': S(1, 'lab', 'science|physic', 'Science Lab', 1), 'Hindi': S(5, rx='hindi', pr=1), 'History': S(2, rx='social|histor', pr=1), 'Geography': S(2, rx='social|geograph', pr=1), 'Civics & Economics': S(2, rx='social|econom', pr=1), 'AI / IT': S(3, 'lab', 'comput|artificial|ai', 'AI Hub Lab', 1), 'Physical Education': S(2, 'sports', 'sport|physical', 'Sports Arena', 2), 'Art / Music': S(1, 'cocurricular', 'art|music', 'Fine Arts Studio', 2), 'Library': S(1, 'cocurricular', '', 'Library', 2), 'Mentoring': S(1, 'cocurricular', '', '', 2)},
 'sci_pcm': {'English Core': S(5, rx='english', pr=0), 'Physics Theory': S(5, rx='physic', pr=0), 'Physics Practical': S(2, 'lab', 'physic', 'Physics Lab', 1), 'Chemistry Theory': S(5, rx='chem', pr=0), 'Chemistry Practical': S(2, 'lab', 'chem', 'Chemistry Lab', 1), 'Mathematics': S(7, rx='math', pr=0), 'Computer Science': S(4, rx='comput|python|informat', pr=1), 'CS Practical': S(2, 'lab', 'comput|python|informat', 'AI Hub Lab', 1), 'Physical Education': S(2, 'sports', 'sport|physical', 'Sports Arena', 2), 'Library': S(2, 'cocurricular', '', 'Library', 2), 'Mentorship': S(2, 'cocurricular', '', '', 2), 'Competitive Prep': S(1, rx='math|physic', pr=2)},
 'sci_pcb': {'English Core': S(5, rx='english', pr=0), 'Physics Theory': S(5, rx='physic', pr=0), 'Physics Practical': S(2, 'lab', 'physic', 'Physics Lab', 1), 'Chemistry Theory': S(5, rx='chem', pr=0), 'Chemistry Practical': S(2, 'lab', 'chem', 'Chemistry Lab', 1), 'Biology Theory': S(5, rx='bio', pr=0), 'Biology Practical': S(2, 'lab', 'bio', 'Biology Lab', 1), 'Biotechnology': S(4, rx='biotech|bio', pr=1), 'Physical Education': S(2, 'sports', 'sport|physical', 'Sports Arena', 2), 'Library': S(2, 'cocurricular', '', 'Library', 2), 'Mentorship': S(2, 'cocurricular', '', '', 2), 'Competitive Prep (NEET)': S(3, rx='bio|chem', pr=2)},
 'commerce': {'English Core': S(5, rx='english', pr=0), 'Accountancy': S(7, rx='account', pr=0), 'Business Studies': S(6, rx='business', pr=0), 'Economics': S(6, rx='econom', pr=0), 'Applied Mathematics': S(5, rx='math', pr=1), 'Informatics Practices': S(3, 'lab', 'informat|comput', 'AI Hub Lab', 1), 'Physical Education': S(2, 'sports', 'sport|physical', 'Sports Arena', 2), 'Library': S(2, 'cocurricular', '', 'Library', 2), 'Mentorship': S(2, 'cocurricular', '', '', 2), 'Competitive Prep (CUET)': S(1, rx='econom|business', pr=2)},
 'humanities': {'English Core': S(5, rx='english', pr=0), 'History': S(6, rx='histor', pr=0), 'Political Science': S(6, rx='polit|liberal|histor', pr=0), 'Economics': S(6, rx='econom', pr=1), 'Psychology': S(5, rx='psych', pr=1), 'Sociology': S(3, rx='sociolog|histor', pr=1), 'Physical Education': S(2, 'sports', 'sport|physical', 'Sports Arena', 2), 'Library': S(2, 'cocurricular', '', 'Library', 2), 'Mentorship': S(2, 'cocurricular', '', '', 2), 'Competitive Prep (CUET)': S(2, rx='histor|polit', pr=2)}}
GLOBAL = {'sport|physical': 'Coach Santosh Oraon', 'music|vocal': 'Mrs. S. Sengupta', 'art|craft': 'Mr. B. Pal'}
def plan_for(m):
    c = m['cls']
    if c == 'Nursery': return 'pre_nursery', PREPRIMARY
    if c == 'Prep': return 'pre_prep', PREPRIMARY
    if 'Science' in c and ('11' in c or '12' in c): return ('sci_pcb' if 'PCB' in m['sec'] else 'sci_pcm'), MORNING
    if 'Commerce' in c: return 'commerce', MORNING
    if 'Humanities' in c: return 'humanities', MORNING
    n = int(re.search(r'\d+', c).group())
    return ('primary_low' if n <= 2 else 'primary_up' if n <= 5 else 'middle' if n <= 8 else 'secondary'), (PRIMARY if n <= 5 else MORNING)
def teacher_for(m, subj, spec):
    rx = spec[2]; ct = re.sub(r'\s*\(.*\)', '', m['classTeacher'])
    if rx:
        for key, name in m['subjectTeachers'].items():
            if re.search(rx, key, re.I): return name
        for pat, name in GLOBAL.items():
            if re.search(pat, rx, re.I): return name
        return 'Subject Teacher (to be assigned)'
    return ct if ct and ct != 'To be assigned' else 'Class Teacher (to be assigned)'
def slug(m): return re.sub(r'[^a-z0-9]+', '-', ((m['cls'] + ' ' + (m['sec'][-1] if m['cls'] == 'Class 10' else '')).lower())).strip('-')
def deal(plan, caps, rng):
    for _ in range(500):
        subs = list(plan); rng.shuffle(subs); order = []
        for k in range(max(v[0] for v in plan.values())):
            order += [s for s in subs if plan[s][0] > k]
        days = [[] for _ in caps]; d = rng.randrange(len(caps))
        for s in order:
            for _ in range(len(caps)):
                if len(days[d]) < caps[d] and days[d].count(s) < (3 if plan[s][0] >= 7 else 2): days[d].append(s); d = (d + 1) % len(caps); break
                d = (d + 1) % len(caps)
            else: break
        if sum(map(len, days)) == sum(caps): return days
    raise RuntimeError('could not schedule')
busy = {}
out = []
for m in SECTIONS:
    key, tpl = plan_for(m); plan = PLANS[key]
    caps = [sum(1 for t in tpl[0] if t[2] == 'per')] * 5 + [sum(1 for t in tpl[1] if t[2] == 'per')]
    assert sum(v[0] for v in plan.values()) == sum(caps), (m['cls'], sum(v[0] for v in plan.values()), sum(caps))
    rng = random.Random(m['cls'] + m['sec']); best = None
    for attempt in range(60):
        days = deal(plan, caps, rng); clash = 0
        for di, day in enumerate(days):
            day.sort(key=lambda s: (plan[s][4], rng.random()))
            if di and day[0] == days[di - 1][0] and len(day) > 1: day[0], day[1] = day[1], day[0]
        for di, day in enumerate(days):
            for pi, s in enumerate(day):
                t = teacher_for(m, s, plan[s])
                if 'to be assigned' not in t and (di, pi, t) in busy: clash += 1
        if best is None or clash < best[0]: best = (clash, days)
        if clash == 0: break
    days = best[1]
    def clashes(ds):
        c = 0
        for di, day in enumerate(ds):
            for pi, s in enumerate(day):
                t = teacher_for(m, s, plan[s])
                if 'to be assigned' not in t and (di, pi, t) in busy: c += 1
            for s in set(day):
                if day.count(s) > (3 if plan[s][0] >= 7 else 2): c += 5
        return c
    cur = clashes(days)
    for _ in range(4000):
        if cur == 0: break
        d1, d2 = rng.randrange(6), rng.randrange(6)
        if d1 > 4 and d2 > 4 and False: continue
        p1, p2 = rng.randrange(len(days[d1])), rng.randrange(len(days[d2]))
        days[d1][p1], days[d2][p2] = days[d2][p2], days[d1][p1]
        new = clashes(days)
        if new <= cur: cur = new
        else: days[d1][p1], days[d2][p2] = days[d2][p2], days[d1][p1]
    best = (cur, days); sched = {}
    for di, day in enumerate(days):
        rows = []; pi = 0; shell = tpl[0] if di < 5 else tpl[1]
        for time, label, kind in shell:
            if kind == 'per':
                s = day[pi]; spec = plan[s]; t = teacher_for(m, s, spec)
                if 'to be assigned' not in t:
                    if (di, pi, t) in busy: t = 'Subject Teacher (to be assigned)'
                    else: busy[(di, pi, t)] = 1
                rows.append({'time': time, 'period': label, 'subject': s, 'teacher': t, 'room': spec[3] or m['room'], 'type': spec[1]}); pi += 1
            elif kind == 'asm':
                rows.append({'time': time, 'period': label, 'subject': ASSEMBLY[di] if label == 'Assembly' else 'Circle Time & Prayers', 'teacher': re.sub(r'\s*\(.*\)', '', m['classTeacher']) or 'House Staff', 'room': 'Main Quadrangle' if label == 'Assembly' else m['room'], 'type': 'cocurricular'})
            else:
                rows.append({'time': time, 'period': label, 'subject': 'Tiffin & Refreshment' if label != 'Dispersal' else 'Dispersal & Bus Boarding', 'teacher': 'Duty Staff', 'room': 'Campus Grounds', 'type': 'break'})
        sched[DAYS[di]] = rows
    out.append({'key': slug(m), 'label': m['displayName'], 'cls': m['cls'], 'sec': m['sec'], 'wing': m['wing'], 'timings': m['timings'], 'classTeacher': m['classTeacher'], 'room': m['room'], 'strength': m['count'], 'days': sched, 'clashes': best[0]})
(ROOT / 'timetable.json').write_text(json.dumps({'note': 'Sample allocation following the common CBSE pattern. Confirm with the school office before printing.', 'days': DAYS, 'classes': out}, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
print('classes', len(out), 'clashes', sum(c['clashes'] for c in out), 'keys', [c['key'] for c in out])
