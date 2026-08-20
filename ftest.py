import json, urllib.request, re
base = 'http://localhost:5000/api/reports'

# extract session cookie value from Netscape cookie file
session = None
for line in open('cookies.txt'):
    if line.strip().startswith('#') or not line.strip():
        continue
    parts = line.split('\t')
    if len(parts) >= 7 and parts[5] == 'session':
        session = parts[6].strip()
        break

def get(q):
    req = urllib.request.Request(base + '?' + q, headers={'Cookie': 'session=' + session})
    return json.load(urllib.request.urlopen(req))

m = get('date_from=2026-08-15&date_to=2026-08-19&machine=Machine%201')
print('machine=Machine 1 count=', m['count'], 'all_match=', all(e['machine'] == 'Machine 1' for e in m['events']))

s = get('date_from=2026-08-15&date_to=2026-08-19&status=running')
print('status=running count=', s['count'], 'all_match=', all(e['status'] == 'running' for e in s['events']))

sh = get('date_from=2026-08-15&date_to=2026-08-19&shift=Day')
print('shift=Day count=', sh['count'], 'all_match=', all(e['shift'] == 'Day' for e in sh['events']))

se = get('date_from=2026-08-15&date_to=2026-08-19&search=Machine%202')
print('search=Machine 2 count=', se['count'], 'all_match=', all('Machine 2' in e['machine'] for e in se['events']))