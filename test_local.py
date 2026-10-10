import requests
import random
import time

s = requests.Session()
s.headers.update({'User-Agent': 'Mozilla/5.0'})

# Get CSRF
r1 = s.get('http://127.0.0.1:8000/signup/')
csrf = s.cookies.get('csrftoken')

username = f"test_{random.randint(10000,99999)}"
data = {
    'csrfmiddlewaretoken': csrf,
    'username': username,
    'first_name': 'TestUser',
    'email': f'{username}@example.com',
    'password': 'Password123!',
    'password_confirm': 'Password123!',
    'relationship_mode': 'DATING'
}
r2 = s.post('http://127.0.0.1:8000/signup/', data=data, headers={'Referer': 'http://127.0.0.1:8000/signup/'})
print('Signup status:', r2.status_code, r2.url)
r = s.get('http://127.0.0.1:8000/app/')
print('App status:', r.status_code)
if r.status_code == 500:
    print("500 text:", r.text[:1000])
