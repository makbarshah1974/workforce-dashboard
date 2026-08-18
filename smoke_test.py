import sys
sys.path.insert(0, r'C:\Users\user\Desktop\workforce-dashboard')
import app as a

# Ensure tables exist for the test
with a.app.app_context():
    a.db.create_all()

client = a.app.test_client()

# 1. login page
r = client.get('/login')
assert r.status_code == 200, r.status_code

# 2. wrong password
r = client.post('/login', data={'password': 'wrong'})
assert r.status_code == 200  # stays on login

# 3. correct password
r = client.post('/login', data={'password': 'admin123'}, follow_redirects=True)
assert r.status_code == 200
assert b'Workforce' in r.data

# 4. dashboard page
r = client.get('/')
assert r.status_code == 200

# 5. upsert a record
payload = {
    'record_date': '2026-08-13',
    'total_workforce': 120,
    'metex_staff': 20, 'csk_staff': 25, 'topquality_staff': 15,
    'bestcare_staff': 18, 'prestige_staff': 22,
    'working_machines': 8, 'out_of_order_machines': 2,
    'working_machine_names': 'Excavator-1, Crane-2, Loader-3',
    'out_of_order_machine_names': 'Drill-5, Mixer-9'
}
r = client.post('/api/record', json=payload)
assert r.status_code == 200, r.status_code
data = r.get_json()
assert data['record']['total_workforce'] == 120
assert data['record']['working_machines'] == 8

# 6. get the record back
r = client.get('/api/record?date=2026-08-13')
assert r.status_code == 200
assert r.get_json()['record']['prestige_staff'] == 22

# 7. history
r = client.get('/api/history?limit=5')
assert r.status_code == 200
assert len(r.get_json()['records']) >= 1

# 8. dates
r = client.get('/api/dates')
assert r.status_code == 200
assert '2026-08-13' in r.get_json()['dates']

# 9. unauthenticated access redirects
with a.app.test_client() as c2:
    r = c2.get('/')
    assert r.status_code == 302

print('ALL_TESTS_PASSED')